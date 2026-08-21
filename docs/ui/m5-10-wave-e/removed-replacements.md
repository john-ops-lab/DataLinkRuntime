# 删除替代组件证明

本 Wave 没有删除任何旧 CSS、组件、依赖或 UI 系统。原因是没有找到能够由 Wave A-D
明确证明已完全替代、且不承载运行时/ACL/AI 状态契约的 custom implementation；为了
避免扩大范围，全部保留。

最终公开变更只允许包含：

- `web/src/components/VersionDiffModal.tsx`、`web/src/components/AiAssistantPanel.tsx`、
  `web/src/App.tsx`：经浏览器复现并回归验证的 Monaco 生命周期/稳定宿主最小修复。
- `web/tests/e2e/m5-10-wave-e-audit.spec.ts`：确定性 Wave E 审计和证据生成。
- `docs/ui/m5-10-wave-e/`：审计说明、矩阵、清单、报告和截图。

预推送检查使用以下命令确认没有删除路径、生成噪声或本机绝对路径：

```bash
git diff --name-status origin/main...HEAD
git status --short
git diff --check
home_prefix="/""Users"
tmp_prefix="/""private/tmp"
grep -RInE "${home_prefix}|${tmp_prefix}|[A-Za-z]:\\\\" \
  docs/ui/m5-10-wave-e web/tests/e2e/m5-10-wave-e-audit.spec.ts \
  web/src/App.tsx web/src/components/AiAssistantPanel.tsx \
  web/src/components/VersionDiffModal.tsx
```

其中 `git diff --name-status` 的最终结果只应包含上面列出的三个 `M` 和新增文件的
`A`，不应包含 `D`；`git status --short` 也不应包含 `node_modules`、test-results、
`.env` 或其他生成目录。组件仍被引用的事实由以下源码搜索复核：

```bash
git grep -nE 'AdapterCatalog|ApplicationShell|LiveLogWorkspace|AiAssistantPanel|VersionDiffModal|AdapterPermissionsPanel|UserManagementDrawer'
```
