"""Issue #132 atomic Template Recipe to Adapter instantiation contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import (
    Adapter,
    AdapterExecutionSlot,
    AdapterInputConfig,
    AdapterPermission,
    AdapterVersion,
    AdapterWebhook,
    User,
)
from dlr.control.schemas.adapter import DEFAULT_EXECUTION_TIMEOUT_SECONDS
from dlr.control.schemas.template import TemplateInstantiateRequest
from dlr.control.services import template as template_service
from dlr.control.services.accounts import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    create_session,
    hash_password,
)
from dlr.control.services.secrets import bootstrap_demo_credentials

ACCOUNT_PREFIX = "/__dlr_account"
TASK_SCENARIO = "rest-single-request"
WEBHOOK_SCENARIO = "webhook-json-normalization"
FORBIDDEN_TABLES = (
    "credentials",
    "adapter_credential_bindings",
    "adapter_schedules",
    "adapter_permissions",
    "package_sources",
    "managed_input_upload_reservations",
    "managed_input_artifacts",
    "adapter_input_artifact_bindings",
    "artifact_deletion_jobs",
    "execution_input_artifact_leases",
    "execution_credential_binding_snapshots",
    "workers",
    "executions",
    "execution_idempotency_records",
    "adapter_execution_admission",
    "global_execution_admission",
    "execution_outbox",
    "execution_attempts",
    "schedule_dispatch_outcomes",
    "execution_infrastructure_incidents",
    "execution_artifact_holds",
    "worker_cleanup_requests",
)
CREATED_GRAPH_TABLES = (
    "adapters",
    "adapter_execution_slots",
    "adapter_input_configs",
    "adapter_webhooks",
    "adapter_versions",
)


def _variant(client: TestClient, scenario: str, language: str = "python") -> dict[str, Any]:
    response = client.get(f"/api/templates/scenarios/{scenario}/variants/{language}")
    assert response.status_code == 200, response.text
    return response.json()


def _instantiate(
    client: TestClient,
    scenario: str,
    *,
    name: str,
    language: str = "python",
    version: str | None = None,
) -> Any:
    selected = _variant(client, scenario, language)
    return client.post(
        f"/api/templates/scenarios/{scenario}/variants/{language}/instantiate",
        json={
            "name": name,
            "expected_template_version": version or selected["template_version"],
        },
    )


def _table_counts(session: Session, tables: tuple[str, ...]) -> dict[str, int]:
    # The identifiers are a closed test constant, never user input.
    return {
        table: int(session.scalar(text(f"SELECT count(*) FROM {table}")) or 0) for table in tables
    }


def _new_account_client(
    app: Any,
    session_factory: sessionmaker[Session],
    *,
    username: str,
    role: str,
) -> tuple[TestClient, int, str]:
    with session_factory() as session:
        user = User(
            username=username,
            password_hash=hash_password(f"{username}-password"),
            role=role,
            enabled=True,
            must_change_password=False,
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        user_id = user.id
        raw_session = create_session(session, user)

    csrf_token = f"{username}-csrf"
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, raw_session)
    client.cookies.set(CSRF_COOKIE_NAME, csrf_token)
    return client, user_id, csrf_token


def test_task_instantiate_creates_independent_unsaved_adapter_and_location(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    detail = api_client.get(f"/api/templates/scenarios/{TASK_SCENARIO}").json()
    response = _instantiate(
        api_client,
        TASK_SCENARIO,
        name="  copied REST Java  ",
        language="java",
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert response.headers["Location"] == f"/api/adapters/{body['id']}"
    assert body["name"] == "copied REST Java"
    assert body["language"] == "java"
    assert body["adapter_type"] == "task"
    assert body["run_mode"] == "manual"
    assert body["timeout_seconds"] == DEFAULT_EXECUTION_TIMEOUT_SECONDS
    assert body["runtime_worker_id"] is None
    assert body["description"] == detail["summary"]["zh-CN"]
    assert body["template_scenario_slug"] is None
    assert body["template_version"] is None
    assert body["latest_version_id"] is None

    with session_factory() as session:
        assert (
            session.scalar(select(AdapterVersion).where(AdapterVersion.adapter_id == body["id"]))
            is None
        )
        assert (
            session.scalar(
                select(AdapterExecutionSlot).where(AdapterExecutionSlot.adapter_id == body["id"])
            )
            is not None
        )
        input_config = session.get(AdapterInputConfig, body["id"])
        assert input_config is not None
        assert input_config.source_type == "none"
        assert (
            session.scalar(select(AdapterWebhook).where(AdapterWebhook.adapter_id == body["id"]))
            is None
        )


def test_webhook_instantiate_uses_fresh_disabled_unbound_public_ids(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    first = _instantiate(api_client, WEBHOOK_SCENARIO, name="hook-copy-one").json()
    second = _instantiate(api_client, WEBHOOK_SCENARIO, name="hook-copy-two").json()

    with session_factory() as session:
        hooks = list(
            session.scalars(
                select(AdapterWebhook)
                .where(AdapterWebhook.adapter_id.in_([first["id"], second["id"]]))
                .order_by(AdapterWebhook.adapter_id)
            ).all()
        )
        assert len(hooks) == 2
        assert hooks[0].public_id != hooks[1].public_id
        assert all(not hook.enabled for hook in hooks)
        assert all(hook.credential_id is None for hook in hooks)
        assert all(len(hook.public_id) == 16 for hook in hooks)
        assert all(session.get(AdapterInputConfig, item["id"]) is None for item in (first, second))


def test_instantiate_never_inherits_demo_credentials_or_runtime_objects(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        bootstrap_demo_credentials(session)
        before = _table_counts(session, FORBIDDEN_TABLES)

    created = _instantiate(api_client, TASK_SCENARIO, name="clean-template-copy")
    assert created.status_code == 201, created.text

    with session_factory() as session:
        after = _table_counts(session, FORBIDDEN_TABLES)
        assert after == before
        adapter_id = created.json()["id"]
        assert (
            session.scalar(
                select(AdapterPermission).where(AdapterPermission.adapter_id == adapter_id)
            )
            is None
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "missing-version"},
        {"name": "blank-version", "expected_template_version": ""},
        {"name": "whitespace-version", "expected_template_version": "   "},
        {"name": "", "expected_template_version": "1.0.0"},
        {"name": "   ", "expected_template_version": "1.0.0"},
        {"name": "x" * 129, "expected_template_version": "1.0.0"},
        {
            "name": "forged-copy",
            "expected_template_version": "1.0.0",
            "code": "malicious",
        },
        {
            "name": "forged-owner",
            "expected_template_version": "1.0.0",
            "owner_user_id": 123,
        },
    ],
)
def test_instantiate_request_is_strict_and_invalid_payload_writes_nothing(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    payload: dict[str, object],
) -> None:
    with session_factory() as session:
        before = int(session.scalar(select(text("count(*)")).select_from(Adapter)) or 0)
    response = api_client.post(
        f"/api/templates/scenarios/{TASK_SCENARIO}/variants/python/instantiate",
        json=payload,
    )
    assert response.status_code == 422
    with session_factory() as session:
        after = int(session.scalar(select(text("count(*)")).select_from(Adapter)) or 0)
    assert after == before


def test_unknown_scenario_language_version_and_name_conflict_are_stable_and_atomic(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    selected = _variant(api_client, TASK_SCENARIO)
    cases = [
        (
            "/api/templates/scenarios/missing/variants/python/instantiate",
            {"name": "unknown-scenario", "expected_template_version": "1.0.0"},
            404,
            "template_scenario_not_found",
        ),
        (
            f"/api/templates/scenarios/{TASK_SCENARIO}/variants/ruby/instantiate",
            {"name": "unknown-language", "expected_template_version": selected["template_version"]},
            404,
            "template_variant_not_found",
        ),
        (
            f"/api/templates/scenarios/{TASK_SCENARIO}/variants/python/instantiate",
            {"name": "stale-version", "expected_template_version": "0.0.0"},
            409,
            "template_version_conflict",
        ),
    ]
    for path, payload, status_code, code in cases:
        response = api_client.post(path, json=payload)
        assert response.status_code == status_code, response.text
        assert response.json()["detail"]["code"] == code

    first = _instantiate(api_client, TASK_SCENARIO, name="duplicate-template-name")
    assert first.status_code == 201
    duplicate = _instantiate(api_client, TASK_SCENARIO, name="duplicate-template-name")
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "adapter_name_conflict"
    assert "duplicate-template-name" not in duplicate.text

    with session_factory() as session:
        names = list(session.scalars(select(Adapter.name)).all())
        assert names == ["duplicate-template-name"]


@pytest.mark.parametrize("role", ["user", "admin"])
def test_account_user_and_admin_own_instantiated_adapter_and_require_csrf(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    role: str,
) -> None:
    client, user_id, csrf_token = _new_account_client(
        api_client.app,
        session_factory,
        username=f"template-{role}",
        role=role,
    )
    selected = client.get(
        f"{ACCOUNT_PREFIX}/api/templates/scenarios/{TASK_SCENARIO}/variants/python"
    )
    assert selected.status_code == 200, selected.text
    path = f"{ACCOUNT_PREFIX}/api/templates/scenarios/{TASK_SCENARIO}/variants/python/instantiate"
    payload = {
        "name": f"{role}-owned-copy",
        "expected_template_version": selected.json()["template_version"],
    }
    missing_csrf = client.post(path, json=payload)
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["detail"]["code"] == "account_csrf_invalid"

    created = client.post(path, json=payload, headers={"X-CSRF-Token": csrf_token})
    assert created.status_code == 201, created.text
    assert created.json()["owner_user_id"] == user_id
    assert created.json()["access_level"] == ("owner" if role == "user" else "admin")
    with session_factory() as session:
        assert (
            session.scalar(
                select(AdapterPermission).where(
                    AdapterPermission.adapter_id == created.json()["id"]
                )
            )
            is None
        )


def test_superadmin_copy_is_system_owned(api_client: TestClient) -> None:
    created = _instantiate(api_client, TASK_SCENARIO, name="system-owned-copy")
    assert created.status_code == 201, created.text
    assert created.json()["owner_user_id"] is None
    assert created.json()["access_level"] == "admin"


def test_copied_adapter_has_no_template_association_and_rejects_forged_origin(
    api_client: TestClient,
) -> None:
    created = _instantiate(api_client, TASK_SCENARIO, name="immutable-template-origin")
    assert created.status_code == 201, created.text
    original = created.json()
    assert original["template_scenario_slug"] is None
    assert original["template_version"] is None

    updated = api_client.patch(
        f"/api/adapters/{original['id']}",
        json={"description": "User-owned description"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["template_scenario_slug"] == original["template_scenario_slug"]
    assert updated.json()["template_version"] == original["template_version"]

    forged = api_client.patch(
        f"/api/adapters/{original['id']}",
        json={"template_scenario_slug": "csv-to-json", "template_version": "9.9.9"},
    )
    assert forged.status_code == 422
    unchanged = api_client.get(f"/api/adapters/{original['id']}")
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["template_scenario_slug"] == original["template_scenario_slug"]
    assert unchanged.json()["template_version"] == original["template_version"]


@pytest.mark.parametrize(
    ("helper_name", "scenario_slug"),
    [
        ("_add_template_slot", TASK_SCENARIO),
        ("_add_template_type_configuration", TASK_SCENARIO),
        ("_add_template_type_configuration", WEBHOOK_SCENARIO),
    ],
)
def test_every_intermediate_failure_rolls_back_and_name_can_retry(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
    scenario_slug: str,
) -> None:
    catalog = template_service.get_template_catalog()
    scenario = catalog.get_scenario(scenario_slug)
    assert scenario is not None
    payload = TemplateInstantiateRequest(
        name=f"rollback-{helper_name}",
        expected_template_version=scenario.version,
    )
    original = getattr(template_service, helper_name)
    with session_factory() as session:
        before = _table_counts(session, CREATED_GRAPH_TABLES)

    def fail_after_write(*args: object, **kwargs: object) -> None:
        original(*args, **kwargs)
        raise RuntimeError("injected template transaction failure")

    monkeypatch.setattr(template_service, helper_name, fail_after_write)
    with (
        session_factory() as session,
        pytest.raises(RuntimeError, match="injected template transaction failure"),
    ):
        template_service.instantiate_template_adapter(
            session,
            scenario_slug=scenario_slug,
            language="python",
            payload=payload,
            owner_user_id=None,
            catalog=catalog,
        )
    with session_factory() as session:
        assert _table_counts(session, CREATED_GRAPH_TABLES) == before
        assert session.scalar(select(Adapter).where(Adapter.name == payload.name)) is None

    monkeypatch.setattr(template_service, helper_name, original)
    with session_factory() as session:
        retried = template_service.instantiate_template_adapter(
            session,
            scenario_slug=scenario_slug,
            language="python",
            payload=payload,
            owner_user_id=None,
            catalog=catalog,
        )
        assert retried.name == payload.name


def test_non_name_integrity_error_is_not_disguised_as_name_conflict(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = template_service.get_template_catalog()
    scenario = catalog.get_scenario(TASK_SCENARIO)
    assert scenario is not None
    payload = TemplateInstantiateRequest(
        name="real-integrity-failure",
        expected_template_version=scenario.version,
    )

    def invalid_slot(session: Session, adapter_id: int) -> None:
        session.add(AdapterExecutionSlot(adapter_id=adapter_id, slot_no=-1))
        session.flush()

    monkeypatch.setattr(template_service, "_add_template_slot", invalid_slot)
    with session_factory() as session, pytest.raises(IntegrityError) as caught:
        template_service.instantiate_template_adapter(
            session,
            scenario_slug=TASK_SCENARIO,
            language="python",
            payload=payload,
            owner_user_id=None,
            catalog=catalog,
        )
    assert getattr(caught.value.orig.diag, "constraint_name", None) == (
        "ck_adapter_execution_slots_slot_no"
    )
    with session_factory() as session:
        assert session.scalar(select(Adapter).where(Adapter.name == payload.name)) is None


def test_instantiate_api_does_not_mask_a_non_name_integrity_error(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = _variant(api_client, TASK_SCENARIO)

    def invalid_slot(session: Session, adapter_id: int) -> None:
        session.add(AdapterExecutionSlot(adapter_id=adapter_id, slot_no=-1))
        session.flush()

    monkeypatch.setattr(template_service, "_add_template_slot", invalid_slot)
    with pytest.raises(IntegrityError) as caught:
        api_client.post(
            f"/api/templates/scenarios/{TASK_SCENARIO}/variants/python/instantiate",
            json={
                "name": "api-integrity-failure",
                "expected_template_version": selected["template_version"],
            },
        )
    assert getattr(caught.value.orig.diag, "constraint_name", None) == (
        "ck_adapter_execution_slots_slot_no"
    )
    with session_factory() as session:
        assert (
            session.scalar(select(Adapter).where(Adapter.name == "api-integrity-failure")) is None
        )


def test_concurrent_same_name_creates_one_complete_object_graph_without_sleep(
    session_factory: sessionmaker[Session],
) -> None:
    catalog = template_service.get_template_catalog()
    scenario = catalog.get_scenario(TASK_SCENARIO)
    assert scenario is not None
    payload = TemplateInstantiateRequest(
        name="concurrent-template-copy",
        expected_template_version=scenario.version,
    )
    barrier = Barrier(2, timeout=15)

    def instantiate() -> tuple[int, int | str]:
        with session_factory() as session:
            reached_insert = False

            def synchronize_first_flush(
                target: Session, flush_context: object, instances: object
            ) -> None:
                nonlocal reached_insert
                if not reached_insert and any(isinstance(item, Adapter) for item in target.new):
                    reached_insert = True
                    barrier.wait()

            event.listen(session, "before_flush", synchronize_first_flush)
            try:
                adapter = template_service.instantiate_template_adapter(
                    session,
                    scenario_slug=TASK_SCENARIO,
                    language="python",
                    payload=payload,
                    owner_user_id=None,
                    catalog=catalog,
                )
                return 201, adapter.id
            except HTTPException as exc:
                return exc.status_code, str(exc.detail["code"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result() for future in [pool.submit(instantiate) for _ in range(2)]]

    assert sorted(status_code for status_code, _ in outcomes) == [201, 409]
    assert [value for status_code, value in outcomes if status_code == 409] == [
        "adapter_name_conflict"
    ]
    with session_factory() as session:
        adapter = session.scalar(select(Adapter).where(Adapter.name == payload.name))
        assert adapter is not None
        assert (
            session.scalar(
                select(text("count(*)"))
                .select_from(AdapterVersion)
                .where(AdapterVersion.adapter_id == adapter.id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(text("count(*)"))
                .select_from(AdapterExecutionSlot)
                .where(AdapterExecutionSlot.adapter_id == adapter.id)
            )
            == 1
        )
        assert session.get(AdapterInputConfig, adapter.id) is not None


def test_ordinary_create_clone_and_first_save_rules_remain_unchanged(
    api_client: TestClient,
) -> None:
    forged_create = api_client.post(
        "/api/adapters",
        json={
            "name": "forged-origin-create",
            "language": "python",
            "adapter_type": "task",
            "template_scenario_slug": "csv-to-json",
            "template_version": "9.9.9",
        },
    )
    assert forged_create.status_code == 422
    ordinary = api_client.post(
        "/api/adapters",
        json={"name": "ordinary", "language": "python", "adapter_type": "task"},
    )
    assert ordinary.status_code == 201, ordinary.text
    assert ordinary.json()["template_scenario_slug"] is None
    assert ordinary.json()["template_version"] is None
    forged_save = api_client.post(
        f"/api/adapters/{ordinary.json()['id']}/versions",
        json={
            "code": "def handle(context, input):\n    return input\n",
            "template_scenario_slug": "csv-to-json",
            "template_version": "9.9.9",
        },
    )
    assert forged_save.status_code == 422
    save_without_worker = api_client.post(
        f"/api/adapters/{ordinary.json()['id']}/versions",
        json={"code": "def handle(context, input):\n    return input\n"},
    )
    assert save_without_worker.status_code == 409
    assert save_without_worker.json()["detail"]["code"] in {
        "worker_offline",
        "runtime_worker_required",
    }

    copied = _instantiate(api_client, TASK_SCENARIO, name="template-source-for-clone")
    assert copied.status_code == 201, copied.text
    forged_clone = api_client.post(
        f"/api/adapters/{copied.json()['id']}/clone",
        json={
            "name": "forged-origin-clone",
            "template_scenario_slug": "csv-to-json",
            "template_version": "9.9.9",
        },
    )
    assert forged_clone.status_code == 422
    clone = api_client.post(
        f"/api/adapters/{copied.json()['id']}/clone",
        json={"name": "ordinary-clone"},
    )
    assert clone.status_code == 201, clone.text
    assert clone.json()["template_scenario_slug"] is None
    assert clone.json()["template_version"] is None


def test_unauthenticated_instantiate_is_rejected_without_writes(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
) -> None:
    anonymous = TestClient(api_client.app)
    with session_factory() as session:
        before = session.scalar(select(text("count(*)")).select_from(Adapter))
    response = anonymous.post(
        f"/api/templates/scenarios/{TASK_SCENARIO}/variants/python/instantiate",
        json={"name": "anonymous-copy", "expected_template_version": "1.0.0"},
    )
    assert response.status_code == 401
    with session_factory() as session:
        after = session.scalar(select(text("count(*)")).select_from(Adapter))
    assert after == before
