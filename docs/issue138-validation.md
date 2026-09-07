# Issue #138 validation and local review

This change adds TypeScript and Go to the runtime, workbench, portable packages and all 17 reference scenarios (34 new variants). It remains unmerged until user review. Hosted CI status belongs to the PR's current commit and must be read from its checks.

## Automated validation

- Backend full run: 1669 passed, 14 skipped; one stale migration-head expectation failed. After changing the expectation to `0038_issue138_languages`, all three tests in that migration module passed. The PR CI reruns the complete suite.
- Backend Ruff check/format and mypy (158 source files) passed.
- Web: 47 files / 546 tests passed; final dependency-panel rerun 5 passed and locale/settings rerun 17 passed. ESLint, TypeScript checking and production build passed.
- All 17 new TypeScript recipes passed strict compilation against their declared SDKs; all 17 Go recipes compiled with the platform harness. Catalog hashes and five-language copy/export contracts passed API tests. These are reference templates; no per-template cloud or database business acceptance was performed.
- New runtime tests cover Context/config/secrets/managed input, real compilation, invalid source rejection, source diagnostics, cache reuse, ZIP roundtrip, builtin Go modules and missing transitive dependencies, material identity, snapshot/download authorization and builtin npm for TypeScript.
- Initial hosted Compose smoke passed its main regression (five Worker capabilities, five-language execution and a 2 MiB Go material upload through Web), then failed in the later Issue #144 gate because it still expected 51 variants. That gate now expects 85 and copies/runs all five language variants. Both Nginx configurations passed syntax validation; the real local upload accepted larger Kubernetes module ZIPs.

## Integration and browser coverage

Real Linux checks exercised five-language execution, TypeScript and Go compilation cancellation, preparation timeout, invalid source rejection, builtin offline module preparation, cache reuse, and workspace cleanup. SDK probes used test fixtures, with no production cloud credentials or cluster access.

Browser checks covered TypeScript creation/save/run, source diagnostics and completion, five-language template selection, Go source settings and module uploads. These checks do not replace user visual acceptance.

Deployment addresses, local infrastructure settings, raw execution receipts and acceptance credentials are private operational records and are not part of this repository.

See [中文使用与迁移说明](zh-CN/issue138-typescript-go.md) and [English runtime guide](en/issue138-typescript-go.md).
