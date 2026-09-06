# Issue #141 validation receipt

Validated on 2026-09-06 from base `7aec2ed3ee16b5ea3c28745472d0ae018f35184a`, on the PR branch `codex/issue141-builtin-dependencies`. The PR records its final commit. These are local automated and browser results; hosted CI and user acceptance are separate facts.

## Automated checks

| Check | Result |
| --- | --- |
| Backend complete PostgreSQL suite | 1,601 passed, 14 skipped |
| Final affected library and localization checks | 29 passed |
| Ruff check / format and mypy | PASS; 148 source files checked by mypy |
| Web complete Vitest suite | 535 passed across 46 files |
| Follow-up library/settings/history/locale tests | 43 passed; final library component 5 passed |
| Web TypeScript, ESLint and production build | PASS; existing bundle-size advisory remains |
| Pinned Ant Design 5.29.3 lint on all three changed components | Zero findings |
| OpenSpec strict validation / git diff whitespace check | PASS |

The backend suite initially found old migration-head/history-field assertions and unlocalized new compatibility messages. The assertions now reflect the additive contract; new domain errors resolve through the existing locale resources. No tests were removed or disabled for this change.

## CI follow-up after the initial PR commit

Hosted CI #285 on `d83a009124525edd1f3267c66e7e04ff9b0a2b9e` passed Web but found two gaps: Compose still expected six initial dependency sources, and CPython 3.13.15 rejected a truncated PAX header with `tarfile.ReadError` before the test's expected custom allocation error. Neither result was covered by the initial local receipt above.

- Compose now checks nine sources, one non-default builtin per language, unchanged external defaults, and builtin identity preservation across default deletion/restoration.
- Archive tests independently assert that an oversized read never reaches the underlying stream, accept early parser rejection of truncated headers, and require the actual upload API to return `422 builtin_package_invalid`.
- The main CI #283 frontend failure held a detached button node after `ActionWithReason` removed its Tooltip wrapper. The existing unsaved-edit regression now queries the live button on each retry; its blocked-run and save-unblocks-run assertions remain intact.

Follow-up local checks: all 26 library tests and all 138 App tests passed; fresh PostgreSQL migration plus the exact Compose dependency-source assertion block passed through the real Control API. Ruff on the changed backend test, ESLint on the changed App test, and Compose Python syntax checks passed. The updated commit still requires its own complete hosted CI result; historical local or hosted results do not imply that result.

## Native installers with networking disabled

Built the repository Worker image, mounted the branch source, and ran `scripts/issue141-offline-runtime.py` with Docker `--network none`. Maven preparation took place separately before the probe, in a fresh material repository.

- Python: real wheel installation and module import, then verified warm reuse.
- npm: real tgz installation with a transitive dependency and module loading, then verified warm reuse.
- Java: 219 JAR/POM materials, Maven dependency-plugin 3.8.1, commons-lang3 3.17.0; real dependency resolution, Java 21 compilation and execution, then verified warm reuse.
- Missing Maven materials fail without external fallback. Unit/API coverage additionally exercises missing Python/npm transitives, direct URL rejection, unsupported npm OS, mismatched SHA/size, source identity changes, bounded archive parsing (including PAX headers), concurrent reservations, stream locks and physical-delete failure.

Reproduce the offline probe after preparing a matching Maven repository:

```sh
docker build -t dlr-issue141-worker -f docker/worker.Dockerfile .
docker run --rm --network none \
  -v "$PWD/backend/src:/app/src:ro" \
  -v "$PWD/scripts/issue141-offline-runtime.py:/probe.py:ro" \
  -v /absolute/prepared/repository:/materials:ro \
  dlr-issue141-worker python /probe.py --maven-repository /materials
```

## Real Control, RabbitMQ and Linux Worker

Used an isolated PostgreSQL/RabbitMQ/Control/Worker topology and the normal claim/Attempt APIs. The Worker ran with `cgroupns=private` under a task-owned delegated cgroup; isolation preflight and `builtin_packages_v1` were reported true. No host-cgroup topology or mocked Sandbox was used for these receipts.

| Execution | Evidence |
| --- | --- |
| #1 Python | Submitted from the actual Chrome management page; installable, cleanup completed |
| #2 JavaScript | Real npm transitive install on the selected Worker; succeeded, cleanup completed |
| #3 Java | 219 materials streamed from Control; Maven resolution and saved-code compilation succeeded, cleanup completed |
| #4 Python reuse | Accepted while the test Worker was briefly paused; deletion rejected with `builtin_package_in_use`; after resume, cache reused and cleanup completed |
| #5 Python after deletion | Physical blob absent, content route 404; new check reached `dead_letter` with `builtin_dependency_missing`, cleanup completed |
| #6 English missing materials | Same explicit error code with English Worker message and completed cleanup |

The saved test scripts contained deliberate business exceptions. Successful checks prove they did not execute business code. The separate executor regression also pins this behavior. Checks resolve no business credentials and do not change the Adapter's configured Worker.

## Browser interaction

Real Chrome with Chrome DevTools MCP, using the branch Web app and isolated Control API:

- Token login, settings route and dependency Tabs.
- Uploaded a generated 708-byte wheel through the real Upload input/change handler, reservation request and streaming PUT. The file-picker tool rejected local fixture paths because its configured roots did not match the workspace; the fixture was generated entirely in the browser for this interaction check.
- Verified stored metadata/status, saved-byte count, zero released reservation, download request and search filtering.
- Selected the Python builtin default without entering a Control URL. External npm/Maven defaults remained selected.
- Changed aggregate quota to 2 GiB, verified returned capacity and rendered value.
- Selected a saved Adapter and real Worker; submitted the check and observed polling converge to installable.
- Confirmed deletion is refused for an accepted queued task, remains visible in the confirmation dialog, and succeeds only after that task completes. Verified actual file removal and download 404 separately.
- Verified missing-material results after deletion; historical check IDs, saved Revision IDs and timestamps remain distinguishable.
- Checked Chinese at 1440px and English at 1280px using DOM, focus, screenshot, network and Console. No page-level horizontal overflow or missing translation keys. Latest stable Console had no errors/warnings. Historical Execution errors retain their accepted locale by existing contract.

The recent-check endpoint returns only bounded summary fields, never input/output/install-log bodies. Detailed logs remain available through execution history.

## Deliberate boundaries

The library snapshots a complete language category rather than resolving its dependency graph in Control. This conservatively holds/downloads more materials and invalidates new-task cache identities when the category changes. Python requires wheels, npm lifecycle scripts remain disabled, and Maven needs build-plugin materials. Preparation and consistent database/filesystem backup instructions are in the linked Chinese and English operations documents. Full general Compose smoke remains a hosted CI gate; the local isolated checks above cover the new real runtime path.
