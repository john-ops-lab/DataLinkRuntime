"""Wave A prompt resources, snapshots, and Provider message boundary."""

import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict
from pathlib import Path

import pytest

from dlr.control.ai import prompts
from dlr.control.ai.attachments import NativeImage, ParsedText
from dlr.control.ai.prompt_builder import PromptDiagnostics, build_prompt
from dlr.control.ai.prompt_context import PromptContext
from dlr.control.schemas.ai import AiAssistRequest


def _context(
    *, language: str = "python", tools: bool = False, knowledge: bool = False
) -> tuple[PromptContext, AiAssistRequest, dict[str, object]]:
    payload = AiAssistRequest.model_validate(
        {
            "message": "Explain the current code",
            "working_copy": {
                "code": "return input",
                "runtime_config": {"nested": ["original"]},
            },
            "recent_messages": [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ],
            "context_snippets": [
                {"source": "log", "text": "[REDACTED]", "start_line": 1, "end_line": 1}
            ],
        }
    )
    managed: dict[str, object] = {
        "source_type": "managed_files",
        "files": [{"filename": "original.txt", "content_type": "text/plain"}],
    }
    context = PromptContext.capture(
        adapter_id=5,
        language=language,
        system_locale="zh-CN",
        base_version={"id": 7, "seq": 2},
        available_secret_keys=["SECRET_NAME"],
        payload=payload,
        saved_managed_input=managed,
        parsed_attachments=[
            ParsedText("note.txt", "text/plain", "text", "attachment text", False, 15)
        ],
        native_images=[NativeImage("img.png", "image/png", "image", "aGVsbG8=", 5)],
        tools_enabled=tools,
        knowledge_search_enabled=knowledge,
    )
    return context, payload, managed


@pytest.mark.parametrize("language", ["python", "javascript", "typescript", "go", "java"])
@pytest.mark.parametrize("tools,knowledge", [(False, False), (True, False), (True, True)])
def test_builder_message_matrix(language: str, tools: bool, knowledge: bool) -> None:
    context, _, _ = _context(language=language, tools=tools, knowledge=knowledge)
    result = build_prompt(context, runtime_contract=f"contract for {language}")
    assert [message["role"] for message in result.messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    system = result.messages[0]["content"]
    assert isinstance(system, str)
    assert f"Runtime Contract for {language}:\ncontract for {language}" in system
    assert ("dlr_docs_list" in system) is tools
    assert ("list_knowledge_bases" in system) is knowledge
    assert ("tool call," in system) is not tools
    assert '"candidate":null' in result.messages[2]["content"]
    assert result.messages[3]["content"][1]["image_url"]["url"] == (
        "data:image/png;base64,aGVsbG8="
    )
    assert "attachment text" in system
    assert "original.txt" in system
    assert "SECRET_NAME" in system
    assert isinstance(result.diagnostics, PromptDiagnostics)
    assert result.diagnostics.revision == prompts.REVISION
    assert result.diagnostics.system_chars == len(system)
    assert result.diagnostics.context_chars > 0
    assert result.diagnostics.tools_enabled is tools
    assert result.diagnostics.knowledge_enabled is knowledge
    assert result.diagnostics.has_snippets
    assert result.diagnostics.has_attachments
    assert ("tools" in result.diagnostics.included_sections) is tools
    assert ("knowledge" in result.diagnostics.included_sections) is knowledge
    assert "aGVsbG8=" not in json.dumps(asdict(result.diagnostics))


def test_prompt_snapshot_is_deep_and_diagnostics_are_non_sensitive() -> None:
    context, payload, managed = _context()
    first = build_prompt(context, runtime_contract="contract")
    payload.working_copy.runtime_config["nested"].append("changed")
    payload.recent_messages[0].content = "changed"
    payload.context_snippets[0].text = "changed"
    managed["files"][0]["filename"] = "changed.txt"
    second = build_prompt(context, runtime_contract="contract")
    assert first.messages == second.messages
    assert '"nested": ["original"]' in second.messages[0]["content"]
    assert "changed.txt" not in second.messages[0]["content"]
    assert second.messages[1]["content"] == "old question"
    with pytest.raises(TypeError):
        context.working_copy["code"] = "changed"
    serialized_diagnostics = json.dumps(asdict(second.diagnostics))
    for sensitive in ("return input", "SECRET_NAME", "attachment text", "aGVsbG8="):
        assert sensitive not in serialized_diagnostics


def test_build_result_is_immutable_and_transport_is_independent() -> None:
    context, _, _ = _context()
    result = build_prompt(context, runtime_contract="contract")
    with pytest.raises(TypeError):
        result.messages[0]["content"] = "changed"
    with pytest.raises(TypeError):
        result.messages[3]["content"][1]["image_url"]["url"] = "changed"
    with pytest.raises(FrozenInstanceError):
        result.diagnostics.revision = "changed"
    copied = result.provider_messages()
    copied[3]["content"][1]["image_url"]["url"] = "changed"
    assert result.provider_messages()[3]["content"][1]["image_url"]["url"] == (
        "data:image/png;base64,aGVsbG8="
    )


def test_package_markdown_is_pure_static() -> None:
    for name, rule in prompts.RULES.items():
        assert rule.strip(), name
        assert "$" not in rule, name
        assert "{{" not in rule, name
        assert "{%" not in rule, name


@pytest.mark.parametrize("problem", ["missing", "empty", "invalid_utf8"])
def test_required_resource_fails_before_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, problem: str
) -> None:
    for name in ("system.md", "adapter.md", "tools.md"):
        (tmp_path / name).write_text("valid", encoding="utf-8")
    target = tmp_path / "adapter.md"
    if problem == "missing":
        target.unlink()
    elif problem == "empty":
        target.write_text(" \n", encoding="utf-8")
    else:
        target.write_bytes(b"\xff")
    monkeypatch.setattr(prompts.resources, "files", lambda _: tmp_path)
    with pytest.raises(RuntimeError, match="adapter.md"):
        prompts._load_resources()


def test_resources_load_outside_repo_cwd(tmp_path: Path) -> None:
    env = os.environ.copy()
    env["DLR_RABBITMQ_URL"] = "amqp://test:test@localhost:5672/"
    env["DLR_RABBITMQ_MANAGEMENT_URL"] = "http://localhost:15672"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from dlr.control.ai import prompts; print(prompts.REVISION, len(prompts.RULES))",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    revision, count = result.stdout.strip().split()
    assert revision == prompts.REVISION
    assert count == "3"


def test_bad_resource_fails_at_module_import(tmp_path: Path) -> None:
    for name in ("system.md", "tools.md"):
        (tmp_path / name).write_text("valid", encoding="utf-8")
    (tmp_path / "adapter.md").write_bytes(b"\xff")
    script = (
        "from importlib import resources\n"
        "from pathlib import Path\n"
        f"resources.files = lambda _: Path({str(tmp_path)!r})\n"
        "from dlr.control.ai import prompts\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "AI prompt resource invalid: adapter.md" in result.stderr
