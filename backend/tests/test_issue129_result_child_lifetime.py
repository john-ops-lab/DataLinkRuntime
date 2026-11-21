"""Real Linux child lifetime across one intent, with synthetic transaction frames."""

import asyncio
import inspect
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import Mock, patch
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
            script = _CHILD
            if command[1] == "-I":
                script = script.replace(
                    "    channel.sendall(b'R')",
                    "    import time\n"
                    "    assert not set(os.environ) - {'LC_CTYPE'}\n"
                    f"    end=time.clock_gettime(time.CLOCK_BOOTTIME)+{module.IDLE_SECONDS!r}\n"
                    "    channel.sendall(b'R'+struct.pack('!d',end))\n"
                    "    assert read(channel,1)==b'T'\n"
                    "    channel.sendall(b'G')",
                )
            child = popen([sys.executable, "-I", "-S", "-c", script, *command[-3:]], **kwargs)
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


def test_idle_dependencies_load_without_deployment_or_business_io(tmp_path):
    for name, value in (
        ("memory.max", "402653184"),
        ("pids.max", "32"),
        ("memory.swap.max", "0"),
        ("memory.swap.current", "0"),
    ):
        (tmp_path / name).write_text(value)
    check = """
import os, sys
violations = []
class Guard:
    def find_spec(self, name, *unused):
        if name in {'dlr.common.config', 'dlr.control.db'}:
            violations.append(name)
            raise RuntimeError(name)
def audit(event, args):
    forbidden = event.startswith(('socket.connect', 'socket.bind', 'socket.getaddrinfo',
                                  'subprocess.Popen', 'os.posix_spawn', 'os.fork'))
    roots = ('/run/secrets/', '/run/dlr-', '/etc/dlr')
    private = event == 'open' and str(args[0]).startswith(roots)
    if forbidden or private:
        violations.append(event)
        raise RuntimeError(event)
sys.meta_path.insert(0, Guard())
sys.addaudithook(audit)
if len(sys.argv) == 2:
    from contextlib import closing
    from pathlib import Path
    from unittest.mock import patch

    from dlr.control.services import claim_envelope_v4
    from dlr.control.services.result_preload_v4 import _load_dependencies
    root = Path(sys.argv[1])
    real_stat = os.stat
    def stat(path, *args, **kwargs):
        return real_stat(root if path == '/proc/self/ns/cgroup' else path, *args, **kwargs)

    kind = claim_envelope_v4.ClaimEnvelope
    with (
        patch.object(kind, '_root', root),
        patch.object(kind, '_location'),
        patch.object(claim_envelope_v4.cgroup_namespace, '_mount_id', return_value=1),
        patch.object(os, 'stat', stat),
        patch.object(sys, 'platform', 'linux'),
        closing(kind.open(child_address_space_bytes=128 * 1024**2, preload=True)) as envelope,
    ):
        envelope.check()
        assert envelope._limits == (402653184, 32)
    _load_dependencies()
else:
    from dlr.control.services.result_preload_v4 import main
    assert main() == 1   # The test closes the channel without handing over a request.
assert not violations
assert not any(key.upper().startswith(('DLR_', 'DATABASE_URL')) for key in os.environ)
"""
    command = [sys.executable, "-I", "-c", check]
    if sys.platform != "linux" or os.environ.get("DLR_TEST_CLAIM_PROCESS") != "1":
        subprocess.run([*command, str(tmp_path)], env={}, check=True, timeout=10)
        return
    parent, channel = socket.socketpair()
    with parent, channel:
        child = subprocess.Popen(
            [*command, str(channel.fileno()), str(os.getpid()), str(512 * 1024**2)],
            env={},
            pass_fds=(channel.fileno(),),
        )
        channel.close()
        try:
            parent.settimeout(8)
            assert module._read_exact(parent, 1) == b"R"
            idle_until = struct.unpack("!d", module._read_exact(parent, 8))[0]
            assert idle_until > module._clock()
            parent.shutdown(socket.SHUT_WR)
            assert child.wait(timeout=4) == 0
        finally:
            if child.returncode is None:
                child.kill()
                child.wait(timeout=2)


