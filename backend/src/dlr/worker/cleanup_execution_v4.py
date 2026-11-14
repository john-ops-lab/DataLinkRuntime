"""Settle one stopped fixed-Worker execution before releasing its delivery."""

from __future__ import annotations

import json
import logging
import os
from uuid import uuid4

from dlr.common.cleanup_intent_proof_v4 import verify_cleanup_intent_proof
from dlr.common.cleanup_receipt_proof_v4 import verify_cleanup_receipt_proof
from dlr.worker import (
    cgroup_intent_v4,
    claim_http_v4,
    claim_preparation_v4,
    journal_v4,
    result_exchange_v4,
    workspace_fs_v4,
    workspace_live_v4,
)
from dlr.worker.execution_supervision_v4 import ResultSnapshot, SupervisedExecution
from dlr.worker.lease_session_v4 import _proof_deadline

logger = logging.getLogger("dlr.worker.v4.cleanup")


def _remove_exact(directory: int, records: dict[str, object]) -> None:
    """Remove only the records checked under the Worker root lock."""
    if any(journal_v4._read(directory, name) != value for name, value in records.items()):
        raise ValueError("cleanup journal changed")
    for name in records:
        os.unlink(name, dir_fd=directory)
    os.fsync(directory)


def settle_execution(
    preparer: claim_preparation_v4.ClaimPreparer,
    owner: SupervisedExecution,
    snapshot: ResultSnapshot,
) -> bool:
    """Remove exact local resources and observe committed cleanup settlement.

    False means responsibility remains unresolved: stop intake and keep the
    delivery unacknowledged. Restart reconciliation is a separate capability.
    """
    if (
        type(preparer) is not claim_preparation_v4.ClaimPreparer
        or type(owner) is not SupervisedExecution
        or type(snapshot) is not ResultSnapshot
        or owner._pid != os.getpid()
    ):
        return False
    staged = owner._staged
    prepared, live, workspace = staged.prepared, staged.cgroup, staged.workspace
    budget, root_lock = live._budget, live._root_lock
    with preparer._lock, snapshot.lock:
        if (
            preparer._pid != os.getpid()
            or preparer._poisoned
            or preparer._active is not prepared
            or prepared.stage_state != "supervising"
            or owner._snapshot is not snapshot
            or snapshot.owner is not owner
            or type(snapshot.receipt) is not str
            or not snapshot.receipt
            or not owner._physical_done.is_set()
            or owner._observation is None
            or owner._observation.code != "cgroup_empty_supervisor_reaped"
            or not live._execution_finished
            or owner._watchdog.is_alive()
            or owner._collector._thread.is_alive()
            or owner._collector._failed
            or owner._stop.is_set()
            or type(snapshot.intent) is not result_exchange_v4.ResultIntent
        ):
            return False
        phase = "physical_cleanup"
        try:
            stopped = cgroup_intent_v4.stop_live_cgroup(
                live,
                config=live._config,
                budget=budget,
                runtime_root=live._runtime_root,
                attempt_journal_root=live._attempt_journal_root,
                root_lock=root_lock,
            )
            if stopped.code != "cgroup_removed":
                raise ValueError("cgroup cleanup unavailable")
            removed = workspace_live_v4.cleanup_live_workspace(workspace)
            if removed.code != "workspace_removed":
                raise ValueError("workspace cleanup unavailable")
            rechecked = workspace_live_v4.observe_removed_workspace(
                workspace, owner=owner, snapshot=snapshot
            )
            if rechecked.code != "workspace_removal_rechecked":
                raise ValueError("workspace cleanup unavailable")

            identity = prepared.identity
            intent_id, nonce = uuid4(), uuid4()
            local_name = f"cleanup-{identity.attempt_id}-{intent_id.hex}.intent.json"
            confirmed_name = f"cleanup-{identity.attempt_id}-{intent_id.hex}.confirmed.json"
            local_record: dict[str, object] = {
                "version": 1,
                "worker_identity_id": str(identity.worker_identity_id),
                "instance_id": str(identity.instance_id),
                "instance_generation": identity.instance_generation,
                "execution_id": identity.execution_id,
                "attempt_id": identity.attempt_id,
                "fencing_token": identity.fencing_token,
                "intent_id": str(intent_id),
                "observation_nonce": str(nonce),
            }
            phase = "intent_journal"
            with root_lock.borrow(live._runtime_root, [live._attempt_journal_root]):
                root_lock.require_v4_journal(live._runtime_root, live._attempt_journal_root)
                directory = preparer._entry(root_lock, live._attempt_journal_root)
                workspace_fs_v4._write_new(directory, local_name, local_record)
                if journal_v4._read(directory, local_name) != local_record:
                    raise ValueError("cleanup journal unavailable")

            parent = prepared.attempt._parent
            body = json.dumps(
                {
                    "status": "completed",
                    "attempt_id": identity.attempt_id,
                    "fencing_token": identity.fencing_token,
                    "worker_identity_id": str(identity.worker_identity_id),
                    "original_instance_id": str(identity.instance_id),
                    "original_instance_generation": identity.instance_generation,
                },
                separators=(",", ":"),
            )
            phase = "control_receipt"
            with parent._lock:
                deadline = parent._active_deadline()
                instance_proof, instance_claims = parent._proof, parent._claims
                if instance_proof is None or instance_claims is None:
                    raise ValueError("cleanup authority unavailable")

                def exchange(
                    operation: str, data: claim_http_v4._Cleanup, cutoff: float
                ) -> tuple[bytes, float, float]:
                    started = parent._require_before(cutoff)
                    io_deadline = min(cutoff, started + 5.0)
                    result = claim_http_v4.request_proof(
                        url=f"{parent._base_url}/api/workers/{parent._worker_id}/v4/"
                        f"executions/{identity.execution_id}/cleanup/{operation}",
                        credential=parent._credential,
                        instance=str(parent._instance_id),
                        generation=instance_claims.generation,
                        body=body,
                        instance_proof=instance_proof,
                        attempt_proof=None,
                        timeout_seconds=min(parent._timeout, io_deadline - started),
                        response_limit_bytes=8192,
                        _absolute_deadline=io_deadline,
                        _cancellation_events=(owner._stop, parent._lost),
                        _cleanup=data,
                    )
                    if result.state == "io_unknown":
                        parent._die()
                    finished = parent._require_before(cutoff)
                    if result.state != "response" or result.status != 200:
                        raise ValueError("cleanup outcome unknown")
                    return result.body, started, finished

                cleanup_token = prepared.journal["cleanup_token"]
                raw, started, finished = exchange(
                    "stage",
                    claim_http_v4._Cleanup(
                        cleanup_token, parent._verification_key, str(intent_id), str(nonce)
                    ),
                    deadline,
                )
                response = parent._body(raw, {"cleanup_proof"})
                proof = response["cleanup_proof"]
                claims = verify_cleanup_intent_proof(
                    proof, public_key=parent._verification_key, instance_token=instance_proof
                )
                if (
                    type(proof) is not str
                    or claims is None
                    or claims.execution_id != identity.execution_id
                    or claims.attempt_id != identity.attempt_id
                    or claims.fencing_token != identity.fencing_token
                    or claims.original_instance_id != identity.instance_id
                    or claims.original_generation != identity.instance_generation
                    or claims.intent_id != intent_id
                    or claims.observation_nonce != nonce
                ):
                    raise ValueError("cleanup proof unavailable")
                parent._check_observation_clock(claims.observed_us, started, finished)
                cutoff = min(
                    deadline, _proof_deadline(started, claims.proof_before_us, claims.observed_us)
                )
                parent._require_before(cutoff)
                confirmed_record = {**local_record, "cleanup_proof": proof}
                with root_lock.borrow(live._runtime_root, [live._attempt_journal_root]):
                    directory = preparer._entry(root_lock, live._attempt_journal_root)
                    if journal_v4._read(directory, local_name) != local_record:
                        raise ValueError("cleanup journal unavailable")
                    workspace_fs_v4._write_new(directory, confirmed_name, confirmed_record)
                    if journal_v4._read(directory, confirmed_name) != confirmed_record:
                        raise ValueError("cleanup journal unavailable")
                parent._observation = (claims.observed_us, started, finished)
                receipt_nonce = uuid4()
                raw, started, finished = exchange(
                    "finalize",
                    claim_http_v4._Cleanup(
                        cleanup_token,
                        parent._verification_key,
                        str(intent_id),
                        str(nonce),
                        proof,
                        str(receipt_nonce),
                        instance_proof,
                    ),
                    cutoff,
                )
                response = parent._body(raw, {"cleanup_receipt"})
                receipt = verify_cleanup_receipt_proof(
                    response["cleanup_receipt"],
                    public_key=parent._verification_key,
                    instance_token=instance_proof,
                    intent_token=proof,
                    observation_nonce=receipt_nonce,
                )
                if receipt is None:
                    raise ValueError("cleanup receipt unavailable")
                parent._check_observation_clock(receipt.observed_us, started, finished)
                parent._require_before(cutoff)
                parent._observation = (receipt.observed_us, started, finished)

            phase = "journal_removal"
            result_intent = snapshot.intent
            assert type(result_intent) is result_exchange_v4.ResultIntent
            result_record = {
                "version": 1,
                "worker_identity_id": str(identity.worker_identity_id),
                "instance_id": str(identity.instance_id),
                "instance_generation": identity.instance_generation,
                "attempt_id": identity.attempt_id,
                "fencing_token": identity.fencing_token,
                "intent_id": str(result_intent.intent_id),
                "observation_nonce": str(result_intent.nonce),
                "result_proof": result_intent.proof,
                "body_mac": result_intent.body_mac.hex(),
            }
            attempt_id = identity.attempt_id
            records = {
                f"cgroup-{attempt_id}.intent.json": live._intent,
                f"cgroup-{attempt_id}.bound.json": live._bound,
                f"physical-parent-{attempt_id}.intent.json": workspace._parent_record,
                f"physical-parent-{attempt_id}.bound.json": workspace._parent_bound,
                f"physical-{attempt_id}.intent.json": workspace._record,
                f"physical-{attempt_id}.bound.json": workspace._bound,
                journal_v4._name(prepared.journal, "attempt"): prepared.journal,
                prepared.intent_names[0]: prepared.intent,
                result_intent.journal_name: result_record,
                local_name: local_record,
                confirmed_name: confirmed_record,
            }
            if len(records) != 11:
                raise ValueError("cleanup journal unavailable")
            os.close(snapshot.fd)
            snapshot.fd = -1
            prepared.attempt.close()
            with root_lock.borrow(live._runtime_root, [live._attempt_journal_root]):
                root_lock.require_v4_journal(live._runtime_root, live._attempt_journal_root)
                with journal_v4._locked_root(live._attempt_journal_root) as directory:
                    _remove_exact(directory, records)
                # Keep the runtime intent until the journal directory is durable.
                # The runtime root is intentionally 0711 so the sandbox can
                # traverse it. Reuse its already-locked descriptor: the
                # journal helper would incorrectly change it to 0700.
                directory = preparer._entry(root_lock, live._runtime_root)
                _remove_exact(directory, {prepared.intent_names[1]: prepared.intent})
            phase = "budget_release"
            budget._release_settled_v4(
                prepared.attempt_reservation, prepared.receive_reservation, attempt_id=attempt_id
            )
            prepared.stage_state = "settled"
            preparer._active = None
            return True
        except BaseException as error:
            preparer._poisoned = True
            budget._mark_claim_receive_accounting_unknown()
            owner._stop.set()
            logger.warning(
                "fixed Worker cleanup unavailable: phase=%s error=%s", phase, type(error).__name__
            )
            if not isinstance(error, Exception):
                raise
            return False
