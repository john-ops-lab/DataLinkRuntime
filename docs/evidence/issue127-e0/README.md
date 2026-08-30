# Issue #127 E0 evidence

状态：`HISTORICAL / SUPERSEDED`。

本目录保留 2026-08-28 旧 dirty-tree/旧栈的 E0 历史机器收据，不代表当前已提交
Candidate，也不代表当前 APP_READY 或人工验收状态。19.6、20.1 已重新打开；修复树冻结后
必须重新生成 exact-SHA、浏览器和 retained-app 证据。下表 PASS 仅表示收集当时的历史结果，
不得复用为后续 Candidate 通过。

| Gate | Receipt | Result |
| --- | --- | --- |
| 19.1 fresh/fixed-base/idempotent/conflict migration | `migration.json` | HISTORICAL PASS |
| 19.2 backend full ruff/format/mypy/pytest | `backend-gates.json` | HISTORICAL PASS |
| 19.3 Web full gates and targeted browser verification | `web-gates.json` | HISTORICAL PASS |
| 19.4 retained Compose runtime | `compose-runtime.json`, `real-postgres-race.json`, `worker-crash-recovery.json` | HISTORICAL PASS |
| 19.5 rollback documentation and flag close/reopen drill | `rollback.json`, `rollback-browser/` | HISTORICAL PASS |
| 19.6 dirty source/scans/OpenSpec snapshot | `source-candidate/`, `scans.json` | SUPERSEDED; not exact committed SHA |

At capture time, `docker-compose` project `dlr-i127-e0-r2-141` was retained for
the Round 1 repair handoff with session label `datalinkruntime-141-e0-r2`.
Its health or continued existence is not asserted by this historical README.

The old repair snapshot was checked against its Compose stack. All browser
receipts here remain historical and are not exact-Candidate evidence; tasks
22.13/23.8 remain open until the new committed Candidate and independent review
exist. No raw credential,
Cookie, storage key, deployment path, file content, or secret is stored in
these receipts.

The authoritative backend gate is the locked project-scoped command run from
`backend/`; all backend Ruff, format, mypy and pytest checks passed.