@pytest.mark.parametrize("late", [False, True])
def test_preload_handoff_reads_one_metadata_frame_before_ready(monkeypatch, late):
    from dlr.common import config
    from dlr.control.services import control_declaration_v4, result_child_v4, result_preload_v4

    snapshot = {
        **module._child_settings(),
        "worker_v4_control_slots": 2,
        "control_ha_slot": "a",
        "worker_remote_v4_enabled": True,
        "worker_v4_deployment_mode": "C",
    }
    space = snapshot["worker_v4_claim_child_address_space_bytes"]
    checked, opened = [], []
    envelope = SimpleNamespace(check=lambda: checked.append(True), close=lambda: None)
    signature = inspect.signature(module.ClaimEnvelope.open)

    def open_envelope(**kwargs):
        binding = signature.bind(**kwargs)
        binding.apply_defaults()
        opened.append(
            (binding.arguments["child_address_space_bytes"], binding.arguments["preload"])
        )
        return envelope

    monkeypatch.setattr(module.ClaimEnvelope, "open", open_envelope)
    monkeypatch.setattr(result_preload_v4, "_restrict", lambda *_: None)
    monkeypatch.setattr(result_preload_v4, "_load_dependencies", lambda: None)
    now = None
    monkeypatch.setattr(result_preload_v4, "_clock", lambda: now or time.monotonic())
    monkeypatch.setattr(result_child_v4, "_clock", time.monotonic)
    monkeypatch.setattr(control_declaration_v4, "read_declaration", lambda _: None)
    monkeypatch.setattr(config, "settings", config.settings)
    parent, child = socket.socketpair()
    monkeypatch.setattr(sys, "argv", ["preload", str(child.detach()), str(os.getpid()), str(space)])
    with parent, patch.dict(os.environ, {}, clear=True):
        parent.settimeout(4)
        thread = threading.Thread(target=result_preload_v4.main)
        thread.start()
        try:
            assert module._read_exact(parent, 1) == b"R"
            assert opened == [(space, True)]
            deadline = struct.unpack("!d", module._read_exact(parent, 8))[0]
            assert deadline > time.monotonic()
            if late:
                now = deadline
            parent.sendall(b"T")
            if late:
                assert parent.recv(1) == b""
                return
            assert module._read_exact(parent, 1) == b"G" and len(checked) >= 2
            module._send_frame(
                parent,
                {
                    **_metadata(),
                    "settings": snapshot,
                    "body_bytes": 2,
                    "control_instance": str(uuid4()),
                    "control_token": "fixture",
                    "control_deadline": time.monotonic() + 4,
                },
            )
            assert module._read_frame(parent) == {"state": "ready"}
            assert opened == [(space, True), (space, False)]
        finally:
            parent.close()
            thread.join(4)
            assert not thread.is_alive()


