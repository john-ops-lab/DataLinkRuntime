# builtin-material-ha Specification

## Purpose

定义内置依赖材料库在多 Control 和远程 Worker 下的内容、授权与资源生命周期，使不可变依赖快照可以从任一节点读取，并在上传中断、取消、删除及节点故障后保留一致的材料与容量责任。

## Requirements

### Requirement: 内置材料使用独立且一致的共享命名空间
模式C的所有Control SHALL 访问相同builtin材料namespace，独立于Managed Files及AI附件；共享后端必须证明跨宿主文件互斥、原子发布、fsync及故障行为。数据库有记录但另一节点不可读 MUST 视为未就绪，不能因Managed Files可用而宣布材料HA完成。

#### Scenario: 共享目录只在一台机器存在
- **WHEN** A/B配置相同路径字符串但实际指向不同文件系统
- **THEN** namespace/preflight失败，拒绝开启材料HA，不以路径相同作为共享证明

### Requirement: 跨节点下载保持冻结内容和 Claim 授权
材料下载 SHALL 校验Worker实例、Attempt/Claim及冻结依赖材料范围，核对size/hash与环境能力；切换Control或Worker MUST 不替换依赖版本、不扩大可见范围，不向Worker泄露宿主路径或共享存储凭据。

#### Scenario: A 上传 B 下载
- **WHEN** 材料经A上传发布，远程Worker通过B为合法Attempt下载
- **THEN** 获得相同不可变字节、大小和哈希，原冻结材料身份保持

#### Scenario: 猜测另一执行的材料
- **WHEN** Worker携合法但不属于该材料快照的Claim访问
- **THEN** 下载被拒绝，不因它属于相同Pool而获得额外材料

### Requirement: 上传取消和删除不提前释放真实占用
上传预留、临时part、正式blob和活跃读取 SHALL 由数据库责任与跨节点文件互斥共同管理。取消/lease过期 MUST 不在真实writer尚未结束或文件状态未知时释放charge；删除与下载/发布并发必须保留有效内容和一次性结账。

#### Scenario: B 取消 A 上的在途上传
- **WHEN** B受理取消，而A的writer或底层IO仍未停止
- **THEN** 先撤销发布资格，保留临时占用/回收责任；确认工作停止和残留收敛后才归还预算

#### Scenario: 活跃下载与删除竞争
- **WHEN** B流式下载合法冻结材料，A请求删除
- **THEN** 锁和引用保护阻止破坏该下载，不能返回半文件或提前释放仍存在blob的容量

### Requirement: 发布和对账在节点故障后可恢复
发布 SHALL 在内容校验和受控原子发布后才提交可用元数据；提交结果不明时不得擅删可能已引用blob。对账 MUST 有可接管所有权、有限扫描和幂等释放，并保留不确定状态供恢复。

#### Scenario: rename 后 Control 崩溃
- **WHEN** 文件发布后数据库提交结果未知，另一Control接管审计
- **THEN** 先查权威记录和引用，再决定保留或清理；不制造可下载记录缺失文件或双重释放

### Requirement: 材料迁移与阶段限制明确
本地材料迁移 SHALL 逐对象核对内容、元数据、引用和上传预留，并提供中断续传及回退边界。阶段性不支持共享材料时 MUST 拒绝依赖它的新任务，保留旧责任读取/排空路径；禁用不算完整材料HA验收通过。

#### Scenario: 旧冻结任务仍引用本地材料
- **WHEN** 升级准备切换共享后端但旧材料尚未校验迁移
- **THEN** 禁止使其失去可读后端，保留原数据与任务；不得通过删除材料或改依赖版本绕过迁移

#### Scenario: 切换期间保持材料冻结与计费
- **WHEN** 逐对象迁移期间仍有冻结材料引用或未结上传预留
- **THEN** 清单保留材料身份、引用及预留，验证内容后才切换本类权威；旧代发布不能修改新代，目标已新增的写入必须反向核验后才能回退。单节点本地部署保留原有材料存储与权限。
