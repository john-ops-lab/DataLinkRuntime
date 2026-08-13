"""M5.3 tests: Webhook Trigger (async 202 + token Credential).

Covers the singleton Webhook configuration API contract, the external
ingress success chain (Bearer token -> unified production gate -> pending
Execution -> 202), every stable rejection (401/404/409/413/400), the
Execution input contract (raw stream cap plus compact JSON cap, standard
JSON only), the real validation precedence, the production-version
pinning against later Publishes, the busy-no-queue contract and the
security requirements (token never in responses or logs, Credential
deletion blocked while referenced).
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, sessionmaker

from conftest import ADMIN_TOKEN
from dlr.common.config import settings
from dlr.control.models import Credential, Execution, Worker
from test_adapters import create_adapter, save_version
from test_credentials import create_credential
from test_production_lifecycle import setup_publishable, start, stop
from test_workers import claim, report

WEBHOOK_TOKEN = "whk-secret-token-value"
NO_AUTH_HEADER = {"Authorization": ""}

_ABSENT = object()


# --- helpers ---------------------------------------------------------------------


def put_webhook(client: TestClient, adapter_id: int, credential_id: int, enabled: bool = True):
    return client.put(
        f"/api/adapters/{adapter_id}/webhook",
        json={"enabled": enabled, "credential_id": credential_id},
    )


def get_webhook(client: TestClient, adapter_id: int):
    return client.get(f"/api/adapters/{adapter_id}/webhook")


def post_hook(
    client: TestClient,
    public_id: str,
    token: str | None = None,
    json_body: object = _ABSENT,
    content: bytes | None = None,
):
    """One external Webhook call. The default admin token is always replaced
    (empty header = absent) so the ingress is exercised exactly like an
    outside system would call it: with its own Bearer token only. The body
    is serialized explicitly so JSON null stays distinguishable from "no
    body"."""
    headers = {"Authorization": f"Bearer {token}" if token is not None else ""}
    data = content if content is not None else json.dumps(json_body).encode()
    return client.post(f"/api/hooks/{public_id}", content=data, headers=headers)


def running_with_webhook(
    client: TestClient, name: str, token_value: str = WEBHOOK_TOKEN
) -> tuple[dict, dict, dict, dict]:
    """Published + started Adapter with an enabled Webhook.

    Returns (adapter, version, worker, webhook) where webhook is the PUT
    response body carrying the public_id.
    """
    adapter, version, worker = setup_publishable(client, name=name, worker_name=f"{name}-worker")
    credential = create_credential(
        client, name=f"{name}-webhook-token", type_="token", fields={"token": token_value}
    )
    response = put_webhook(client, adapter["id"], credential["id"])
    assert response.status_code == 200, response.text
    assert start(client, adapter["id"]).status_code == 200
    return adapter, version, worker, response.json()


def executions_of(session_factory: sessionmaker[Session], adapter_id: int) -> list[Execution]:
    with session_factory() as session:
        return list(
            session.scalars(
                select(Execution).where(Execution.adapter_id == adapter_id).order_by(Execution.id)
            ).all()
        )


def webhook_executions_of(
    session_factory: sessionmaker[Session], adapter_id: int
) -> list[Execution]:
    """Only webhook-triggered rows: the publish-gate test run never counts."""
    return [row for row in executions_of(session_factory, adapter_id) if row.trigger == "webhook"]


def finish_active_execution(client: TestClient, worker_id: int, execution_id: int) -> None:
    """Claim and finish one pending Execution so the production slot frees."""
    claimed = claim(client, worker_id)
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["execution_id"] == execution_id
    finished = report(client, worker_id, execution_id, {"status": "succeeded"})
    assert finished.status_code == 200, finished.text


def insert_active_execution(
    session_factory: sessionmaker[Session],
    adapter_id: int,
    version_id: int,
    trigger: str,
) -> int:
    """An active production-class Execution inserted directly (busy state)."""
    with session_factory() as session:
        execution = Execution(
            adapter_id=adapter_id,
            version_id=version_id,
            trigger=trigger,
            status="pending",
            input={},
        )
        session.add(execution)
        session.commit()
        return execution.id


