"""Confirm one stopped Worker's sealed result with signed Control receipts."""

from __future__ import annotations

import fcntl
import hmac
import logging
import math
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID, uuid4

from dlr.common.result_intent_proof_wire_v4 import verify_result_intent_binding
from dlr.common.result_receipt_proof_v4 import verify_result_receipt
from dlr.worker import claim_http_v4, claim_preparation_v4, journal_v4, sandbox, workspace_fs_v4
from dlr.worker.execution_supervision_v4 import ResultSnapshot, SupervisedExecution
from dlr.worker.lease_session_v4 import _proof_deadline

logger = logging.getLogger("dlr.worker.v4.result")


@dataclass(frozen=True)
class ResultIntent:
    intent_id: UUID
    nonce: UUID
    proof: str = field(repr=False)
    deadline: float
    journal_name: str
    body_mac: bytes = field(repr=False)


def _valid_snapshot(snapshot: ResultSnapshot, owner: SupervisedExecution) -> bool:
    try:
        info = os.fstat(snapshot.fd)
        seals = sandbox._launch_seals()
        return bool(
            snapshot.owner is owner
            and type(snapshot.body_mac) is bytes
            and len(snapshot.body_mac) == 32
            and stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and info.st_nlink == 0
            and (info.st_dev, info.st_ino, info.st_size) == snapshot.identity
            and info.st_size == snapshot.size
            and fcntl.fcntl(snapshot.fd, fcntl.F_GET_SEALS) & seals == seals  # type: ignore[attr-defined, unused-ignore]
        )
    except (AttributeError, OSError, ValueError):
        return False


