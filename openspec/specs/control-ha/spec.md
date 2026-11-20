# control-ha Specification

## Purpose

定义 W3 第一批中双 Control 的注册、请求权限、健康探测和功能边界。后台和来源沿用各自既有所有权；三类文件的跨节点共享与故障接管分别验收。

## Requirements

### Requirement: 同镜像 Control 分别注册并受部署权威约束
显式双节点部署 SHALL 使用相同发布镜像、schema、部署权威及已验证认证材料。两个固定位置各自至多登记一个有效进程，注册竞争须由数据库串行裁决。业务事务 MUST 验证本次进程的身份、令牌、有效租约及部署 epoch。运行角色、Worker 凭据隔离和原独占升级权限继续有效。

#### Scenario: 并发登记或重启
- **WHEN** 两个匹配实例登记不同位置、另一个进程争用已占位置，或过期进程在位置被重新登记后尝试写入
- **THEN** 合法实例分别持有独立身份，争用者不能占用有效位置或启动 intake，过期身份不能续行原操作。

#### Scenario: 离线升级 0093 到 0094
- **WHEN** 管理员从规定的 W2 发布来源停止所有写入者并执行升级
- **THEN** 使用独占 owner 和原运行角色隔离条件，在原数据库保留集群身份、Worker 责任及原权限，以新发布身份登记；旧实例不能用先前授权恢复写入。

### Requirement: Worker 可通过统一入口跨节点办理请求
Worker SHALL 沿用原协议和认证，通过同一入口办理注册、心跳、Claim、续租、纯 JSON 结果及清理。接收 Control MUST 对 Worker、实例、generation、Attempt 及证明执行原有校验，业务请求无需客户端选择 Control。

#### Scenario: Claim prepare 与 finalize 落在不同实例
- **WHEN** A 生成带静态位置标志的 43 字符 opaque proposal，B 收到 Worker 原样回传的 finalize
- **THEN** B 仅向预配置且通过 TLS 验证的同部署 HTTPS 对端转交一次；A 校验完整随机 proposal、身份及原截止时间。路由标志不授权，转交不接受客户端目的地、重定向或非幂等自动重发。

#### Scenario: A 办理 Claim 后在 B 暂存结果，finalize 回到 A
- **WHEN** Worker 提交 B 已签名且持久化的有效 intent，而 A 仍有等待 stage 的本机子进程
- **THEN** A 核对身份、正文与同一 Attempt 的证明和期限，只在原槽内安全回收自己的子进程后使用剩余预算提交；回收未知保留占用。重复办理只结算同一结果，无效请求不能驱逐其他工作。

### Requirement: LB、管理入口和能力健康分别定界
LB SHALL 使用 `/api/health/ready` 判定节点能否承担数据库及身份相关请求，`/api/health/live` 单独报告进程活性。`/api/health` 保留聚合诊断并分别显示本节点、集群登记及功能状态。账户、管理 token、CSRF、来源与 Worker 的入口授权 MUST 沿用原规则；外部传入的私有路由或身份字段不得被当成可信上游。

#### Scenario: Broker 或单项后端失效
- **WHEN** Broker 不可用，但数据库可提交且身份有效
- **THEN** 经真实 LB 的合法续租、纯 JSON 结果和 Receipt 读回可继续；新接收和 MQ 发布按各自条件拒绝。双节点下 Managed Files、内置材料和 AI 附件的共享路径在对应后端交付前显示不可用，且不能借用另一项的健康状态。

#### Scenario: 数据库不可提交或长连接中断
- **WHEN** 数据库只读或不可用，或者客户端正在使用的 SSE/Assist 连接中断
- **THEN** 数据库不可提交的节点 ready 返回 503，新责任不能被报告为已接受。长连接中断对用户可见；查询或经用户发起的重试沿原权限进行，断连不自动重发 Provider 请求。
