"""Restricted-stage ingress rejects unsupported work before taking responsibility."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select, update

from dlr.common.config import settings
from dlr.control.models import (
    Adapter,
    AdapterExecutionAdmission,
    AdapterInputConfig,
    AdapterSchedule,
    Execution,
    ExecutionAttempt,
    ExecutionIdempotencyRecord,
    ExecutionOutbox,
    GlobalExecutionAdmission,
    Worker,
)
from dlr.control.models.platform import PackageSource
from dlr.control.services.worker_stage_capability_v4 import execution_block_reason
from test_adapters import create_adapter, save_version
from test_credentials import create_credential, set_bindings
from test_webhook_trigger import put_webhook


def _assert_no_responsibility(session_factory):
    with session_factory() as session:
        for model in (Execution, ExecutionAttempt, ExecutionOutbox, ExecutionIdempotencyRecord):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        for model in (AdapterExecutionAdmission, GlobalExecutionAdmission):
            assert session.scalar(select(func.coalesce(func.sum(model.outstanding_count), 0))) == 0
            assert session.scalar(select(func.coalesce(func.sum(model.outstanding_bytes), 0))) == 0


@pytest.mark.parametrize(
    "unsupported", ["language", "requirements", "input_source", "credentials", "builtin_packages"]
)
def test_unsupported_manual_and_schedule_take_no_responsibility(
    api_client, session_factory, monkeypatch, unsupported
):
    adapter = create_adapter(
        api_client, language="javascript" if unsupported == "language" else "python"
    )
    save_version(
        api_client, adapter["id"], requirements="requests" if unsupported == "requirements" else ""
    )
    if unsupported == "input_source":
        with session_factory.begin() as session:
            session.execute(
                update(AdapterInputConfig)
                .where(AdapterInputConfig.adapter_id == adapter["id"])
                .values(source_type="managed_files", json_value=None)
            )
    if unsupported == "credentials":
        credential = create_credential(api_client)
        assert (
            set_bindings(
                api_client,
                adapter["id"],
                [
                    {
                        "env_key": "PASSWORD",
                        "credential_id": credential["id"],
                        "field": "password",
                    }
                ],
            ).status_code
            == 200
        )
    if unsupported == "builtin_packages":
        with session_factory.begin() as session:
            session.execute(update(PackageSource).values(is_default=False))
            session.add(
                PackageSource(
                    name="stage-builtin",
                    kind="pypi",
                    index_url="dlr-builtin://pypi",
                    is_default=True,
                )
            )
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/executions",
        json={},
        headers={"Idempotency-Key": "unsupported"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "worker_task_capability_unavailable"
    assert response.json()["detail"]["params"]["reason"] == unsupported
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"}
        ).status_code
        == 200
    )
    response = api_client.put(
        f"/api/adapters/{adapter['id']}/schedule",
        json={"enabled": True, "cron": "0 * * * *", "timezone": "UTC"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "worker_task_capability_unavailable"
    with session_factory() as session:
        schedule = session.scalar(
            select(AdapterSchedule).where(AdapterSchedule.adapter_id == adapter["id"])
        )
        assert schedule is None or not schedule.enabled
    _assert_no_responsibility(session_factory)


def test_supported_manual_keeps_frozen_capability_and_idempotency(
    api_client, session_factory, monkeypatch
):
    adapter = create_adapter(api_client)
    save_version(api_client, adapter["id"])
    # Producer-only setup: runtime identity/physical isolation are tested on
    # the real Worker chain, not claimed by this API acceptance regression.
    with session_factory.begin() as session:
        session.execute(
            update(Worker).values(
                protocol_version=4,
                identity_lifecycle="reserved",
                identity_enrolled_at=datetime.now(UTC),
            )
        )
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    response = api_client.post(
        f"/api/adapters/{adapter['id']}/executions",
        json={},
        headers={"Idempotency-Key": "supported"},
    )
    assert response.status_code == 202, response.text
    execution_id = response.json()["id"]
    with session_factory.begin() as session:
        # A later global default must not invalidate the already frozen work.
        session.execute(update(PackageSource).values(is_default=False))
        session.add(
            PackageSource(
                name="later-builtin", kind="pypi", index_url="dlr-builtin://pypi", is_default=True
            )
        )
    again = api_client.post(
        f"/api/adapters/{adapter['id']}/executions",
        json={},
        headers={"Idempotency-Key": "supported"},
    )
    assert again.status_code == 202 and again.json()["id"] == execution_id
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert (
            execution_block_reason(session, session.get(Adapter, adapter["id"]), execution) is None
        )
        assert session.scalar(select(func.count()).select_from(Execution)) == 1
        assert session.scalar(select(func.count()).select_from(ExecutionOutbox)) == 1
        assert session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0


def test_remote_stage_does_not_enable_webhook(api_client, session_factory, monkeypatch):
    adapter = create_adapter(api_client, adapter_type="webhook")
    save_version(api_client, adapter["id"])
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    response = put_webhook(api_client, adapter["id"], credential_id=None)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "worker_task_capability_unavailable"
    _assert_no_responsibility(session_factory)
