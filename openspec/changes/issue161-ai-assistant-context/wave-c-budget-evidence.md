# #151 Wave C budget checkpoint (tasks 3.1–3.2)

The request's one monotonic Assist deadline now starts before adapter/setting,
attachment, and prompt preparation. Initial, tool follow-up, and tools-disabled
finalization calls each recheck both the remaining deadline and a conservative
context estimate immediately before Provider transport. Unknown models use the
configurable 32,768-token ceiling. No model-specific ceiling is asserted until
its provider/model snapshot has been verified. The estimate counts UTF-8 JSON
bytes at two bytes per estimated token, role/tool wrappers, 8,192 tokens per
native image, an 8,192-token output reserve, and a 2,048-token safety margin.
It is deliberately not described as a tokenizer-exact measurement.

Optional history is removed as whole user/assistant turns, then attachment and
snippet entries as whole items, then whole native images. Tool call/result
pairs and the complete current request, Working Copy, and protocol stay intact.
Responses identify the number of omitted optional items. Required content
overflow is rejected before the initial Provider call with
`ai_context_over_budget`; an overfull tool follow-up/finalization cannot issue
another Provider call and follows the existing safe-stop response path.

Validation on the task's disposable PostgreSQL container:

- `ruff check` on changed Python sources and test: passed.
- `mypy` on changed source modules and config: passed.
- `pytest -q tests/test_ai_context_budget.py tests/test_ai_tools.py tests/test_ai_prompt_builder.py`:
  72 passed; one third-party Starlette deprecation warning.
- `pytest -q tests/test_ai*.py`: 421 passed; the same deprecation warning.
  The pre-budget parser truncation test now runs with an explicitly larger
  window so it continues to test the parser cap. A separate default-32k test
  verifies the large attachment is omitted, the user sees the omission, and
  the Provider still gets the complete Working Copy.

The budget tests cover unknown-model default, small windows, required content
and tool-definition overflow, complete history turn removal, optional large
reference removal with intact tool call/result pair, initial no-call rejection,
finalization no-call overflow, deadline recomputation after budget work, and
an older user message spoofing the current-envelope prefix.
No real Provider quality claim is made at this checkpoint.
