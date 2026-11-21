"""The standalone Worker must keep uncertain local responsibility."""

import json
import os
import socket
import struct
import sys
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dlr.control.schemas.reliable_runtime import V4TaskPayload
from dlr.worker import (
    agent_v4,
    attempt_supervisor_child_v4,
    attempt_supervisor_v4,
    broker_config_v4,
    cgroup_namespace,
    claim_http_v4,
    claim_preparation_v4,
    claim_task_child_v4,
    execution_supervision_v4,
    node_runtime_v4,
    sandbox,
    task_fingerprint_v4,
)
from worker_runtime_support import attempt_payload


@pytest.mark.skipif(sys.platform != "linux", reason="sealed launch and SCM_RIGHTS require Linux")
@pytest.mark.parametrize("failure", [None, "known", "unknown"])
def test_file_stage_keeps_frozen_h_fd_and_never_launches_business(tmp_path, monkeypatch, failure):
    """Actual staging and descriptor handoff; no cgroup or Control authority claim."""
    import hashlib
    import time

    from dlr.worker import managed_files_v4

    seal_api = (
        "F_ADD_SEALS",
        "F_GET_SEALS",
        "F_SEAL_WRITE",
        "F_SEAL_GROW",
        "F_SEAL_SHRINK",
        "F_SEAL_SEAL",
    )
    if any(type(getattr(sandbox.fcntl, name, None)) is not int for name in seal_api):
        with pytest.raises(ValueError, match="launch unavailable"):
            sandbox.create_sealed_launch(
                ["/usr/bin/python", "/attempt-2/harness.py"],
                {"PATH": "/usr/bin:/bin"},
                max_bytes=65536,
            )
        pytest.skip("Python build lacks Linux memfd sealing constants; Worker fails closed")

    body = b"file-fixture"
    entry = {
        "id": 1,
        "ordinal": 0,
        "mount_name": "input-00.txt",
        "original_filename": "fixture.txt",
        "content_type": "text/plain",
        "size_bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
    }
    task = V4TaskPayload.model_validate(
        attempt_payload(
            {
                "protocol_version": 4,
                "execution_id": 1,
                "attempt_id": 2,
                "adapter_id": 3,
                "version_id": 4,
                "latest_version_id": 4,
                "worker_identity_id": str(uuid4()),
                "instance_id": str(uuid4()),
                "instance_generation": 1,
                "language": "python",
                "code": "raise AssertionError('business must never run in staging')",
                "input": None,
                "input_source_type": "managed_files",
                "input_snapshot": {
                    "source_type": "managed_files",
                    "revision": 1,
                    "artifacts": [
                        {k: v for k, v in entry.items() if k not in {"id", "mount_name"}}
                    ],
                },
                "input_files": [entry],
            }
        )
    )
    task.credential_bindings = [
        {"binding_id": 1, "credential_id": 2, "env_key": "BOUND", "field": "token"}
    ]
    task.secrets = {"BOUND": "test-only-frozen-secret"}
    fingerprint = task_fingerprint_v4.fingerprint(task, task.claim_token).hex()
    root = tmp_path / "attempt-2" / "dlr-exec-1"
    root.mkdir(parents=True, mode=0o700)
    parent, child = socket.socketpair()
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(directory)

    def fetch(_channel, _task, _descriptor, fd, _cutoff):
        os.write(fd, body)
        if failure == "known":
            raise managed_files_v4.FilePreparationFailed()
        if failure == "unknown":
            raise managed_files_v4.FileIOUnknown()

    monkeypatch.setattr(managed_files_v4, "_fetch", fetch)
    command = {
        "op": "stage",
        "workspace": str(root),
        "workspace_device": info.st_dev,
        "workspace_inode": info.st_ino,
        "staging_bytes": 1 << 20,
        "launch_bytes": 65536,
        "fingerprint": fingerprint,
        "input_deadline": time.clock_gettime(time.CLOCK_BOOTTIME) + 5,
    }
    launch_fd = -1
    try:
        parent.sendmsg(
            [b"D"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", directory))]
        )
        if failure == "unknown":
            with pytest.raises(managed_files_v4.FileIOUnknown):
                claim_task_child_v4._stage_task(child, task, fingerprint, command)
        else:
            claim_task_child_v4._stage_task(child, task, fingerprint, command)
        assert claim_task_child_v4._receive(parent)["state"] == "input_staging"
        launch_fd = claim_task_child_v4._receive_fd(parent)
        _, environment = sandbox._read_sealed_launch(launch_fd, 65536)
        assert environment["DLR_SECRET_BOUND"] == "test-only-frozen-secret"
        assert not (root / "output.json").exists()
        if failure == "unknown":
            assert (root / "input.part").exists()
            assert not (root / "input_manifest.json").exists()
        else:
            frame = claim_task_child_v4._receive(parent)
            assert frame["state"] == ("preparation_failed" if failure else "staged")
            manifest = json.loads((root / "input_manifest.json").read_text())
            assert len(manifest["files"]) == (0 if failure else 1)
            assert not (root / "input.part").exists()
    finally:
        if (root / "input").exists():
            (root / "input").chmod(0o700)
        if launch_fd >= 0:
            os.close(launch_fd)
        os.close(directory)
        parent.close()
        child.close()


@pytest.mark.parametrize("ending", ["decision", "deadline", "stop"])
def test_claim_prepare_keeps_copied_proof_current_until_tentative_reply(
    tmp_path, monkeypatch, ending
):
    """Thread/transport arbitration only; PG verifies successor proof rejection."""
    now = [100.0]
    renewed, waiting = threading.Event(), threading.Event()
    stop = threading.Event()
    lease = SimpleNamespace(
        _lock=threading.Lock(),
        _worker_id=1,
        _base_url="https://control.example",
        _identity_id=uuid4(),
        _instance_id=uuid4(),
        _cluster_id=uuid4(),
        _authority_epoch=uuid4(),
        _verification_key=b"k" * 32,
        _credential="test-only",
        _claims=SimpleNamespace(generation=1),
        _proof="initial-test-proof",
        _lost=threading.Event(),
        _same_process=lambda: True,
        _active_deadline=lambda: 110.0,
        _require_before=lambda deadline: now[0],
    )

    def usable():
        with lease._lock:  # A non-reentrant lease must be released before this call.
            return True

    lease.is_usable = usable
    envelope = SimpleNamespace(cpu_cores=3, memory_bytes=2**31, pids=512, tmp_bytes=2**32)
    envelope.source = "test-only"
    snapshot = {
        "accounting_state": "unflagged",
        "envelope_source": envelope.source,
        "capacity": {"cpu": 3, "memory": 2**31, "pids": 512, "tmp": 2**32},
        "agent_reserve": {"cpu": 1, "memory": 2**30, "pids": 256, "tmp": 2**31},
    }
    released, requests, deadlines = [], [], []
    charge = object()
    budget = SimpleNamespace(
        snapshot=lambda: snapshot,
        try_reserve=lambda _: charge,
        release=released.append,
    )
    config = SimpleNamespace(cgroup_path=tmp_path, **vars(envelope))
    monkeypatch.setattr(sandbox, "validate_private_cgroup_namespace", lambda _: None)
    monkeypatch.setattr(sandbox, "read_verified_resource_envelope", lambda _: envelope)
    monkeypatch.setattr(sandbox, "_read", lambda _: "0")
    monkeypatch.setattr(claim_http_v4, "_clock", lambda: now[0])
    monkeypatch.setattr(claim_preparation_v4.os, "pidfd_open", lambda *_: 99, raising=False)

    def heartbeat():
        waiting.set()
        with lease._lock:
            lease._proof = "successor-test-proof"
            renewed.set()

    renewer = threading.Thread(target=heartbeat)

    def spawn(ticket, deadline, *_args, **_kwargs):
        deadlines.append(deadline)
        renewer.start()
        assert waiting.wait(1)
        renewed.wait(0.05)  # Unprotected old code lets the renewal commit here.
        ticket.process = SimpleNamespace(pid=123)
        ticket.thread = SimpleNamespace(is_alive=lambda: True)

    def send(_channel, raw, deadline, *_events):
        deadlines.append(deadline)
        requests.append(json.loads(raw[4:]))

    reply = json.dumps(
        {
            "state": "decision",
            "decision": "ACK_NOOP",
            "reason": "stale_generation",
            "retry_after_seconds": None,
        }
    ).encode()
    frames = iter([b"R", struct.pack("!I", len(reply)), reply])

    def receive(_channel, _size, deadline, events):
        deadlines.append(deadline)
        if requests:
            # Models the existing DB contract: a committed successor rejects this proof.
            if requests[0]["instance_proof"] != lease._proof:
                raise OSError("test-only stale proof")
            if ending == "deadline":
                now[0] = deadline + 1
            elif ending == "stop":
                stop.set()
        claim_http_v4._check_cancelled(events)
        if now[0] >= deadline:
            raise TimeoutError
        return next(frames)

    preparer = claim_preparation_v4.ClaimPreparer()

    def reap(ticket, _pidfd, channel):
        if ticket is not None:
            ticket.child.close()
        if channel is not None:
            channel.close()
        return True

    monkeypatch.setattr(claim_http_v4, "_spawn_until", spawn)
    monkeypatch.setattr(claim_http_v4, "_send_all", send)
    monkeypatch.setattr(claim_http_v4, "_receive", receive)
    monkeypatch.setattr(preparer, "_stop_uncommitted", reap)
    result = preparer._prepare_locked(
        lease=lease,
        dispatch={
            "schema_version": 1,
            "message_id": str(uuid4()),
            "execution_id": 1,
            "dispatch_generation": 1,
            "adapter_id": 1,
            "language": "python",
            "resource_class": "standard",
            "target_worker_id": 1,
        },
        root_lock=None,
        runtime_root=tmp_path,
        attempt_journal_root=tmp_path,
        sandbox_config=config,
        budget=budget,
        receive_limits=object(),
        child_address_space_bytes=2**28,
        wire_budget=65536,
        result_wire_budget=65536,
        timeout_seconds=5.0,
        stop=stop,
    )
    renewer.join(timeout=1)
    assert not renewer.is_alive() and renewed.is_set()
    assert result.state == ("decision" if ending == "decision" else "blocked")
    assert result.reason == (
        "stale_generation" if ending == "decision" else "claim_preparation_unavailable"
    )
    assert requests[0]["instance_proof"] == "initial-test-proof"
    assert set(deadlines) == {105.0} and requests[0]["timeout"] == 5.0
    assert released == [charge] and not preparer._poisoned


@pytest.mark.skipif(sys.platform != "linux", reason="real Linux seqpacket polling")
@pytest.mark.parametrize("competing", [None, "control", "parent", "helper"])
def test_gated_timer_is_not_classified_when_another_stop_source_is_ready(competing):
    """Real poll/IPC arbitration only; physical cutoff is verified by G2."""
    control, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    helper, helper_sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent_r, parent_w = os.pipe()
    timer_r, timer_w = os.pipe()
    seen = []
    try:
        os.write(timer_w, b"timer")
        if competing == "control":
            sender.send(b'{"op":"stop"}')
        elif competing == "parent":
            os.write(parent_w, b"parent")
        elif competing == "helper":
            helper_sender.send(b"GATED")
        assert not attempt_supervisor_child_v4._probe_wait_signal(
            control, helper, parent_r, timer_r, b"GATED", lambda: seen.append("cutoff")
        )
        assert seen == (["cutoff"] if competing is None else [])
    finally:
        for stream in (control, sender, helper, helper_sender):
            stream.close()
        for fd in (parent_r, parent_w, timer_r, timer_w):
            os.close(fd)


@pytest.mark.parametrize(
    "state,reason,expected",
    [
        ("observed", "execution_cutoff", True),
        ("observed", None, False),
        ("unknown", "execution_cutoff", False),
    ],
)
def test_failed_launch_gate_requires_observed_cutoff_before_result_handoff(
    monkeypatch, state, reason, expected
):
    """Control-flow gate only; this fixture supplies no physical stop proof."""
    staged = object.__new__(claim_preparation_v4.StagedClaim)
    object.__setattr__(
        staged,
        "prepared",
        SimpleNamespace(
            managed_files=False,
            business_credentials=False,
            attempt=SimpleNamespace(
                local_deadline=lambda: 1e20, send_with_authority=lambda _: False
            ),
            reservation=SimpleNamespace(
                limits=SimpleNamespace(execution_timeout_seconds=1, stream_max_bytes=1024)
            ),
        ),
    )
    supervisor = SimpleNamespace(prepare_sandbox=lambda: False, request_stop=lambda: None)
    owner = SimpleNamespace(
        _watchdog=SimpleNamespace(start=lambda: None),
        observe=lambda **_: SimpleNamespace(state=state, stop_reason=reason),
    )
    monkeypatch.setattr(
        attempt_supervisor_v4,
        "start_sandbox_supervisor",
        lambda *_a, **_k: (
            SimpleNamespace(code="supervisor_started"),
            supervisor,
            object(),
        ),
    )
    monkeypatch.setattr(
        execution_supervision_v4,
        "_OutputCollector",
        lambda *_, **__: SimpleNamespace(start=lambda: None),
    )
    monkeypatch.setattr(execution_supervision_v4, "SupervisedExecution", lambda *_: owner)
    stop = threading.Event()
    result = execution_supervision_v4.start_execution(staged, stop=stop)
    assert result is (owner if expected else None)
    assert stop.is_set() is (not expected)


def test_worker_requires_prebuilt_anchor_and_retains_unknown_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_v4, "sys", SimpleNamespace(platform="linux"))
    anchor = tmp_path / "journal"
    runtime = tmp_path / "runtime"
    cleanup = anchor / "cleanup"
    attempts = anchor / "attempts"
    config = agent_v4.WorkerConfig(
        base_url="https://control.example",
        worker_id=1,
        worker_identity_id=uuid4(),
        credential_directory=tmp_path / "credential",
        proof_trust_file=tmp_path / "trust.json",
        runtime_root=runtime,
        journal_anchor=anchor,
        cleanup_journal_root=cleanup,
        attempt_journal_root=attempts,
    )
    for mode in ("javascript", "python_js", "", "PYTHON"):
        assert agent_v4.run(replace(config, language_mode=mode), threading.Event()) == (
            "configuration_invalid"
        )
    assert not anchor.exists() and not runtime.exists()
    assert agent_v4.run(config, threading.Event()) == "worker_unavailable"
    assert not anchor.exists() and not runtime.exists()

    anchor.mkdir(mode=0o700)
    attempts.mkdir(mode=0o700)
    unknown = attempts / "unresolved.json"
    unknown.write_text("unknown responsibility")
    assert agent_v4.run(config, threading.Event()) == "recovery_required"
    assert unknown.read_text() == "unknown responsibility"
    assert cleanup.is_dir() and attempts.is_dir()


