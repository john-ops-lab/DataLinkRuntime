"""Identical synthetic extensions on both runtimes; no product tools added."""

import json
from typing import Any

import pytest

from ai_runtime_support import response, server
from dlr.control.ai import tools
from test_ai import assist_body, configure, create_adapter


def test_same_read_only_tool_extension_needs_no_runtime_or_codec_branch(
    api_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched: list[int] = []

    def read(args: dict[str, Any]) -> dict[str, Any]:
        launched.append(args["value"])
        return {"value": args["value"], "read_only": True}

    spec = tools.ToolSpec(
        "dlr_synthetic_read",
        "Synthetic read only extension",
        {
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        read,
    )
    monkeypatch.setitem(tools._TOOLS, spec.name, spec)
    with server(
        response("openai", calls=[(spec.name, "new-tool", '{"value":42}')]), response("openai")
    ) as script:
        adapter = create_adapter(api_client, "same-tool-extension")
        configure(api_client, base_url=script.url)
        result = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
        assert result.status_code == 200, result.text
        assert launched == [42]
        assert [(entry["tool_name"], entry["status"]) for entry in result.json()["tool_calls"]] == [
            (spec.name, "success")
        ]
        assert len(script.requests) == 2
        offered = script.requests[0].json()["tools"]
        assert [entry for entry in offered if entry["function"]["name"] == spec.name][0][
            "function"
        ]["parameters"] == spec.parameters
        returned = next(
            item for item in script.requests[1].json()["messages"] if item["role"] == "tool"
        )
        assert json.loads(returned["content"]) == {"value": 42, "read_only": True}
        assert offered == tools.tools_payload(include_knowledge=False)


def test_same_existing_protocol_model_extension_needs_only_configuration(
    api_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    with server(response("openai")) as script:
        adapter = create_adapter(api_client, "same-model-extension")
        configure(api_client, base_url=script.url + "/gateway/v1", model="synthetic-profile-v2")
        result = api_client.post(f"/api/adapters/{adapter['id']}/ai/assist", json=assist_body())
        assert result.status_code == 200, result.text
        assert result.json()["model"] == "synthetic-profile-v2"
        assert len(script.requests) == 1
        assert script.requests[0].path == "/gateway/v1/chat/completions"
        assert script.requests[0].json()["model"] == "synthetic-profile-v2"
