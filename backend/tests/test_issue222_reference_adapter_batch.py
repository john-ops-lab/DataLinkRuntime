"""The standalone batch example obeys the exact Control wire and retry budgets."""

import asyncio
import copy
import json
import time
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from dlr.control.services import subscription_batch_protocol as protocol
from test_issue222_reference_adapter import Context, body, event, marks, reference


def frame(count: int = 3, run_id: str = "reference-batch-unit") -> dict[str, Any]:
    return {
        "schema": "dlr.event.batch.v1",
        "batch_id": "a" * 32,
        "manifest_hash": "b" * 64,
        "subscription": {"id": "1", "config_version": 1, "source_epoch": "e" * 32},
        "members": [
            {
                "receipt_id": index + 1,
                "delivery_id": index + 101,
                "delivery_ordinal": 1,
                "member_index": index,
                "input": event(body(run_id=run_id, message_id=f"{index + 1:06}", seq=index + 1)),
            }
            for index in range(count)
        ],
    }


def committed(item: Any) -> dict[str, Any]:
    return {
        "run_id": item[0],
        "message_id": item[1],
        "payload_sha256": item[3],
        "normalized_value": item[4],
    }


def assert_valid(source: dict[str, Any], result: dict[str, Any]) -> None:
    manifest = [
        {key: value for key, value in item.items() if key != "input"} for item in source["members"]
    ]
    decision = protocol.validate_result(
        source["batch_id"],
        source["manifest_hash"],
        manifest,
        result,
        copy.deepcopy(result),
        worker_truncated=False,
        control_truncated=False,
    )
    assert decision.provenance == "worker_validated"
    assert len(protocol.encode(result)) <= min(65536, 512 + 512 * len(manifest))
    assert all(set(item) == protocol.MEMBER_KEYS for item in result["members"])


def test_hundred_members_have_exact_bounded_results_and_no_protocol_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted = []

    async def write(_driver: Any, _options: Any, item: Any, *_args: Any) -> dict[str, Any]:
        attempted.append(item[1])
        return committed(item)

    monkeypatch.setattr(reference, "_batch_write_once", write)
    context, source = Context(), frame(100)
    result = reference.handle(context, source)
    assert_valid(source, result)
    assert len(attempted) == 100
    assert {item["status"] for item in result["members"]} == {"succeeded"}
    assert "timing" not in result
    assert sum(len(message.encode()) for message in context.messages) <= 65536
    assert json.loads(context.messages[-1])["extra_retries_used"] == 0


@pytest.mark.parametrize(
    "error,code",
    [
        (psycopg.errors.InsufficientPrivilege("password=not-logged"), "target_permission_denied"),
        (
            reference.AuthenticationUnverified("target_authentication_unverified"),
            "target_unavailable",
        ),
        (
            reference.ReferenceFailure("target_identity_content_conflict"),
            "target_identity_conflict",
        ),
        (psycopg.errors.InvalidTextRepresentation("private body"), "target_invalid_data"),
        (psycopg.errors.SyntaxError("private SQL"), "target_unavailable"),
        (KeyError("private payload field"), "target_invalid_data"),
    ],
)
def test_first_nonretryable_failure_preserves_success_prefix_and_never_runs_successor(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    attempted = []

    async def write(_driver: Any, _options: Any, item: Any, *_args: Any) -> dict[str, Any]:
        attempted.append(item[1])
        if item[2] == 2:
            raise error
        return committed(item)

    monkeypatch.setattr(reference, "_batch_write_once", write)
    context, source = Context(), frame()
    result = reference.handle(context, source)
    assert_valid(source, result)
    assert attempted == ["000001", "000002"]
    assert [(item["status"], item["error_code"]) for item in result["members"]] == [
        ("succeeded", None),
        ("failed", code),
        ("not_run", "predecessor_failed"),
    ]
    assert "not-logged" not in " ".join(context.messages)
    assert "private" not in " ".join(context.messages)


def test_extra_retry_budget_is_shared_across_members_not_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted, delays = [], []

    async def write(_driver: Any, _options: Any, item: Any, *_args: Any) -> dict[str, Any]:
        attempted.append(item[2])
        if item[2] == 1 and attempted.count(1) <= 10 or item[2] == 2:
            raise psycopg.OperationalError("network")
        return committed(item)

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(reference, "_batch_write_once", write)
    monkeypatch.setattr(reference.asyncio, "sleep", sleep)
    monkeypatch.setattr(reference.random, "uniform", lambda _a, _b: 1.0)
    context, source = Context(), frame()
    result = reference.handle(context, source)
    assert_valid(source, result)
    assert attempted.count(1) == 11
    assert attempted.count(2) == 10
    assert 3 not in attempted
    assert len(delays) == 19
    assert delays[:6] == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]
    assert json.loads(context.messages[-1])["extra_retries_used"] == 19
    assert result["members"][1]["error_code"] == "target_retry_exhausted"


