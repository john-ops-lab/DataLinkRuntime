"""Explicit supervised recovery; no consumer, business execution or old release.

The administrator's bound host evidence is an operational attestation. Only
the local original-root/object facts are independently checked by this process.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from dlr.common.cleanup_intent_proof_v4 import verify_cleanup_intent_proof
from dlr.common.cleanup_receipt_proof_v4 import verify_cleanup_receipt_proof
from dlr.common.recovery_observation_v4 import RecoveryClaims, verify_observation
from dlr.worker import (
    cgroup_namespace,
    claim_http_v4,
    journal_v4,
    recovery_local_v4,
    workspace_fs_v4,
)
from dlr.worker import recovery_checkpoint_v4 as checkpoint
from dlr.worker.lease_session_v4 import WorkerLeaseSession, _proof_deadline


@dataclass(frozen=True)
class RecoveryState:
    prepared: dict[str, Any]
    stopped: dict[str, Any]
    attempt_record: dict[str, Any]
    terminal: dict[str, Any] | None


def scan(
    *,
    lock: cgroup_namespace.RootLockHandle,
    runtime: Path,
    attempts: Path,
    worker_id: int,
    identity_id: UUID,
    boot_id: str,
) -> RecoveryState:
    """Read-only bounded discovery. Missing W1-A provenance remains blocked."""
    with lock.borrow(runtime, [attempts]):
        anchor = lock._v4_anchor
        if anchor is None:
            raise ValueError("recovery anchor unavailable")
        anchor.verify()
        directory = checkpoint._root_fd(lock, attempts)
        names = recovery_local_v4._entries(directory)
        terminals = [
            name for name in names if re.fullmatch(r"recovery-[1-9][0-9]*\.terminal.json", name)
        ]
        prepared_names = [
            name for name in names if re.fullmatch(r"recovery-[1-9][0-9]*\.prepared.json", name)
        ]
        terminal = None
        if len(terminals) == 1:
            terminal = journal_v4._read(directory, terminals[0])
            if (
                terminal is None
                or set(terminal)
                != {
                    "version",
                    "prepared",
                    "stopped",
                    "attempt_record",
                    "finalization_id",
                    "completion_id",
                    "records",
                    "runtime_records",
                }
                or type(terminal["version"]) is not int
                or terminal["version"] != 1
            ):
                raise ValueError("recovery terminal invalid")
            prepared, stopped = terminal["prepared"], terminal["stopped"]
            attempt_record = terminal["attempt_record"]
            if not all(
                checkpoint._uuid(terminal[key]) for key in ("finalization_id", "completion_id")
            ):
                raise ValueError("recovery terminal invalid")
        elif not terminals and len(prepared_names) == 1:
            prepared = journal_v4._read(directory, prepared_names[0])
            if not checkpoint.valid_prepared(prepared) or prepared is None:
                raise ValueError("recovery prepared invalid")
            stopped = journal_v4._read(directory, checkpoint.names(prepared["attempt_id"])[1])
            records = {
                name: journal_v4._read(directory, name) for name in prepared["journal_records"]
            }
            attempts_found = [
                record for record in records.values() if journal_v4.valid_record(record, "attempt")
            ]
            if len(attempts_found) != 1:
                raise ValueError("recovery Attempt unavailable")
            attempt_record = attempts_found[0]
        else:
            raise ValueError("recovery checkpoint unavailable")
        if (
            not checkpoint.valid_prepared(prepared)
            or not checkpoint.valid_stopped(stopped, prepared)
            or prepared["worker_id"] != worker_id
            or prepared["worker_identity_id"] != str(identity_id)
            or prepared["boot_id"] != boot_id
            or not journal_v4.valid_record(attempt_record, "attempt")
            or any(
                attempt_record[key] != prepared[key]
                for key in (
                    "execution_id",
                    "attempt_id",
                    "attempt_no",
                    "fencing_token",
                    "worker_identity_id",
                    "instance_id",
                    "instance_generation",
                )
            )
            or checkpoint.digest(attempt_record) not in prepared["journal_records"].values()
        ):
            raise ValueError("recovery provenance conflict")
        roots = {
            "runtime": checkpoint.binding(checkpoint._root_fd(lock, runtime)),
            "attempts": checkpoint.binding(directory),
            "anchor": checkpoint.binding(anchor.fd),
        }
        if roots != prepared["roots"]:
            raise ValueError("recovery original storage unavailable")
        refs = {**prepared["journal_records"], **stopped["result_record"]}
        first, last = checkpoint.names(prepared["attempt_id"])
        refs.update({first: checkpoint.digest(prepared), last: checkpoint.digest(stopped)})
        original_refs = dict(refs)
        if terminal is not None:
            if (
                not checkpoint._refs(terminal["records"])
                or terminal["runtime_records"] != prepared["runtime_record"]
                or any(terminal["records"].get(name) != sha for name, sha in refs.items())
                or terminals[0] != recovery_local_v4.terminal_name(prepared["attempt_id"])
            ):
                raise ValueError("recovery terminal conflict")
            refs = terminal["records"]
        progress = recovery_local_v4.expected_progress(prepared, stopped)
        allowed = set(original_refs) | {".dlr-instance.lock"}
        if terminal is not None:
            allowed.add(terminals[0])
        for name in (names | set(refs)) - allowed:
            record = journal_v4._read(directory, name)
            if record is None and (
                terminal is None or not recovery_local_v4._absent(directory, name)
            ):
                raise ValueError("recovery journal unknown")
            # Additional normal cleanup/progress records must still belong to
            # this exact old responsibility. They grant no result authority.
            attempt = prepared["attempt_id"]
            if re.fullmatch(rf"cleanup-{attempt}-[0-9a-f]{{32}}\.(intent|confirmed)\.json", name):
                if record is not None and any(
                    record.get(key) != prepared[key]
                    for key in (
                        "worker_identity_id",
                        "instance_id",
                        "instance_generation",
                        "execution_id",
                        "attempt_id",
                        "fencing_token",
                    )
                ):
                    raise ValueError("recovery cleanup journal conflict")
            elif name in progress:
                if record is not None and record != progress[name]:
                    raise ValueError("recovery progress conflict")
                if name in refs and refs[name] != checkpoint.digest(progress[name]):
                    raise ValueError("recovery progress digest conflict")
            elif re.fullmatch(rf"recovery-{attempt}\.instance-[0-9a-f]{{32}}\.json", name):
                if record is not None and (
                    set(record)
                    != {
                        "version",
                        "operation_id",
                        "prepared_sha256",
                        "process",
                        "instance_id",
                        "generation",
                    }
                    or type(record["version"]) is not int
                    or record["version"] != 1
                    or not checkpoint._uuid(record["operation_id"])
                    or not checkpoint._uuid(record["instance_id"])
                    or name != f"recovery-{attempt}.instance-{UUID(record['instance_id']).hex}.json"
                    or record["prepared_sha256"] != checkpoint.digest(prepared)
                    or not checkpoint._positive(record["generation"])
                    or record["generation"] <= prepared["instance_generation"]
                    or not isinstance(record["process"], dict)
                    or set(record["process"]) != {"start_ticks", "pid_namespace", "mount_namespace"}
                    or not checkpoint._positive(record["process"]["start_ticks"])
                    or not all(
                        checkpoint._binding(record["process"][key])
                        and record["process"][key] != prepared["process"][key]
                        for key in ("pid_namespace", "mount_namespace")
                    )
                ):
                    raise ValueError("recovery new instance observation conflict")
            else:
                raise ValueError("recovery journal unknown")
        for fd, expected in (
            (directory, refs),
            (checkpoint._root_fd(lock, runtime), prepared["runtime_record"]),
        ):
            for name, sha in expected.items():
                record = journal_v4._read(fd, name)
                if record is None:
                    if terminal is None or not recovery_local_v4._absent(fd, name):
                        raise ValueError("recovery journal missing")
                elif checkpoint.digest(record) != sha:
                    raise ValueError("recovery journal conflict")
        # No second runtime claim is permitted, including an unknown one.
        runtime_names = recovery_local_v4._entries(checkpoint._root_fd(lock, runtime))
        if not runtime_names <= {
            ".dlr-instance.lock",
            "workspaces",
            "attempt-journal",
            "cleanup-journal",
            "version-cache",
            *prepared["runtime_record"],
        }:
            raise ValueError("recovery runtime unknown")
        runtime_fd = checkpoint._root_fd(lock, runtime)
        recovery_local_v4.inspect_tree(
            directory, runtime_fd, runtime, prepared, stopped, terminal=terminal is not None
        )

        def empty_layout(fd: int, allowed: dict[str, Any], depth: int = 0) -> None:
            if depth > 8:
                raise ValueError("recovery scan limit")
            for name in recovery_local_v4._entries(fd):
                if name not in allowed:
                    raise ValueError("recovery unrelated residual")
                child = workspace_fs_v4._open_private_child(fd, name)
                try:
                    if cgroup_namespace._mount_id(child) != cgroup_namespace._mount_id(fd):
                        raise ValueError("recovery extra mount")
                    empty_layout(child, allowed[name], depth + 1)
                finally:
                    os.close(child)

        if "version-cache" in runtime_names:
            cache = workspace_fs_v4._open_private_child(runtime_fd, "version-cache")
            try:
                empty_layout(
                    cache,
                    {
                        "entries": {},
                        ".dlr-cache-lifecycle": {
                            "items": {},
                            "uses": {},
                            "locks": {},
                            "deletions": {},
                        },
                    },
                )
            finally:
                os.close(cache)
        for entry in lock._entries:
            if entry.path in {runtime, attempts}:
                continue
            remaining = recovery_local_v4._entries(entry.directory_fd) - {".dlr-instance.lock"}
            for child_entry in lock._entries:
                if child_entry.path.parent == entry.path:
                    remaining.discard(child_entry.path.name)
            if remaining - {"sandbox-recovery"}:
                raise ValueError("recovery unrelated cleanup")
            if "sandbox-recovery" in remaining:
                child = workspace_fs_v4._open_private_child(entry.directory_fd, "sandbox-recovery")
                try:
                    empty_layout(child, {})
                finally:
                    os.close(child)
        return RecoveryState(prepared, stopped, attempt_record, terminal)


def read_operation(
    lease: WorkerLeaseSession, state: RecoveryState, operation: UUID
) -> dict[str, Any]:
    payload = json.dumps(
        {"operation_id": str(operation), "prepared_sha256": checkpoint.digest(state.prepared)}
    ).encode()
    request = urllib.request.Request(
        f"{lease._base_url}/api/workers/{lease._worker_id}/v4/recovery-intent/read",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {lease._credential}",
            "X-DLR-Worker-Instance": str(lease._instance_id),
            "Content-Type": "application/json",
        },
    )
    raw = lease._post(request, timeout=lease._timeout, deadline=lease._now() + 5, expected=200)
    response = lease._body(raw, {"request_id", "maintenance_version", "binding", "host_evidence"})
    binding = response["binding"]
    p = state.prepared
    expected = {
        "attempt_id": p["attempt_id"],
        "fencing_token": p["fencing_token"],
        "worker_identity_id": p["worker_identity_id"],
        "original_instance_id": p["instance_id"],
        "original_instance_generation": p["instance_generation"],
    }
    if (
        response["request_id"] != str(operation)
        or response["host_evidence"] != "operator_attestation"
        or not isinstance(binding, dict)
        or set(binding)
        != {
            "execution_id",
            "attempt",
            "old_container_id",
            "deployment_record_id",
            "evidence_sha256",
            "boot_id",
            "prepared_sha256",
        }
        or binding["execution_id"] != p["execution_id"]
        or binding["attempt"] != expected
        or binding["boot_id"] != p["boot_id"]
        or binding["prepared_sha256"] != checkpoint.digest(p)
        or any(
            not isinstance(binding[key], str) or not re.fullmatch(r"[0-9a-f]{64}", binding[key])
            for key in ("old_container_id", "evidence_sha256")
        )
        or not checkpoint._uuid(binding["deployment_record_id"])
    ):
        raise ValueError("recovery operation conflict")
    return {
        "operation_id": str(operation),
        "binding": binding,
        "result_intent_id": state.stopped["intent_id"],
        "body_mac": state.stopped["body_mac"],
    }


class RecoveryExchange:
    def __init__(
        self,
        lease: WorkerLeaseSession,
        state: RecoveryState,
        lookup: dict[str, Any],
        stop: threading.Event,
    ):
        self.lease, self.state, self.lookup, self.stop = lease, state, lookup, stop
        self.body = json.dumps(lookup, separators=(",", ":"))

    def _call(self, operation: str, headers: claim_http_v4._Cleanup) -> tuple[bytes, float, float]:
        lease = self.lease
        deadline = lease._active_deadline()
        started = lease._require_before(deadline)
        result = claim_http_v4.request_proof(
            url=f"{lease._base_url}/api/workers/{lease._worker_id}/v4/executions/"
            f"{self.state.prepared['execution_id']}/recovery/{operation}",
            credential=lease._credential,
            instance=str(lease._instance_id),
            generation=lease._claims.generation if lease._claims is not None else 0,
            body=self.body,
            instance_proof=lease._proof,
            attempt_proof=None,
            timeout_seconds=min(lease._timeout, deadline - started),
            response_limit_bytes=8192,
            _absolute_deadline=min(deadline, started + 5),
            _cancellation_events=(self.stop, lease._lost),
            _cleanup=headers,
        )
        if result.state == "io_unknown":
            lease._die()
        finished = lease._require_before(deadline)
        if result.state != "response" or result.status != 200:
            raise ValueError("recovery exchange unknown")
        return result.body, started, finished

    def observe(self) -> RecoveryClaims:
        lease = self.lease
        with lease._lock:
            nonce = uuid4()
            raw, started, finished = self._call(
                "observe",
                claim_http_v4._Cleanup(
                    self.state.attempt_record["cleanup_token"],
                    lease._verification_key,
                    None,
                    recovery_nonce=str(nonce),
                ),
            )
            response = lease._body(raw, {"recovery_proof"})
            claims = verify_observation(
                response["recovery_proof"],
                public_key=lease._verification_key,
                instance_token=lease._proof,
            )
            p, s = self.state.prepared, self.state.stopped
            if claims is None or (
                claims.execution_id,
                claims.attempt_id,
                claims.original_instance_id,
                claims.original_generation,
                claims.fencing_token,
                claims.operation_id,
                claims.prepared_sha256,
                claims.result_intent_id,
                claims.body_mac,
                claims.observation_nonce,
            ) != (
                p["execution_id"],
                p["attempt_id"],
                UUID(p["instance_id"]),
                p["instance_generation"],
                p["fencing_token"],
                UUID(self.lookup["operation_id"]),
                bytes.fromhex(checkpoint.digest(p)),
                UUID(s["intent_id"]),
                bytes.fromhex(s["body_mac"]),
                nonce,
            ):
                raise ValueError("recovery observation conflict")
            lease._check_observation_clock(claims.observed_us, started, finished)
            lease._require_before(
                _proof_deadline(started, claims.proof_before_us, claims.observed_us)
            )
            lease._observation = (claims.observed_us, started, finished)
            return claims

    def settle(self) -> None:
        lease = self.lease
        with lease._lock:
            intent, nonce = uuid4(), uuid4()
            token = self.state.attempt_record["cleanup_token"]
            raw, started, finished = self._call(
                "cleanup/stage",
                claim_http_v4._Cleanup(
                    token,
                    lease._verification_key,
                    str(intent),
                    str(nonce),
                ),
            )
            proof = lease._body(raw, {"cleanup_proof"})["cleanup_proof"]
            claims = verify_cleanup_intent_proof(
                proof, public_key=lease._verification_key, instance_token=lease._proof
            )
            p = self.state.prepared
            if (
                claims is None
                or (
                    claims.execution_id,
                    claims.attempt_id,
                    claims.original_instance_id,
                    claims.original_generation,
                    claims.fencing_token,
                    claims.intent_id,
                    claims.observation_nonce,
                )
                != (
                    p["execution_id"],
                    p["attempt_id"],
                    UUID(p["instance_id"]),
                    p["instance_generation"],
                    p["fencing_token"],
                    intent,
                    nonce,
                )
                or type(proof) is not str
            ):
                raise ValueError("recovery cleanup conflict")
            lease._check_observation_clock(claims.observed_us, started, finished)
            cutoff = _proof_deadline(started, claims.proof_before_us, claims.observed_us)
            lease._require_before(cutoff)
            lease._observation = (claims.observed_us, started, finished)
            receipt_nonce = uuid4()
            raw, started, finished = self._call(
                "cleanup/finalize",
                claim_http_v4._Cleanup(
                    token,
                    lease._verification_key,
                    str(intent),
                    str(nonce),
                    proof,
                    str(receipt_nonce),
                    lease._proof,
                ),
            )
            receipt = verify_cleanup_receipt_proof(
                lease._body(raw, {"cleanup_receipt"})["cleanup_receipt"],
                public_key=lease._verification_key,
                instance_token=lease._proof,
                intent_token=proof,
                observation_nonce=receipt_nonce,
            )
            if receipt is None:
                raise ValueError("recovery cleanup receipt unavailable")
            lease._check_observation_clock(receipt.observed_us, started, finished)
            lease._require_before(cutoff)
            lease._observation = (receipt.observed_us, started, finished)


def recover(
    *,
    lease: WorkerLeaseSession,
    state: RecoveryState,
    lookup: dict[str, Any],
    lock: cgroup_namespace.RootLockHandle,
    runtime: Path,
    attempts: Path,
    stop: threading.Event,
) -> None:
    exchange = RecoveryExchange(lease, state, lookup, stop)
    observed = exchange.observe()
    with lock.borrow(runtime, [attempts]):
        directory, runtime_fd = (
            checkpoint._root_fd(lock, attempts),
            checkpoint._root_fd(lock, runtime),
        )
        current = scan(
            lock=lock,
            runtime=runtime,
            attempts=attempts,
            worker_id=state.prepared["worker_id"],
            identity_id=UUID(state.prepared["worker_identity_id"]),
            boot_id=state.prepared["boot_id"],
        )
        if current != state:
            raise ValueError("recovery journal changed after observation")
        if state.terminal is None:
            if lease._claims is None:
                raise ValueError("recovery instance unavailable")
            checkpoint.persist(
                directory,
                f"recovery-{state.prepared['attempt_id']}.instance-{lease._instance_id.hex}.json",
                {
                    "version": 1,
                    "operation_id": lookup["operation_id"],
                    "prepared_sha256": checkpoint.digest(state.prepared),
                    "process": checkpoint.process_identity(),
                    "instance_id": str(lease._instance_id),
                    "generation": lease._claims.generation,
                },
            )
            recovery_local_v4.clean_workspace(
                directory, runtime_fd, runtime, state.prepared, state.stopped
            )
    # All HTTP IO children have been reaped by the existing bounded transport;
    # physical cleanup is synchronous. A stalled IO never releases intake.
    lease.heartbeat(stop=stop)
    if observed.completion_id is None:
        # Unknown commit is resolved by the next explicit run's observe, not
        # by resetting a completion or assuming that HTTP failure rolled back.
        exchange.settle()
        observed = exchange.observe()
    if observed.completion_id is None:
        raise ValueError("recovery cleanup uncommitted")
    with lock.borrow(runtime, [attempts]):
        directory, runtime_fd = (
            checkpoint._root_fd(lock, attempts),
            checkpoint._root_fd(lock, runtime),
        )
        terminal = state.terminal
        if terminal is None:
            records = {}
            for name in recovery_local_v4._entries(directory) - {".dlr-instance.lock"}:
                record = journal_v4._read(directory, name)
                if record is None:
                    raise ValueError("recovery retirement unavailable")
                records[name] = checkpoint.digest(record)
            terminal = {
                "version": 1,
                "prepared": state.prepared,
                "stopped": state.stopped,
                "attempt_record": state.attempt_record,
                "finalization_id": str(observed.finalization_id),
                "completion_id": str(observed.completion_id),
                "records": records,
                "runtime_records": state.prepared["runtime_record"],
            }
        elif terminal["finalization_id"] != str(observed.finalization_id) or terminal[
            "completion_id"
        ] != str(observed.completion_id):
            raise ValueError("recovery terminal committed IDs conflict")
        recovery_local_v4.retire(
            directory, runtime_fd, terminal, allow_missing=state.terminal is not None
        )
