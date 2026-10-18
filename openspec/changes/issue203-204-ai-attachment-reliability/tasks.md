# Tasks

## 1. Authorized reference tasks

- [x] 1.1 统一 Prompt 附件业务授权与 Working Copy 边界，以 Prompt 单测检查。
- [x] 1.2 增加显式任务意图及业务状态，以 API/schema 回归区分拒绝、咨询和 Candidate。

## 2. Response recovery

- [x] 2.1 核对模型/端点策略与最终提取，以确定性 Provider 测试验证协议与 reasoning 边界。
- [x] 2.2 增加预算内一次格式纠正及脱敏审计，验证成功、再次失败、无预算、安全和配置失败不纠正。
- [x] 2.3 确定性回归坏附件后同会话新请求及传输失败恢复，保留冻结请求/fencing，前端中英文恢复入口一致，以 Backend/Web 测试验证。
- [ ] 2.4 主 Agent 在真实 Chrome 检查恢复入口与任务状态的视觉/交互表现，确认迟到响应与切换范围。

## 3. Validation

- [x] 3.1 完成相称 Backend/Web 静态检查与回归，记录 SHA 与结果。
- [ ] 3.2 主 Agent 完成 #203 MiniMax/Kimi 同请求对照与人工 Apply/Save/Run，分别记录 Candidate 与业务运行结果。
- [ ] 3.3 主 Agent 固定轮数完成 #204 真实 Chrome 同会话首次恢复，保留失败、显式 Retry 和恢复结果。
