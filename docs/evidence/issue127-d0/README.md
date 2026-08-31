# Issue #127 D0 evidence

## Pre-implementation focused red test

- Runtime: `gpt-5.6-luna`, `reasoning_effort=max`
- Working directory: `web/` (repository-relative)
- Command: `npm run test -- --run src/issue127-d0-contract.test.ts`
- Timing: after `web/src/issue127-d0-contract.test.ts` was created and before
  `web/src/types.ts` or the missing transport was implemented.
- Expected failure observed:

```text
Failed to resolve import "./managed-input-client"
from "src/issue127-d0-contract.test.ts"
Test Files 1 failed
Tests no tests
```

The first dependency-missing attempt (`vitest: command not found`) is not used
as product evidence. The import-resolution failure above is the focused D0 red
receipt.

## Green gate

The implementation runtime reported:

- focused Vitest plus i18n: 10 passed
- focused backend capability tests: 3 passed
- related B0 tests: 14 passed
- Web ESLint and typecheck: passed
- backend Ruff, format check and mypy: passed
- OpenSpec change/all strict and `git diff --check`: passed

The temporary PostgreSQL used for backend verification was labeled
`ao.session=datalinkruntime-141-d0` and was removed after the focused run.
