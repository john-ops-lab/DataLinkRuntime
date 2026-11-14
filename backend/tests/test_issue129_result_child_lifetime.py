"""Real Linux child lifetime across one intent, with synthetic transaction frames."""

import asyncio
import os
import signal
import subprocess
import sys
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dlr.control.services import claim_coordinator_v4 as module

_CHILD = """
import ctypes,json,os,signal,socket,struct,sys
fd,parent_pid,_limit=map(int,sys.argv[1:])
libc=ctypes.CDLL(None)
assert libc.prctl(1,signal.SIGKILL,0,0,0)==0 and os.getppid()==parent_pid
def read(channel,size):
    value=bytearray()
    while len(value)<size:
        part=channel.recv(size-len(value))
        if not part: raise EOFError
        value.extend(part)
    return bytes(value)
def frame(channel):
    return json.loads(read(channel,struct.unpack('!I',read(channel,4))[0]))
def send(channel,value):
    raw=json.dumps(value).encode();channel.sendall(struct.pack('!I',len(raw))+raw)
with socket.socket(fileno=fd) as channel:
    channel.sendall(b'R')
    for _ in range(2):
        request=frame(channel);send(channel,{'state':'ready'})
        read(channel,request['body_bytes'])
        if request['operation']=='stage':
            send(channel,{'intent_id':'11111111-1111-4111-8111-111111111111',
                          'result_proof':'test-only-intent'})
        else:
            send(channel,{'result_receipt':'test-only-receipt'});break
"""


@contextmanager
def _world(seconds=10):
    """A real IPC child with synthetic transaction frames, never a result proof."""
    engine = create_engine("sqlite://")  # No connection or database operation is performed.
    coordinator = module.ClaimCoordinator()
    deadline = module._clock() + seconds
    authority = SimpleNamespace(
        _sessions=sessionmaker(bind=engine),
        check_authority=lambda: None,
        claim_child_identity=lambda: ("22222222-2222-4222-8222-222222222222", "test", deadline),
        _deployment=SimpleNamespace(worker_proof_signing_key=b"s" * 32),
    )
    children = []
    popen = subprocess.Popen

    def launch(command, **kwargs):
        child = popen([sys.executable, "-I", "-S", "-c", _CHILD, *command[3:]], **kwargs)
        children.append(child)
        return child

    class Envelope:
        def __init__(self):
            self.closed = False

        def check(self):
            if self.closed:
                raise ValueError("closed envelope")

        def close(self):
            self.closed = True

    try:
        with (
            patch.object(module.ClaimEnvelope, "open", side_effect=lambda **_kwargs: Envelope()),
            patch.object(module.subprocess, "Popen", side_effect=launch),
        ):
            yield coordinator, authority, children
    finally:
        coordinator.close()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
        engine.dispose()


def _metadata():
    return {
        "operation": "stage",
        "worker_id": 1,
        "attempt_id": 9,
        "credential": "test-only",
        "instance_id": str(uuid4()),
        "generation": 1,
        "instance_proof": "test-only-instance",
        "attempt_proof": "test-only-attempt",
        "observation_nonce": str(uuid4()),
    }


def _stage(coordinator, authority):
    entry = coordinator.begin_result(authority, metadata=_metadata(), body_bytes=2)
    coordinator.send_result(entry, b"{}")
    response = coordinator.finish_result(entry)
    assert response["result_proof"] == "test-only-intent"
    return entry


def _claimed_child(coordinator, authority):
    """Supply a real child at the Claim handoff, with no synthetic business state."""
    import socket

    parent, child = socket.socketpair()
    parent.settimeout(2)
    spawn, process = coordinator._spawn_child(child, module._clock() + 3, 512 * 1024 * 1024)
    assert parent.recv(1) == b"R"
    configuration = module.settings.model_dump(mode="json")
    configuration["database_url"] = authority._sessions.kw["bind"].url.render_as_string(
        hide_password=False
    )
    entry = module._Claim(
        process,
        spawn,
        os.pidfd_open(process.pid),
        parent,
        module.ClaimEnvelope.open(child_address_space_bytes=512 * 1024 * 1024),
        authority,
        "test-proposal",
        "test-only",
        1,
        uuid4(),
        1,
        module._clock() + 0.15,
        "EXECUTE",
        2,
        configuration=configuration,
        state="finalizing",
    )
    coordinator._current = entry
    coordinator._retain_result_from_claim(
        entry, attempt_id=9, execution_timeout=1, idle_until=module._clock() + 5
    )
    return entry, coordinator._result


