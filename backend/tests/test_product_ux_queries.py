"""Issue 212 read-only UX helpers preserve scheduling and credential boundaries."""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from dlr.control.models import Adapter, AdapterSchedule, AdapterWebhook
from dlr.control.models.knowledge_source import KnowledgeSourceSetting
from dlr.control.models.platform import AiCustomProvider, AiModelSetting, Credential, PackageSource
from dlr.control.security import Principal, require_principal
from dlr.control.services import schedule
from test_adapters import create_adapter
from test_credentials import create_credential, set_bindings


def test_preview_uses_scheduler_and_never_persists(
    api_client: TestClient, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    fixed = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)

    class Clock:
        @staticmethod
        def now(tz: object) -> datetime:
            return fixed

    monkeypatch.setattr("dlr.control.api.schedules.datetime", Clock)
    for timezone, cron in [("Asia/Shanghai", "0 9 * * *"), ("America/New_York", "30 1 * * *")]:
        response = api_client.post(
            "/api/schedules/preview", json={"cron": cron, "timezone": timezone, "count": 3}
        )
        assert response.status_code == 200, response.text
        actual = [datetime.fromisoformat(s) for s in response.json()["occurrences"]]
        cursor = fixed
        for item in actual:
            assert item == schedule.next_run_after(cron, timezone, cursor)
            assert item > cursor
            cursor = item
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AdapterSchedule)) == 0
        assert session.scalar(select(func.count()).select_from(Adapter)) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"cron": "* * * * * *"},
        {"cron": "0 0 31 2 *"},
        {"timezone": "invalid/timezone"},
        {"count": 0},
        {"count": 11},
        {"count": True},
    ],
)
def test_preview_invalid_input(api_client: TestClient, change: dict) -> None:
    response = api_client.post(
        "/api/schedules/preview", json={"cron": "0 9 * * *", "timezone": "UTC", **change}
    )
    assert response.status_code == 422


def test_usage_reports_current_references_without_secrets(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    credential = create_credential(
        api_client, "shared-token", "token", {"token": "secret-never-return"}
    )
    cid = credential["id"]
    adapter = create_adapter(api_client, "current-task")
    retired = create_adapter(api_client, "retired-task")
    webhook = create_adapter(api_client, "incoming-events", adapter_type="webhook")
    for item in (adapter, retired):
        response = set_bindings(
            api_client,
            item["id"],
            [{"env_key": "API_TOKEN", "credential_id": cid, "field": "token"}],
        )
        assert response.status_code == 200
    with session_factory() as session:
        row = session.get(Adapter, retired["id"])
        assert row is not None
        row.archived_at = datetime.now(UTC)
        webhook_row = session.scalar(
            select(AdapterWebhook).where(AdapterWebhook.adapter_id == webhook["id"])
        )
        assert webhook_row is not None
        webhook_row.credential_id = cid
        session.add_all(
            [
                AiModelSetting(
                    id=1,
                    provider="minimax",
                    base_url="https://private-url.invalid",
                    model="model-name",
                    credential_id=cid,
                ),
                AiCustomProvider(
                    name="custom-service",
                    protocol="openai_compatible",
                    base_url="https://private-url.invalid",
                    credential_id=cid,
                ),
                KnowledgeSourceSetting(id=1, source_id="ima", enabled=False, credential_id=cid),
                PackageSource(
                    name="private-repository",
                    kind="pypi",
                    index_url="https://private-url.invalid",
                    credential_id=cid,
                ),
            ]
        )
        session.commit()
        secret = session.get(Credential, cid)
        assert secret is not None
        ciphertext = secret.ciphertext
    response = api_client.get(f"/api/credentials/{cid}/usage")
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert {x["kind"] for x in items} == {
        "adapter_binding",
        "webhook",
        "ai_model",
        "ai_custom_provider",
        "knowledge_source",
        "package_source",
    }
    assert {x["name"] for x in items} == {
        "current-task",
        "incoming-events",
        "model-name",
        "custom-service",
        "ima",
        "private-repository",
    }
    assert "secret-never-return" not in response.text
    assert "private-url.invalid" not in response.text
    assert ciphertext not in response.text
    with session_factory() as session:
        unchanged = session.get(Credential, cid)
        assert unchanged is not None
        assert unchanged.ciphertext == ciphertext
    assert api_client.get("/api/credentials/999999/usage").status_code == 404


def test_usage_requires_admin_and_preview_requires_auth(api_client: TestClient) -> None:
    assert (
        api_client.get(
            "/api/credentials/1/usage", headers={"Authorization": "Bearer bad"}
        ).status_code
        == 401
    )
    assert (
        api_client.post(
            "/api/schedules/preview",
            json={"cron": "* * * * *", "timezone": "UTC"},
            headers={"Authorization": "Bearer bad"},
        ).status_code
        == 401
    )
    api_client.app.dependency_overrides[require_principal] = lambda: Principal(
        kind="account", role="user", user_id=999
    )
    try:
        assert api_client.get("/api/credentials/1/usage").status_code == 403
        assert (
            api_client.post(
                "/api/schedules/preview", json={"cron": "* * * * *", "timezone": "UTC"}
            ).status_code
            == 200
        )
    finally:
        api_client.app.dependency_overrides.pop(require_principal, None)
