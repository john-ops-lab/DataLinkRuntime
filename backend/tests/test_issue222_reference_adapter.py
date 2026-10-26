"""The opt-in reference retry contract; no Worker or model service is mocked as capacity."""

import asyncio
import base64
import hashlib
import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import psycopg.conninfo
import pytest

_SPEC = importlib.util.spec_from_file_location(
    "reference_adapter_fixture",
    Path(__file__).resolve().parents[2] / "tools/ingress/reference_adapter.py",
)
assert _SPEC is not None and _SPEC.loader is not None
reference = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reference)


class Context:
    def __init__(self, dsn: str = "hostaddr=127.0.0.1 user=fixture password=private") -> None:
        self.secrets = {"TARGET_DSN": dsn}
        self.messages: list[str] = []
        self.logger = SimpleNamespace(info=self.messages.append)


def body(**overrides: Any) -> dict[str, Any]:
    return {"run_id": "reference-unit", "message_id": "000001", "seq": 1, "value": 1, **overrides}


def event(value: dict[str, Any] | None = None, source: str = "http") -> dict[str, Any]:
    value = body() if value is None else value
    if source == "http":
        data = {"payload": value}
    else:
        data = {
            "value": base64.b64encode(json.dumps(value).encode()).decode(),
            "encoding": "base64",
        }
    return {
        "source": {"type": source, "position": {"message_id": value["message_id"]}},
        "data": data,
    }


def marks() -> dict[str, Any]:
    return {"handle_started_utc": "2026-10-26T00:00:00+00:00"}


def test_http_and_rabbit_payload_hash_share_business_semantics_and_not_original_bytes() -> None:
    item = body(padding="字节")
    assert reference._payload(event(item)) == reference._payload(event(item, "rabbitmq"))
    compact = json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert reference._payload(event(item))[3] == hashlib.sha256(compact).hexdigest()
    assert (
        hashlib.sha256(json.dumps(item).encode()).hexdigest() != hashlib.sha256(compact).hexdigest()
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"seq": True},
        {"value": False},
        {"value": 2**62},
        {"seq": 0},
        {"run_id": ""},
        {"message_id": "x" * 129},
    ],
)
def test_invalid_fixture_identity_does_not_start_target_io(overrides: dict[str, Any]) -> None:
    with pytest.raises(reference.ReferenceFailure):
        reference._payload(event(body(**overrides)))


def test_source_message_identity_must_match_business_identity() -> None:
    item = event(source="rabbitmq")
    item["source"]["position"]["message_id"] = "other"
    with pytest.raises(reference.ReferenceFailure):
        reference._payload(item)


def test_target_endpoint_does_not_resolve_dns_or_weaken_tls() -> None:
    options = reference._connection_options(
        "host=database.example hostaddr=127.0.0.1 sslmode=verify-full", psycopg
    )
    assert options["host"] == "database.example"
    assert options["hostaddr"] == "127.0.0.1"
    assert options["sslmode"] == "verify-full"
    for dsn in (
        "host=database.example",
        "hostaddr=127.0.0.1,127.0.0.2",
        "service=example hostaddr=127.0.0.1",
    ):
        with pytest.raises(reference.ReferenceFailure):
            reference._connection_options(dsn, psycopg)


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (psycopg.OperationalError("connection unavailable"), True),
        (psycopg.errors.ConnectionFailure("connection"), True),
        (psycopg.errors.SerializationFailure("serialization"), True),
        (psycopg.errors.DeadlockDetected("deadlock"), True),
        (psycopg.errors.InsufficientPrivilege("permission"), False),
        (psycopg.errors.SyntaxError("syntax"), False),
        (psycopg.errors.UniqueViolation("conflict"), False),
        (psycopg.errors.InvalidTextRepresentation("data"), False),
        (psycopg.errors.QueryCanceled("timeout"), False),
        (reference.AttemptTimedOut("connect"), True),
        (reference.AttemptTimedOut("commit"), True),
        (reference.AttemptTimedOut("query"), False),
    ],
)
def test_retry_allowlist_is_closed(error: Exception, retryable: bool) -> None:
    assert reference._retryable(error, psycopg) is retryable


