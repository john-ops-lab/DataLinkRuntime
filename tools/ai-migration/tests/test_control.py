"""P4 observations distinguish passing probes from unresolved compatibility."""

import time
from typing import Any

import pytest
from dlr.control.ai import tools as dlr_tools
from dlr.control.services.ai import _AssistToolState
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ToolCallPart

from control_probe import SCHEMA, Ledger, PolicyStop, agent_for


@pytest.fixture
def handler_starts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    started: list[str] = []

    def handler(args: dict[str, Any]) -> dict[str, Any]:
        started.append("start:" + args["value"])
        time.sleep(0.005)
        started.append("end:" + args["value"])
        return {"value": args["value"]}

    monkeypatch.setitem(
        dlr_tools._TOOLS,
        "probe_read",
        dlr_tools.ToolSpec("probe_read", "Synthetic read", SCHEMA, handler),
    )
    return started


def call(value: str, cid: str = "b0-1") -> ToolCallPart:
    return ToolCallPart("probe_read", {"value": value}, cid)


def test_agent_drives_multiple_batches_and_public_run_mode_keeps_real_start_order(
    handler_starts: list[str],
) -> None:
    ledger = Ledger()
    result = agent_for(ledger, [call("a"), call("b", "b0-2")], [call("c", "b0-3")]).run_sync(
        "Synthetic consultation", deps=ledger
    )
    assert result.output.message == "B0 answer"
    assert ledger.model_calls == 3
    assert ledger.state.tool_rounds == 2
    assert ledger.state.total_tool_calls == 3
    assert handler_starts == ["start:a", "end:a", "start:b", "end:b", "start:c", "end:c"]


@pytest.mark.parametrize("args", [{"value": 1}, {"value": "a", "extra": 1}, {}])
def test_from_schema_bridge_preserves_dispatcher_bad_argument_budget(
    handler_starts: list[str], args: dict[str, Any]
) -> None:
    ledger = Ledger()
    result = agent_for(ledger, [ToolCallPart("probe_read", args, "b0-bad")]).run_sync(
        "Synthetic consultation", deps=ledger
    )
    assert result.output.candidate is None
    assert ledger.state.total_tool_calls == 1
    assert ledger.state.consecutive_failures == 1
    assert ledger.dispatch_results[0].error_code == "ai_tool_args_invalid"
    assert handler_starts == []
    assert ledger.model_calls == 2


@pytest.mark.parametrize(
    ("used_calls", "used_rounds", "incoming", "reason"),
    [
        (15, 0, 2, "tool_call_budget"),
        (0, 8, 1, "tool_round_budget"),
    ],
)
def test_entire_batch_is_refused_before_any_handler(
    handler_starts: list[str], used_calls: int, used_rounds: int, incoming: int, reason: str
) -> None:
    ledger = Ledger()
    ledger.state.total_tool_calls = used_calls
    ledger.state.tool_rounds = used_rounds
    batch = [call(str(index), f"b0-{index}") for index in range(incoming)]
    with pytest.raises(PolicyStop, match=reason):
        agent_for(ledger, batch).run_sync("Synthetic consultation", deps=ledger)
    assert handler_starts == []
    assert ledger.state.total_tool_calls == used_calls
    assert ledger.state.tool_rounds == used_rounds
    assert ledger.model_calls == 1


def test_duplicate_stops_without_dispatching_again(handler_starts: list[str]) -> None:
    ledger = Ledger()
    with pytest.raises(PolicyStop, match="duplicate_tool_call"):
        agent_for(ledger, [call("a")], [call("a", "b0-again")]).run_sync(
            "Synthetic consultation", deps=ledger
        )
    assert ledger.state.total_tool_calls == 1
    assert ledger.state.tool_rounds == 2
    assert handler_starts == ["start:a", "end:a"]
    assert ledger.model_calls == 2


def test_three_dispatch_failures_stop_remaining_batch_without_starting_handler(
    handler_starts: list[str],
) -> None:
    ledger = Ledger()
    batch = [ToolCallPart("probe_read", {"value": number}, f"bad-{number}") for number in range(4)]
    with pytest.raises(PolicyStop, match="consecutive_tool_failures"):
        agent_for(ledger, batch).run_sync("Synthetic consultation", deps=ledger)
    assert ledger.state.total_tool_calls == 3
    assert ledger.state.consecutive_failures == 3
    assert len(ledger.dispatch_results) == 3
    assert handler_starts == []
    assert ledger.model_calls == 1


@pytest.mark.gap
def test_unknown_tool_does_not_reach_public_validation_hook_and_is_not_dlr_compatible(
    handler_starts: list[str],
) -> None:
    legacy = _AssistToolState.create(150)
    legacy.record_execution(dlr_tools.execute_tool_call("unknown_probe", "{}", None))
    assert legacy.total_tool_calls == 1
    assert legacy.consecutive_failures == 1

    ledger = Ledger()
    with pytest.raises(UnexpectedModelBehavior, match="max retries count of 0"):
        agent_for(ledger, [ToolCallPart("unknown_probe", {}, "b0-unknown")]).run_sync(
            "Synthetic consultation", deps=ledger
        )
    assert ledger.state.tool_rounds == 1
    assert ledger.state.total_tool_calls == 0
    assert ledger.validations == []
    assert handler_starts == []
    assert ledger.model_calls == 1
    # A green observation test documents a migration gap. It is not a P4 PASS.
