## 1. Checkpoint A — Identity and local protection

- [x] 1.1 建立独立、版本化生命周期 sidecar、持久 root owner 与有界惰性初始化，保持旧 manifest 格式/identity/digest；验证旧 ready 直接命中、元数据缺失/中断/不可写不损坏内容、注册 ID 变化保留原 owner，以及真实篡改仍失败。
- [x] 1.2 增加跨进程项锁和持久 cache-use 记录，覆盖五语言依赖准备/加载/运行至 Sandbox 清空，包括 dependency_check 早退、取消、异常和 ownership lost；通过真实多进程锁、deferred cleanup、use 写入失败拒绝而生命周期写入失败只禁回收测试验证。
- [x] 1.3 关联旧 Attempt/Workspace/Sandbox journal 与 Control Execution/Attempt 版本；未知映射保守阻断；用旧 journal fixture、Control 断联和重启场景验证。

## 2. Checkpoint A — Control references and deletion

- [x] 2.1 增量迁移稳定 worker/version guard 与最小操作事实，冻结锁序、generation 和无自动到期语义；验证 fresh Alembic、已有库升级、未完成门禁拒绝降级及已删除 Adapter 锚点。
- [x] 2.2 在统一新 Execution、Replay、retry/recovery、Incident 恢复和 claim 路径接入 guard，枚举所有引用创建/复活入口；用独立 PostgreSQL 两事务竞争验证“引用先行保留/删除先行暂缓”和 Admission/Schedule 事务回滚。
- [x] 2.3 实现权威引用查询，包含 Worker 快照/实际 Attempt、queued/retry/running、open Incident、可恢复材料与 deferred cleanup；用迁节点/停用/删除、dead_letter 仍有责任和纯历史不永久保护测试验证。
- [x] 2.4 实现安全删除原语、原子 trash、幂等完成/撤销回执、有界续作与 3 次失败停止；故障注入覆盖 Control acquire 已 commit 但无本地记录、rename 前后、部分删除、回执丢失、陈旧 operation、Worker 重启及实际占用不重复扣减。
- [x] 2.5 收口 stale/Adapter/pre-cache 旧清理与损坏缓存修复，部分保留不得假报整体成功；实现当前 cleanup 精确哨兵例外、claim_attempt 回报 fencing、已删除旧对象验证身份引导与预算续作，未知 pre-cache 保留；按 design 冻结的当前 running Attempt/slot、同 key 使用保护与 replacement guard 保留同身份修复和合法身份更新。测试多个同版本/exact builtin snapshot 的未领取 queued 或干净 retry_wait 不互卡、无 Attempt 的初始 cleanup pending 不误阻止、后续 claim 选源变化继续 prepare；不同 builtin snapshot、其他 claimed payload、历史 deferred cleanup/use/journal/open Incident 必须保留旧实例。验证新 ready 发布前准备失败/所有权丢失/切换中断的恢复，不新增源/凭据全局锁或解析指纹 API，普通 GC 仍拒绝排队引用。
- [x] 2.6 完成 A 独立安全 Review 与针对性 Ruff/Mypy/pytest，保留 Review 对应提交 SHA；在专用数据环境用基线准备缓存后升级同版本验证真实 cache hit、无重装、业务成功。提交 A 后才进入 B，不创建阶段 PR。

## 3. Checkpoint B — Policy and classified governance

- [ ] 3.1 实现 design 默认值/范围与统一 cache factory，默认关闭周期/容量回收；测试五语言使用同一有效预算、非法配置失败、显式禁用和近期保留边界。
- [ ] 3.2 实现 pin、identity/digest 绑定的可重建确认及默认离线保护；验证未知/固定/材料变化/源不可用保留与内置材料可确认路径，确认操作记录非敏感审计。
- [ ] 3.3 实现有界周期扫描、稳定游标、容量高低水位和 reserve 失败至多一轮后重试一次；验证单 entry 的 nodes/hash/depth/时限预算、多版本淘汰顺序、无候选/预算不足明确拒绝，以及 round→key/key→round 交错均不等待造成互锁。
- [ ] 3.4 分类统计版本、共享下载、失败 staging、trash 与未知目录；共享仅报告不计可回收，staging 需归属/失活 reservation/无 use-journal；测试共享其他 Adapter 不受影响、未知目录不删及账目核对。

## 4. Checkpoint B — Worker communication and administrator interface

- [ ] 4.1 接入治理 capability、Worker snapshot/command/guard APIs 与客户端轮询，旧 Worker 不领新命令；测试鉴权、归属、幂等、过期/不完整快照及重复/断联重试。
- [ ] 4.2 实现管理员查询、preview/clean/protect/retry、单 Worker 活动操作限制、分页审计与有界 retention；支持 cleanup_id 复用失败 Adapter 清理请求、单次额外逻辑尝试、单调 attempts、原通道执行和关联审计；测试非管理员拒绝、预览后新引用、重复/并发请求、三次失败后 retry 到第 4/5 次、旧领取回报拒绝和未完成关联操作不被审计清理。
- [ ] 4.3 读取项目 Ant Design skill 和 5.29.3 snapshot 后实现 Worker 缓存管理 UI 及中英文文案；通过 Web lint/typecheck/Vitest/build，并用本地 Chrome 验证占用、保护原因、预览、实际结果、pin、失败重试和离线/不支持状态。
- [ ] 4.4 更新中英文部署/运维文档，说明默认关闭、阈值、重建确认、共享不支持、失败恢复与安全回滚；运行相关双语/链接文档测试，确认不写入私有部署参数。

## 5. Final Group Gates and Delivery

- [ ] 5.1 在专用数据环境完成真实 v3 低容量释放旧版本后新版本执行、多进程竞争、queued/retry/Incident、取消、重启、未知 journal、断联与旧 ready 升级；保留逐项结果与明确未验证项，不使用清空生产缓存作为步骤。
- [ ] 5.2 第三组最终 head 跑必要 Backend/Web/迁移/适用 Gate，完成独立 Review 并修复实质发现；证据标明 SHA、当前重跑或继承，不重做无关组2变更。
- [ ] 5.3 集成负责人按第三组 REMOTE_RELEASE 完成单个最终 PR、当前 head CI、授权部署/合并和部署 SHA 核对；子任务不自行 push/PR/部署，不新增 A/B 阶段 PR。
- [ ] 5.4 汇总 #139 所有验收条款、真实管理员浏览器证据与最终用户验收状态；未完成用户验收前保持 Issue 开启，未经本组门禁不推进第四组。