def make_worker_stale(session_factory: sessionmaker[Session], worker_id: int) -> None:
    """Push the heartbeat far outside the effective-online window."""
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE workers SET last_heartbeat = now() - interval '1 hour', "
                "status = 'online' WHERE id = :worker_id"
            ),
            {"worker_id": worker_id},
        )
        session.commit()


def set_worker_capabilities(
    session_factory: sessionmaker[Session], worker_id: int, capabilities: list[str]
) -> None:
    with session_factory() as session:
        session.execute(
            update(Worker).where(Worker.id == worker_id).values(capabilities=capabilities)
        )
        session.commit()


# --- configuration API -------------------------------------------------------------


def test_get_before_configuration_is_stable_404(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="whk-unconfigured")
    response = get_webhook(api_client, adapter["id"])
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "webhook_not_configured"
    # Unknown Adapter id and not-configured are both stable 404s.
    assert get_webhook(api_client, 999999).json()["detail"]["code"] in (
        "adapter_not_found",
        "webhook_not_configured",
    )


def test_put_generates_unique_stable_public_id(api_client: TestClient) -> None:
    adapter_a = create_adapter(api_client, name="whk-a")
    adapter_b = create_adapter(api_client, name="whk-b")
    credential_a = create_credential(
        api_client, name="whk-a-token", type_="token", fields={"token": "token-a"}
    )
    credential_b = create_credential(
        api_client, name="whk-b-token", type_="token", fields={"token": "token-b"}
    )

    first = put_webhook(api_client, adapter_a["id"], credential_a["id"])
    assert first.status_code == 200
    second = put_webhook(api_client, adapter_b["id"], credential_b["id"])
    assert second.status_code == 200

    public_a, public_b = first.json()["public_id"], second.json()["public_id"]
    # Random, unguessable, never a sequential numeric Adapter id.
    assert len(public_a) >= 32 and len(public_b) >= 32
    assert public_a != public_b
    assert not public_a.isdigit() and not public_b.isdigit()
    # Stable across later PUTs: no URL rotation in M5.3.
    updated = put_webhook(api_client, adapter_a["id"], credential_a["id"], enabled=False)
    assert updated.json()["public_id"] == public_a
    assert updated.json()["enabled"] is False
    # The hook path is derived from the routing id.
    assert first.json()["hook_path"] == f"/api/hooks/{public_a}"


def test_put_requires_token_credential_type(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="whk-type-check")
    password_credential = create_credential(api_client, name="whk-password")
    response = put_webhook(api_client, adapter["id"], password_credential["id"])
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "webhook_credential_type_invalid"

    missing = put_webhook(api_client, adapter["id"], 999999)
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "credential_not_found"


