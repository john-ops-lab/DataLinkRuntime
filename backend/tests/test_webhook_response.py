"""Issue #137: real PostgreSQL acceptance, response waiting and replay contracts."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from dlr.control.api import webhooks
from dlr.control.models import Execution
from runtime_api_support import claim_execution, report_attempt
from test_webhook_trigger import WEBHOOK_TOKEN, setup_webhook


def configure(client, name="response", **policy):
    adapter, version, worker, credential, hook = setup_webhook(client, name, enabled=False)
    response = client.put(
        f"/api/adapters/{adapter['id']}/webhook",
        json={
            "enabled": True,
            "public_id": hook["public_id"],
            "credential_id": credential["id"],
            "response_mode": "completed",
            "response_timeout_seconds": 2,
            **policy,
        },
    )
    assert response.status_code == 200, response.text
    return adapter, worker, response.json()


def call(client, hook, key="receipt", token=WEBHOOK_TOKEN):
    return client.post(
        hook["hook_path"],
        json={"ticket": 1},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": key,
        },
    )


def wait_execution(factory):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        with factory() as session:
            execution = session.scalar(select(Execution))
            if execution:
                return execution.id
        time.sleep(0.02)
    pytest.fail("No committed receipt while HTTP request was waiting")


def test_policy_defaults_validation_lock_and_clone(api_client):
    adapter, _, _, credential, hook = setup_webhook(api_client, "policy", enabled=False)
    assert hook["response_mode"] == "accepted"
    assert hook["response_timeout_seconds"] == 30
    url = f"/api/adapters/{adapter['id']}/webhook"
    payload = {"enabled": False, "public_id": hook["public_id"], "credential_id": credential["id"]}
    for field, value in [
        ("response_mode", "sync"),
        ("response_mode", None),
        ("response_timeout_seconds", 0),
        ("response_timeout_seconds", 301),
        ("response_timeout_seconds", 1.5),
        ("response_timeout_seconds", True),
    ]:
        assert api_client.put(url, json=payload | {field: value}).status_code == 422
    configured = api_client.put(
        url, json=payload | {"response_mode": "completed", "response_timeout_seconds": 5}
    )
    assert configured.status_code == 200
    clone = api_client.post(f"/api/adapters/{adapter['id']}/clone", json={"name": "policy-copy"})
    assert clone.status_code == 201, clone.text
    cloned = api_client.get(f"/api/adapters/{clone.json()['id']}/webhook").json()
    assert (cloned["response_mode"], cloned["response_timeout_seconds"], cloned["enabled"]) == (
        "completed",
        5,
        False,
    )
    # An old client omitting the new fields may still Start/Stop without resetting policy.
    started = api_client.put(url, json=payload | {"enabled": True})
    assert started.json()["response_mode"] == "completed"
    assert (
        api_client.put(
            url, json=payload | {"enabled": True, "response_mode": "accepted"}
        ).status_code
        == 409
    )
    assert api_client.put(url, json=payload | {"response_timeout_seconds": 6}).status_code == 409
    stopped = api_client.put(url, json=payload)
    assert stopped.status_code == 200
    assert stopped.json()["response_timeout_seconds"] == 5


@pytest.mark.parametrize(
    "output",
    [
        {"ok": True, "resource_id": "host-1"},
        {"ok": False, "errors": [{"field": "resource_id", "message": "资源唯一标识必填"}]},
        [1, False],
        None,
    ],
)
def test_completed_returns_actual_worker_result(api_client, session_factory, output):
    _, worker, hook = configure(api_client)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(call, api_client, hook)
        execution_id = wait_execution(session_factory)
        assert not future.done()
        assert api_client.get("/api/health").status_code == 200
        claimed = claim_execution(api_client, worker["id"], execution_id=execution_id)
        assert claimed.status_code == 200, claimed.text
        report = report_attempt(
            api_client,
            worker["id"],
            execution_id,
            {
                "status": "succeeded",
                "output": output,
                "workspace_cleanup_status": "completed",
            },
        )
        assert report.status_code == 200, report.text
        response = future.result(timeout=4)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "execution_id": execution_id,
        "status": "succeeded",
        "output": output,
    }
    replay = call(api_client, hook)
    assert replay.json() == response.json()
    assert call(api_client, hook, token="wrong").status_code == 401
    mismatch = api_client.post(
        hook["hook_path"],
        json={"ticket": 2},
        headers={
            "Authorization": f"Bearer {WEBHOOK_TOKEN}",
            "Idempotency-Key": "receipt",
        },
    )
    assert mismatch.status_code == 409
    with session_factory() as session:
        assert len(list(session.scalars(select(Execution)))) == 1


def test_wait_timeout_does_not_cancel_and_replay_uses_snapshot(api_client, session_factory):
    adapter, worker, hook = configure(api_client, response_timeout_seconds=1)
    response = call(api_client, hook)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "webhook_response_timeout"
    execution_id = response.json()["execution_id"]
    with session_factory() as session:
        execution = session.get(Execution, execution_id)
        assert execution.status == "queued"
        assert execution.cancel_requested is False
    assert claim_execution(api_client, worker["id"], execution_id=execution_id).status_code == 200
    assert (
        report_attempt(
            api_client,
            worker["id"],
            execution_id,
            {
                "status": "succeeded",
                "output": {"done": True},
                "workspace_cleanup_status": "completed",
            },
        ).status_code
        == 200
    )
    url = f"/api/adapters/{adapter['id']}/webhook"
    payload = {"public_id": hook["public_id"], "credential_id": hook["credential_id"]}
    assert api_client.put(url, json=payload | {"enabled": False}).status_code == 200
    assert (
        api_client.put(
            url, json=payload | {"enabled": True, "response_mode": "accepted"}
        ).status_code
        == 200
    )
    replay = call(api_client, hook)
    assert replay.status_code == 200
    assert replay.json()["execution_id"] == execution_id
    assert call(api_client, hook, key="new-call").status_code == 202


@pytest.mark.parametrize(
    "status,code,http_status",
    [
        ("dead_letter", "adapter_error", 422),
        ("dead_letter", "execution_timeout", 504),
        ("cancelled", "execution_cancelled", 409),
        ("expired", "execution_expired", 504),
        ("succeeded", "output_too_large", 502),
    ],
)
def test_terminal_response_mapping(api_client, session_factory, status, code, http_status):
    _, _, hook = configure(api_client)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(call, api_client, hook)
        execution_id = wait_execution(session_factory)
        with session_factory() as session:
            session.execute(
                update(Execution)
                .where(Execution.id == execution_id)
                .values(
                    status=status,
                    error="validation detail",
                    error_code=code,
                    output_truncated=code == "output_too_large",
                )
            )
            session.commit()
        response = future.result(timeout=4)
    assert response.status_code == http_status
    assert response.json()["error"]["code"] == code
    assert "stdout" not in response.json()


def test_retry_wait_is_not_completion(api_client, session_factory):
    _, _, hook = configure(api_client, response_timeout_seconds=1)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(call, api_client, hook)
        execution_id = wait_execution(session_factory)
        with session_factory() as session:
            session.execute(
                update(Execution)
                .where(Execution.id == execution_id)
                .values(
                    status="retry_wait",
                    error="first attempt failed",
                    error_code="adapter_error",
                )
            )
            session.commit()
        response = future.result(timeout=4)
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "webhook_response_timeout"


def test_disconnect_stops_polling_without_mutation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        webhooks, "_read_hook_result_sync", lambda execution_id: calls.append(execution_id)
    )

    async def disconnected():
        return True

    response = asyncio.run(
        webhooks._wait_hook_result(42, 30, SimpleNamespace(is_disconnected=disconnected))
    )
    assert response.status_code == 499
    assert calls == [42]
