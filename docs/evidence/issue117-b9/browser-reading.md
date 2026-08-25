# Issue #117 Batch 9 static document reading evidence

- Dispatch: `issue117-b9-logdocs-20260825-r1`
- Browser mode: AO Browser static Markdown preview only.
- Business app/browser flow: not started.
- Network capture: not enabled.

## README

Opened `README.md` with `ao preview`, waited for the `快速开始` heading and a
stable DOM, and inspected the snapshot. The rendered page contained the new
`2. 准备平台日志 bind mount`, `3. 启动 PostgreSQL 并执行迁移`, and
`4. 启动完整平台` headings plus the `平台日志部署文档` link. `ao browser errors --json`
and `ao browser console --json` both returned empty message lists.

Screenshot: [readme-static-v3.png](readme-static-v3.png)

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
