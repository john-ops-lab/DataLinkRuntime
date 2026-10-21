"""A single domain-authorized correction; framework output retries stay off."""

import asyncio
from typing import Any

from dlr.control.ai.diagnostics import AiResponseInvalid
from dlr.control.services.ai import _parse_model_output
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.capabilities import AbstractCapability, OutputContext
from pydantic_ai.models import Model
from pydantic_ai.tools import RunContext

from control_probe import Ledger
from probe_support import Final


class StrictFinal(AbstractCapability[Ledger]):
    async def before_output_validate(
        self, ctx: RunContext[Ledger], *, output_context: OutputContext, output: Any
    ) -> Any:
        _parse_model_output(output, ctx.deps.api_key)
        return output


async def corrected_final(model: Model, ledger: Ledger, prompt: str) -> Final:
    async def run(instructions: str | None = None) -> Final:
        remaining = ledger.state.remaining_total_seconds()
        if remaining <= 0:
            raise TimeoutError("hard deadline")
        async with asyncio.timeout(remaining):
            agent = Agent(
                model,
                output_type=PromptedOutput(Final),
                retries=0,
                capabilities=[StrictFinal()],
                instructions=instructions,
            )
            ledger.model_calls += 1
            return (await agent.run(prompt, deps=ledger)).output

    try:
        return await run()
    except AiResponseInvalid as error:
        if (
            error.stage not in {"final_json", "output_schema"}
            or ledger.state.format_corrections
            or ledger.state.stop_reason is not None
        ):
            raise
        ledger.state.format_corrections += 1
        # The same original prompt and deadline are used; no failed model
        # response or ValidationError detail is ever added to the new run.
        return await run(
            "DLR rejected the final answer at "
            + error.stage
            + "/"
            + error.reason
            + ". Answer the original USER_REQUEST with exactly one strict JSON object. "
            "No tools or Markdown. This is the only format correction."
        )


async def stopped_final(model: Model, ledger: Ledger, prompt: str) -> Final | None:
    """One tools-disabled finalization; None delegates to DLR's server fallback."""
    assert ledger.state.stop_reason is not None
    if (
        not any(item.status == "success" for item in ledger.dispatch_results)
        or ledger.state.attachment_read_required
        and not ledger.state.successful_attachment_reads
        or ledger.state.remaining_total_seconds() <= 0
    ):
        return None
    try:
        result = await corrected_final(model, ledger, prompt)
    except Exception:
        return None
    if ledger.state.remaining_total_seconds() <= 0:
        return None
    return result