@unittest.skipUnless(
    sys.platform.startswith("linux")
    and hasattr(os, "pidfd_open")
    and hasattr(signal, "pidfd_send_signal"),
    "requires Linux supervised child ownership",
)
class ResultChildLifetimeTests(unittest.TestCase):
    def test_claim_handoff_keeps_one_child_and_requires_fresh_operation_authority(self):
        with _world() as (coordinator, authority, children):
            old, retained = _claimed_child(coordinator, authority)
            self.assertFalse(coordinator._expire(old))
            self.assertIsNone(old.process.poll())
            time.sleep(0.2)  # Only resource ownership survives the old Claim deadline.
            self.assertGreater(module._clock(), old.deadline)
            metadata = {
                **retained.pending_stage,
                "instance_proof": "test-only-current-instance",
                "attempt_proof": "test-only-current-attempt",
                "observation_nonce": str(uuid4()),
            }
            with self.assertRaises(module.ClaimUnavailable):
                coordinator.begin_result(
                    authority, metadata={**metadata, "attempt_id": 10}, body_bytes=2
                )
            self.assertIsNone(old.process.poll())
            stage = coordinator.begin_result(authority, metadata=metadata, body_bytes=2)
            self.assertIs(stage.process, old.process)
            coordinator.send_result(stage, b"{}")
            coordinator.finish_result(stage)
            deadline = stage.deadline
            metadata = {**stage.pending_finalize, "receipt_nonce": str(uuid4())}
            final = coordinator.begin_result(authority, metadata=metadata, body_bytes=2)
            self.assertEqual(final.deadline, deadline)
            coordinator.send_result(final, b"{}")
            coordinator.finish_result(final)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(old.process.poll())
            self.assertIsNone(coordinator._result)

    def test_claim_retention_cannot_authorize_result_after_control_is_lost(self):
        with _world() as (coordinator, authority, _children):
            old, retained = _claimed_child(coordinator, authority)
            metadata = {
                **retained.pending_stage,
                "instance_proof": "test-only-current-instance",
                "attempt_proof": "test-only-current-attempt",
                "observation_nonce": str(uuid4()),
            }
            with (
                patch.object(authority, "check_authority", side_effect=ValueError("lost")),
                self.assertRaises(module.ClaimUnavailable),
            ):
                coordinator.begin_result(authority, metadata=metadata, body_bytes=2)
            self.assertIsNotNone(old.process.poll())
            self.assertIsNone(coordinator._result)
            self.assertFalse(coordinator._unresolved)

    def test_only_matching_finalization_reuses_one_child_and_original_deadline(self):
        with _world() as (coordinator, authority, children):
            entry = _stage(coordinator, authority)
            deadline, pid = entry.deadline, entry.process.pid
            self.assertIsNone(entry.process.poll())
            metadata = {**entry.pending_finalize, "receipt_nonce": str(uuid4())}
            with self.assertRaises(module.ClaimUnavailable):
                coordinator.begin_result(
                    authority, metadata={**metadata, "credential": "another"}, body_bytes=2
                )
            self.assertIsNone(entry.process.poll())
            with self.assertRaises(module.ClaimUnavailable):
                coordinator.prepare(
                    authority,
                    worker_id=1,
                    instance_id=UUID(metadata["instance_id"]),
                    generation=1,
                    credential="test-only",
                    instance_proof="test-only",
                    message=None,
                    wire_budget=4096,
                )
            resumed = coordinator.begin_result(authority, metadata=metadata, body_bytes=2)
            self.assertIs(resumed, entry)
            self.assertEqual((resumed.process.pid, resumed.deadline), (pid, deadline))
            coordinator.send_result(resumed, b"{}")
            self.assertEqual(
                coordinator.finish_result(resumed), {"result_receipt": "test-only-receipt"}
            )
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(entry.process.poll())
            self.assertFalse(entry.spawn.thread.is_alive())
            self.assertIsNone(coordinator._result)
            self.assertFalse(coordinator._unresolved)

    def test_abandoned_intent_expires_and_reaps_its_child(self):
        with _world(seconds=0.8) as (coordinator, authority, _children):
            entry = _stage(coordinator, authority)
            entry.process.wait(timeout=3)
            entry.spawn.thread.join(timeout=3)
            self.assertTrue(entry.closed)
            self.assertIsNone(coordinator._result)
            self.assertFalse(entry.spawn.thread.is_alive())
            self.assertFalse(coordinator._unresolved)

    def test_unknown_reap_keeps_exact_owner_and_blocks_new_work(self):
        with _world() as (coordinator, authority, _children):
            entry = _stage(coordinator, authority)
            wait = entry.process.wait
            with patch.object(
                entry.process, "wait", side_effect=subprocess.TimeoutExpired("test", 2)
            ):
                self.assertFalse(coordinator.end_result(entry))
            self.assertTrue(coordinator._poisoned)
            self.assertEqual(coordinator._unresolved, [(entry.process, entry.pidfd, entry.spawn)])
            with self.assertRaises(module.ClaimUnavailable):
                coordinator.begin_result(authority, metadata=_metadata(), body_bytes=2)
            # Test teardown reaps the retained owner; it never restores authority.
            wait(timeout=2)
            os.close(entry.pidfd)
            entry.spawn.release.set()
            entry.spawn.thread.join(timeout=2)
            self.assertFalse(entry.spawn.thread.is_alive())

    def test_invalid_stage_proof_reaps_the_retained_child(self):
        from fastapi import HTTPException
        from starlette.requests import Request

        from dlr.control.api import worker_result_v4 as api

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        with _world() as (coordinator, authority, children):
            metadata = _metadata()
            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/result/stage",
                    "query_string": b"",
                    "headers": [
                        (b"content-length", b"2"),
                        (b"content-type", b"application/json"),
                        (b"x-dlr-worker-attempt-proof", b"test-only-attempt"),
                        (b"x-dlr-result-observation-nonce", metadata["observation_nonce"].encode()),
                    ],
                },
                receive,
            )
            with (
                patch.object(api, "_coordinator", return_value=coordinator),
                patch.object(api, "_process", return_value=authority),
                patch.object(
                    api,
                    "_proved_identity",
                    return_value=("test-only", UUID(metadata["instance_id"]), 1, "test-only"),
                ),
                self.assertRaises(HTTPException) as error,
            ):
                asyncio.run(api._exchange(request, 1, 9, "stage"))
            self.assertEqual(error.exception.status_code, 503)
            self.assertEqual(len(children), 1)
            self.assertIsNotNone(children[0].poll())
            self.assertIsNone(coordinator._result)
            self.assertFalse(coordinator._unresolved)


if __name__ == "__main__":
    unittest.main()
