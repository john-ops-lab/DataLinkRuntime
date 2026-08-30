# Issue #127 C4 evidence

This directory contains the sanitized evidence summary for OpenSpec tasks
14.1–14.6. Raw HTTP bodies, headers, tokens, service logs, host paths and
temporary bind data are intentionally not copied into the repository.

## Provenance and scope

- `DISPATCH_ID`: `issue127-c4-gate-20260828-r1`
- `APPROVED_CHANGE`: `openspec/changes/issue127-unified-input-object`
- `FIXED_BASE`: `7aa7d5904946a2145601a43ac79145a791adf80c`
- Scope: only C4 tasks 14.1–14.6; no D0 task was implemented or checked.
- Candidate state: an uncommitted working-tree Candidate for independent human
  review. No automatic review, commit, push or PR was performed.
- Runtime: isolated Compose projects and test databases owned by
  `ao.session=datalinkruntime-141` or the dedicated C4 smoke session.

## 14.1 protocol rollout

The isolated recovery stack ran legacy `none` and `json/null` Executions with
a protocol-v1 Worker. Both completed successfully and produced the required
`workspace_cleanup_legacy_unverified` deferred receipt. A managed-files
Execution remained pending and retained two Leases while only v1 was
available; changing only the Worker to protocol v2 allowed the same Execution
to succeed, after which cleanup completed and its Workspace, journal and
Leases converged to zero. See `rollout.json`.

## 14.2 real multi-language and GC race

Python 3.13, Node.js 22 and Java 21 each executed one public-API-created
managed-files task with two files. Every runtime opened its Workspace path,
recomputed size and SHA-256, and validated ordinal/metadata. The captured
TaskPayload facts contained neither Control storage paths nor storage keys or
raw Tokens. Each run ended with cleanup completed, no Lease and no Workspace
or journal. A real PostgreSQL lock-order test covered both Lease-first and
governance-first GC/Execution creation races (`2 passed`). See
`multilang.json`.

## 14.3 fault recovery

The fresh C0/C1/C3 fault suite passed `59` tests, including claim replay,
download interruption/tamper, separated Token authorities, stale convergence,
cleanup budgets and late-report behavior. A live late-report probe confirmed
that swapped Tokens return stable 422 codes and late reports do not change the
business-result SHA. A live Worker-process crash produced exactly one Adapter
start; the business state converged to `timeout`, cleanup later converged to
`completed`, Lease count reached zero, and the Workspace/journal disappeared.
See `fault-injection.json`.

## 14.4 pre-open gate and 14.5 rollback

The final deployment state kept `DLR_MANAGED_FILES_ENABLED=false` on Control
and Worker, with minimum protocol 1 and the live Worker restored to protocol
2. The focused flag test confirmed managed upload is rejected before store
creation while the flag is off. Rollback was ordered as flag-off, active
drain, Worker v1 compatibility check, then Worker v2 restoration. Artifact
rows, 170 bytes of capacity truth and historical Executions were retained;
active Executions and Leases ended at zero. See `rollout.json`.

## 14.6 Wave C gate

- C4 configuration/registration tests: `24 passed`.
- PostgreSQL GC/Execution lock-order race: `2 passed`.
- C0/C1/C3 recovery and fault suite: `59 passed`.
- Full backend suite after repairing four stale test baselines: `1000 passed,
  8 warnings`.
- Ruff check, Ruff format check and mypy passed.
- Strict change/all OpenSpec validation and `git diff --check` passed.
- The existing managed-input UI closed-state Vitest passed `15` tests inside
  an isolated Web build image; no repository-local dependency install was
  needed.
- Full isolated Compose smoke passed, including fresh migration to
  `0029_issue127_c0_exec_lease`, Web production build, five-service health,
  M5.4.4 regression chain and its sensitive-value log assertions.

The first final-smoke attempt used an invalid Compose project name containing
underscores. That produced an invalid fake-IMA hostname and correctly failed
the existing SSRF hostname validator with `ks_config_invalid`. The unchanged
code and smoke script passed with a DNS-valid hyphenated project name. This was
a test invocation error, not a product repair.

See `quality.json`, `scans.json` and `resources.json` for the final receipts.