def submit_result(
    owner: SupervisedExecution,
    snapshot: ResultSnapshot,
    *,
    root_lock: object,
    runtime_root: Path,
    attempt_journal_root: Path,
) -> bool:
    """Obtain a signed result receipt; cleanup and Broker ACK remain separate.

    Unknown network or commit outcomes keep the original snapshot and journal.
    A previously observed intent can be retried only within its original local
    deadline; a restart still requires physical and durable reconciliation.
    """
    from dlr.worker.cgroup_namespace import RootLockHandle

    if (
        type(owner) is not SupervisedExecution
        or type(snapshot) is not ResultSnapshot
        or owner._pid != os.getpid()
        or type(root_lock) is not RootLockHandle
        or owner._staged.prepared.stage_state != "supervising"
        or not owner._physical_done.is_set()
        or owner._stop.is_set()
    ):
        return False
    with snapshot.lock:
        if not _valid_snapshot(snapshot, owner):
            return False
        if snapshot.receipt is not None:
            return True
        if not owner.stop_watchdog():
            return False
        prepared = owner._staged.prepared
        attempt, parent = prepared.attempt, prepared.attempt._parent
        identity = prepared.identity
        budget = owner._staged.cgroup._budget
        journal_uncertain = False
        phase = "authority"
        try:
            with attempt._lock, parent._lock:
                attempt_claims, attempt_proof, deadline = attempt._live_locked()
                instance_claims, instance_proof = parent._claims, parent._proof
                if (
                    not attempt._start_confirmed
                    or instance_claims is None
                    or instance_proof is None
                    or owner._stop.is_set()
                    or not _valid_snapshot(snapshot, owner)
                ):
                    raise ValueError("result authority unavailable")

                def upload(
                    operation: str, value: claim_http_v4._Upload, cutoff: float
                ) -> tuple[bytes, float, float]:
                    nonlocal phase
                    phase = operation + "_request"
                    started = parent._require_before(cutoff)
                    io_deadline = min(cutoff, started + 5.0)

                    def authorize(send: object) -> bool:
                        if not callable(send) or owner._stop.is_set():
                            return False
                        attempt._live_locked()
                        if not _valid_snapshot(snapshot, owner):
                            return False
                        send()
                        attempt._live_locked()
                        return True

                    result = claim_http_v4.request_claim(
                        url=f"{parent._base_url}/api/workers/{parent._worker_id}/v4/attempts/"
                        f"{identity.attempt_id}/result/{operation}",
                        credential=parent._credential,
                        instance=str(parent._instance_id),
                        generation=instance_claims.generation,
                        body="",
                        timeout_seconds=min(parent._timeout, io_deadline - started),
                        _response_limit_bytes=8192,
                        _child_address_space_bytes=128 * 1024 * 1024,
                        _instance_proof=instance_proof,
                        _proof_request=True,
                        _collect_request_timing=True,
                        _attempt_proof=attempt_proof,
                        _absolute_deadline=io_deadline,
                        _upload=value,
                        _upload_authorize=authorize,
                        _cancellation_events=(owner._stop, attempt._lost, parent._lost),
                    )
                    if result.state == "io_unknown":
                        parent._die()
                        budget._mark_claim_receive_accounting_unknown()
                    finished = parent._require_before(cutoff)
                    if result.state != "response" or result.status != 200:
                        raise ValueError("result outcome unknown")
                    if result.request_interval_ns is None:
                        raise ValueError("result request timing unavailable")
                    sent_ns, received_ns = result.request_interval_ns
                    sent = math.nextafter(sent_ns / 1_000_000_000, -math.inf)
                    received = math.nextafter(received_ns / 1_000_000_000, math.inf)
                    if not started <= sent <= received <= finished:
                        raise ValueError("result request timing unavailable")
                    logger.info(
                        "fixed Worker result response: operation=%s elapsed_ms=%d",
                        operation,
                        round((finished - started) * 1000),
                    )
                    return result.body, sent, received

                intent = snapshot.intent
                if intent is None:
                    nonce = uuid4()
                    raw, started, finished = upload(
                        "stage",
                        claim_http_v4._Upload(
                            snapshot.fd, snapshot.size, observation_nonce=str(nonce)
                        ),
                        deadline,
                    )
                    phase = "stage_proof"
                    body = parent._body(raw, {"intent_id", "result_proof"})
                    if type(body["intent_id"]) is not str:
                        raise ValueError("result intent unavailable")
                    intent_id = UUID(body["intent_id"])
                    proof_token = body["result_proof"]
                    claims = verify_result_intent_binding(
                        proof_token,
                        public_key=parent._verification_key,
                        instance_token=instance_proof,
                        attempt_token=attempt_proof,
                        cluster_id=parent._cluster_id,
                        authority_epoch=parent._authority_epoch,
                        worker_id=parent._worker_id,
                        worker_identity_id=parent._identity_id,
                        instance_id=parent._instance_id,
                        generation=instance_claims.generation,
                        attempt_id=identity.attempt_id,
                        fencing_token=identity.fencing_token,
                        intent_id=intent_id,
                        observation_nonce=nonce,
                        body_mac=snapshot.body_mac,
                    )
                    if claims is None or type(proof_token) is not str:
                        raise ValueError("result proof unavailable")
                    phase = "stage_clock"
                    parent._check_observation_clock(claims.observed_us, started, finished)
                    intent_deadline = _proof_deadline(
                        started, claims.proof_before_us, claims.observed_us
                    )
                    parent._require_before(min(deadline, intent_deadline))
                    name = f"result-{identity.attempt_id}-{nonce.hex}.intent.json"
                    record = {
                        "version": 1,
                        "worker_identity_id": str(identity.worker_identity_id),
                        "instance_id": str(identity.instance_id),
                        "instance_generation": identity.instance_generation,
                        "attempt_id": identity.attempt_id,
                        "fencing_token": identity.fencing_token,
                        "intent_id": str(intent_id),
                        "observation_nonce": str(nonce),
                        "result_proof": proof_token,
                        "body_mac": snapshot.body_mac.hex(),
                    }
                    phase = "stage_journal"
                    with root_lock.borrow(runtime_root, [attempt_journal_root]):
                        root_lock.require_v4_journal(runtime_root, attempt_journal_root)
                        if (
                            journal_v4.load(
                                attempt_journal_root,
                                runtime_root,
                                execution_id=identity.execution_id,
                                attempt_id=identity.attempt_id,
                                kind="attempt",
                            )
                            != prepared.journal
                        ):
                            raise ValueError("result journal unavailable")
                        directory = claim_preparation_v4.ClaimPreparer._entry(
                            root_lock, attempt_journal_root
                        )
                        journal_uncertain = True
                        workspace_fs_v4._write_new(directory, name, record)
                        if journal_v4._read(directory, name) != record:
                            raise ValueError("result journal unavailable")
                    parent._require_before(min(deadline, intent_deadline))
                    parent._observation = (claims.observed_us, started, finished)
                    intent = ResultIntent(
                        intent_id, nonce, proof_token, intent_deadline, name, snapshot.body_mac
                    )
                    snapshot.intent = intent
                    journal_uncertain = False
                if type(intent) is not ResultIntent or not hmac.compare_digest(
                    snapshot.body_mac, intent.body_mac
                ):
                    raise ValueError("result intent unavailable")
                expected_record = {
                    "version": 1,
                    "worker_identity_id": str(identity.worker_identity_id),
                    "instance_id": str(identity.instance_id),
                    "instance_generation": identity.instance_generation,
                    "attempt_id": identity.attempt_id,
                    "fencing_token": identity.fencing_token,
                    "intent_id": str(intent.intent_id),
                    "observation_nonce": str(intent.nonce),
                    "result_proof": intent.proof,
                    "body_mac": intent.body_mac.hex(),
                }
                journal_uncertain = True
                with root_lock.borrow(runtime_root, [attempt_journal_root]):
                    root_lock.require_v4_journal(runtime_root, attempt_journal_root)
                    directory = claim_preparation_v4.ClaimPreparer._entry(
                        root_lock, attempt_journal_root
                    )
                    if journal_v4._read(directory, intent.journal_name) != expected_record:
                        raise ValueError("result journal unavailable")
                journal_uncertain = False
                cutoff = min(deadline, intent.deadline)
                parent._require_before(cutoff)
                receipt_nonce = uuid4()
                raw, started, finished = upload(
                    "finalize",
                    claim_http_v4._Upload(
                        snapshot.fd,
                        snapshot.size,
                        str(intent.intent_id),
                        str(intent.nonce),
                        intent.proof,
                        str(receipt_nonce),
                    ),
                    cutoff,
                )
                phase = "finalize_receipt"
                response = parent._body(raw, {"result_receipt"})
                receipt = verify_result_receipt(
                    response["result_receipt"],
                    public_key=parent._verification_key,
                    intent_token=intent.proof,
                    observation_nonce=receipt_nonce,
                )
                if receipt is None or type(response["result_receipt"]) is not str:
                    raise ValueError("result receipt unavailable")
                phase = "finalize_clock"
                parent._check_observation_clock(receipt.observed_us, started, finished)
                parent._require_before(cutoff)
                parent._observation = (receipt.observed_us, started, finished)
                snapshot.receipt = response["result_receipt"]
                return True
        except BaseException as error:
            logger.warning(
                "fixed Worker result unconfirmed: phase=%s error=%s", phase, type(error).__name__
            )
            if journal_uncertain:
                budget._mark_claim_receive_accounting_unknown()
                owner._stop.set()
            if not isinstance(error, Exception):
                raise
            return False