def test_admin_api_never_returns_token_material(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter = create_adapter(api_client, name="whk-no-leak")
    create_credential(
        api_client, name="whk-leak-token", type_="token", fields={"token": WEBHOOK_TOKEN}
    )
    with session_factory() as session:
        credential = session.scalar(select(Credential).where(Credential.name == "whk-leak-token"))
        assert credential is not None
        ciphertext = credential.ciphertext

    put = put_webhook(api_client, adapter["id"], credential.id)
    assert put.status_code == 200
    get = get_webhook(api_client, adapter["id"])
    assert get.status_code == 200
    for body in (put.text, get.text):
        assert WEBHOOK_TOKEN not in body
        assert ciphertext not in body
    # Only display metadata about the Credential.
    assert get.json()["credential_name"] == "whk-leak-token"
    assert get.json()["credential_id"] == credential.id


def test_put_on_archived_adapter_is_rejected(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="whk-archived")
    credential = create_credential(
        api_client, name="whk-archived-token", type_="token", fields={"token": "t"}
    )
    assert api_client.post(f"/api/adapters/{adapter['id']}/archive").status_code == 200
    response = put_webhook(api_client, adapter["id"], credential["id"])
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_archived"


def test_configuration_api_requires_admin_token(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="whk-admin-auth")
    unauthenticated = api_client.get(
        f"/api/adapters/{adapter['id']}/webhook", headers=NO_AUTH_HEADER
    )
    assert unauthenticated.status_code == 401


# --- success chain -----------------------------------------------------------------


def test_success_chain_creates_pending_webhook_execution(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-success")
    body = {"event": "vm.created", "data": {"id": 42}}

    response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, json_body=body)
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "accepted"

    execution_id = payload["execution_id"]
    detail = api_client.get(f"/api/executions/{execution_id}").json()
    assert detail["status"] == "pending"
    assert detail["trigger"] == "webhook"
    assert detail["input"] == body
    # Locked production version and Worker, never the latest published version.
    assert detail["version_id"] == version["id"]
    assert detail["target_worker_id"] == worker["id"]
    rows = executions_of(session_factory, adapter["id"])
    webhook_rows = [row for row in rows if row.trigger == "webhook"]
    assert [row.id for row in webhook_rows] == [execution_id]


def test_execution_runs_on_production_worker_via_claim(api_client: TestClient) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-claim")
    response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, json_body={})
    assert response.status_code == 202
    execution_id = response.json()["execution_id"]

    claimed = claim(api_client, worker["id"])
    assert claimed.status_code == 200
    assert claimed.json()["execution_id"] == execution_id
    assert api_client.get(f"/api/executions/{execution_id}").json()["status"] == "running"


def test_input_json_types_follow_contract(api_client: TestClient) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-input-types")
    for value in ({"k": "v"}, [1, 2, 3], "plain-string", 42, True, None):
        response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, json_body=value)
        assert response.status_code == 202, (value, response.text)
        execution_id = response.json()["execution_id"]
        assert api_client.get(f"/api/executions/{execution_id}").json()["input"] == value
        # Finish it so the next call does not hit production_busy.
        finish_active_execution(api_client, worker["id"], execution_id)


def test_publishing_new_version_keeps_locked_production_version(
    api_client: TestClient,
) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-version-pin")

    # Publish v2 while the entry runs (gate satisfied by a succeeded test).
    v2 = save_version(api_client, adapter["id"])
    test_run = api_client.post(
        f"/api/adapters/{adapter['id']}/executions", json={"version_id": v2["id"]}
    )
    assert test_run.status_code == 202
    finish_active_execution(api_client, worker["id"], test_run.json()["id"])
    assert (
        api_client.post(f"/api/adapters/{adapter['id']}/versions/{v2['id']}/publish").status_code
        == 200
    )

    # The Webhook still executes the locked v1 until Stop -> Start.
    response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, json_body={})
    assert response.status_code == 202
    execution = api_client.get(f"/api/executions/{response.json()['execution_id']}").json()
    assert execution["version_id"] == version["id"]
    assert execution["version_id"] != v2["id"]


# --- rejections ----------------------------------------------------------------------


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_missing_or_wrong_token_is_401_with_zero_executions(
    api_client: TestClient, session_factory: sessionmaker[Session], token: str | None
) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-auth")
    response = post_hook(api_client, webhook["public_id"], token=token, json_body={"x": 1})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "unauthorized"
    assert WEBHOOK_TOKEN not in response.text
    assert webhook_executions_of(session_factory, adapter["id"]) == []


def test_admin_token_does_not_authenticate_the_hook(api_client: TestClient) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-admin-reject")
    response = post_hook(api_client, webhook["public_id"], token=ADMIN_TOKEN, json_body={})
    assert response.status_code == 401


def test_unknown_public_id_is_404(api_client: TestClient) -> None:
    response = post_hook(api_client, "no-such-public-id", token=WEBHOOK_TOKEN, json_body={})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "webhook_not_found"


