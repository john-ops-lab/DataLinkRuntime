"""SSE event stream over plain PostgreSQL polling (M3 spec §7).

No Redis/Kafka/WebSocket: each connection re-reads the Execution row about
once per second and forwards status changes and appended stdout/stderr as
events. After a terminal status the final state is sent and the stream
closes. Progress is best effort; the M2 final result stays authoritative.
"""

import json
import time
from collections.abc import Iterator

from sqlalchemy.orm import Session

from dlr.common.config import settings
from dlr.control.db import SessionLocal
from dlr.control.models import Execution
from dlr.control.schemas.execution import ExecutionResponse
from dlr.control.services.adapter import domain_error
from dlr.control.services.execution import TERMINAL_STATUSES


def _sse(event: str, data: object) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _execution_event(execution: Execution) -> str:
    return _sse("execution", ExecutionResponse.model_validate(execution).model_dump(mode="json"))


def _diff_stream(
    stream: str, seen: str, current: str, was_truncated: bool, is_truncated: bool
) -> tuple[list[str], str, bool]:
    """Translate one stored stream's change into SSE events.

    Returns the encoded events, the new "seen" watermark and the new
    truncation flag. While the stored text only grows by appending, plain
    ``log`` deltas are sent; the moment truncation drops the head, the
    prefix relation breaks and a ``log_snapshot`` carries the current stored
    text instead, so clients never reconstruct a corrupted prefix.
    """
    if is_truncated and not was_truncated:
        event = _sse("log_snapshot", {"stream": stream, "content": current, "truncated": True})
        return [event], current, True
    if current.startswith(seen):
        delta = current[len(seen) :]
        if delta:
            return [_sse("log", {"stream": stream, "chunk": delta})], current, is_truncated
        return [], seen, is_truncated
    if current != seen:
        event = _sse(
            "log_snapshot", {"stream": stream, "content": current, "truncated": is_truncated}
        )
        return [event], current, is_truncated
    return [], seen, is_truncated


def event_stream(execution_id: int) -> Iterator[str]:
    """Yield SSE events for one Execution until it reaches a terminal state.

    Runs in a threadpool worker (Starlette iterates sync generators there),
    so blocking sleeps and synchronous SQLAlchemy reads are fine. The stream
    owns its own session: FastAPI dependency sessions are unsuitable here
    because the response outlives the request handler.
    """
    session: Session = SessionLocal()
    try:
        execution = session.get(Execution, execution_id)
        if execution is None:
            raise domain_error(404, "execution_not_found", "Execution not found")

        # Immediate snapshot so clients never wait a poll cycle for state.
        yield _execution_event(execution)
        last_status = execution.status
        seen_stdout, seen_stderr = execution.stdout, execution.stderr
        stdout_truncated, stderr_truncated = (
            execution.stdout_truncated,
            execution.stderr_truncated,
        )
        if execution.status in TERMINAL_STATUSES:
            return
        # Release the read snapshot; polling must never hold a long
        # transaction (which would also block autovacuum).
        session.rollback()

        poll = max(settings.sse_poll_interval_seconds, 0.1)
        keepalive = max(settings.sse_keepalive_seconds, poll)
        idle_seconds = 0.0
        while True:
            time.sleep(poll)
            idle_seconds += poll
            session.expire_all()
            execution = session.get(Execution, execution_id)
            if execution is None:
                return
            changed = False

            events, seen_stdout, stdout_truncated = _diff_stream(
                "stdout",
                seen_stdout,
                execution.stdout,
                stdout_truncated,
                execution.stdout_truncated,
            )
            for event in events:
                yield event
            changed = changed or bool(events)

            events, seen_stderr, stderr_truncated = _diff_stream(
                "stderr",
                seen_stderr,
                execution.stderr,
                stderr_truncated,
                execution.stderr_truncated,
            )
            for event in events:
                yield event
            changed = changed or bool(events)

            if execution.status != last_status:
                yield _execution_event(execution)
                last_status = execution.status
                changed = True

            terminal = execution.status in TERMINAL_STATUSES
            session.rollback()
            if terminal:
                return
            if not changed and idle_seconds >= keepalive:
                # SSE comment; keeps proxies from closing an idle stream.
                yield ": keepalive\n\n"
                idle_seconds = 0.0
    finally:
        session.close()