def test_explicit_recovery_cannot_use_empty_roots_as_normal_startup(tmp_path, monkeypatch):
    """Mode/authorization gate only; synthetic bootstrap proves no physical stop."""
    monkeypatch.setattr(agent_v4, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(sandbox.SandboxConfig, "from_environment", lambda: object())
    monkeypatch.setattr(cgroup_namespace, "prepare", lambda config: config)
    monkeypatch.setattr(sandbox, "_mount", lambda *_: None)
    calls = []

    def forbidden(*_args, **_kwargs):
        calls.append("broker_config_read")
        raise AssertionError("no normal startup before recovery qualification")

    monkeypatch.setattr(broker_config_v4.BrokerConfigV4, "from_private_file", forbidden)
    anchor = tmp_path / "journal"
    anchor.mkdir(mode=0o700)
    config = agent_v4.WorkerConfig(
        base_url="https://control.example",
        worker_id=1,
        worker_identity_id=uuid4(),
        credential_directory=tmp_path / "credential",
        proof_trust_file=tmp_path / "trust.json",
        runtime_root=tmp_path / "runtime",
        journal_anchor=anchor,
        cleanup_journal_root=anchor,
        attempt_journal_root=anchor / "attempt-v4",
        broker_config_file=tmp_path / "broker.json",
        recovery_operation=uuid4(),
    )
    assert agent_v4.run(config, threading.Event()) == "recovery_required"
    assert calls == []


def test_unavailable_fixed_node_cannot_emit_a_successful_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(node_runtime_v4, "NODE_EXECUTABLE", str(tmp_path / "missing-node"))
    assert not node_runtime_v4.run_preflight(
        sandbox.SandboxConfig(cgroup_path=None),
        recovery_root=tmp_path / "journal",
        runtime_root=tmp_path / "runtime",
    )
    assert not (tmp_path / "runtime").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="sealed launch requires Linux memfd")