def test_disabled_webhook_is_rejected_with_zero_executions(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-disabled")
    assert (
        put_webhook(api_client, adapter["id"], webhook["credential_id"], enabled=False).status_code
        == 200
    )
    response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, json_body={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "webhook_disabled"
    assert webhook_executions_of(session_factory, adapter["id"]) == []


def test_production_not_running_is_409(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-stopped")
    assert stop(api_client, adapter["id"]).status_code == 200
    response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, json_body={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "production_not_running"
    assert webhook_executions_of(session_factory, adapter["id"]) == []

    # Idle (published, never started) is rejected the same way.
    idle, idle_version, idle_worker = setup_publishable(api_client, name="whk-idle")
    idle_credential = create_credential(
        api_client, name="whk-idle-token", type_="token", fields={"token": WEBHOOK_TOKEN}
    )
    idle_webhook = put_webhook(api_client, idle["id"], idle_credential["id"]).json()
    idle_response = post_hook(
        api_client, idle_webhook["public_id"], token=WEBHOOK_TOKEN, json_body={}
    )
    assert idle_response.status_code == 409
    assert idle_response.json()["detail"]["code"] == "production_not_running"


def test_offline_worker_is_409(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-offline")
    make_worker_stale(session_factory, worker["id"])
    response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, json_body={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "worker_offline"
    assert webhook_executions_of(session_factory, adapter["id"]) == []


def test_capability_mismatch_is_409(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-capability")
    set_worker_capabilities(session_factory, worker["id"], ["java"])
    response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, json_body={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "worker_capability_missing"
    assert webhook_executions_of(session_factory, adapter["id"]) == []


@pytest.mark.parametrize("trigger", ["production", "schedule", "webhook"])
def test_active_production_execution_is_busy_no_queue(
    api_client: TestClient, session_factory: sessionmaker[Session], trigger: str
) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, f"whk-busy-{trigger}")
    insert_active_execution(session_factory, adapter["id"], version["id"], trigger)
    before = len(webhook_executions_of(session_factory, adapter["id"]))

    response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, json_body={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "production_busy"
    # No queueing, no delayed replay: nothing was persisted.
    assert len(webhook_executions_of(session_factory, adapter["id"])) == before


def test_oversized_body_is_413_with_zero_executions(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-oversized")
    oversized = json.dumps({"blob": "x" * (600 * 1024)}).encode()
    response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, content=oversized)
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "execution_input_too_large"
    assert webhook_executions_of(session_factory, adapter["id"]) == []


def test_invalid_json_is_rejected_with_zero_executions(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-bad-json")
    for content in (b"{not json", b"", b"\xff\xfe\x00binary"):
        response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, content=content)
        assert response.status_code == 400, (content, response.text)
        assert response.json()["detail"]["code"] == "webhook_body_invalid_json"
    assert webhook_executions_of(session_factory, adapter["id"]) == []


def test_non_standard_json_is_rejected_with_zero_executions(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """NaN / Infinity / numeric overflow are non-standard JSON: stable 400
    instead of a JSONB persistence failure, and zero Executions."""
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-nonfinite")
    for content in (b'{"v": NaN}', b'{"v": Infinity}', b'{"v": -Infinity}', b'{"v": 1e309}'):
        response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, content=content)
        assert response.status_code == 400, (content, response.text)
        assert response.json()["detail"]["code"] == "webhook_body_invalid_json"
    assert webhook_executions_of(session_factory, adapter["id"]) == []


def test_jsonb_unpersistable_unicode_is_rejected_with_zero_executions(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """U+0000 and unpaired surrogates parse as JSON but cannot be persisted
    as PostgreSQL JSONB: stable 400 instead of a write-time failure, zero
    Executions. Covers top-level values, nested values and object keys."""
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-jsonb")
    for content in (
        b'"\\u0000"',  # top-level string value: U+0000
        b'"\\ud800"',  # top-level string value: unpaired high surrogate
        b'{"outer": {"inner": "\\udc00"}}',  # nested string value
        b'{"key\\u0000": 1}',  # object key with U+0000
        b'{"\\ud800key": [1]}',  # object key with unpaired surrogate
        b'{"v": "raw\x00nul"}',  # raw NUL byte accepted by json.loads
    ):
        response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, content=content)
        assert response.status_code == 400, (content, response.text)
        assert response.json()["detail"]["code"] == "webhook_body_invalid_json"
    assert webhook_executions_of(session_factory, adapter["id"]) == []

    # Persistable Unicode stays accepted and really lands in JSONB:
    # astral characters (emoji) are single code points, not surrogates.
    accepted = post_hook(
        api_client,
        webhook["public_id"],
        token=WEBHOOK_TOKEN,
        json_body={"emoji": "\U0001f600", "nested": {"中文": ["é"]}},
    )
    assert accepted.status_code == 202, accepted.text
    rows = webhook_executions_of(session_factory, adapter["id"])
    assert len(rows) == 1
    assert rows[0].input == {"emoji": "\U0001f600", "nested": {"中文": ["é"]}}


def test_compact_json_input_cap_is_enforced_after_parsing(
    api_client: TestClient,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw-small body can normalize above the Execution input cap.

    float repr expands on compact serialization: ``1e9`` (3 raw bytes)
    becomes ``1000000000.0`` (12 compact bytes), so the raw stream cap
    alone is not the Execution input contract.
    """
    monkeypatch.setattr(settings, "execution_input_max_bytes", 200)
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-compact-cap")
    raw = b"[" + b",".join([b"1e9"] * 20) + b"]"
    assert len(raw) <= 200  # passes the raw stream cap
    response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, content=raw)
    assert response.status_code == 413, response.text
    assert response.json()["detail"]["code"] == "execution_input_too_large"
    assert webhook_executions_of(session_factory, adapter["id"]) == []


def test_body_cap_precedes_routing_and_auth(
    api_client: TestClient, session_factory: sessionmaker[Session]
) -> None:
    """Real precedence: the ingress reads/caps the body before routing and
    authentication, so oversized calls answer 413 even for unknown ids and
    wrong tokens (the ingress must never read an unbounded body)."""
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-precedence")
    oversized = json.dumps({"blob": "x" * (600 * 1024)}).encode()
    unknown = post_hook(api_client, "unknown-public-id", token=WEBHOOK_TOKEN, content=oversized)
    assert unknown.status_code == 413
    assert unknown.json()["detail"]["code"] == "execution_input_too_large"
    wrong_token = post_hook(api_client, webhook["public_id"], token="wrong", content=oversized)
    assert wrong_token.status_code == 413
    assert wrong_token.json()["detail"]["code"] == "execution_input_too_large"
    assert webhook_executions_of(session_factory, adapter["id"]) == []


def test_archived_adapter_hook_is_rejected(api_client: TestClient) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-archive-gate")
    assert stop(api_client, adapter["id"]).status_code == 200
    assert api_client.post(f"/api/adapters/{adapter['id']}/archive").status_code == 200
    response = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, json_body={})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "adapter_archived"


# --- Credential lifecycle -----------------------------------------------------------


def test_credential_referenced_by_webhook_cannot_be_deleted(api_client: TestClient) -> None:
    adapter = create_adapter(api_client, name="whk-credential-lock")
    credential = create_credential(
        api_client, name="whk-lock-token", type_="token", fields={"token": "locked"}
    )
    assert put_webhook(api_client, adapter["id"], credential["id"]).status_code == 200

    blocked = api_client.delete(f"/api/credentials/{credential['id']}")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "credential_in_use"


# --- logging hygiene ------------------------------------------------------------------


def test_token_never_appears_in_logs(
    api_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    adapter, version, worker, webhook = running_with_webhook(api_client, "whk-log-hygiene")
    with caplog.at_level("INFO"):
        ok = post_hook(api_client, webhook["public_id"], token=WEBHOOK_TOKEN, json_body={"a": 1})
        bad = post_hook(api_client, webhook["public_id"], token="wrong", json_body={})
    assert ok.status_code == 202
    assert bad.status_code == 401
    for record in caplog.records:
        assert WEBHOOK_TOKEN not in record.getMessage()
