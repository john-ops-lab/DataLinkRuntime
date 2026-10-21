"""Only exact documented endpoint/model combinations gain native output."""

import jsonschema
import pytest

import ai_sdk_fixtures
from dlr.control.ai import providers
from dlr.control.schemas.ai import AiSettingDraft


@pytest.mark.parametrize(
    "provider,url,model,strategy",
    [
        ("openai", "https://api.openai.com", "gpt-4o-2024-08-06", "json_schema"),
        ("openai", "https://api.openai.com/", "gpt-4o-2024-08-06", "json_schema"),
        ("openai", "https://api.openai.com/v1/", "gpt-4o-2024-08-06", "json_schema"),
        ("openai", "https://api.openai.com/proxy", "gpt-4o-2024-08-06", "json_object"),
        ("openai", "https://api.openai.com/custom/v1", "gpt-4o-2024-08-06", "json_object"),
        ("openai", "https://gateway.invalid", "gpt-4o-2024-08-06", "json_object"),
        ("openai", "https://api.openai.com", "unknown-model", "json_object"),
        ("minimax", "https://api.minimax.cn", "MiniMax-M3", "prompt_only"),
        ("kimi", "https://api.kimi.com/coding", "k3-256k", "prompt_only"),
        ("kimi", "https://api.moonshot.cn", "moonshot-v1-8k", "json_object"),
    ],
)
def test_exact_combination_output_strategy(provider, url, model, strategy) -> None:
    setting = AiSettingDraft(provider=provider, base_url=url, model=model)
    adapter = providers.resolve_output_adapter(setting, providers.get_provider(provider))
    assert adapter.structured_output == strategy
    payload = ai_sdk_fixtures.sdk_payload(setting, [], structured=True, adapter=adapter)
    if strategy == "prompt_only":
        assert "response_format" not in payload
    else:
        assert payload["response_format"]["type"] == strategy
    assert payload["model"] == model


def test_native_schema_is_closed_code_only_and_locally_compatible() -> None:
    schema = providers.code_only_output_schema()
    output = {
        "message": "done",
        "request_intent": "code_change",
        "candidate": {
            "summary": "replace",
            "code": "return input",
            "required_secret_keys": [],
        },
    }
    jsonschema.validate(output, schema)
    from dlr.control.schemas.ai import AiModelOutput

    assert AiModelOutput.model_validate(output, strict=True).candidate is not None
    output["candidate"]["runtime_config"] = {"unbounded": True}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(output, schema)
    # Compatibility echoes remain accepted locally, subject to service equality checks.
    assert AiModelOutput.model_validate(output, strict=True).candidate.runtime_config
