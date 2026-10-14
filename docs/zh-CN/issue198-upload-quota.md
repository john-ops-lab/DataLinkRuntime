# Issue #198：上传预留与净文件容量边界

上传容量以 PostgreSQL 中的实际占用加有效预留为权威。单文件上限、Adapter 配额、平台配额和磁盘低水位仍独立生效；本次修复不修改配置上限、数据库结构、HTTP 接口或错误码。

原实现从 64KiB 开始预留，按 1MiB 批量扩容，仅以单文件上限截断。Adapter 剩余 1MiB、单文件上限 100MiB 时，第一次增长会请求 1MiB + 64KiB，从而误拒合法文件。

修复先校验当前已读净字节所需的新增容量，然后将可选批量增长截断到单文件、Adapter、平台及磁盘可用余量。初始预留也按余量截断，额度耗尽或超额仍拒绝新增上传。Adapter → platform capacity → reservation 锁序、流式写入前低水位检查、续租、核销与取消清理保持原合同；余量充足时仍使用批量增长，不增加逐网络块的策略查询。

## 自动验证

测试使用专用 PostgreSQL 测试库，执行真实 Alembic 迁移及 API 调用，禁止指向业务或本地验收数据库。

```bash
cd backend
uv sync --frozen
# DATABASE_URL 指向任务专用 PostgreSQL 实例；test fixture 仅重建下面的专用数据库。
DLR_TEST_DATABASE=dlr_issue198_test uv run pytest \
  tests/test_issue198_upload_quota.py \
  tests/test_issue127_b1_artifact_store.py \
  tests/test_issue127_b2_binding.py \
  tests/test_issue127_b3_gc.py \
  tests/test_issue127_pr131_upload_hardening.py \
  tests/test_issue150_upload_proxy_contract.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

2026-10-14 在初始 HEAD `2e878f3383b0d80ec6a6d9cbd4779837aa104977` 上执行净 1MiB 红测，得到 HTTP 409 / `adapter_input_quota_exceeded`。修复后上述回归共 97 项通过（新增 23 项），Ruff、format、mypy（176 个源码文件）和 OpenSpec strict 校验通过。回归验证覆盖：

- Issue 同 hash 的 `x` 重复 1,048,576 次，SHA-256 为 `8f990ba0b577b51cf009ea049368c16bbda1b21e1b93be07a824758bb253c39b`；单文件上限 1MiB 和 100MiB 时均上传成功，独立读取 Blob 大小/hash 正确，保存后成为 READY。
- 使用合法策略配置及已有真实 Blob 制造 Adapter/平台剩余额度；覆盖单文件上限小于、等于、大于剩余额度，以及真实边界多 1B，错误原因准确。
- 剩余 1B、32KiB、64KiB - 1B 的初始预留，不放宽最小配置值；额度用完后拒绝新增上传。
- 同 Adapter 及跨 Adapter 的平台并发竞争不得超卖，失败预留、部分文件、取消、过期与幂等删除回收正确。
- 可选批增长受磁盘余量约束，必需写入违反低水位仍被拒绝；既有批量效率、绑定、GC 与 finalize/cancel 竞争保护继续通过。

## 真实浏览器验收边界

自动 API 与 PostgreSQL 验证不替代真实 Chrome 文件控件验收。本候选提交未启动本地验收部署、未修改私有入口配置；PR 的固定入口部署由主流程完成。

本地验收需要在页面设置 Adapter 配额 1MiB、单文件上限 100MiB，上传上述净 1MiB TXT，确认保存可使用并独立核对大小/hash。继续验证多 1B、单文件 1MiB 对照和删除后容量回收，并恢复测试策略与清理本次测试文件。
