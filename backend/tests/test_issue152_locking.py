"""Issue #152B lock-order and terminal shortcut regressions."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import (
    AdapterExecutionAdmission,
    AdapterExecutionSlot,
    Execution,
    ExecutionAttempt,
    GlobalExecutionAdmission,
)
from test_issue130_b2_runtime import (
    _claim,
    _dispatch,
    _enable_runtime,
    _execution,
    _rabbit_adapter,
    _ready_worker,
)


def test_claim_cancel_shortcut_preserves_queued_execution_with_active_attempt(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupt queued projection cannot release a still-active responsibility."""

    _enable_runtime(monkeypatch)
    worker = _ready_worker(api_client, "issue152-queued-active-worker")
    adapter = _rabbit_adapter(api_client, worker, "issue152-queued-active-adapter")
    execution = _execution(api_client, adapter["id"])
    dispatch = _dispatch(session_factory, execution["id"])
    claimed = _claim(session_factory, worker["id"], dispatch)
    assert claimed.attempt_id is not None

    with session_factory.begin() as session:
        row = session.get(Execution, execution["id"])
        assert row is not None
        row.status = "queued"
        row.cancel_requested = True

    repeated = _claim(session_factory, worker["id"], dispatch)
    assert repeated.decision == "ACK_NOOP"
    assert repeated.reason == "cancel_requested"
    assert repeated.attempt_id == claimed.attempt_id
    assert repeated.cancel_requested is True

    with session_factory() as session:
        row = session.get(Execution, execution["id"])
        attempt = session.get(ExecutionAttempt, claimed.attempt_id)
        slot = session.scalar(
            select(AdapterExecutionSlot).where(AdapterExecutionSlot.adapter_id == adapter["id"])
        )
        adapter_admission = session.get(AdapterExecutionAdmission, adapter["id"])
        global_admission = session.get(GlobalExecutionAdmission, "global")
        assert row is not None and row.status == "queued"
        assert row.cancel_requested is True and row.admission_released_at is None
        assert attempt is not None and attempt.status == "claimed"
        assert slot is not None and slot.active_attempt_id == attempt.id
        assert adapter_admission is not None and adapter_admission.outstanding_count == 1
        assert global_admission is not None and global_admission.outstanding_count == 1
