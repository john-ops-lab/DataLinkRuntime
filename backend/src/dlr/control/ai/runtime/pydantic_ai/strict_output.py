"""Strict raw final validation before Pydantic AI's typed output parser."""

from typing import Any

from pydantic_ai.capabilities import AbstractCapability, OutputContext
from pydantic_ai.tools import RunContext

from dlr.control.ai import policy
from dlr.control.ai.diagnostics import AiResponseInvalid
from dlr.control.schemas.ai import AiAssistRequest


class StrictOutput(AbstractCapability[Any]):
    def __init__(self, api_key: str | None, payload: AiAssistRequest | None = None) -> None:
        self.api_key = api_key
        self.payload = payload

    async def before_output_validate(
        self, ctx: RunContext[Any], *, output_context: OutputContext, output: Any
    ) -> Any:
        if not isinstance(output, str):
            raise AiResponseInvalid("output_schema", "schema_mismatch")
        parsed = policy._parse_model_output(output, self.api_key)
        if self.payload is not None:
            policy._reject_candidate_configuration_changes(parsed, self.payload)
        return output
