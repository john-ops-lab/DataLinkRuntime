# Issue #117 Batch 9 static document reading evidence

- Dispatch: `issue117-b9-logdocs-20260825-r1`
- Browser mode: AO Browser static Markdown preview only.
- Business app/browser flow: not started.
- Network capture: not enabled.
- Repair rerun: README was reopened after the exact `/platform-logs/` ignore
  rule and CI documentation-check step were added; the DOM/text snapshot
  contained the new `.gitignore` statement, and `ao browser errors --json` and
  `ao browser console --json` again returned empty message lists.

## README

Opened `README.md` with `ao preview`, waited for the `快速开始` heading and a
stable DOM, and inspected the snapshot. The rendered page contained the new
`2. 准备平台日志 bind mount`, `3. 启动 PostgreSQL 并执行迁移`, and
`4. 启动完整平台` headings plus the `平台日志部署文档` link. `ao browser errors --json`
and `ao browser console --json` both returned empty message lists.

Screenshot: [readme-static-v3.png](readme-static-v3.png)

The repair changes are documentation text and CI wiring only; no layout or
deployment-document rendering changed. The existing screenshot is retained as
the static-layout evidence for the reviewed Candidate. Two attempts to capture
a replacement screenshot during repair returned AO `Internal server error`, so
no replacement screenshot is claimed.

## Platform log deployment document

Opened `docs/deployment/platform-logs.md` with `ao preview`, waited for the
`Choose the host-side root` heading and a stable DOM, and inspected the
snapshot. The rendered page contained both host roots, all five bind-mounted
directories, the `postgres` container-user write requirement, rotation,
redaction, credential exclusions, and the `chmod 777` prohibition. `ao browser errors --json`
and `ao browser console --json` both returned empty message lists.

Screenshot: [platform-logs-static-v2.png](platform-logs-static-v2.png)

Relative Markdown links were checked by
`scripts/check-platform-log-docs.sh` and passed.