def test_commit_uncertainty_retries_only_current_member_and_retains_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted = []

    async def write(
        _driver: Any, _options: Any, item: Any, _marks: Any, _deadline: Any, state: Any
    ) -> dict[str, Any]:
        attempted.append(item[2])
        if item[2] == 2 and attempted.count(2) == 1:
            state["phase"] = "commit"
            raise reference.AttemptTimedOut("commit")
        return committed(item)

    async def sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(reference, "_batch_write_once", write)
    monkeypatch.setattr(reference.asyncio, "sleep", sleep)
    source = frame()
    result = reference.handle(Context(), source)
    assert_valid(source, result)
    assert attempted == [1, 2, 2, 3]
    assert all(item["status"] == "succeeded" for item in result["members"])


def test_exhausted_commit_uncertainty_remains_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    attempted = []

    async def write(
        _driver: Any, _options: Any, item: Any, _marks: Any, _deadline: Any, state: Any
    ) -> dict[str, Any]:
        attempted.append(item[2])
        if item[2] == 2:
            state["phase"] = "commit"
            raise reference.AttemptTimedOut("commit")
        return committed(item)

    async def sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(reference, "_batch_write_once", write)
    monkeypatch.setattr(reference.asyncio, "sleep", sleep)
    source = frame()
    result = reference.handle(Context(), source)
    assert_valid(source, result)
    assert attempted.count(1) == 1 and attempted.count(2) == 20 and 3 not in attempted
    assert result["members"][1]["status"] == "unknown"
    assert result["members"][1]["error_code"] == "target_commit_unknown"


def test_overall_deadline_does_not_restart_after_successful_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def write(_driver: Any, _options: Any, item: Any, *_args: Any) -> dict[str, Any]:
        if item[2] == 2:
            await asyncio.Event().wait()
        return committed(item)

    monkeypatch.setattr(reference, "_batch_write_once", write)
    monkeypatch.setattr(reference, "RETRY_SECONDS", 0.03)
    source, started = frame(), time.monotonic()
    result = reference.handle(Context(), source)
    assert_valid(source, result)
    assert time.monotonic() - started < 0.3
    assert [item["status"] for item in result["members"]] == ["succeeded", "unknown", "not_run"]
    assert result["members"][1]["error_code"] == "execution_timeout"


