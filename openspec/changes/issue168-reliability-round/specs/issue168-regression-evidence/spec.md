## Purpose

本合同规定 #168 本轮修复的证据与状态边界，使固定 main 基线上的首次失败、修复后结果、模拟回归和真实运行能够独立核对，并保持未复现事项及缺少验收前态的事项可见，不通过变更预期或统计归类制造完成。

## ADDED Requirements

### Requirement: Fixed baseline and reachable regressions
本轮开发 SHALL 固定 `0a71d8280fd4ba8ebf65ed0057ef0121a595b4f4` 为源基线，保留 #168 及其子单的跟踪关系并排除 #129。每个修复 MUST 先记录基线复现或实际生产路径可达的回归条件。

#### Scenario: A defect cannot be reproduced
- **WHEN** 当前环境不能复现已记录缺陷或不能形成规定前态
- **THEN** 系统跟踪保留待证/阻塞状态、缺失条件和已有首次失败，不能擅自关闭 Issue、放宽业务预期或将缺少前态转为新增功能

### Requirement: Evidence preserves attempt and execution boundaries
验证记录 SHALL 区分首次失败、有界重试、修复后尝试、模拟测试、直接运行、完整 Worker/UI 验收、CI 和部署 SHA。#191–#194 SHALL 随相关修复逐条补验，不以部分正常路径代替其并发、异常或权限分支。

#### Scenario: A later attempt succeeds
- **WHEN** 某项首次失败后重试或修复版本运行成功
- **THEN** 两次结果均独立保留，成功不能覆盖首次失败，也不能自动令关联未执行的变体通过

### Requirement: Existing safety and product boundaries remain effective
修复 MUST 保留权限、脱敏、缓存完整性、资源和时间限制、durable Claim/journal 后 ACK、fencing 与人工 Apply/Save/Run，不扩大到 HA、DAG 或通用 Agent 框架。

#### Scenario: Verification could be bypassed
- **WHEN** 减少校验、提高消息投递上限或无限重试能够暂时消除失败表现
- **THEN** 本轮 SHALL 拒绝该替代方案并修复已经证实的原因或保留待证状态
