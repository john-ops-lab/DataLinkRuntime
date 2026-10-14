"""Protocol success and code task completion are independent."""

import pytest
from fastapi.testclient import TestClient

from dlr.control.ai import providers
from test_ai import assist_body, configure, create_adapter, fake_chat_response, valid_output


@pytest.mark.parametrize(
    "explicit,declared,candidate,status",
    [
        ("auto", None, False, "unclassified"),
        ("auto", "consultation", False, "answered"),
        ("auto", "code_change", False, "incomplete"),
        ("auto", None, True, "candidate_ready"),
        ("code_change", "consultation", False, "incomplete"),
        ("consultation", "code_change", False, "answered"),
        ("code_change", None, True, "candidate_ready"),
    ],
)
def test_task_status_is_explicit_and_legacy_compatible(
    api_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    explicit: str,
    declared: str | None,
    candidate: bool,
    status: str,
) -> None:
    adapter = create_adapter(api_client, "issue203-status")
    configure(api_client)
    output = valid_output()
    if not candidate:
        output["candidate"] = None
    if declared is not None:
        output["request_intent"] = declared
    monkeypatch.setattr(providers, "_request_json", lambda *a, **k: fake_chat_response(output))
    body = assist_body()
    body["task_intent"] = explicit
    result = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=body)
    assert result.status_code == 200
    assert result.json()["task_status"] == status
    assert bool(result.json()["candidate"]) is candidate
