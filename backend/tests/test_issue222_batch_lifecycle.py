"""Real Claim/finish/cancellation/member recovery paths, independent of batch core helpers."""

from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from dlr.common.config import settings
from dlr.control.api.subscriptions import serialize_event
from dlr.control.models import (
    EventDelivery,
    EventReceipt,
    Execution,
    ExecutionOutbox,
    SourceCheckpoint,
    SubscriptionExecutionBatch,
)
from dlr.control.schemas.reliable_runtime import AttemptResultBody
from dlr.control.services import attempt as attempts
from dlr.control.services import execution as executions
from dlr.control.services import http_ingress
from dlr.control.services import subscription_delivery as delivery
from dlr.control.services.input_config import database_now
from dlr.control.services.subscription_batch_view import delivery_records
from test_issue222_business_batch import batch_world as core_batch_world
from test_issue222_business_batch import facts, produce
from test_issue222_business_batch import world as foundation_world

world = foundation_world
batch_world = core_batch_world


def created(factory: Any, world: Any, *, claim: bool = True) -> tuple[Any, ...]:
    with factory.begin() as session:
        ids, execution = produce(session, world)
        execution_id, worker_id = execution.id, execution.target_worker_id
    decision = claimed(factory, execution_id, worker_id) if claim else None
    return ids, execution_id, worker_id, decision


def claimed(factory: Any, execution_id: int, worker_id: int) -> Any:
    with factory() as session:
        execution = session.get(Execution, execution_id)
        row = session.scalar(
            select(ExecutionOutbox).where(
                ExecutionOutbox.execution_id == execution_id,
                ExecutionOutbox.dispatch_generation == execution.dispatch_generation,
            )
        )
        payload = deepcopy(row.payload_json)
        result = attempts.claim_dispatch(session, worker_id, payload)
    assert result.decision == "EXECUTE", result.reason
    return result


def result_frame(factory: Any, execution_id: int, statuses: Any = None) -> dict[str, Any]:
    with factory() as session:
        group = session.get(SubscriptionExecutionBatch, execution_id)
        members = [
            {
                key: member[key]
                for key in ("receipt_id", "delivery_id", "delivery_ordinal", "member_index")
            }
            | {"status": status, "error_code": code}
            for member, (status, code) in zip(
                group.manifest, statuses or [("succeeded", None)] * group.member_count, strict=True
            )
        ]
        return {
            "schema": "dlr.event.batch.result.v1",
            "batch_id": group.batch_id,
            "manifest_hash": group.manifest_hash,
            "members": members,
        }


def finish(factory: Any, worker_id: int, decision: Any, output: Any, **extra: Any) -> Any:
    payload = {
        "attempt_id": decision.attempt_id,
        "claim_token": decision.payload.claim_token,
        "fencing_token": decision.payload.fencing_token,
        "status": "succeeded",
        "output": output,
        "workspace_cleanup_status": "completed",
        **extra,
    }
    with factory() as session:
        return attempts.finish_attempt(
            session, worker_id, decision.attempt_id, AttemptResultBody.model_validate(payload)
        )


def settle(factory: Any, execution_id: int) -> None:
    with factory.begin() as session:
        item = session.scalar(
            select(EventDelivery)
            .where(EventDelivery.batch_execution_id == execution_id)
            .order_by(EventDelivery.id)
        )
        delivery.observe_terminal(session, item.id)


def checkpoint(session: Any, world: Any) -> Any:
    return session.scalar(
        select(SourceCheckpoint).where(SourceCheckpoint.subscription_id == world["sub"])
    )


