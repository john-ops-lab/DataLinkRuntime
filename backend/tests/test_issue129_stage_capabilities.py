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
from dlr.control.services.worker_stage_capability_v4 import dispatch_blocked, execution_block_reason
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
@pytest.mark.parametrize("task_language", ["python", "javascript"])
def test_unsupported_manual_and_schedule_take_no_responsibility(
    api_client, session_factory, monkeypatch, unsupported, task_language
):
    adapter = create_adapter(
        api_client, language="java" if unsupported == "language" else task_language
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
        kind = "npm" if task_language == "javascript" else "pypi"
        with session_factory.begin() as session:
            session.execute(update(PackageSource).values(is_default=False))
            session.add(
                PackageSource(
                    name="stage-builtin",
                    kind=kind,
                    index_url=f"dlr-builtin://{kind}",
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
    assert response.json()["detail"]["params"]["reason"] == (
        "credentials_mode" if unsupported == "credentials" else unsupported
    )
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


def _javascript_adapter(client, session_factory, monkeypatch):
    adapter = create_adapter(client, language="javascript")
    save_version(
        client, adapter["id"], code="export async function handle(c, input) { return input; }"
    )
    with session_factory.begin() as session:
        session.execute(
            update(Worker).values(
                protocol_version=4,
                identity_lifecycle="reserved",
                identity_enrolled_at=datetime.now(UTC),
                capabilities=["python", "javascript"],
            )
        )
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    return adapter


@pytest.mark.parametrize("kind", ["pypi", "npm"])
def test_javascript_uses_its_own_package_source_gate(
    api_client, session_factory, monkeypatch, kind
):
    adapter = _javascript_adapter(api_client, session_factory, monkeypatch)
    with session_factory.begin() as session:
        session.execute(update(PackageSource).values(is_default=False))
        session.add(
            PackageSource(
                name="language-builtin",
                kind=kind,
                index_url=f"dlr-builtin://{kind}",
                is_default=True,
            )
        )
    result = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    if kind == "npm":
        assert result.status_code == 409, result.text
        assert result.json()["detail"]["params"]["reason"] == "builtin_packages"
        _assert_no_responsibility(session_factory)
    else:
        assert result.status_code == 202, result.text


@pytest.mark.parametrize(
    "languages",
    [
        ["python"],
        ["javascript"],
        ["javascript", "python"],
        ["python", "javascript", "typescript"],
        ["python", "python"],
        [],
    ],
)
def test_javascript_target_mismatch_refuses_manual_and_schedule_before_responsibility(
    api_client, session_factory, monkeypatch, languages
):
    adapter = _javascript_adapter(api_client, session_factory, monkeypatch)
    with session_factory.begin() as session:
        session.execute(update(Worker).values(capabilities=languages))
    result = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert result.status_code == 409, result.text
    assert result.json()["detail"]["code"] == "runtime_worker_invalid"
    assert (
        api_client.patch(
            f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"}
        ).status_code
        == 200
    )
    result = api_client.put(
        f"/api/adapters/{adapter['id']}/schedule",
        json={
            "enabled": True,
            "cron": "0 * * * *",
            "timezone": "UTC",
        },
    )
    assert result.status_code == 409, result.text
    _assert_no_responsibility(session_factory)


def test_frozen_javascript_dispatch_waits_without_attempt_or_retry_and_ignores_later_defaults(
    api_client, session_factory, monkeypatch
):
    from dlr.control.services.outbox import lease_due_outbox

    adapter = _javascript_adapter(api_client, session_factory, monkeypatch)
    result = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert result.status_code == 202, result.text
    execution_id = result.json()["id"]
    with session_factory.begin() as session:
        session.execute(update(Worker).values(capabilities=["python"]))
    with session_factory() as session:
        assert dispatch_blocked(session, execution_id)
        assert lease_due_outbox(session, "test-g") == []
        assert session.get(Execution, execution_id).attempt_count == 0
        assert session.scalar(select(func.count()).select_from(ExecutionAttempt)) == 0
        row = session.scalar(select(ExecutionOutbox))
        assert row.status == "pending" and row.publish_attempts == 0
        assert row.last_error_code == "worker_task_capability_unavailable"
    with session_factory.begin() as session:
        session.execute(update(Worker).values(capabilities=["python", "javascript"]))
        session.execute(update(PackageSource).values(is_default=False))
        session.add(
            PackageSource(
                name="later-npm", kind="npm", index_url="dlr-builtin://npm", is_default=True
            )
        )
        assert not dispatch_blocked(session, execution_id)
        execution = session.get(Execution, execution_id)
        assert execution.builtin_package_snapshot is None
        assert (
            execution_block_reason(session, session.get(Adapter, adapter["id"]), execution) is None
        )
        execution.builtin_package_snapshot = {"unsupported": True}
        session.flush()
        assert dispatch_blocked(session, execution_id)


def _business_adapter(
    client, session_factory, monkeypatch, *, kind="token", fields=None, field="token"
):
    from dlr.common import business_credentials_v4 as contract
    from dlr.control.services import business_credentials_v4 as business

    adapter = create_adapter(client)
    save_version(client, adapter["id"])
    credential = create_credential(client, type_=kind, fields=fields or {"token": "private-canary"})
    assert (
        set_bindings(
            client,
            adapter["id"],
            [{"env_key": "BOUND", "credential_id": credential["id"], "field": field}],
        ).status_code
        == 200
    )
    with session_factory.begin() as session:
        worker = session.scalar(select(Worker).order_by(Worker.id))
        adapter["runtime_worker_id"] = worker.id
        session.get(Adapter, adapter["id"]).runtime_worker_id = worker.id
        worker.protocol_version = 4
        worker.identity_lifecycle = "reserved"
        worker.identity_enrolled_at = datetime.now(UTC)
        worker.isolation_capabilities = {contract.CAPABILITY: True}
        policy = business.Policy(
            (business.Grant(worker.id, worker.identity_id, (credential["id"],)),)
        )
    monkeypatch.setattr(settings, "_business_credential_policy", policy)
    monkeypatch.setattr(settings, "worker_remote_v4_enabled", True)
    return adapter, credential


@pytest.mark.parametrize(
    "kind,fields,field,expected",
    [
        ("password", {"username": "user", "password": "pass"}, "username", "user"),
        ("token", {"token": "token-value"}, "token", "token-value"),
        (
            "access_key",
            {"access_key_id": "id", "access_key_secret": "secret"},
            "access_key_id",
            "id",
        ),
        ("secret", {"value": "custom-value"}, "value", "custom-value"),
    ],
)
def test_business_binding_freezes_references_but_resolves_current_value(
    api_client, session_factory, monkeypatch, kind, fields, field, expected
):
    from dlr.control.services import business_credentials_v4 as business

    adapter, credential = _business_adapter(
        api_client, session_factory, monkeypatch, kind=kind, fields=fields, field=field
    )
    result = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert result.status_code == 202, result.text
    execution_id = result.json()["id"]
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        refs = business.execution_bindings(session, execution_id)
        assert refs[0].credential_id == credential["id"]
        assert not dispatch_blocked(session, execution_id)
        assert business.resolve_values(
            session, execution, session.get(Worker, adapter["runtime_worker_id"])
        ) == {"BOUND": expected}
    changed = dict(fields, **{field: "rotated-value"})
    assert (
        api_client.patch(
            f"/api/credentials/{credential['id']}", json={"fields": changed}
        ).status_code
        == 200
    )
    assert set_bindings(api_client, adapter["id"], []).status_code == 200
    # RESTRICT still protects accepted references after the current binding is removed.
    assert api_client.delete(f"/api/credentials/{credential['id']}").status_code == 409
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert business.resolve_values(
            session, execution, session.get(Worker, adapter["runtime_worker_id"])
        ) == {"BOUND": "rotated-value"}
        assert (
            execution_block_reason(session, session.get(Adapter, adapter["id"]), execution) is None
        )


@pytest.mark.parametrize(
    "blocked",
    ["no_policy", "wrong_identity", "capability_missing", "ciphertext", "bindings", "key_missing"],
)
def test_business_denied_before_new_responsibility(
    api_client, session_factory, monkeypatch, blocked
):
    from uuid import uuid4

    from dlr.control.models.platform import Credential
    from dlr.control.services import business_credentials_v4 as business

    adapter, credential = _business_adapter(api_client, session_factory, monkeypatch)
    if blocked == "no_policy":
        monkeypatch.setattr(settings, "_business_credential_policy", business.Policy())
    elif blocked == "wrong_identity":
        monkeypatch.setattr(
            settings,
            "_business_credential_policy",
            business.Policy(
                (business.Grant(adapter["runtime_worker_id"], uuid4(), (credential["id"],)),)
            ),
        )
    elif blocked == "capability_missing":
        with session_factory.begin() as session:
            session.get(Worker, adapter["runtime_worker_id"]).isolation_capabilities = {}
    elif blocked == "ciphertext":
        with session_factory.begin() as session:
            session.get(Credential, credential["id"]).ciphertext = "x" * 32769
    elif blocked == "bindings":
        assert (
            set_bindings(
                api_client,
                adapter["id"],
                [
                    {"env_key": "B" + str(n), "credential_id": credential["id"], "field": "token"}
                    for n in range(17)
                ],
            ).status_code
            == 200
        )
    elif blocked == "key_missing":
        monkeypatch.setattr(settings, "master_key", None)
    result = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert result.status_code == (503 if blocked == "key_missing" else 409), result.text
    _assert_no_responsibility(session_factory)
    if blocked == "no_policy":
        assert (
            api_client.patch(
                f"/api/adapters/{adapter['id']}", json={"run_mode": "schedule"}
            ).status_code
            == 200
        )
        assert (
            api_client.put(
                f"/api/adapters/{adapter['id']}/schedule",
                json={"enabled": True, "cron": "0 * * * *", "timezone": "UTC"},
            ).status_code
            == 409
        )
        _assert_no_responsibility(session_factory)


def test_ciphertext_bound_precedes_plaintext_and_frozen_snapshot_tamper_is_rejected(
    api_client, session_factory, monkeypatch
):
    from dlr.common.business_credentials_v4 import BusinessCredentialError
    from dlr.control.models.platform import Credential
    from dlr.control.services import business_credentials_v4 as business
    from dlr.control.services import secrets

    adapter, credential = _business_adapter(api_client, session_factory, monkeypatch)
    result = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert result.status_code == 202, result.text
    execution_id = result.json()["id"]
    for value in ["中" * 1366, "bad\x00value", ""]:
        with session_factory.begin() as session:
            session.get(Credential, credential["id"]).ciphertext = secrets.encrypt_fields(
                {"token": value}
            )
        with session_factory() as session, pytest.raises(BusinessCredentialError):
            business.resolve_values(
                session,
                session.get(Execution, execution_id),
                session.get(Worker, adapter["runtime_worker_id"]),
            )
    with session_factory.begin() as session:
        session.get(Credential, credential["id"]).ciphertext = "x" * 32769

    def forbidden(_ciphertext):
        raise AssertionError("unbounded ciphertext reached decryption")

    monkeypatch.setattr(secrets, "decrypt_fields", forbidden)
    with (
        session_factory() as session,
        pytest.raises(BusinessCredentialError, match="credentials_budget"),
    ):
        business.resolve_values(
            session,
            session.get(Execution, execution_id),
            session.get(Worker, adapter["runtime_worker_id"]),
        )
    with session_factory.begin() as session:
        session.get(Execution, execution_id).credential_bindings_snapshot = [{"bad": True}]
    with session_factory() as session, pytest.raises(BusinessCredentialError):
        business.execution_bindings(session, execution_id)


def test_distinct_ciphertext_total_is_checked_before_any_decryption(
    api_client, session_factory, monkeypatch
):
    from dlr.common.business_credentials_v4 import BusinessCredentialError
    from dlr.control.models.platform import Credential
    from dlr.control.services import business_credentials_v4 as business

    adapter, first = _business_adapter(api_client, session_factory, monkeypatch)
    ids = [first["id"]]
    for number in range(2):
        ids.append(
            create_credential(
                api_client, name=f"extra-{number}", type_="token", fields={"token": "canary"}
            )["id"]
        )
    assert (
        set_bindings(
            api_client,
            adapter["id"],
            [
                {"env_key": f"K{n}", "field": "token", "credential_id": cid}
                for n, cid in enumerate(ids)
            ],
        ).status_code
        == 200
    )
    with session_factory.begin() as session:
        worker = session.get(Worker, adapter["runtime_worker_id"])
        monkeypatch.setattr(
            settings,
            "_business_credential_policy",
            business.Policy((business.Grant(worker.id, worker.identity_id, tuple(ids)),)),
        )
        for cid in ids:
            session.get(Credential, cid).ciphertext = "x" * 22000
    with (
        session_factory() as session,
        pytest.raises(BusinessCredentialError, match="credentials_budget"),
    ):
        business.check(
            session, business.adapter_bindings(session, adapter["id"]), adapter["runtime_worker_id"]
        )
    result = api_client.post(f"/api/adapters/{adapter['id']}/executions", json={})
    assert result.status_code == 409, result.text
    _assert_no_responsibility(session_factory)
