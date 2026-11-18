"""File bounds, actual local I/O, bounded IPC and selected PostgreSQL grants."""

from __future__ import annotations

import hashlib
import io
import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from email.message import Message
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from dlr.common import managed_files_v4 as contract
from dlr.control.services import artifact_store
from dlr.control.services.attempt_lease_candidates_v4 import AttemptLeaseCandidateError
from dlr.control.services.worker_credentials import WorkerCredentialError


@pytest.fixture
def file_staging(monkeypatch, tmp_path):
    from dlr.control.schemas.reliable_runtime import V4TaskInputFile
    from dlr.worker import managed_files_v4 as module

    monkeypatch.setattr(module, "_clock", time.monotonic)
    instance = uuid4()
    content = b"verified-file" * 6000
    metadata = descriptor(size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
    task = SimpleNamespace(
        input_files=[V4TaskInputFile(**metadata)],
        execution_id=7,
        instance_id=instance,
        instance_generation=2,
        claim_token="test-only",
    )
    (tmp_path / "input").mkdir(mode=0o700)
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    events = []
    monkeypatch.setattr(module, "_send", lambda _channel, frame: events.append(frame))
    auth = {
        "base_url": "https://control.invalid",
        "worker_id": 1,
        "instance_id": str(instance),
        "generation": 2,
        "credential": "test-only",
        "instance_proof": "test-only",
        "attempt_proof": "test-only",
        "claim_token": "test-only",
        "deadline": time.monotonic() + 4,
    }
    replies = iter([auth, {"op": "read_input"}])
    monkeypatch.setattr(module, "_receive", lambda _channel: next(replies))

    class Response(io.BytesIO):
        status = 200
        fp = None  # This fixture tests body framing, not a real TLS socket.
        close_calls = 0
        fail_close = False

        def read(self, size):
            assert events[-1] == {"state": "input_admitted", "ordinal": 0}
            assert 0 < size <= contract.CHUNK_BYTES
            return super().read(size)

        def geturl(self):
            return self.url

        def close(self):
            self.close_calls += 1
            if self.fail_close:
                raise OSError("test-only uncertain close")
            super().close()

    response = Response(content)
    response.headers = Message()
    response.headers["Content-Length"] = str(len(content))

    def opened(request, *, timeout):
        assert request.method == "GET" and 0 < timeout <= 4
        assert request.headers["Accept-encoding"] == "identity"
        assert request.headers["X-dlr-claim-token"] == "test-only"
        response.url = request.full_url
        return response

    monkeypatch.setattr(module, "_open_v4", opened)
    try:
        yield module, task, directory, response, content, tmp_path
    finally:
        # Even an intentionally unknown test close is cleaned only by the
        # fixture after assertions; production retains it until child reap.
        response.fail_close = False
        io.BytesIO.close(response)
        os.close(directory)


def test_worker_publishes_only_verified_readonly_input_set(file_staging):
    module, task, directory, response, content, root = file_staging
    records, manifest, used = module.stage(
        None, task, directory, budget=2 * len(content) + 4096, deadline=time.monotonic() + 5
    )
    assert used == len(content) and response.closed and response.close_calls == 1
    assert not (root / "input.part").exists()
    path = root / "input" / "input-00.txt"
    assert path.read_bytes() == content
    assert path.stat().st_mode & 0o777 == 0o444
    assert path.parent.stat().st_mode & 0o777 == 0o555
    assert module.receipt(records) == records
    assert manifest[0]["artifact_id"] == 1 and manifest[0]["sha256"] == records[0]["sha256"]


def test_worker_can_publish_a_verified_empty_file(file_staging):
    module, task, directory, response, _content, root = file_staging
    task.input_files[0].size_bytes = 0
    task.input_files[0].sha256 = hashlib.sha256(b"").hexdigest()
    response.truncate(0)
    response.headers.replace_header("Content-Length", "0")
    records, _, used = module.stage(
        None, task, directory, budget=4096, deadline=time.monotonic() + 5
    )
    assert used == 0 and response.closed
    assert (root / "input" / records[0]["mount_name"]).read_bytes() == b""


def test_asgi_file_response_rejects_transport_variants_before_slot_and_closes_body(monkeypatch):
    """ASGI framing/cleanup only; PG tests separately verify real signed grants."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from dlr.control.api import worker_input_v4 as module

    monkeypatch.setattr(module, "_clock", time.monotonic)
    instance = uuid4()
    monkeypatch.setattr(
        module,
        "parse_untrusted_attempt_proof",
        lambda _value: SimpleNamespace(
            worker_id=1,
            instance_id=instance,
            generation=1,
            attempt_id=2,
            fencing_token=3,
        ),
    )
    events = []
    parts = iter([b"abc", b""])
    entry = SimpleNamespace(deadline=time.monotonic() + 5, size=3, closed=False)

    class Coordinator:
        def begin_input(self, _process, *, metadata):
            assert metadata["attempt_id"] == 2 and metadata["artifact_id"] == 4
            events.append("begin")
            return entry

        def read_input(self, _entry):
            assert not entry.closed
            events.append("read")
            return next(parts)

        def close_input(self, _entry):
            if not entry.closed:
                entry.closed = True
                events.append("close")

    monkeypatch.setattr(module, "_coordinator", lambda _request: Coordinator())
    monkeypatch.setattr(module, "_process", lambda _request: object())
    app = FastAPI()
    app.include_router(module.router)
    path = "/api/workers/1/v4/executions/3/input-artifacts/4/content"
    headers = {
        "Authorization": "Bearer test-only",
        "Accept-Encoding": "identity",
        "X-DLR-Worker-Instance": str(instance),
        "X-DLR-Worker-Generation": "1",
        "X-DLR-Worker-Lease-Proof": "test-only",
        "X-DLR-Worker-Attempt-Proof": "test-only",
        "X-DLR-Claim-Token": "test-only",
    }
    with TestClient(app) as client:
        assert client.get("http://testserver" + path, headers=headers).status_code == 503
        assert (
            client.get(
                "https://testserver" + path + "?token=forbidden", headers=headers
            ).status_code
            == 503
        )
        for extra in [
            {"Range": "bytes=0-1"},
            {"Accept-Encoding": "gzip"},
            {"Content-Encoding": "gzip"},
            {"X-DLR-Cleanup-Token": "forbidden"},
        ]:
            assert (
                client.get("https://testserver" + path, headers={**headers, **extra}).status_code
                == 503
            )
        assert events == []
        duplicate = list(headers.items()) + [("Accept-Encoding", "gzip")]
        assert client.get("https://testserver" + path, headers=duplicate).status_code == 503
        assert events == []
        response = client.get("https://testserver" + path, headers=headers)
    assert response.status_code == 200 and response.content == b"abc"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-length"] == "3"
    assert events == ["begin", "read", "read", "close"]


@pytest.mark.parametrize("case", ["short", "extra", "hash", "copies"])
def test_worker_known_file_failure_closes_io_without_publishing(file_staging, case):
    module, task, directory, response, content, root = file_staging
    budget = 2 * len(content) + 4096
    if case == "short":
        response.truncate(len(content) - 1)
    elif case == "extra":
        response.seek(0, 2)
        response.write(b"x")
        response.seek(0)
    elif case == "hash":
        task.input_files[0].sha256 = "0" * 64
    else:
        budget = len(content) + 4096
    with pytest.raises(module.FilePreparationFailed):
        module.stage(None, task, directory, budget=budget, deadline=time.monotonic() + 5)
    assert list((root / "input").iterdir()) == []
    assert not (root / "input.part").exists()
    if case != "copies":
        assert response.closed and response.close_calls == 1


def test_worker_unknown_close_is_not_retried_or_reported_as_known_failure(file_staging):
    module, task, directory, response, content, root = file_staging
    response.fail_close = True
    response.truncate(len(content) - 1)
    with pytest.raises(module.FileIOUnknown):
        module.stage(
            None, task, directory, budget=2 * len(content) + 4096, deadline=time.monotonic() + 5
        )
    assert response.close_calls == 1
    assert (root / "input.part").exists()


def test_reaped_staging_cleanup_refuses_links_without_deleting_target(tmp_path):
    from dlr.worker import managed_files_v4 as module

    part = tmp_path / "input.part"
    part.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.write_bytes(b"must remain")
    (part / "input-00.txt").symlink_to(outside)
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(module.FileIOUnknown):
            module.discard_after_reap(directory, 7)
    finally:
        os.close(directory)
    assert outside.read_bytes() == b"must remain" and (part / "input-00.txt").is_symlink()


def test_normal_guardian_cannot_attest_preparation_failure():
    from dlr.worker.attempt_supervisor_v4 import AttemptSupervisor

    supervisor = AttemptSupervisor(None, None, None)
    with pytest.raises(ValueError, match="preparation stop unverified"):
        supervisor._collect_terminal(
            {"event": "stopped", "cgroup_drained": True, "stop_reason": "preparation_failed"},
            time.monotonic() + 1,
        )


def test_preparation_guardian_refuses_both_business_launch_operations():
    from dlr.worker.attempt_supervisor_v4 import AttemptSupervisor

    supervisor = AttemptSupervisor(None, None, None, sandbox_launch=True, preparation_only=True)
    assert supervisor.prepare_sandbox() is False
    assert supervisor.release_sandbox(send_with_authority=lambda _send: True) is False


def test_file_admission_holds_original_lock_order_but_body_ack_has_no_renewal_lock(monkeypatch):
    from dlr.worker.claim_preparation_v4 import ClaimPreparer

    attempt_lock, parent_lock = threading.Lock(), threading.Lock()
    parent = SimpleNamespace(
        _lock=parent_lock,
        _proof="instance-current",
        _claims=SimpleNamespace(generation=2),
        _base_url="https://control.invalid",
        _worker_id=1,
        _instance_id=uuid4(),
        _credential="test-only",
    )

    def live():
        assert attempt_lock.locked() and parent_lock.locked()
        return None, "attempt-current", 20.0

    prepared = SimpleNamespace(
        attempt=SimpleNamespace(
            _lock=attempt_lock,
            _parent=parent,
            _start_confirmed=True,
            _claim_token="test-only",
            _live_locked=live,
        )
    )
    frames = []

    def sent(_prepared, value, deadline, _stop):
        if value == {"op": "read_input"}:
            assert not attempt_lock.locked() and not parent_lock.locked()
        else:
            assert attempt_lock.locked() and parent_lock.locked()
            assert value["instance_proof"] == "instance-current"
            assert value["attempt_proof"] == "attempt-current"
        assert deadline == 10.0
        frames.append(value)

    def admitted(_prepared, _deadline, _stop):
        assert attempt_lock.locked() and parent_lock.locked()
        return {"state": "input_admitted", "ordinal": 0}

    preparer = ClaimPreparer(managed_files=True)
    monkeypatch.setattr(preparer, "_stage_send", sent)
    monkeypatch.setattr(preparer, "_stage_frame", admitted)
    preparer._authorize_file(prepared, 0, 10.0, threading.Event())
    assert len(frames) == 2


@pytest.fixture
def file_grant(api_client, session_factory):
    """Persisted service fixture, not an enrollment, HTTP or physical-stop proof."""
    from sqlalchemy import func, select

    from dlr.control.models import (
        Execution,
        ExecutionAttempt,
        ExecutionInputArtifactLease,
        ManagedInputArtifact,
        Worker,
    )
    from test_issue127_c0 import _create_staged_artifact
    from test_issue129_attempt_commit_v4 import _setup

    facts, authority, instance, candidate, action, proof = _setup(api_client, session_factory)
    artifact_id = _create_staged_artifact(session_factory, facts["adapter_id"], "fixture.txt")
    with session_factory.begin() as session:
        now = session.scalar(select(func.clock_timestamp()))
        worker = session.get(Worker, facts["worker_id"])
        worker.identity_lifecycle = "reserved"
        worker.isolation_capabilities = {contract.CAPABILITY: True}
        attempt = session.get(ExecutionAttempt, candidate.attempt_id)
        attempt.status, attempt.started_at = "running", now
        execution = session.get(Execution, facts["execution_id"])
        execution.status = "running"
        execution.input_source_type = "managed_files"
        execution.timeout_seconds_snapshot = 30
        execution.input_snapshot = {
            "source_type": "managed_files",
            "revision": 1,
            "artifacts": [
                {
                    "ordinal": 0,
                    "original_filename": "fixture.txt",
                    "content_type": "text/plain",
                    "size_bytes": 8,
                    "sha256": "a" * 64,
                }
            ],
        }
        artifact = session.get(ManagedInputArtifact, artifact_id)
        artifact.status = "READY"
        session.add(
            ExecutionInputArtifactLease(
                execution_id=execution.id,
                artifact_id=artifact_id,
                ordinal=0,
            )
        )
    kwargs = {
        "worker_id": facts["worker_id"],
        "execution_id": facts["execution_id"],
        "artifact_id": artifact_id,
        "credential": facts["credential"],
        "instance_id": facts["instance_id"],
        "generation": 1,
        "instance_proof": instance,
        "attempt_proof": proof,
        "action": action,
        "authority": authority,
    }
    return kwargs, facts, candidate


def test_pg_file_admission_requires_both_current_proofs_and_exact_frozen_lease(
    file_grant, session_factory
):
    from dlr.control.services import managed_input_download_v4 as module

    kwargs, _facts, _candidate = file_grant
    with session_factory() as session:
        grant, budget = module.admit(session, **kwargs)
        assert 0 < budget <= contract.FILE_TIMEOUT_SECONDS
        assert grant.file.artifact_id == kwargs["artifact_id"]
        session.rollback()
    for changed in [
        {"worker_id": kwargs["worker_id"] + 1},
        {"execution_id": kwargs["execution_id"] + 1},
        {"artifact_id": kwargs["artifact_id"] + 1},
        {"instance_proof": "invalid"},
        {"attempt_proof": "invalid"},
        {"action": kwargs["action"].model_copy(update={"claim_token": "invalid"})},
    ]:
        with (
            session_factory() as session,
            pytest.raises(
                (module.InputDownloadUnavailable, AttemptLeaseCandidateError, WorkerCredentialError)
            ),
        ):
            module.admit(session, **{**kwargs, **changed})


def test_pg_fresh_proofs_do_not_restart_original_claim_preparation_cutoff(
    file_grant, session_factory
):
    from datetime import timedelta

    from sqlalchemy import func, select

    from dlr.control.models import AdapterExecutionSlot, ExecutionAttempt
    from dlr.control.services import managed_input_download_v4 as module

    kwargs, facts, candidate = file_grant
    with session_factory.begin() as session:
        now = session.scalar(select(func.clock_timestamp()))
        session.get(ExecutionAttempt, candidate.attempt_id).claimed_at = now - timedelta(seconds=6)
    with session_factory() as session, pytest.raises(module.InputDownloadUnavailable):
        module.admit(session, **kwargs)
    with session_factory() as session:
        attempt = session.get(ExecutionAttempt, candidate.attempt_id)
        assert attempt.status == "running"
        assert (
            session.get(AdapterExecutionSlot, (facts["adapter_id"], 0)).active_attempt_id
            == candidate.attempt_id
        )


def test_pg_renewal_keeps_original_read_grant_but_old_proof_cannot_start_another(
    file_grant, session_factory
):
    from dlr.control.services import managed_input_download_v4 as module
    from dlr.control.services.attempt_lease_candidates_v4 import stage_attempt_renewal

    kwargs, _facts, candidate = file_grant
    with session_factory() as session:
        grant, _ = module.admit(session, **kwargs)
        session.rollback()
    original_until = grant.original_until
    args = {k: v for k, v in kwargs.items() if k not in {"execution_id", "artifact_id"}}
    args["attempt_id"] = candidate.attempt_id
    with session_factory.begin() as session:
        stage_attempt_renewal(session, **args)
    with session_factory() as session:
        module.recheck(session, grant)
        assert grant.original_until == original_until
        session.rollback()
    with session_factory() as session, pytest.raises(AttemptLeaseCandidateError):
        module.admit(session, **kwargs)


@pytest.mark.parametrize("change", ["cancel", "revoke", "slot", "artifact"])
def test_pg_admitted_file_stream_stops_on_revocation_or_ownership_change(
    file_grant, session_factory, change
):
    from dlr.control.models import (
        AdapterExecutionSlot,
        Execution,
        ManagedInputArtifact,
        WorkerCredential,
    )
    from dlr.control.services import managed_input_download_v4 as module
    from dlr.control.services.worker_availability import current_time

    kwargs, facts, _ = file_grant
    with session_factory() as session:
        grant, _ = module.admit(session, **kwargs)
        session.rollback()
    with session_factory.begin() as session:
        if change == "cancel":
            session.get(Execution, facts["execution_id"]).cancel_requested = True
        elif change == "revoke":
            session.get(WorkerCredential, facts["credential_id"]).revoked_at = current_time(session)
        elif change == "slot":
            session.get(AdapterExecutionSlot, (facts["adapter_id"], 0)).active_attempt_id = None
        else:
            session.get(ManagedInputArtifact, kwargs["artifact_id"]).sha256 = "b" * 64
    with (
        session_factory() as session,
        pytest.raises((module.InputDownloadUnavailable, WorkerCredentialError)),
    ):
        module.recheck(session, grant)


@pytest.mark.parametrize("mode,max_attempts", [("legacy", 3), ("v4", 3), ("v4", 2)])
def test_pg_preparation_result_preserves_frozen_retry_and_retains_cleanup_slot(
    file_grant, session_factory, monkeypatch, mode, max_attempts
):
    """Terminal classifier only; this is not a result-intent or physical-stop receipt."""
    from dlr.common.config import settings
    from dlr.control.models import AdapterExecutionSlot, Execution, ExecutionAttempt
    from dlr.control.schemas.reliable_runtime import AttemptPrepareFailedBody, AttemptResultBody
    from dlr.control.services import attempt as attempts
    from dlr.control.services.attempt_lease_candidates_v4 import validate_current_attempt_proof

    kwargs, facts, candidate = file_grant
    frozen = {
        "max_attempts": max_attempts,
        "initial_backoff_seconds": 7,
        "multiplier": 2,
        "max_backoff_seconds": 60,
        "jitter_ratio": 0,
        "retryable_error_classes": ["platform_transient"],
    }
    with session_factory.begin() as session:
        session.get(Execution, facts["execution_id"]).retry_policy_snapshot = frozen
    monkeypatch.setattr(settings, "execution_retry_max_attempts", 1)
    monkeypatch.setattr(settings, "execution_retry_initial_backoff_seconds", 59)
    with session_factory() as session:
        if mode == "legacy":
            attempts.prepare_failed(
                session,
                kwargs["worker_id"],
                candidate.attempt_id,
                AttemptPrepareFailedBody(**kwargs["action"].model_dump()),
            )
        else:
            action = AttemptResultBody(
                **kwargs["action"].model_dump(),
                status="failed",
                error_code="attempt_prepare_failed",
                error_class="platform_transient",
            )

            def check():
                validate_current_attempt_proof(
                    session,
                    **{k: v for k, v in kwargs.items() if k not in {"execution_id", "artifact_id"}},
                    attempt_id=candidate.attempt_id,
                )

            checked_terminal = []
            attempts.finish_attempt(
                session,
                kwargs["worker_id"],
                candidate.attempt_id,
                action,
                authority=attempts.V4AttemptAuthority(
                    kwargs["credential"], kwargs["instance_id"], 1
                ),
                _proof_check=check,
                _proof_after_terminal=lambda: checked_terminal.append(
                    session.get(ExecutionAttempt, candidate.attempt_id).status
                ),
            )
            assert checked_terminal == ["failed"]
    with session_factory() as session:
        execution = session.get(Execution, facts["execution_id"])
        attempt = session.get(ExecutionAttempt, candidate.attempt_id)
        assert execution.retry_policy_snapshot == frozen and execution.attempt_count == 2
        assert attempt.status == "failed" and attempt.error_code == "attempt_prepare_failed"
        assert (
            session.get(AdapterExecutionSlot, (facts["adapter_id"], 0)).active_attempt_id
            == attempt.id
        )
        assert attempt.cleanup_settled_at is None
        if max_attempts == 3:
            assert execution.status == "retry_wait"
            assert (execution.next_attempt_at - attempt.ended_at).total_seconds() == 14
        else:
            assert execution.status == "dead_letter" and execution.next_attempt_at is None


def descriptor(index=0, **changes):
    return {
        "id": index + 1,
        "ordinal": index,
        "mount_name": f"input-{index:02d}.txt",
        "original_filename": "example.txt",
        "content_type": "text/plain",
        "size_bytes": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
        **changes,
    }


def test_file_snapshot_accepts_empty_files_and_exact_four_file_limit():
    assert contract.descriptors([descriptor()])[0].size_bytes == 0
    maximum = [descriptor(i, size_bytes=contract.MAX_FILE_BYTES) for i in range(4)]
    assert sum(x.size_bytes for x in contract.descriptors(maximum)) == contract.MAX_TOTAL_BYTES
    for value in [
        [],
        maximum + [descriptor(4)],
        [descriptor(size_bytes=contract.MAX_FILE_BYTES + 1)],
    ]:
        with pytest.raises(contract.ManagedFilesError, match="input_files_budget"):
            contract.descriptors(value)


@pytest.mark.parametrize(
    "change",
    [
        {"id": True},
        {"id": 0},
        {"ordinal": True},
        {"ordinal": 1},
        {"size_bytes": True},
        {"size_bytes": -1},
        {"mount_name": "../outside"},
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"storage_key": "a" * 64},
        {"content_type": None},
        {"original_filename": None},
        {"original_filename": "\ud800.txt"},
    ],
)
def test_file_snapshot_rejects_ambiguous_or_path_bearing_metadata(change):
    with pytest.raises(contract.ManagedFilesError):
        contract.descriptors([descriptor(**change)])


def test_file_snapshot_cannot_duplicate_artifact_or_reorder_ordinal():
    for value in [[descriptor(), descriptor(1, id=1)], [descriptor(1), descriptor()]]:
        with pytest.raises(contract.ManagedFilesError):
            contract.descriptors(value)


def test_file_capability_is_strict_and_old_ready_reports_default_disabled():
    from dlr.control.api.worker_instance_v4 import _ReadyReport

    assert _ReadyReport(isolation_capabilities={}).managed_files_v1 is False
    assert _ReadyReport(isolation_capabilities={}, managed_files_v1=True).managed_files_v1 is True
    for value in [1, 0, "true", None, []]:
        with pytest.raises(ValidationError):
            _ReadyReport(isolation_capabilities={}, managed_files_v1=value)


def publish(store, key, content=b"fixture"):
    with store.put_part(key) as stream:
        stream.write(content)
    store.commit(key)


def test_another_process_cannot_delete_until_every_reader_has_closed(tmp_path):
    store = artifact_store.LocalFileArtifactStore(tmp_path / "store")
    key = store.new_storage_key()
    publish(store, key)
    first, second = store.open(key), store.open(key)
    program = """
import sys
from dlr.control.services.artifact_store import LocalFileArtifactStore, ArtifactStoreBusyError
try:
    LocalFileArtifactStore(sys.argv[1]).delete(sys.argv[2])
except ArtifactStoreBusyError:
    print('busy')
else:
    print('deleted')
"""

    def deleting():
        return subprocess.run(
            [sys.executable, "-c", program, str(store.root), key],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()

    try:
        assert deleting() == "busy"
        first.close()
        assert deleting() == "busy"
        assert second.read() == b"fixture"
    finally:
        first.close()
        second.close()
    assert deleting() == "deleted"


def test_reader_also_excludes_publish_and_quarantine_on_colliding_stripe(tmp_path):
    store = artifact_store.LocalFileArtifactStore(tmp_path / "store")
    keys = {}
    for n in range(257):
        key = f"{n:064x}"
        stripe = hashlib.sha256(key.encode("ascii")).digest()[0]
        if stripe in keys:
            first, other = keys[stripe], key
            break
        keys[stripe] = key
    else:
        raise AssertionError("257 keys must collide in 256 stripes")
    publish(store, first)
    with store.put_part(other) as stream:
        stream.write(b"second")
    with store.open(first):
        for operation, key in [(store.commit, other), (store.quarantine, first)]:
            with pytest.raises(artifact_store.ArtifactStoreBusyError):
                operation(key)
        assert store.stat(first) is not None
        assert store.stat(other) is None
    store.commit(other)
    assert store.quarantine(first)
    with store.open(other) as stream:
        assert stream.read() == b"second"


def test_permanent_lock_namespace_is_bounded_and_private(tmp_path):
    store = artifact_store.LocalFileArtifactStore(tmp_path / "store")
    for n in range(512):
        fd = store._acquire_object_lock(f"{n:064x}", shared=True)
        os.close(fd)
    files = list(store.locks_root.iterdir())
    assert 1 <= len(files) <= 256
    assert all(len(x.name) == 2 and x.stat().st_mode & 0o777 == 0o600 for x in files)
    key = store.new_storage_key()
    publish(store, key)
    before = {x.name: x.stat().st_ino for x in store.locks_root.iterdir()}
    store.delete(key)
    assert {x.name: x.stat().st_ino for x in store.locks_root.iterdir()} == before


def test_lock_entry_replaced_during_acquisition_is_rejected(tmp_path, monkeypatch):
    store = artifact_store.LocalFileArtifactStore(tmp_path / "store")
    key = store.new_storage_key()
    lock = store.locks_root / hashlib.sha256(key.encode("ascii")).hexdigest()[:2]
    original = artifact_store.fcntl.flock

    def replacing(fd, operation):
        original(fd, operation)
        lock.rename(lock.with_suffix(".old"))
        lock.touch(mode=0o600)

    monkeypatch.setattr(artifact_store.fcntl, "flock", replacing)
    with pytest.raises(artifact_store.ArtifactStoreSecurityError):
        store._acquire_object_lock(key, shared=True)


def test_uncertain_object_close_does_not_release_the_reader_lock(tmp_path):
    store = artifact_store.LocalFileArtifactStore(tmp_path / "store")
    key = store.new_storage_key()
    publish(store, key)
    lock = store._acquire_object_lock(key, shared=True)

    class FailingClose(io.FileIO):
        def close(self):
            raise OSError("injected uncertain close")

    raw = FailingClose(store.object_path(key), "rb")
    reader = artifact_store._ObjectReader(raw, lock)
    try:
        with pytest.raises(OSError, match="uncertain close"):
            reader.close()
        with pytest.raises(artifact_store.ArtifactStoreError, match="uncertain"):
            reader.close()
        with pytest.raises(artifact_store.ArtifactStoreBusyError):
            store.delete(key)
    finally:
        # Fixture-only recovery after independently closing the real fd.
        io.FileIO.close(raw)
        os.close(reader._store_lock_fd)
        reader._store_lock_fd = -1


def test_uncertain_lock_close_also_retains_reader_ownership(tmp_path, monkeypatch):
    store = artifact_store.LocalFileArtifactStore(tmp_path / "store")
    key = store.new_storage_key()
    publish(store, key)
    reader = store.open(key)
    lock = reader._store_lock_fd
    original = artifact_store.os.close

    def failing(fd):
        if fd == lock:
            raise OSError("injected lock close uncertainty")
        return original(fd)

    try:
        with monkeypatch.context() as changes:
            changes.setattr(artifact_store.os, "close", failing)
            with pytest.raises(OSError, match="lock close uncertainty"):
                reader.close()
        with pytest.raises(artifact_store.ArtifactStoreError, match="uncertain"):
            reader.close()
        with pytest.raises(artifact_store.ArtifactStoreBusyError):
            store.delete(key)
    finally:
        original(lock)
        reader._store_lock_fd = -1


@contextmanager
def input_frames(monkeypatch, serve):
    """Real local IPC only; authority/resource stand-ins are not PG proofs."""
    from sqlalchemy import create_engine

    from dlr.control.services import claim_coordinator_v4 as module

    # Protocol-only fixture: Linux production continues to require BOOTTIME.
    monkeypatch.setattr(module, "_clock", time.monotonic)
    engine = create_engine("sqlite://")  # Never connected, used only for URL identity.
    parent, child = socket.socketpair()
    parent.settimeout(1)
    child.settimeout(1)
    coordinator = module._ClaimSlot()
    now = time.monotonic()
    authority = SimpleNamespace(
        _sessions=SimpleNamespace(kw={"bind": engine}),
        check_authority=lambda: None,
        claim_child_identity=lambda: (str(uuid4()), "test-only", now + 30),
    )
    bound = {
        "operation": "stage",
        "worker_id": 1,
        "attempt_id": 9,
        "credential": "test-only",
        "instance_id": str(uuid4()),
        "generation": 1,
    }
    configuration = module.settings.model_dump(mode="json")
    configuration["database_url"] = engine.url.render_as_string(hide_password=False)
    entry = module._Result(
        SimpleNamespace(poll=lambda: None),
        SimpleNamespace(),
        -1,
        parent,
        SimpleNamespace(check=lambda: None, close=lambda: None),
        authority,
        now + 30,
        0,
        configuration=configuration,
        pending_stage=bound,
        retention_deadline=now + 30,
        input_cutoff=now + 4,
    )
    coordinator._result = entry
    monkeypatch.setattr(coordinator, "_discard_unpublished", lambda *_args: True)
    errors = []

    def run():
        try:
            serve(child, module)
        except BaseException as error:
            errors.append(type(error).__name__)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    metadata = {
        **bound,
        "operation": "input",
        "execution_id": 2,
        "artifact_id": 3,
        "instance_proof": "test-only",
        "attempt_proof": "test-only",
        "fencing_token": 1,
        "claim_token": "test-only",
    }
    try:
        yield coordinator, entry, authority, metadata, module
    finally:
        if coordinator._result is entry:
            coordinator.end_result(entry)
        parent.close()
        child.close()
        thread.join(timeout=2)
        engine.dispose()
        assert not thread.is_alive()
        assert not errors, errors


def test_input_frames_reuse_claim_slot_and_bound_every_chunk(monkeypatch):
    data = b"x" * (contract.CHUNK_BYTES + 1)

    def server(channel, module):
        request = module._read_frame(channel)
        assert request["operation"] == "input"
        module._send_frame(
            channel,
            {
                "state": "input_ready",
                "size": len(data),
                "stream_id": str(uuid4()),
                "control_deadline": request["control_deadline"],
            },
        )
        for index, part in enumerate([data[: contract.CHUNK_BYTES], data[contract.CHUNK_BYTES :]]):
            assert module._read_frame(channel) == {"op": "read_input"}
            module._send_frame(
                channel,
                {
                    "state": "input_chunk",
                    "bytes": len(part),
                    **({"closed": True} if index == 1 else {}),
                },
            )
            channel.sendall(part)

    with input_frames(monkeypatch, server) as (slot, entry, authority, metadata, _module):
        stream = slot.begin_input(authority, metadata)
        before = entry.process
        assert slot.read_input(stream) == data[: contract.CHUNK_BYTES]
        assert slot.read_input(stream) == b"x"
        assert slot.read_input(stream) == b""
        assert stream.closed and not entry.input_active and not slot._spawning
        assert slot._result is entry and entry.process is before
        assert entry.pending_stage is not None


def test_empty_file_releases_read_phase_before_headers_without_another_rpc(monkeypatch):
    def server(channel, module):
        request = module._read_frame(channel)
        module._send_frame(
            channel,
            {
                "state": "input_ready",
                "size": 0,
                "stream_id": str(uuid4()),
                "control_deadline": request["control_deadline"],
                "closed": True,
            },
        )

    with input_frames(monkeypatch, server) as (slot, entry, authority, metadata, _):
        stream = slot.begin_input(authority, metadata)
        assert stream.closed and not entry.input_active and not slot._spawning
        assert slot.read_input(stream) == b""


def test_control_file_cutoff_is_frozen_before_finalize_rpc(monkeypatch):
    from dlr.control.services import claim_coordinator_v4 as module

    clock = [100.0]
    monkeypatch.setattr(module, "_clock", lambda: clock[0])
    instance = uuid4()
    entry = SimpleNamespace(
        state="ready",
        token="proposal",
        worker_id=1,
        instance_id=instance,
        generation=1,
        credential="test-only",
        authority=SimpleNamespace(check_authority=lambda: None),
        envelope=object(),
        deadline=120.0,
        input_cutoff=None,
        channel=SimpleNamespace(settimeout=lambda _value: None),
    )
    slot = module._ClaimSlot()
    slot._current = entry
    monkeypatch.setattr(slot, "_check_envelope", lambda *_a, **_k: None)
    monkeypatch.setattr(slot, "_expire", lambda _entry: True)
    observed = []

    def sending(_channel, request):
        observed.append(request["input_cutoff"])
        # A blocked RPC cannot move the already frozen preparation deadline.
        clock[0] = 104.0
        assert entry.input_cutoff == request["input_cutoff"] < 105.0
        raise OSError("test-only RPC failure")

    monkeypatch.setattr(module, "_send_frame", sending)
    with pytest.raises(module.ClaimUnavailable):
        slot.finalize(
            token="proposal",
            worker_id=1,
            instance_id=instance,
            generation=1,
            credential="test-only",
            instance_proof="test-only",
        )
    assert len(observed) == 1 and entry.input_cutoff == observed[0]


def test_later_file_gets_share_original_cutoff_and_late_get_keeps_result_owner(monkeypatch):
    seen = []

    def server(channel, module):
        for _ in range(2):
            request = module._read_frame(channel)
            seen.append(request["control_deadline"])
            module._send_frame(
                channel,
                {
                    "state": "input_ready",
                    "size": 0,
                    "stream_id": str(uuid4()),
                    "control_deadline": request["control_deadline"],
                    "closed": True,
                },
            )

    with input_frames(monkeypatch, server) as (slot, entry, authority, metadata, module):
        now = time.monotonic()
        clock = [now]
        monkeypatch.setattr(module, "_clock", lambda: clock[0])
        cutoff = entry.input_cutoff
        assert slot.begin_input(authority, metadata).closed
        clock[0] += 2
        assert slot.begin_input(authority, {**metadata, "artifact_id": 4}).closed
        assert seen == [cutoff, cutoff]
        clock[0] = cutoff + 0.01
        with pytest.raises(module.ClaimUnavailable):
            slot.begin_input(authority, {**metadata, "artifact_id": 5})
        assert slot._result is entry and not slot._poisoned
        assert not slot._spawning and not entry.input_active


@pytest.mark.parametrize("crossing", ["authority", "identity"])
def test_cutoff_crossed_before_input_ipc_preserves_original_result_owner(monkeypatch, crossing):
    with input_frames(monkeypatch, lambda *_: None) as (slot, entry, authority, metadata, module):
        clock = [entry.input_cutoff - 1.0]
        monkeypatch.setattr(module, "_clock", lambda: clock[0])
        identity = authority.claim_child_identity

        def expire():
            clock[0] = entry.input_cutoff

        def identify():
            expire()
            return identity()

        if crossing == "authority":
            authority.check_authority = expire
        else:
            authority.claim_child_identity = identify
        sent = []
        monkeypatch.setattr(module, "_send_frame", lambda *_: sent.append(True))
        with pytest.raises(module.ClaimUnavailable):
            slot.begin_input(authority, metadata)
        assert not sent
        assert slot._result is entry and not entry.closed
        assert not slot._spawning and not entry.input_active
        assert not slot._poisoned and not slot._unresolved


def test_result_child_clamps_every_file_to_claim_cutoff_and_rejects_late_get(monkeypatch):
    from dlr.common import config
    from dlr.control.services import claim_envelope_v4, input_child_v4, result_child_v4

    clock = [100.0]
    closed, sent, served = [], [], []
    monkeypatch.setattr(config, "settings", config.settings)
    snapshot = config.settings.model_dump(mode="json")
    monkeypatch.setattr(result_child_v4, "_clock", lambda: clock[0])
    monkeypatch.setattr(
        claim_envelope_v4.ClaimEnvelope,
        "open",
        lambda **_kw: SimpleNamespace(close=lambda: closed.append(True)),
    )
    requests = iter([(101.0, 1), (103.0, 2), (105.0, 3)])

    def read(_channel):
        try:
            clock[0], artifact = next(requests)
        except StopIteration:
            raise EOFError from None
        return {
            "operation": "input",
            "artifact_id": artifact,
            "settings": snapshot,
            "control_deadline": clock[0] + 5,
        }

    monkeypatch.setattr(result_child_v4, "_read_frame", read)
    monkeypatch.setattr(result_child_v4, "_send_frame", lambda _c, value: sent.append(value))
    monkeypatch.setattr(
        input_child_v4,
        "serve",
        lambda _c, request, _e, **_kw: served.append(request["control_deadline"]),
    )
    with pytest.raises(EOFError):
        result_child_v4._run(
            SimpleNamespace(settimeout=lambda _t: None),
            512 * 1024 * 1024,
            _stage_binding={"operation": "stage"},
            _idle_deadline=120.0,
            _input_cutoff=104.0,
        )
    assert served == [104.0, 104.0]
    assert sent == [{"state": "input_rejected"}]
    assert closed == [True]


def test_last_data_without_physical_close_marker_retains_unknown_slot(monkeypatch):
    def server(channel, module):
        request = module._read_frame(channel)
        module._send_frame(
            channel,
            {
                "state": "input_ready",
                "size": 1,
                "stream_id": str(uuid4()),
                "control_deadline": request["control_deadline"],
            },
        )
        assert module._read_frame(channel) == {"op": "read_input"}
        module._send_frame(channel, {"state": "input_chunk", "bytes": 1})

    with input_frames(monkeypatch, server) as (slot, entry, authority, metadata, module):
        stream = slot.begin_input(authority, metadata)
        with pytest.raises(module.ClaimUnavailable):
            slot.read_input(stream)
        assert slot._poisoned and entry in slot._unresolved


@pytest.mark.parametrize("data", [b"", b"final bounded block"])
def test_control_fd_is_really_closed_before_empty_headers_or_last_bytes(
    tmp_path, monkeypatch, data
):
    """Real Store/socket I/O ordering; transaction/envelope stand-ins are not authority proofs."""
    from dlr.common import config
    from dlr.control import db
    from dlr.control.services import (
        attempt_result_finalize_v4,
        control_declaration_v4,
        input_child_v4,
        managed_input_download_v4,
    )
    from dlr.control.services.claim_child_v4 import _read_frame, _send_frame

    store = artifact_store.LocalFileArtifactStore(tmp_path / "store")
    key = store.new_storage_key()
    publish(store, key, data)
    monkeypatch.setattr(config.settings, "artifact_store_root", str(store.root))
    monkeypatch.setattr(input_child_v4, "_clock", time.monotonic)
    declaration = object()
    monkeypatch.setattr(control_declaration_v4, "read_declaration", lambda _settings: declaration)
    monkeypatch.setattr(
        attempt_result_finalize_v4,
        "_control_authority",
        lambda *_: (
            SimpleNamespace(check=lambda: None),
            None,
        ),
    )

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def rollback(self):
            pass

    monkeypatch.setattr(db, "SessionLocal", Session)
    grant = SimpleNamespace(
        stream_id=uuid4(),
        storage_key=key,
        file=contract.InputFile(1, 0, "input-00.txt", len(data), hashlib.sha256(data).hexdigest()),
    )
    monkeypatch.setattr(managed_input_download_v4, "admit", lambda *_a, **_k: (grant, 3.0))
    monkeypatch.setattr(managed_input_download_v4, "recheck", lambda *_a: None)
    observed = []

    def sending(channel, value):
        if value.get("closed") is True:
            # A second writer really acquires the native exclusive lock here.
            # The test deliberately bypasses DB Lease policy to inspect only
            # the physical fd/lock lifetime before any final transport bytes.
            assert store.delete(key) is True
            observed.append("physically_closed")
        _send_frame(channel, value)

    monkeypatch.setattr(input_child_v4, "_send_frame", sending)
    parent, child = socket.socketpair()
    parent.settimeout(2)
    request = {
        "control_deadline": time.monotonic() + 3,
        "control_instance": str(uuid4()),
        "control_token": "test-only",
        "worker_id": 1,
        "execution_id": 1,
        "artifact_id": 1,
        "generation": 1,
        "instance_id": str(uuid4()),
        "instance_proof": "test-only",
        "attempt_proof": "test-only",
        "attempt_id": 1,
        "fencing_token": 1,
        "claim_token": "test-only",
        "credential": "test-only",
    }
    errors = []

    def serve():
        try:
            input_child_v4.serve(
                child, request, SimpleNamespace(check=lambda: None), idle_until=time.monotonic() + 5
            )
        except BaseException as error:
            errors.append(type(error).__name__)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        ready = _read_frame(parent)
        assert ready["size"] == len(data)
        if data:
            with pytest.raises(artifact_store.ArtifactStoreBusyError):
                store.delete(key)
            _send_frame(parent, {"op": "read_input"})
            assert _read_frame(parent) == {
                "state": "input_chunk",
                "bytes": len(data),
                "closed": True,
            }
            assert parent.recv(len(data)) == data
        else:
            assert ready["closed"] is True
        thread.join(timeout=2)
        assert observed == ["physically_closed"] and not errors
        assert not thread.is_alive()
    finally:
        parent.close()
        child.close()
        thread.join(timeout=2)


def test_input_frames_exclude_result_exchange_until_confirmed_close(monkeypatch):
    def server(channel, module):
        request = module._read_frame(channel)
        module._send_frame(
            channel,
            {
                "state": "input_ready",
                "size": 1,
                "stream_id": str(uuid4()),
                "control_deadline": request["control_deadline"],
            },
        )
        assert module._read_frame(channel) == {"op": "cancel_input"}
        module._send_frame(channel, {"state": "input_closed"})

    with input_frames(monkeypatch, server) as (slot, entry, authority, metadata, module):
        stream = slot.begin_input(authority, metadata)
        with pytest.raises(module.ClaimUnavailable):
            slot.begin_result(authority, metadata={**metadata, "operation": "stage"}, body_bytes=2)
        assert entry.input_active and slot._result is entry
        slot.close_input(stream)
        assert stream.closed and not slot._spawning and not slot._poisoned
        assert slot._result is entry


def test_oversized_input_frame_retains_uncertain_slot(monkeypatch):
    def server(channel, module):
        request = module._read_frame(channel)
        module._send_frame(
            channel,
            {
                "state": "input_ready",
                "size": contract.MAX_FILE_BYTES,
                "stream_id": str(uuid4()),
                "control_deadline": request["control_deadline"],
            },
        )
        assert module._read_frame(channel) == {"op": "read_input"}
        module._send_frame(channel, {"state": "input_chunk", "bytes": contract.CHUNK_BYTES + 1})

    with input_frames(monkeypatch, server) as (slot, entry, authority, metadata, module):
        stream = slot.begin_input(authority, metadata)
        with pytest.raises(module.ClaimUnavailable):
            slot.read_input(stream)
        assert slot._poisoned and entry in slot._unresolved


def test_j_node_revoke_retains_running_file_attempt_and_all_existing_responsibility(
    file_grant, session_factory, tmp_path
):
    """Real PG rows/file preservation; not a physical-stop or full Worker proof."""
    from sqlalchemy import text

    from dlr.control.models import Worker, WorkerIdentityReservation
    from dlr.control.security import SUPERADMIN_PRINCIPAL
    from dlr.control.services import control_cluster_identity as control
    from dlr.control.services import managed_input_download_v4 as download
    from dlr.control.services import worker_credential_operations as operations
    from dlr.control.services import worker_credentials

    kwargs, facts, _ = file_grant
    declaration = control.ControlDeclaration(
        cluster_id=uuid4(),
        image_digest="sha256:" + "1" * 64,
        managed_fingerprint="2" * 64,
        builtin_fingerprint="3" * 64,
        attachment_fingerprint="4" * 64,
        capacity_fingerprint="5" * 64,
        master_key="m" * 64,
        admin_token="a" * 64,
        worker_token="w" * 64,
    )
    with session_factory.begin() as session:
        control.provision_authority(session, declaration, authority_epoch=uuid4())
        row = session.get(Worker, facts["worker_id"])
        row.scheduling_state = "disabled"
        identity = row.identity_id
        version = row.maintenance_version
        session.add(
            WorkerIdentityReservation(
                request_id=uuid4(), worker_id=row.id, name=row.name, actor_kind="superadmin"
            )
        )
    request = operations.RotateRequest(
        request_id=uuid4(),
        worker_identity_id=identity,
        expected_latest_credential_record_id=facts["credential_id"],
        expected_maintenance_version=version,
    )
    with session_factory.begin() as session, pytest.raises(operations.OperationConflict):
        operations.execute(
            session, facts["worker_id"], request, SUPERADMIN_PRINCIPAL, action="rotate"
        )
    with session_factory.begin() as session:
        session.get(Worker, facts["worker_id"]).scheduling_state = "active"
    with session_factory() as session:
        grant, _ = download.admit(session, **kwargs)
    journal = tmp_path / "retained-version3.json"
    journal.write_text('{"version":3,"unknown":true}')
    original = journal.read_bytes()
    tables = (
        "executions",
        "execution_attempts",
        "adapter_execution_slots",
        "execution_input_artifact_leases",
        "global_execution_admission",
        "adapter_execution_admission",
    )

    def snapshot():
        with session_factory() as session:
            return {
                t: session.execute(text(f"SELECT row_to_json(r) FROM {t} r")).scalars().all()
                for t in tables
            }

    before = snapshot()
    assert before["execution_input_artifact_leases"] and before["adapter_execution_slots"]
    with session_factory.begin() as session:
        payload = operations.RevokeRequest.model_validate(
            {
                k: v
                for k, v in request.model_dump().items()
                if k not in {"expected_maintenance_version", "overlap_seconds"}
            }
        )
        operations.execute(
            session, facts["worker_id"], payload, SUPERADMIN_PRINCIPAL, action="revoke"
        )
    assert snapshot() == before and journal.read_bytes() == original
    with (
        session_factory() as session,
        pytest.raises(
            (download.InputDownloadUnavailable, worker_credentials.WorkerCredentialError)
        ),
    ):
        download.recheck(session, grant)
