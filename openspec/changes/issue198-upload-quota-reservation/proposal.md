# Proposal

## Why

Issue #198 的合法净 1MiB 文件在 Adapter 余量恰好 1MiB 时，被 64KiB 初始预留加 1MiB 批量增长误拒。预留策略必须使用真实余量，不能使单文件上限改变配额内文件的可上传性。

## What Changes

- 初始与扩容预留按实际需求、单文件上限、Adapter 和平台权威剩余额度有界增长。
- 保留原子锁序、actual + reserved、流式批量效率、磁盘低水位和失败清理保护。
- 增加真实 PostgreSQL/API 的边界、并发和回收回归验证。
- 非目标：不涉及 #129、输入规范化、AI、UI、配额放宽或生命周期重构。

## Capabilities

### New Capabilities

无。

### Modified Capabilities

- `managed-input-lifecycle`：原子上传预留不得因可选批量增长而误拒真实配额内的文件。

## Impact

仅 Backend 上传预留服务及相关 tests/docs。HTTP 接口、错误码、数据库结构与依赖保持兼容；不需要迁移。回滚应用代码即可，但会恢复旧误拒问题。不会改私有部署配置或启动生产环境。