def test_batch_members_share_the_original_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, deadlines, attempted = [0.0], [], []

    async def write(
        _driver: Any, _options: Any, item: Any, _marks: Any, deadline: float, _state: Any
    ) -> dict[str, Any]:
        attempted.append(item[2])
        deadlines.append(deadline)
        clock[0] = 6.0 if item[2] == 1 else 10.0
        return committed(item)

    monkeypatch.setattr(reference, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(reference, "RETRY_SECONDS", 10.0)
    monkeypatch.setattr(reference, "_batch_write_once", write)
    source = frame()
    result = asyncio.run(reference._run_batch(Context(), source, psycopg, marks()))

    assert_valid(source, result)
    assert attempted == [1, 2]
    assert deadlines == [10.0, 10.0]
    assert [(item["status"], item["error_code"]) for item in result["members"]] == [
        ("succeeded", None),
        ("succeeded", None),
        ("unknown", "execution_timeout"),
    ]


def test_deadline_before_next_member_does_not_inherit_previous_commit_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, attempted = [0.0], []

    async def write(
        _driver: Any, _options: Any, item: Any, _marks: Any, _deadline: Any, state: Any
    ) -> dict[str, Any]:
        attempted.append(item[2])
        state["phase"] = "commit"
        clock[0] = 10.0
        return committed(item)

    monkeypatch.setattr(reference, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(reference, "_deadline", lambda _marks: 10.0)
    monkeypatch.setattr(reference, "_batch_write_once", write)
    source = frame()
    result = asyncio.run(reference._run_batch(Context(), source, psycopg, marks()))

    assert_valid(source, result)
    assert attempted == [1]
    assert [(item["status"], item["error_code"]) for item in result["members"]] == [
        ("succeeded", None),
        ("unknown", "execution_timeout"),
        ("not_run", "predecessor_failed"),
    ]


@pytest.mark.parametrize(
    "error,code",
    [
        (psycopg.errors.SerializationFailure("retry"), "execution_timeout"),
        (psycopg.errors.DeadlockDetected("retry"), "execution_timeout"),
        (reference.AttemptTimedOut("commit"), "target_commit_unknown"),
    ],
)
def test_deadline_after_commit_error_preserves_only_actual_uncertainty(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    clock = [0.0]

    async def write(
        _driver: Any, _options: Any, _item: Any, _marks: Any, _deadline: Any, state: Any
    ) -> dict[str, Any]:
        state["phase"] = "commit"
        clock[0] = 10.0
        raise error

    monkeypatch.setattr(reference, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(reference, "_deadline", lambda _marks: 10.0)
    monkeypatch.setattr(reference, "_batch_write_once", write)
    source = frame()
    result = asyncio.run(reference._run_batch(Context(), source, psycopg, marks()))

    assert_valid(source, result)
    assert (result["members"][0]["status"], result["members"][0]["error_code"]) == (
        "unknown",
        code,
    )
    assert [item["status"] for item in result["members"][1:]] == ["not_run", "not_run"]


def test_later_known_abort_does_not_erase_earlier_uncertain_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, attempts = [0.0], []

    async def write(
        _driver: Any, _options: Any, _item: Any, _marks: Any, _deadline: Any, state: Any
    ) -> dict[str, Any]:
        attempts.append(1)
        state["phase"] = "commit"
        if len(attempts) == 1:
            raise reference.AttemptTimedOut("commit")
        clock[0] = 10.0
        raise psycopg.errors.SerializationFailure("retry")

    async def sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(reference, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(reference, "_deadline", lambda _marks: 10.0)
    monkeypatch.setattr(reference, "_batch_write_once", write)
    monkeypatch.setattr(reference.asyncio, "sleep", sleep)
    source = frame()
    result = asyncio.run(reference._run_batch(Context(), source, psycopg, marks()))

    assert_valid(source, result)
    assert len(attempts) == 2
    assert (result["members"][0]["status"], result["members"][0]["error_code"]) == (
        "unknown",
        "target_commit_unknown",
    )


def test_elapsed_handle_import_time_consumes_batch_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted = []

    async def write(*_args: Any) -> Any:
        attempted.append(1)
        await asyncio.Event().wait()

    monkeypatch.setattr(reference, "_batch_write_once", write)
    source, started = frame(), time.monotonic()
    result = asyncio.run(
        reference._run_batch(
            Context(),
            source,
            psycopg,
            {**marks(), "handle_started_monotonic_ns": time.monotonic_ns() - 121_000_000_000},
        )
    )
    assert_valid(source, result)
    assert time.monotonic() - started < 0.1
    assert result["members"][0]["error_code"] == "execution_timeout"
    assert attempted == []


def test_external_cancellation_propagates_and_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = []

    async def write(*_args: Any) -> Any:
        attempts.append(1)
        await asyncio.Event().wait()

    async def cancel() -> None:
        task = asyncio.create_task(reference._run_batch(Context(), frame(), psycopg, marks()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(reference, "_batch_write_once", write)
    asyncio.run(cancel())
    assert attempts == [1]


@pytest.mark.parametrize(
    "corruption", ["too_many", "boolean_id", "duplicate", "index", "extra", "hash", "oversize"]
)
def test_invalid_manifest_frame_fails_before_target_io(
    corruption: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = frame(101 if corruption == "too_many" else 3)
    if corruption == "boolean_id":
        source["members"][0]["receipt_id"] = True
    elif corruption == "duplicate":
        source["members"][1]["delivery_id"] = source["members"][0]["delivery_id"]
    elif corruption == "index":
        source["members"][1]["member_index"] = 0
    elif corruption == "extra":
        source["members"][0]["credential"] = "not-permitted"
    elif corruption == "hash":
        source["manifest_hash"] = "no-hash"
    elif corruption == "oversize":
        source["members"][0]["input"]["padding"] = "x" * 262144

    async def never(*_args: Any) -> Any:
        pytest.fail("invalid frame attempted target I/O")

    monkeypatch.setattr(reference, "_batch_write_once", never)
    with pytest.raises(reference.ReferenceFailure, match="invalid_batch_input"):
        asyncio.run(reference._run_batch(Context(), source, psycopg, marks()))
