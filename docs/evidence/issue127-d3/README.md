# Issue #127 Wave D3 evidence

Status: `HISTORICAL / SUPERSEDED`.

These receipts describe the Wave D3 dirty-tree snapshot captured on 2026-08-28.
They do not represent the current committed Candidate or current human-acceptance
state. Task 18.5 is reopened; the final exact-SHA/browser gate must be generated
after the repaired tree is frozen. Historical PASS facts below are retained for
audit only and must not be promoted into a later Candidate receipt.

## Scope

At capture time this receipt covered OpenSpec tasks 16.6 and 18.1–18.4. The isolated
Compose project was `dlr-i127-d3-141`; every live service carried the label
`ao.session=datalinkruntime-141-d3`. Web and account ports were 8923 and 9023.

The in-app Browser was attempted with the repository Browser client and
returned `No browser is available`. Per the fallback rule, repository-local
Playwright with Chromium was used. The fallback helper was checked with
`python3 .../webapp-testing/scripts/with_server.py --help`; the `python`
executable was not installed, while `python3` completed successfully. The
Python Playwright package was unavailable, so the locked Node Playwright
dependency under `web/` drove Chromium. This is recorded as an environment
fallback, not as in-app Browser evidence.

## Browser and API evidence

`browser-matrix.json` is the final sanitized runner receipt. It records a
machine gate of `PASS`, 11 business steps, 2 account-upload steps, and 8
locale/viewport entries (`zh-CN` and `en` × 1280/1440/1680/1920). The flow
covered replacement, custom retention with server `expires_at`, explicit
Python Context clipboard copy, manual run, schedule configuration, runtime
lock, a real `409 adapter_runtime_locked` response, schedule run-now, history
safe summary, and managed-files clone. Account upload proved same-origin
Cookie + CSRF without Bearer and then deleted the staged artifact.

The matrix saved 32 state/detail screenshots plus the business screenshot.
Each matrix entry reports no page errors, no API request failures, no raw
deployment detail, no forbidden history controls, no horizontal overflow, a
viewable long filename, and keyboard selection of the Managed Files card.

`flag-reclose.json` proves the pre-open/close gate: with the flag off,
capability was `false/false`, the card was disabled, and both multipart upload
and a managed-files config PUT returned `422 input_source_not_available`.
After the flag was restored, the card and capability reopened. Baseline and
reclosed config retained revision 10 and the same READY artifact id/SHA;
the 15 existing Execution IDs and statuses were unchanged.

## Static and build receipts

Commands run from `web/`:

```text
npm test                         PASS: 37 files, 402 tests
npm run lint                     PASS
npm run typecheck                PASS
npm run build                    PASS: 6123 modules transformed
```

The build emitted the existing large-chunk warning and Vitest emitted the
existing `TimeoutNaNWarning`; neither was converted into a product failure.
`openspec validate issue127-unified-input-object --type change --strict`,
`openspec validate --all --strict`, and `git diff --check` all completed with
exit status 0. `source-tree.json` records the historical base SHA/tree, tracked
diff SHA-256, dirty working-source SHA-256, and relative file hashes. It is not
an exact committed-Candidate SHA receipt.

## Resource ownership and cleanup

`compose-resources.json` records the five containers, four named volumes, one
network, five project image tags, labels, and the exact cleanup command. The
temporary Compose stack, named volumes, network, project images, session-owned
platform-log directory, and transient partial runner receipt were cleaned by
exact project/resource names only. Unrelated Compose projects were not
touched.

No raw response bodies, cookies, Authorization values, storage keys, runtime
paths, or credentials are retained in this evidence directory.
