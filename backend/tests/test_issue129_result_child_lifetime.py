"""Real Linux child lifetime across one intent, with synthetic transaction frames."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from dlr.control.services import claim_coordinator_v4 as module
from dlr.control.services.claim_envelope_v4 import ClaimEnvelopeUnavailable

_CHILD = """
import ctypes,json,os,resource,signal,socket,struct,sys
fd,parent_pid,_limit=map(int,sys.argv[1:])
resource.setrlimit(resource.RLIMIT_AS,(_limit,_limit))
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
def _world(seconds=10, slots=None, real_resources=False):
    """A real IPC child with synthetic transaction frames, never a result proof."""
    with ExitStack() as stack:
        stack.enter_context(patch.object(module.settings, "worker_v4_control_slots", slots or 1))
        engine = create_engine("sqlite://")  # No connection or database operation is performed.
        coordinator = module._ClaimSlot() if slots is None else module.ClaimCoordinator()
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
            if not real_resources:
                stack.enter_context(
                    patch.object(
                        module.ClaimEnvelope, "open", side_effect=lambda **_kwargs: Envelope()
                    )
                )
            with patch.object(module.subprocess, "Popen", side_effect=launch):
                yield coordinator, authority, children
        finally:
            coordinator.close()
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
            engine.dispose()