@unittest.skipUnless(
    sys.platform.startswith("linux")
    and hasattr(os, "pidfd_open")
    and hasattr(signal, "pidfd_send_signal"),
    "requires Linux supervised child ownership",
)
class ResultChildLifetimeTests(unittest.TestCase):
    def test_unstarted_preload_leaves_both_slots_available(self):
        with (
            _world(slots=2) as (pool, authority, children),
            patch.object(module.settings, "control_ha_slot", "a"),
        ):
            started = []
            start = threading.Thread.start

            def start_then_raise(thread):
                start(thread)
                started.append(thread)
                raise RuntimeError("native thread already started")

            with patch.object(threading.Thread, "start", start_then_raise):
                pool.anticipate_result(authority)
            self.assertEqual(len(started), 1)
            started[0].join(3)
            self.assertFalse(started[0].is_alive())
            self.assertIsNone(pool._warm)
            self.assertFalse(children)
            with pool._borrow(1, existing=False), pool._borrow(2, existing=False):
                self.assertFalse(pool._poisoned.is_set())

    def test_only_observed_stage_can_overlap_a_second_sterile_child(self):
        from dlr.control.api import worker_attempt_authority_v4 as api

        current, previous = "test-only-attempt", "previous-proof"
        for proof, credential in (
            (previous, "test-only"),
            (current, "other"),
            (current, "test-only"),
        ):
            with (
                self.subTest(proof=proof, credential=credential),
                _world(slots=2) as (pool, authority, children),
                patch.object(module.settings, "control_ha_slot", "a"),
                patch.object(api, "_process", return_value=authority),
                patch.object(
                    api, "_proved_identity", return_value=("test-only", uuid4(), 1, "instance")
                ),
                patch.object(api, "_one_header", return_value=previous),
                patch.object(api.candidates, "stage_attempt_renewal"),
            ):
                request = SimpleNamespace(
                    app=SimpleNamespace(state=SimpleNamespace(claim_coordinator_v4=pool))
                )
                authority.worker_fence = Mock()
                authority.worker_proof_authority = Mock()
                authority.observe_attempt_candidate = Mock(return_value=current)
                response = api._renew(request, Mock(), 1, 9, Mock())
                self.assertEqual(json.loads(response.body), {"attempt_proof": current})
                metadata = _metadata()
                metadata.update(attempt_proof=proof, credential=credential)
                matches = proof == current and credential == "test-only"
                original = pool._warm
                with patch.object(
                    pool, "anticipate_result", wraps=pool.anticipate_result
                ) as next_child:
                    entry = pool.begin_result(authority, metadata=metadata, body_bytes=2)
                    if matches:
                        next_child.assert_called_once_with(authority)
                    else:
                        next_child.assert_not_called()
                self.assertTrue(original.finished.wait(3))
                if matches:
                    spare = pool._warm
                    self.assertIsNot(spare, original)
                    with spare.condition:
                        self.assertTrue(spare.condition.wait_for(lambda w=spare: w.transport, 3))
                        self.assertIsNot(spare.transport.process, entry.process)
                    self.assertIsNone(spare.demand)
                self.assertEqual(len(children), 2 if matches else 1)
                self.assertTrue(pool.end_result(entry))
                pool.close()
                self.assertTrue(all(p.poll() is not None for p in children))

    def _bounded_scenario(self, method):
        # A regression can leave close() or the fixture teardown waiting
        # on another thread. Bound the entire scenario in a subprocess.
        subprocess.run(
            [sys.executable, __file__, f"{type(self).__name__}.{method}"], check=True, timeout=18
        )

    def test_shutdown_waits_for_timer_reap(self):
        self._bounded_scenario("_shutdown_during_timer_reap")

    def _shutdown_during_timer_reap(self):
        with _world(slots=2) as (pool, authority, _):
            entry = pool.begin_result(authority, metadata=_metadata(), body_bytes=2)
            slot = entry.owner
            reap_entered, allow_reap = threading.Event(), threading.Event()
            original = slot._discard_unpublished

            def delayed(*args):
                reap_entered.set()
                assert allow_reap.wait(3)
                return original(*args)

            with patch.object(slot, "_discard_unpublished", side_effect=delayed):
                timer = threading.Timer(0, slot.end_result, args=(entry,))
                timer.start()
                self.assertTrue(reap_entered.wait(3))
                closer = threading.Thread(target=pool.close)
                closer.start()
                try:
                    self.assertTrue(pool._poisoned.wait(3))
                    closer.join(0.05)
                    self.assertTrue(closer.is_alive())
                    self.assertIsNone(entry.process.returncode)
                finally:
                    allow_reap.set()
                    timer.join(3)
                    closer.join(3)
            self.assertFalse(timer.is_alive() or closer.is_alive())
            self.assertFalse(pool._shutdown_failures)
            self.assertIsNotNone(entry.process.returncode)

    def test_shutdown_waits_after_pidfd_transfer_before_result_publication(self):
        self._bounded_scenario("_shutdown_after_transfer")

    def _shutdown_after_transfer(self):
        with (
            _world(slots=2) as (pool, authority, children),
            patch.object(module.settings, "control_ha_slot", "a"),
        ):
            pool.anticipate_result(authority)
            warm = pool._warm
            transferred, release = threading.Event(), threading.Event()
            reaping, allow_reap = threading.Event(), threading.Event()
            original = warm.take
            discard = warm.slot._discard_unpublished

            def take(deadline):
                transport = original(deadline)
                assert transport is not None
                transferred.set()
                assert release.wait(3)
                return transport

            def delayed_reap(*args):
                reaping.set()
                assert allow_reap.wait(3)
                return discard(*args)

            with (
                patch.object(warm, "take", side_effect=take),
                patch.object(warm.slot, "_discard_unpublished", side_effect=delayed_reap),
                ThreadPoolExecutor(1) as executor,
            ):
                caller = executor.submit(
                    pool.begin_result, authority, metadata=_metadata(), body_bytes=2
                )
                self.assertTrue(transferred.wait(3))
                self.assertIsNone(warm.slot._result)
                closer = threading.Thread(target=pool.close)
                closer.start()
                try:
                    self.assertTrue(pool._poisoned.wait(3))
                    closer.join(0.05)
                    self.assertTrue(closer.is_alive())
                    release.set()
                    self.assertTrue(reaping.wait(3))
                    closer.join(0.05)
                    self.assertTrue(closer.is_alive())
                    self.assertIsNone(children[0].returncode)
                finally:
                    release.set()
                    allow_reap.set()
                    closer.join(3)
                with self.assertRaises(module.ClaimUnavailable):
                    caller.result(timeout=3)
            self.assertFalse(closer.is_alive())
            self.assertTrue(all(p.returncode is not None for p in children))
            self.assertFalse(pool._shutdown_failures)

    def test_shutdown_waits_for_allocated_handoff_and_reports_unconfirmed_reap(self):
        with (
            _world(slots=2) as (pool, authority, _),
            patch.object(module.settings, "control_ha_slot", "a"),
        ):
            pool.anticipate_result(authority)
            warm = pool._warm
            with warm.condition:
                self.assertTrue(warm.condition.wait_for(lambda: warm.transport is not None, 3))
            original = warm.slot._discard_unpublished

            def unconfirmed(*args):
                assert original(*args)
                return False

            with patch.object(warm.slot, "_discard_unpublished", side_effect=unconfirmed):
                with pool._borrow(7, existing=True, result=True):
                    closer = threading.Thread(target=pool.close)
                    closer.start()
                    self.assertTrue(warm.finished.wait(3))
                    self.assertTrue(closer.is_alive())
                closer.join(3)
            self.assertFalse(closer.is_alive())
            self.assertTrue(pool._shutdown_failures)

    def test_idle_result_uses_one_slot_and_transfers_one_pid(self):
        with (
            _world(slots=2) as (pool, authority, children),
            patch.object(module.settings, "control_ha_slot", "a"),
            patch.dict(os.environ, {"DLR_TEST_PARENT_SENTINEL": "present"}),
        ):
            pool.anticipate_result(authority)
            warm = pool._warm
            self.assertIsNotNone(warm)
            with warm.condition:
                self.assertTrue(warm.condition.wait_for(lambda: warm.transport is not None, 3))
                original = warm.transport.process
            with pool._borrow(11, existing=False) as (other, _):
                self.assertIsNot(other, warm.slot)
                pool.anticipate_result(authority)
                with pool._borrow(12, existing=True, result=True) as (slot, attached):
                    self.assertIs(attached, warm)
                    with self.assertRaises(module.ClaimCapacityBusy):
                        pool.begin_result(authority, metadata=_metadata(12), body_bytes=2)
                    with self.assertRaises(module.ClaimCapacityBusy):
                        pool.begin_result(authority, metadata=_metadata(13), body_bytes=2)
                    entry = slot.begin_result(
                        authority, metadata=_metadata(12), body_bytes=2, warm=attached
                    )
                    self.assertIs(entry.process, original)
                    self.assertTrue(pool.end_result(entry))
                    self.assertIsNotNone(original.poll())
                    self.assertFalse(entry.spawn.thread.is_alive())
            self.assertEqual(len(children), 1)

    def test_claim_preempts_spare_only_after_reap_and_idle_does_not_rotate(self):
        with (
            _world(slots=2) as (pool, authority, children),
            patch.object(module.settings, "control_ha_slot", "b"),
        ):
            pool.anticipate_result(authority)
            warm = pool._warm
            with warm.condition:
                self.assertTrue(warm.condition.wait_for(lambda: warm.transport is not None, 3))
                child = warm.transport
            with (
                pool._borrow(1, existing=False),
                pool._borrow(2, existing=False) as (slot, _),
            ):
                self.assertIs(slot, warm.slot)
                self.assertIsNotNone(child.process.poll())
                self.assertFalse(child.spawn.thread.is_alive())
            with patch.object(module, "IDLE_SECONDS", 0.1):
                pool.anticipate_result(authority)
                warm = pool._warm
                self.assertTrue(warm.finished.wait(3))
            self.assertIsNotNone(children[-1].poll())
            self.assertEqual(len(children), 2)

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