@pytest.mark.parametrize("language", ["python", "javascript"])
@pytest.mark.parametrize("tamper", [None, "fingerprint", "directory", "bytes"])
@pytest.mark.parametrize("business", [False, True])
def test_bounded_stage_uses_same_task_directory_and_sealed_minimal_launch(
    tmp_path, monkeypatch, language, tamper, business
):
    """Real staging/SCM_RIGHTS/memfd only; physical isolation is G2/G4."""
    seal_api = (
        "F_ADD_SEALS",
        "F_GET_SEALS",
        "F_SEAL_WRITE",
        "F_SEAL_GROW",
        "F_SEAL_SHRINK",
        "F_SEAL_SEAL",
    )
    if tamper is None and any(
        type(getattr(sandbox.fcntl, name, None)) is not int for name in seal_api
    ):
        pytest.skip("Python build lacks Linux memfd sealing constants; Worker fails closed")
    code = (
        "export async function handle(c, input) { return input; }"
        if language == "javascript"
        else "def handle(c, input):\n    return input\n"
    )
    task = V4TaskPayload.model_validate(
        attempt_payload(
            {
                "protocol_version": 4,
                "execution_id": 1,
                "attempt_id": 2,
                "adapter_id": 3,
                "version_id": 4,
                "latest_version_id": 4,
                "worker_identity_id": str(uuid4()),
                "instance_id": str(uuid4()),
                "instance_generation": 1,
                "language": language,
                "code": code,
                "input": {"value": "中文"},
                "input_source_type": "json",
                "input_snapshot": {"source_type": "json"},
            }
        )
    )
    if business:
        task.credential_bindings = [
            {"binding_id": 1, "credential_id": 2, "env_key": "BOUND", "field": "token"}
        ]
        task.secrets = {"BOUND": "bound-private"}
    monkeypatch.setenv("DLR_SECRET_BOUND", "host-must-not-be-inherited")
    monkeypatch.setenv("DLR_SECRET_OTHER", "host-extra")
    fingerprint = task_fingerprint_v4.fingerprint(task, task.claim_token).hex()
    workspace = tmp_path / "attempt-2" / "dlr-exec-1"
    workspace.mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("NODE_OPTIONS", "--require=/untrusted/file")
    monkeypatch.setenv("NODE_PATH", "/untrusted/modules")
    monkeypatch.setenv("NPM_CONFIG_PREFIX", "/untrusted")
    parent, child = socket.socketpair()
    directory = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(directory)
        command = {
            "op": "stage",
            "workspace": str(workspace),
            "workspace_device": info.st_dev,
            "workspace_inode": info.st_ino,
            "staging_bytes": 1 << 20,
            "launch_bytes": 65536,
            "fingerprint": fingerprint,
        }
        if tamper == "fingerprint":
            task.input = {"value": "changed"}
        elif tamper == "directory":
            command["workspace_inode"] += 1
        elif tamper == "bytes":
            command["staging_bytes"] = 1
        assert (
            parent.sendmsg(
                [b"D"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", directory))]
            )
            == 1
        )
        if tamper:
            with pytest.raises(ValueError, match="task staging unavailable"):
                claim_task_child_v4._stage_task(child, task, fingerprint, command)
            return
        claim_task_child_v4._stage_task(child, task, fingerprint, command)
        assert claim_task_child_v4._receive(parent)["fingerprint"] == fingerprint
        data, ancillary, flags, _ = parent.recvmsg(1, socket.CMSG_SPACE(struct.calcsize("i")))
        assert data == b"D" and flags == 0
        launch_fd = struct.unpack("i", ancillary[0][2])[0]
        try:
            argv, environment = sandbox._read_sealed_launch(launch_fd, 65536)
        finally:
            os.close(launch_fd)
        suffix = "mjs" if language == "javascript" else "py"
        assert (workspace / f"adapter.{suffix}").read_text() == code
        assert json.loads((workspace / "input.json").read_text()) == {"value": "中文"}
        assert set(environment) == ({"PATH", "DLR_SECRET_BOUND"} if business else {"PATH"})
        if business:
            assert environment["DLR_SECRET_BOUND"] == "bound-private"
        if language == "javascript":
            assert argv == [
                "/usr/local/bin/node",
                str(workspace / ".dlr-harness.mjs"),
                str(workspace),
                str(workspace / "adapter.mjs"),
            ]
            assert environment["PATH"] == "/usr/bin:/bin"
        else:
            assert argv == [
                sys.executable,
                "-I",
                str(workspace / ".dlr-harness.py"),
                str(workspace),
            ]
    finally:
        os.close(directory)
        parent.close()
        child.close()


@pytest.mark.parametrize("tamper", ["symlink", "hardlink", "older", "metadata"])
def test_runtime_install_refuses_unsafe_response_without_overwriting_current(tmp_path, tamper):
    from dlr.worker import runtime_credential_v4

    identity = uuid4()
    current = {
        "worker_id": 1,
        "worker_identity_id": str(identity),
        "credential_id": "dlrw4_" + "a" * 64,
        "credential_record_id": 2,
    }
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    target = directory / "runtime-credential.json"
    target.write_text(json.dumps(current))
    target.chmod(0o600)
    staged = directory / "runtime-credential.recovery.json"
    value = {**current, "credential_id": "dlrw4_" + "b" * 64, "credential_record_id": 3}
    if tamper == "older":
        value["credential_record_id"] = 1
    if tamper == "metadata":
        value["operation_id"] = str(uuid4())
    staged.write_text(json.dumps(value))
    staged.chmod(0o600)
    if tamper == "symlink":
        other = directory / "unsafe"
        staged.rename(other)
        staged.symlink_to(other)
    elif tamper == "hardlink":
        os.link(staged, directory / "second-link")
    before = target.read_bytes()
    with pytest.raises(runtime_credential_v4.RuntimeCredentialError):
        runtime_credential_v4.install_recovered_credential(
            directory, worker_id=1, worker_identity_id=identity
        )
    assert target.read_bytes() == before


def test_new_http_credential_cannot_revive_expired_local_lease(monkeypatch):
    from dlr.worker import lease_session_v4, runtime_credential_v4

    identity = uuid4()
    instance = uuid4()
    lease = lease_session_v4.WorkerLeaseSession(
        base_url="https://control.example",
        worker_id=1,
        worker_identity_id=identity,
        credential="dlrw4_" + "a" * 64,
        credential_record_id=1,
        instance_id=instance,
        verification_key=b"a" * 32,
        cluster_id=uuid4(),
        authority_epoch=uuid4(),
        clock=lambda: 11.0,
    )
    # Explicitly synthetic clock/proof fixture; real authority/reload is covered
    # by the restricted-PG API fixture, never inferred from these assignments.
    lease._state = "registered"
    lease._proof = "synthetic"
    lease._claims = SimpleNamespace(generation=3)
    lease._nominal_deadline = 10.0
    lease._guard_deadline = 10.0
    replacement = runtime_credential_v4.RuntimeCredential(
        worker_id=1, worker_identity_id=identity, credential_record_id=2, secret="dlrw4_" + "b" * 64
    )
    with pytest.raises(lease_session_v4.WorkerLeaseError):
        lease.replace_runtime_credential(replacement)
    assert lease._credential_record_id is None and lease._instance_id == instance
    assert lease._credential == "" and lease._claims is None
    assert lease._lost.is_set()


@pytest.mark.parametrize(
    ("exchanges", "failure"),
    [
        ([(105.0, 109.0, 9.8999)], None),
        ([(105.0, 109.0, 9.9001)], "elapsed_bounds"),
        ([(105.0, 105.1, 8.0), (110.0, 110.1, 16.0)], "rolling_bounds"),
        ([(106.0, 106.1, 3.1), (112.0, 112.1, 6.1)], "rolling_bounds"),
        ([(123.0, 125.0, 23.1)], "local_deadline"),
    ],
    ids=["under-five", "over-five", "db-faster", "db-slower", "late-renewal"],
)
def test_five_second_margin_uses_signed_observations_and_earlier_stop(
    monkeypatch, caplog, exchanges, failure
):
    from dlr.control.services.worker_lease_proof_v4 import (
        LeaseProofBinding,
        issue_observation_proof,
        public_key_bytes,
    )
    from dlr.worker.lease_session_v4 import WorkerLeaseError, WorkerLeaseSession

    cluster, epoch, identity, instance = (uuid4() for _ in range(4))
    signing_key = b"a" * 32
    now = 100.0
    lease = WorkerLeaseSession(
        base_url="https://control.example",
        worker_id=1,
        worker_identity_id=identity,
        credential="dlrw4_" + "a" * 64,
        instance_id=instance,
        verification_key=public_key_bytes(signing_key),
        cluster_id=cluster,
        authority_epoch=epoch,
        clock=lambda: now,
    )
    db_start = datetime(2026, 1, 1, tzinfo=UTC)
    scheduled = iter([(100.0, 100.1, 0.0), *exchanges])
    revision = 0
    nominal = guard = db_start + timedelta(seconds=30)

    def request(operation, **_kwargs):
        nonlocal now, revision, nominal, guard
        started, now, db_offset = next(scheduled)
        assert lease._last_clock == started
        observed = db_start + timedelta(seconds=db_offset)
        proof_before = min(nominal, guard)
        nominal, guard = observed + timedelta(seconds=30), nominal
        revision += 1
        claim = LeaseProofBinding(
            cluster,
            epoch,
            1,
            identity,
            instance,
            1,
            revision,
            uuid4(),
            nominal,
            guard,
            proof_before,
        )
        proof = issue_observation_proof(key=signing_key, binding=claim, observed_at=observed)
        response = {"instance_proof": proof.token}
        if operation == "register":
            response.update(
                worker_identity_id=str(identity),
                instance_id=str(instance),
                instance_generation=1,
            )
        return json.dumps(response).encode()

    monkeypatch.setattr(lease, "_request", request)
    lease.register()
    assert 124.999999 < lease._active_deadline() < 125
    for index, (start, _finish, observed) in enumerate(exchanges, start=1):
        now = start
        if index == len(exchanges) and failure:
            with pytest.raises(WorkerLeaseError):
                lease.heartbeat()
            assert lease._lost.is_set()
            assert lease._proof is None and revision == index + 1
            with pytest.raises(WorkerLeaseError):
                lease.heartbeat()
            if failure != "local_deadline":
                assert f"reason={failure}" in caplog.text
        else:
            lease.heartbeat()
            assert lease._claims is not None and lease._claims.revision == index + 1
            assert now < lease._active_deadline() < 125
            assert lease._active_deadline() == pytest.approx(min(125, start + 25 - observed))


def test_missing_sealing_api_has_no_plaintext_launch_fallback(monkeypatch):
    monkeypatch.delattr(sandbox.fcntl, "F_ADD_SEALS", raising=False)
    with pytest.raises(ValueError, match="launch unavailable"):
        sandbox.create_sealed_launch(
            ["/usr/bin/python", "/attempt-2/harness.py"],
            {"PATH": "/usr/bin:/bin", "DLR_SECRET_K": "test-only-value"},
            max_bytes=65536,
        )