def _metadata(worker_id=1):
    return {
        "operation": "stage",
        "worker_id": worker_id,
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
        input_cutoff=module._clock() + 5,
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
            self.assertEqual(old.input_cutoff, retained.input_cutoff)
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

    def test_two_slots_keep_exact_results_and_reject_busy_prepare_before_spawn(self):
        # Opt in only in the existing bounded Linux deployment fixture. Normal
        # unit runs do not pretend an arbitrary runner has a private cgroup.
        native = os.environ.get("DLR_TEST_CLAIM_PROCESS") == "1"
        with _world(slots=2, real_resources=native) as (pool, authority, children):
            a = pool.begin_result(authority, metadata=_metadata(1), body_bytes=2)
            pool.send_result(a, b"{}")
            pool.finish_result(a)
            b = pool.begin_result(authority, metadata=_metadata(2), body_bytes=2)
            pool.send_result(b, b"{}")
            pool.finish_result(b)
            self.assertEqual(len(children), 2)
            if native:
                from pathlib import Path

                self.assertIsNotNone(pool._envelope)
                pool._envelope.check()
                for child in children:
                    self.assertEqual(Path(f"/proc/{child.pid}/cgroup").read_text(), "0::/\n")
                    limit = next(
                        line
                        for line in Path(f"/proc/{child.pid}/limits").read_text().splitlines()
                        if line.startswith("Max address space")
                    )
                    self.assertEqual(
                        limit.split()[-3:-1],
                        [str(module.settings.worker_v4_claim_child_address_space_bytes)] * 2,
                    )
            for worker_id in (1, 3):
                with self.assertRaises(module.ClaimCapacityBusy):
                    pool.prepare(
                        authority,
                        worker_id=worker_id,
                        instance_id=uuid4(),
                        generation=1,
                        credential="test-only",
                        instance_proof="test-only",
                        message=None,
                        wire_budget=4096,
                    )
            from starlette.requests import Request

            from dlr.control.api import worker_claim_v4 as api
            from dlr.control.services.dispatch import build_dispatch_message

            message = build_dispatch_message(
                execution_id=3,
                dispatch_generation=1,
                adapter_id=3,
                language="python",
                resource_class="standard",
                target_worker_id=3,
            )

            async def receive():
                return {
                    "type": "http.request",
                    "body": message.model_dump_json().encode(),
                    "more_body": False,
                }

            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/claim/prepare",
                    "query_string": b"",
                    "headers": [(b"x-dlr-worker-claim-wire-bytes", b"4096")],
                },
                receive,
            )
            with (
                patch.object(api, "_coordinator", return_value=pool),
                patch.object(api, "_process", return_value=authority),
                patch.object(
                    api, "_proved_identity", return_value=("test-only", uuid4(), 1, "test-only")
                ),
            ):
                response = asyncio.run(api.prepare(request, 3))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                json.loads(response.body),
                {
                    "decision": "DEFER",
                    "reason": "control_transport_busy",
                    "retry_after_seconds": 1,
                    "attempt_id": None,
                    "cancel_requested": False,
                    "payload": None,
                },
            )
            self.assertNotIn("x-dlr-claim-proposal", response.headers)
            self.assertEqual(len(children), 2)
            with self.assertRaises(module.ClaimUnavailable):
                pool.begin_result(
                    authority, metadata={**a.pending_finalize, "worker_id": 2}, body_bytes=2
                )
            self.assertTrue(all(child.poll() is None for child in children))
            for entry in (b, a):
                final = pool.begin_result(
                    authority,
                    metadata={**entry.pending_finalize, "receipt_nonce": str(uuid4())},
                    body_bytes=2,
                )
                self.assertIs(final, entry)
                pool.send_result(final, b"{}")
                self.assertEqual(pool.finish_result(final), {"result_receipt": "test-only-receipt"})
            self.assertEqual(len(children), 2)
            self.assertTrue(all(child.poll() is not None for child in children))
            self.assertTrue(all(slot._worker_id is None for slot in pool._slots))

    def test_closing_is_charged_without_blocking_other_slot_or_old_timer_reuse(self):
        with _world(slots=2) as (pool, authority, children):
            a = pool.begin_result(authority, metadata=_metadata(1), body_bytes=2)
            b = pool.begin_result(authority, metadata=_metadata(2), body_bytes=2)
            started, release = threading.Event(), threading.Event()
            discard = a.owner._discard_unpublished

            def slow(*args):
                started.set()
                if not release.wait(3):
                    raise ValueError("test close timeout")
                return discard(*args)

            with patch.object(a.owner, "_discard_unpublished", side_effect=slow):
                closer = threading.Thread(target=pool.end_result, args=(a,))
                closer.start()
                try:
                    self.assertTrue(started.wait(2))
                    with self.assertRaises(module.ClaimCapacityBusy):
                        pool.prepare(
                            authority,
                            worker_id=3,
                            instance_id=uuid4(),
                            generation=1,
                            credential="test-only",
                            instance_proof="test-only",
                            message=None,
                            wire_budget=4096,
                        )
                    with self.assertRaises(module.ClaimUnavailable):
                        pool.begin_result(authority, metadata=_metadata(1), body_bytes=2)
                    pool.send_result(b, b"{}")
                    pool.finish_result(b)
                    self.assertEqual(len(children), 2)
                finally:
                    release.set()
                    closer.join(timeout=3)
                self.assertFalse(closer.is_alive())
            c = pool.begin_result(authority, metadata=_metadata(3), body_bytes=2)
            self.assertIs(c.owner, a.owner)
            self.assertGreater(c.allocation, a.allocation)
            self.assertFalse(a.owner.end_result(a))
            self.assertFalse(pool.end_result(a))
            self.assertIsNone(c.process.poll())
            self.assertIsNone(b.process.poll())

    def test_unknown_reap_poison_is_global_and_retains_one_charged_slot(self):
        with _world(slots=2) as (pool, authority, _children):
            a = pool.begin_result(authority, metadata=_metadata(1), body_bytes=2)
            b = pool.begin_result(authority, metadata=_metadata(2), body_bytes=2)
            wait = a.process.wait
            with patch.object(a.process, "wait", side_effect=subprocess.TimeoutExpired("test", 2)):
                self.assertFalse(pool.end_result(a))
            self.assertTrue(pool._poisoned.is_set())
            self.assertEqual(a.owner._worker_id, 1)
            with self.assertRaises(module.ClaimUnavailable):
                pool.send_result(b, b"{}")
            wait(timeout=2)
            os.close(a.pidfd)
            a.spawn.release.set()
            a.spawn.thread.join(timeout=2)

    def test_envelope_requires_all_slots_and_pins_the_total_limit(self):
        from dlr.control.services.claim_envelope_v4 import ClaimEnvelope, ClaimEnvelopeUnavailable

        facts = {
            "memory.max": str(300 * 1024**2),
            "pids.max": "256",
            "memory.swap.max": "0",
            "memory.swap.current": "0",
        }
        with patch.object(module.settings, "worker_v4_control_slots", 2):
            fd = os.open("/tmp", os.O_RDONLY | os.O_DIRECTORY)
            envelope = ClaimEnvelope(fd, child_address_space_bytes=128 * 1024**2)
            try:
                with (
                    patch.object(envelope, "_location"),
                    patch.object(envelope, "_read", side_effect=lambda name: facts[name]),
                ):
                    with self.assertRaises(ClaimEnvelopeUnavailable):
                        envelope.check()
                    facts["memory.max"] = str(384 * 1024**2)
                    envelope.check()
                    facts["memory.max"] = str(385 * 1024**2)
                    with self.assertRaises(ClaimEnvelopeUnavailable):
                        envelope.check()
            finally:
                envelope.close()

    def test_resource_change_is_not_reported_as_pretransaction_busy(self):
        with _world(slots=1) as (pool, authority, _children):
            pool.begin_result(authority, metadata=_metadata(1), body_bytes=2)
            pool._envelope.close()
            with self.assertRaises(module.ClaimUnavailable) as raised:
                pool.prepare(
                    authority,
                    worker_id=2,
                    instance_id=uuid4(),
                    generation=1,
                    credential="test-only",
                    instance_proof="test-only",
                    message=None,
                    wire_budget=4096,
                )
            self.assertNotIsInstance(raised.exception, module.ClaimCapacityBusy)
            self.assertTrue(pool._poisoned.is_set())

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

    def test_idle_notification_cannot_release_a_new_same_worker_result(self):
        with _world(slots=2) as (pool, authority, _children):
            old = pool.begin_result(authority, metadata=_metadata(1), body_bytes=2)
            slot = old.owner
            idle_checked = threading.Event()
            release_idle = threading.Event()
            begin_entered = threading.Event()
            published = threading.Event()
            results = []
            errors = []
            release = pool._release_idle
            begin = slot.begin_result

            def paused_release(owner, allocation):
                if owner is slot and owner._calls == 0 and not idle_checked.is_set():
                    idle_checked.set()
                    if not release_idle.wait(2):
                        raise AssertionError("idle release barrier expired")
                return release(owner, allocation)

            def entered_begin(*args, **kwargs):
                begin_entered.set()
                return begin(*args, **kwargs)

            def run(operation):
                try:
                    result = operation()
                    if isinstance(result, module._Result):
                        results.append(result)
                        published.set()
                except BaseException as error:
                    errors.append(error)

            closing = threading.Thread(target=run, args=(lambda: pool.end_result(old),))
            opening = threading.Thread(
                target=run,
                args=(lambda: pool.begin_result(authority, metadata=_metadata(1), body_bytes=2),),
            )
            with (
                patch.object(pool, "_release_idle", side_effect=paused_release),
                patch.object(slot, "begin_result", side_effect=entered_begin),
            ):
                closing.start()
                try:
                    self.assertTrue(idle_checked.wait(2))
                    # If the old notification dropped its lock, force B to
                    # publish before allowing that stale release to continue.
                    # A lock-held implementation may instead serialize B.
                    acquired = slot._lock.acquire(blocking=False)
                    if acquired:
                        slot._lock.release()
                    opening.start()
                    self.assertTrue(begin_entered.wait(2))
                    if acquired:
                        self.assertTrue(published.wait(2))
                finally:
                    release_idle.set()
                    closing.join(2)
                    if opening.ident is not None:
                        opening.join(2)
            self.assertFalse(closing.is_alive() or opening.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(results), 1)
            current = results[0]
            self.assertEqual(current.owner._worker_id, 1)
            pool.send_result(current, b"{}")
            self.assertEqual(pool.finish_result(current)["result_proof"], "test-only-intent")

    def test_shutdown_attempts_both_slots_and_envelope_after_first_close_error(self):
        with _world(slots=2) as (pool, authority, children):
            first = pool.begin_result(authority, metadata=_metadata(1), body_bytes=2)
            second = pool.begin_result(authority, metadata=_metadata(2), body_bytes=2)
            with (
                patch.object(first.envelope, "close", side_effect=OSError("test-close")),
                self.assertLogs("dlr.control.services.claim_coordinator_v4", level="ERROR") as logs,
            ):
                pool.close()
            self.assertTrue(pool._poisoned.is_set())
            self.assertEqual(len(pool._shutdown_failures), 1)
            self.assertIn(first, first.owner._unresolved)
            self.assertTrue(second.closed and pool._envelope.closed)
            self.assertTrue(all(child.poll() is not None for child in children))
            self.assertIn("shutdown incomplete", logs.output[0])

    def test_live_resource_failure_stays_global_after_limits_recover(self):
        for phase in ("send", "finish"):
            with self.subTest(phase=phase), _world(slots=2) as (pool, authority, _children):
                a = pool.begin_result(authority, metadata=_metadata(1), body_bytes=2)
                b = pool.begin_result(authority, metadata=_metadata(2), body_bytes=2)
                if phase == "finish":
                    pool.send_result(a, b"{}")
                with (
                    patch.object(
                        a.envelope,
                        "check",
                        side_effect=ClaimEnvelopeUnavailable("test-boundary"),
                    ),
                    self.assertRaises(ClaimEnvelopeUnavailable),
                ):
                    if phase == "send":
                        pool.send_result(a, b"{}")
                    else:
                        pool.finish_result(a)
                pool.end_result(a)
                # Restore the envelope before B checks it: the original loss
                # must remain latched rather than silently recovering.
                b.envelope.check()
                self.assertTrue(pool._poisoned.is_set())
                with self.assertRaises(module.ClaimUnavailable):
                    pool.send_result(b, b"{}")

    def test_closed_entry_check_does_not_poison_another_live_worker(self):
        with _world(slots=2) as (pool, authority, _children):
            a = pool.begin_result(authority, metadata=_metadata(1), body_bytes=2)
            b = pool.begin_result(authority, metadata=_metadata(2), body_bytes=2)
            pool.end_result(a)
            with self.assertRaises(module.ClaimUnavailable):
                a.owner._check_envelope(a.envelope, entry=a)
            self.assertFalse(pool._poisoned.is_set())
            pool.send_result(b, b"{}")
            self.assertEqual(pool.finish_result(b)["result_proof"], "test-only-intent")


if __name__ == "__main__":
    unittest.main()


@pytest.mark.parametrize("value", [0, 3, -1, True, False, 1.0, "1.0", "true"])
def test_control_slot_configuration_is_strict_and_bounded(value):
    from dlr.common.config import Settings

    with pytest.raises(ValueError):
        Settings(DLR_WORKER_V4_CONTROL_SLOTS=value)


@pytest.mark.parametrize("value, expected", [(1, 1), (2, 2), ("1", 1), ("2", 2)])
def test_control_slot_configuration_accepts_explicit_deployment_values(value, expected):
    from dlr.common.config import Settings

    assert Settings(DLR_WORKER_V4_CONTROL_SLOTS=value).worker_v4_control_slots == expected