@pytest.mark.parametrize(
    "case",
    [
        "success",
        "partial_failed",
        "partial_unknown",
        "forged_id",
        "wrong_hash",
        "missing_member",
        "success_after_failure",
        "worker_truncated",
        "control_truncated",
    ],
)
def test_authoritative_finish_validates_members_before_shared_success_and_admission_release(
    batch_world: Any, session_factory: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    ids, execution_id, worker_id, claim = created(session_factory, batch_world)
    statuses = None
    if case in {"partial_failed", "partial_unknown", "success_after_failure"}:
        middle = (
            ("unknown", "target_commit_unknown")
            if case == "partial_unknown"
            else ("failed", "business_error")
        )
        statuses = [("succeeded", None), middle, ("not_run", "predecessor_failed")]
    output = result_frame(session_factory, execution_id, statuses)
    if case == "forged_id":
        output["members"][1]["receipt_id"] = 999999
    elif case == "wrong_hash":
        output["manifest_hash"] = "f" * 64
    elif case == "missing_member":
        output["members"].pop()
    elif case == "success_after_failure":
        output["members"][2] |= {"status": "succeeded", "error_code": None}
    elif case == "control_truncated":
        monkeypatch.setattr(settings, "execution_output_max_bytes", 128)
    outcome = finish(
        session_factory,
        worker_id,
        claim,
        None if case == "worker_truncated" else output,
        output_truncated=case == "worker_truncated",
        output_size=1024 if case == "worker_truncated" else None,
        output_preview='{"schema":"dlr.event.batch.result.v1","members":[{"status":"succeeded"}]}'
        if case == "worker_truncated"
        else None,
    )
    assert outcome.reason == "terminal_recorded"
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        group = session.get(SubscriptionExecutionBatch, execution_id)
        assert execution.admission_released_at is not None
        assert group.result_attempt_id == claim.attempt_id
        assert execution.status == ("succeeded" if case == "success" else "dead_letter")
        if case == "success":
            assert group.result_counts == {"succeeded": 3, "failed": 0, "unknown": 0, "not_run": 0}
        elif case in {"partial_failed", "partial_unknown"}:
            assert group.result_counts["succeeded"] == 1
            assert group.result_counts["not_run"] == 1
        else:
            assert group.result_counts == {"succeeded": 0, "failed": 0, "unknown": 3, "not_run": 0}
    settle(session_factory, execution_id)
    with session_factory() as session:
        states = [session.get(EventReceipt, item).state for item in ids]
        assert states == (
            ["succeeded"] * 3
            if case == "success"
            else ["succeeded", "failed", "failed"]
            if case in {"partial_failed", "partial_unknown"}
            else ["failed"] * 3
        )
        assert checkpoint(session, batch_world).blocked_receipt_id == (
            None
            if case == "success"
            else ids[1]
            if case in {"partial_failed", "partial_unknown"}
            else ids[0]
        )
        facts(session, batch_world)
    # Duplicate authoritative result cannot overwrite the frozen first facts.
    duplicate = finish(
        session_factory, worker_id, claim, result_frame(session_factory, execution_id)
    )
    assert duplicate.reason == "already_terminal"


def test_infrastructure_retry_wait_keeps_processing_members_and_same_frozen_input(
    batch_world: Any, session_factory: Any
) -> None:
    ids, execution_id, worker_id, first = created(session_factory, batch_world)
    frozen = result_frame(session_factory, execution_id)
    finish(
        session_factory,
        worker_id,
        first,
        frozen,
        status="failed",
        error_class="platform_transient",
        error_code="target_temporarily_unavailable",
    )
    settle(session_factory, execution_id)
    with session_factory.begin() as session:
        execution = session.get(Execution, execution_id)
        group = session.get(SubscriptionExecutionBatch, execution_id)
        assert execution.status == "retry_wait" and execution.admission_released_at is None
        assert group.result is group.result_counts is group.result_attempt_id is None
        assert [session.get(EventReceipt, item).state for item in ids] == ["processing"] * 3
        execution.next_attempt_at = database_now(session) - timedelta(seconds=1)
    with session_factory() as session:
        assert attempts.retry_dispatcher_once(session) == 1
    second = claimed(session_factory, execution_id, worker_id)
    assert second.attempt_id != first.attempt_id
    assert second.payload.input == first.payload.input
    finish(session_factory, worker_id, second, frozen)
    settle(session_factory, execution_id)
    with session_factory() as session:
        assert session.get(Execution, execution_id).status == "succeeded"
        assert (
            session.get(SubscriptionExecutionBatch, execution_id).result_attempt_id
            == second.attempt_id
        )
        assert [session.get(EventReceipt, item).state for item in ids] == ["succeeded"] * 3
        facts(session, batch_world)


def test_cancelling_unclaimed_batch_records_all_members_not_run_and_cannot_claim(
    batch_world: Any, session_factory: Any
) -> None:
    ids, execution_id, worker_id, _ = created(session_factory, batch_world, claim=False)
    with session_factory() as session:
        executions.cancel_execution(session, execution_id)
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        group = session.get(SubscriptionExecutionBatch, execution_id)
        assert execution.status == "cancelled" and execution.admission_released_at is not None
        assert execution.attempt_count == 0
        assert group.result_counts == {"succeeded": 0, "failed": 0, "unknown": 0, "not_run": 3}
        assert group.result_provenance == "control_not_started"
        row = session.scalar(
            select(ExecutionOutbox).where(ExecutionOutbox.execution_id == execution_id)
        )
        claim = attempts.claim_dispatch(session, worker_id, row.payload_json)
        assert claim.decision == "ACK_NOOP" and claim.reason == "cancelled"
    settle(session_factory, execution_id)
    with session_factory() as session:
        assert [session.get(EventReceipt, item).state for item in ids] == ["failed"] * 3
        for identity in ids:
            view = serialize_event(session, session.get(EventReceipt, identity))
            assert view.batch_member_outcome == "not_run"
            assert view.batch_member_code == "execution_not_started"
        facts(session, batch_world)


def test_member_replay_and_skip_keep_other_blockers_and_project_each_delivery(
    batch_world: Any, session_factory: Any
) -> None:
    ids, original_id, worker_id, claim = created(session_factory, batch_world)
    output = result_frame(
        session_factory,
        original_id,
        [("succeeded", None), ("failed", "business_error"), ("not_run", "predecessor_failed")],
    )
    finish(session_factory, worker_id, claim, output)
    settle(session_factory, original_id)
    with session_factory.begin() as session:
        for index, identity in enumerate(ids):
            receipt = session.get(EventReceipt, identity)
            view = serialize_event(session, receipt)
            public = http_ingress._status(session, "public-fixture", receipt)
            assert view.batch_execution_id == public["execution_id"] == original_id
            assert view.batch_member_count == 3 and view.batch_member_index == index
            assert public["batch_member_outcome"] == output["members"][index]["status"]
            assert public["error_code"] == output["members"][index]["error_code"]
        assert checkpoint(session, batch_world).blocked_receipt_id == ids[1]
        replay = delivery.replay_event(
            session, ids[1], "replay-middle", None, "repair middle fixture"
        )
        fresh = delivery.dispatch_delivery(session, replay.id)
        assert fresh is not None
        replay_id, execution_id = replay.id, fresh.id
        assert session.get(SubscriptionExecutionBatch, execution_id).member_count == 1
    second_claim = claimed(session_factory, execution_id, worker_id)
    finish(session_factory, worker_id, second_claim, result_frame(session_factory, execution_id))
    settle(session_factory, execution_id)
    with session_factory.begin() as session:
        assert checkpoint(session, batch_world).blocked_receipt_id == ids[2]
        old = session.get(SubscriptionExecutionBatch, original_id)
        assert old.result_counts == {"succeeded": 1, "failed": 1, "unknown": 0, "not_run": 1}
        receipt = session.get(EventReceipt, ids[1])
        records = delivery_records(session, receipt)
        assert len(records) == 2
        assert records[0]["batch_member_outcome"] == "failed"
        assert records[1]["id"] == replay_id and records[1]["batch_member_outcome"] == "succeeded"
        public = http_ingress._status(session, "public-fixture", receipt)
        assert public["execution_id"] == execution_id and public["error_code"] is None
        delivery.skip_event(
            session, ids[2], "skip-last", None, "explicitly discard remaining fixture"
        )
        assert checkpoint(session, batch_world).blocked_receipt_id is None
        assert (
            session.get(EventReceipt, ids[0]).state
            == session.get(EventReceipt, ids[1]).state
            == "succeeded"
        )
        assert session.get(EventReceipt, ids[2]).state == "skipped"
        facts(session, batch_world)


@pytest.mark.parametrize("mismatch", ["token", "fence", "worker"])
def test_unowned_report_cannot_seal_member_facts_or_release_shared_admission(
    batch_world: Any, session_factory: Any, mismatch: str
) -> None:
    ids, execution_id, worker_id, claim = created(session_factory, batch_world)
    output = result_frame(session_factory, execution_id)
    changes = (
        {"claim_token": "invalid-claim-token"}
        if mismatch == "token"
        else {"fencing_token": claim.payload.fencing_token + 1}
        if mismatch == "fence"
        else {}
    )
    with pytest.raises(HTTPException):
        finish(
            session_factory,
            worker_id + 99999 if mismatch == "worker" else worker_id,
            claim,
            output,
            **changes,
        )
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        group = session.get(SubscriptionExecutionBatch, execution_id)
        assert execution.status == "running" and execution.admission_released_at is None
        assert group.result is group.result_attempt_id is None
        assert [session.get(EventReceipt, item).state for item in ids] == ["processing"] * 3
    finish(session_factory, worker_id, claim, output)
    settle(session_factory, execution_id)
    with session_factory() as session:
        assert session.get(Execution, execution_id).status == "succeeded"
        facts(session, batch_world)


def test_cancel_requested_racing_with_validated_success_keeps_cancellation_and_member_facts(
    batch_world: Any,
    session_factory: Any,
) -> None:
    ids, execution_id, worker_id, claim = created(session_factory, batch_world)
    output = result_frame(session_factory, execution_id)
    with session_factory() as session:
        cancelled = executions.cancel_execution(session, execution_id)
        assert cancelled.status == "running" and cancelled.cancel_requested is True
    finish(session_factory, worker_id, claim, output)
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        group = session.get(SubscriptionExecutionBatch, execution_id)
        # Existing cancellation priority is independent of already validated
        # business facts: the logical Execution remains cancelled, but those
        # proven successes must not be rejected or converted into unknown.
        assert execution.status == "cancelled" and execution.admission_released_at is not None
        assert group.result_counts == {"succeeded": 3, "failed": 0, "unknown": 0, "not_run": 0}
        assert group.result_provenance == "worker_validated"
        assert group.result_attempt_id == claim.attempt_id
    settle(session_factory, execution_id)
    with session_factory() as session:
        assert [session.get(EventReceipt, identity).state for identity in ids] == ["succeeded"] * 3
        assert checkpoint(session, batch_world).blocked_receipt_id is None
        facts(session, batch_world)