def test_commit_result_unknown_retries_the_same_item_and_keeps_safe_attempt_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted = []
    delays = []

    async def write(
        _driver: Any, _options: Any, item: Any, _marks: Any, _deadline: Any
    ) -> dict[str, Any]:
        attempted.append(item)
        if len(attempted) == 1:
            raise reference.AttemptTimedOut("commit")
        return {"target_outcome": "duplicate"}

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(reference, "_write_once", write)
    monkeypatch.setattr(reference.asyncio, "sleep", sleep)
    monkeypatch.setattr(reference.random, "uniform", lambda a, b: 1.0)
    context = Context()
    result = asyncio.run(reference._run(context, event(), psycopg, marks()))
    assert attempted[0] == attempted[1]
    assert result["target_attempt_count"] == 2
    assert result["target_failures"] == [
        {
            "attempt": 1,
            "error_type": "AttemptTimedOut",
            "sqlstate": None,
            "reason": "target_attempt_timeout",
            "delay_seconds": 0.5,
        }
    ]
    assert delays == [0.5]


def test_permission_is_not_retried_and_driver_message_is_never_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = []

    async def write(*_args: Any) -> Any:
        attempts.append(1)
        raise psycopg.errors.InsufficientPrivilege("password=do-not-log confidential SQL")

    monkeypatch.setattr(reference, "_write_once", write)
    context = Context()
    with pytest.raises(RuntimeError, match="reference_target_write_failed"):
        reference.handle(context, event())
    assert attempts == [1]
    assert "do-not-log" not in " ".join(context.messages)
    assert "confidential" not in " ".join(context.messages)
    assert any('"sqlstate": "42501"' in item for item in context.messages)


def test_twenty_attempt_budget_and_capped_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempted = []
    delays = []

    async def write(*_args: Any) -> Any:
        attempted.append(1)
        raise psycopg.OperationalError("connection unavailable")

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(reference, "_write_once", write)
    monkeypatch.setattr(reference.asyncio, "sleep", sleep)
    monkeypatch.setattr(reference.random, "uniform", lambda a, b: 1.0)
    with pytest.raises(reference.ReferenceFailure, match="target_attempt_budget_exhausted"):
        asyncio.run(reference._run(Context(), event(), psycopg, marks()))
    assert len(attempted) == 20
    assert len(delays) == 19
    assert delays[:6] == [0.5, 1.0, 2.0, 4.0, 8.0, 8.0]
    assert max(delays) == 8


def test_overall_deadline_cancels_real_wait_instead_of_stacking_per_query_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stalled(*_args: Any) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(reference, "RETRY_SECONDS", 0.03)
    monkeypatch.setattr(reference, "_write_once", stalled)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(reference._run(Context(), event(), psycopg, marks()))
    assert time.monotonic() - started < 0.3


def test_connect_deadline_is_five_seconds_or_remaining_whichever_is_smaller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def connect(**_options: Any) -> Any:
        await asyncio.Event().wait()

    driver = SimpleNamespace(
        AsyncConnection=SimpleNamespace(connect=connect), OperationalError=psycopg.OperationalError
    )
    monkeypatch.setattr(reference, "ATTEMPT_SECONDS", 0.03)
    started = time.monotonic()
    with pytest.raises(reference.AttemptTimedOut) as error:
        asyncio.run(
            reference._write_once(driver, {}, reference._payload(event()), marks(), started + 1)
        )
    assert error.value.phase == "connect"
    assert time.monotonic() - started < 0.3


def test_cancellation_propagates_without_turning_into_target_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def stalled(*_args: Any) -> Any:
        calls.append(1)
        await asyncio.Event().wait()

    async def cancel() -> None:
        task = asyncio.create_task(reference._run(Context(), event(), psycopg, marks()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(reference, "_write_once", stalled)
    asyncio.run(cancel())
    assert calls == [1]


def test_safe_failure_only_emits_closed_local_reasons() -> None:
    assert (
        reference._safe_failure(reference.ReferenceFailure("target_identity_content_conflict"))[
            "reason"
        ]
        == "target_identity_content_conflict"
    )
    for untrusted in ("password=never-log", {"password": "never-log"}):
        diagnostic = reference._safe_failure(reference.ReferenceFailure(untrusted))
        assert "reason" not in diagnostic
        assert "never-log" not in json.dumps(diagnostic)
