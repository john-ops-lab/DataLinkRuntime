"""DLR's closed code-only transport schema, independent of runtime SDK types."""

from dlr.control.ai.provider_config import JsonObject
from dlr.control.schemas.ai import AiModelOutput


def code_only_output_schema() -> JsonObject:
    """Derive a closed schema; local parsing still accepts compatibility echoes."""
    schema = AiModelOutput.model_json_schema()
    definitions = schema.get("$defs", {})
    candidate = definitions["AiCandidate"]
    for legacy in ("requirements", "runtime_config"):
        candidate["properties"].pop(legacy, None)
    definitions.pop("JsonValue", None)

    def close(value: object) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            if value.get("type") == "object" and isinstance(value.get("properties"), dict):
                value["additionalProperties"] = False
                value["required"] = list(value["properties"])
            for item in value.values():
                close(item)
        elif isinstance(value, list):
            for item in value:
                close(item)

    close(schema)
    return schema


def transport_output_schema() -> JsonObject:
    """Return a fresh inlined schema shared by SDK configuration and estimation."""
    schema = code_only_output_schema()
    definitions = schema.pop("$defs", {})
    assert isinstance(definitions, dict)

    def inline(value: object) -> object:
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                return inline(definitions[reference.removeprefix("#/$defs/")])
            return {key: inline(item) for key, item in value.items()}
        if isinstance(value, list):
            return [inline(item) for item in value]
        return value

    result = inline(schema)
    assert isinstance(result, dict)
    return result
