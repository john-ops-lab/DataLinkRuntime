"""Direct policy/value/redaction contracts; physical execution is H2/H4."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from dlr.common import business_credentials_v4 as contract
from dlr.common.config import Settings
from dlr.control.services import business_credentials_v4 as service
from dlr.worker.result_build_child_v4 import _redact_json, _redact_text, _RedactionCollision


def binding(**changes):
    return {
        "binding_id": 1,
        "credential_id": 2,
        "env_key": "API_TOKEN",
        "field": "token",
        **changes,
    }


def policy(identity, **changes):
    return {
        "version": 1,
        "workers": [
            {"worker_id": 3, "worker_identity_id": str(identity), "credential_ids": [2], **changes}
        ],
    }


@pytest.mark.parametrize(
    "change",
    [
        {"binding_id": True},
        {"credential_id": 0},
        {"env_key": "A-B"},
        {"env_key": "a" * 65},
        {"field": "other"},
        {"extra": "value"},
    ],
)
def test_closed_binding_rejects_unknown_or_ambiguous_references(change):
    with pytest.raises(contract.BusinessCredentialError):
        contract.bindings([binding(**change)])


def test_binding_and_value_bounds_are_utf8_and_exact_key_set():
    refs = contract.bindings([binding()])
    assert contract.values(refs, {"API_TOKEN": "中" * 1365})
    for value in ["中" * 1366, "x" * 4097, "", "a\x00b", "\ud800"]:
        with pytest.raises(contract.BusinessCredentialError):
            contract.values(refs, {"API_TOKEN": value})
    for values in [{}, {"API_TOKEN": "x", "OTHER": "y"}]:
        with pytest.raises(contract.BusinessCredentialError):
            contract.values(refs, values)
    with pytest.raises(contract.BusinessCredentialError):
        contract.bindings([binding(), binding(binding_id=2)])
    refs = contract.bindings([binding(binding_id=i + 1, env_key=f"K{i}") for i in range(5)])
    with pytest.raises(contract.BusinessCredentialError, match="credentials_budget"):
        contract.values(refs, {ref.env_key: "x" * 4096 for ref in refs})


@pytest.mark.parametrize(
    "kind,field,expected",
    [
        ("password", "password", "password"),
        ("token", "token", "token"),
        ("secret", "value", "value"),
        ("access_key", "secret_key", "access_key_secret"),
        ("access_key", "access_key", "access_key_id"),
    ],
)
def test_existing_credential_field_contract(kind, field, expected):
    assert contract.credential_field(kind, field) == expected
    with pytest.raises(contract.BusinessCredentialError):
        contract.credential_field("token", "password")


@pytest.mark.parametrize(
    "change",
    [
        {"worker_id": True},
        {"worker_id": "3"},
        {"worker_id": 0},
        {"worker_identity_id": str(uuid4()).upper()},
        {"credential_ids": [True]},
        {"credential_ids": [2, 2]},
        {"credential_ids": list(range(1, 18))},
        {"extra": 1},
    ],
)
def test_node_permission_is_closed_and_never_self_reported(change):
    with pytest.raises(contract.BusinessCredentialError):
        service._parse(policy(uuid4(), **change))


def test_policy_freezes_once_and_child_receives_same_private_snapshot(tmp_path):
    identity = uuid4()
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy(identity)))
    path.chmod(0o600)
    config = Settings(DLR_WORKER_BUSINESS_CREDENTIAL_POLICY_FILE=str(path))
    service.freeze(config)
    snapshot = service.frozen(config).snapshot()
    worker = SimpleNamespace(id=3, identity_id=identity)
    refs = contract.bindings([binding()])
    assert service.frozen(config).permits(worker, refs)
    assert not service.frozen(config).permits(SimpleNamespace(id=4, identity_id=identity), refs)
    assert not service.frozen(config).permits(SimpleNamespace(id=3, identity_id=uuid4()), refs)
    path.write_text(json.dumps(policy(identity, credential_ids=[2, 9])))
    service.freeze(config)
    assert service.frozen(config).snapshot() == snapshot
    child = Settings(DLR_WORKER_BUSINESS_CREDENTIAL_POLICY_FILE="/not/read/by/child")
    service.install_snapshot(snapshot, child)
    assert service.frozen(child).snapshot() == snapshot
    bad = {**snapshot, "digest": "0" * 64}
    with pytest.raises(contract.BusinessCredentialError):
        service.install_snapshot(bad, Settings())
    assert not service.frozen(Settings()).permits(worker, refs)


def test_policy_file_rejects_duplicate_keys_permissions_and_links(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text('{"version":1,"version":1,"workers":[]}')
    path.chmod(0o600)
    with pytest.raises(contract.BusinessCredentialError):
        service.freeze(Settings(DLR_WORKER_BUSINESS_CREDENTIAL_POLICY_FILE=str(path)))
    path.write_text('{"version":1,"workers":[]}')
    path.chmod(0o644)
    with pytest.raises(contract.BusinessCredentialError):
        service.freeze(Settings(DLR_WORKER_BUSINESS_CREDENTIAL_POLICY_FILE=str(path)))
    path.chmod(0o600)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(contract.BusinessCredentialError):
        service.freeze(Settings(DLR_WORKER_BUSINESS_CREDENTIAL_POLICY_FILE=str(link)))


def test_redaction_precedes_result_encoding_and_never_overwrites_keys():
    secrets = contract.launch_values(
        {"PATH": "/usr/bin:/bin", "DLR_SECRET_K": "长秘密-token", "DLR_SECRET_OTHER": "长秘密"}
    )
    value = {"prefix-长秘密-token": ["长秘密-token", {"normal": "other"}], "n": 1}
    result = _redact_json(value, secrets)
    assert result == {"prefix-[REDACTED]": ["[REDACTED]", {"normal": "other"}], "n": 1}
    assert "prefix-长秘密-token" in value
    assert _redact_text("before 长秘密-token after", secrets) == "before [REDACTED] after"
    with pytest.raises(_RedactionCollision):
        _redact_json({"长秘密-token": 1, "[REDACTED]": 2}, secrets)


@pytest.mark.skipif(__import__("sys").platform != "linux", reason="sealed Linux result child")
@pytest.mark.parametrize(
    "case",
    [
        "normal",
        "collision",
        "truncated_stream",
        "truncated_output",
        "invalid_json",
        "failure",
        "cutoff",
        "replaced_fd",
        "missing_fd",
    ],
)
def test_real_result_child_uses_exact_launch_snapshot_before_result_mac(tmp_path, case):
    import os
    import socket
    import struct
    import subprocess
    import sys
    import time

    from dlr.common.result_intent_mac_v4 import result_body_mac
    from dlr.worker import claim_task_child_v4 as ipc
    from dlr.worker import sandbox

    secret, shorter = "canary-秘密-abcdef", "canary-秘密"
    output = {secret: [secret, shorter]} if case != "collision" else {secret: 1, "[REDACTED]": 2}
    raw_output = json.dumps(output, ensure_ascii=False).encode()
    if case == "truncated_output":
        raw_output = b'"' + secret.encode() * 100
    if case == "invalid_json":
        raw_output = b'{"invalid":' + secret.encode()
    limit = 128
    for name, data in {
        "output.json": raw_output[:limit],
        ".dlr-output-meta": json.dumps(
            {"size": len(raw_output), "truncated": len(raw_output) > limit}
        ).encode(),
    }.items():
        path = tmp_path / name
        path.write_bytes(data)
        path.chmod(0o600)
    launch = sandbox.create_sealed_launch(
        ["/usr/bin/python", "/attempt-2/harness"],
        {"PATH": "/usr/bin:/bin", "DLR_SECRET_K": secret, "DLR_SECRET_L": shorter},
        max_bytes=65536,
    )
    duplicate = os.dup(launch)
    os.close(launch)
    info = os.fstat(duplicate)
    identity = (info.st_dev, info.st_ino, info.st_size)
    sent_fd = duplicate
    replacement = -1
    if case == "replaced_fd":
        replacement = sandbox.create_sealed_launch(
            ["/usr/bin/python", "/another-attempt/harness"],
            {"PATH": "/usr/bin:/bin", "DLR_SECRET_K": secret},
            max_bytes=65536,
        )
        sent_fd = replacement
    directory = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    di = os.fstat(directory)
    parent, child = socket.socketpair()
    parent.settimeout(5)
    deadline = time.clock_gettime_ns(time.CLOCK_BOOTTIME) + 5_000_000_000
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "dlr.worker.result_build_child_v4",
            str(child.fileno()),
            str(os.getpid()),
            str(deadline),
            str(256 * 1024**2),
        ],
        pass_fds=(child.fileno(),),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child.close()
    result_fd = -1
    try:
        assert parent.recv(1) == b"R"
        stdout = ("x" * 4093 + secret + shorter).encode()
        request = {
            "attempt_id": 2,
            "fencing_token": 1,
            "claim_token": "test-claim-token",
            "exit_code": 1 if case == "failure" else None if case == "cutoff" else 0,
            "stop_reason": "execution_cutoff" if case == "cutoff" else None,
            "stdout_bytes": len(stdout),
            "stdout_truncated": case == "truncated_stream",
            "output_max_bytes": limit,
            "preview_max_bytes": 64,
            "max_body_bytes": 65536,
            "workspace_device": di.st_dev,
            "workspace_inode": di.st_ino,
            "redaction_required": True,
            "redaction_identity": identity,
            "launch_max_bytes": 65536,
        }
        ipc._send(parent, request)
        # Deliberately divide the literal secret across transport reads.
        parent.sendall(stdout[:4096])
        parent.sendall(stdout[4096:])
        parent.sendmsg(
            [b"D"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", directory))]
        )
        if case == "missing_fd":
            parent.shutdown(socket.SHUT_WR)
        else:
            parent.sendmsg(
                [b"D"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", sent_fd))]
            )
        if case in {"replaced_fd", "missing_fd"}:
            assert process.wait(timeout=5) != 0
            assert parent.recv(1) == b""
            return
        answer = ipc._receive(parent)
        result_fd = ipc._receive_fd(parent)
        raw = os.pread(result_fd, answer["size_bytes"], 0)
        document = json.loads(raw)
        token = document.pop("claim_token")
        assert result_body_mac(document, token, 65536).hex() == answer["body_mac"]
        assert secret.encode() not in raw and shorter.encode() not in raw
        assert process.wait(timeout=5) == 0
        if case == "collision":
            assert document["status"] == "succeeded"
            assert document["output"] is None and document["output_preview"] == ""
            assert document["error_code"] == "output_redaction_conflict"
        elif case == "truncated_output":
            assert document["output_preview"] == "" and document["output_truncated"]
        elif case == "truncated_stream":
            assert document["stdout"] == "[REDACTED]"
        elif case in {"failure", "invalid_json"}:
            assert document["status"] == "failed"
        elif case == "cutoff":
            assert document["status"] == "timed_out"
        else:
            assert document["output"] == {"[REDACTED]": ["[REDACTED]", "[REDACTED]"]}
    finally:
        parent.close()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        for fd in (directory, duplicate, replacement, result_fd):
            if fd >= 0:
                os.close(fd)


def test_repeated_bounded_value_can_still_exceed_original_encoded_launch_limit():
    from dlr.worker import sandbox

    refs = contract.bindings([binding(binding_id=n + 1, env_key=f"K{n}") for n in range(4)])
    selected = contract.values(refs, {ref.env_key: "\x01" * 4000 for ref in refs})
    with pytest.raises(ValueError, match="launch unavailable"):
        sandbox._encode_launch(
            ["/fixed/python", "/attempt/harness"],
            {"PATH": "/usr/bin:/bin", **{f"DLR_SECRET_{k}": v for k, v in selected.items()}},
            65536,
        )
