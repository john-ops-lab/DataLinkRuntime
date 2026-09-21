## 当前交付路径：测试环境验收

本节依据用户最新范围修订，对本轮交付路径优先于下文 D10–D13 的历史执行顺序；产品需求及现有严格恢复入口自身的检查不变。

- 原固定 Compose project、双入口监听范围、认证和资源隔离设置继续使用；六个 logical volume 显式映射到本轮新命名卷，所有平台日志写入新私有目录。旧卷、旧日志、原控制器文件与历史证据不删除、不作为运行挂载，不为了验收消费旧排队工作。
- 从最终审查和 CI 通过的提交准备镜像，以实际 image ID 和源码绑定核验。私有 Compose wrapper 每次渲染完整配置并拒绝旧卷/旧日志映射；启动后核实际 mount、端口、image 和 Worker 隔离。禁止对固定 project 使用 `down --volumes`、prune 或带清卷 trap 的 smoke 脚本。
- 原 watcher 保持 disabled 且驻留项不加载，私有操作遵循原 operation/config 与 VM deploy 锁。旧 state/current/transaction/receipt 保留历史含义，不改写为验收成功；新 receipt 单独记录 `test-acceptance`、精确 SHA、镜像、存储、schema、健康、真实 RabbitMQ→Worker 执行和自然 cleanup。只有各项实际通过才报告可验收。
- 在新验收数据库运行正常 Alembic 初始化，不导入旧事故责任；复用已有合成夹具。必要的历史信息不足及非法源记录可在明确归属的新验收数据库建立合成兼容夹具，标明测试构造，不能伪称旧生产记录或正常产品写入。
- 已有产品、五语言、文件边界/持久化/Lease/GC、依赖冷源等结果逐项按源码适用性继承；尚缺的 Chrome 保存/运行/历史、queued 取消、双页面锁和草稿、双入口上传/ACL、源字段反馈仍须真实完成。持久化与重启用本轮样本验证，不以旧容器/PID/boot 身份连续为判据。
- 合并前核候选 SHA/CI/独立审查与实际验收；合并后核 merged-main CI，并在同一验收卷上部署/核对合并版本，保留本轮有用样本。自动 watcher 不会因验收成功恢复；后续更新使用已记录的验收编排，恢复自动管理另按实际配置处理。

D13 请求不执行，D11 失败与 D12 已完成结果如实保留；不把停止使用旧交付路径记作该路径运行通过，不重做历史处置。

## Context

范围、Issue 承接关系与非目标见 proposal。设计起点为 `f6ef4c126690fb70ed35890b92d058f91b72590e`，读取了 README、当前产品/架构、OpenSpec 配置、七个相关 Issue 及 #161；历史运行证据只作为复现线索，不计本次通过。

当前源码的重要事实：

| 子项 | 现状与入口 |
| --- | --- |
| #153 A | `services/input_config.py::_apply_input_config_update_locked` 每次成功保存 revision+1，同时给 JSONB 当前值及 Schedule input 赋值；普通 Python 等值判断可能令 ORM 不发出类型变化 UPDATE，必须先用真实 PostgreSQL 定位 |
| #153 B | `worker/executor.py` 对完整 null 产生 4 字节 output_size；`services/execution.py::_normalize_output` 对 null 保留上报大小；`OutputView.tsx` 当前把 null/undefined 都作为无输出，旧大小可以缺失 |
| #155 A | `ExecutionHistoryPanel.tsx` 已有选中 ID、detail request epoch 和 watcher；`api.cancelExecution` 及第一组服务端取消合同可复用 |
| #155 B | `adapter_runtime.py` 的 runtime_locked 已含启用计划和 running；`App.tsx` 当前 runtime poll 在未锁/无 active 时不启动，`TaskRunSettingsPanel` 的 loadSchedule 只更新本地计划，refreshAdapter 还会清空若干用户 override |
| #150 A | 两套 Nginx 没有 input-artifacts 专用路由；后端已使用 `L + MultipartReader.REQUEST_OVERHEAD_LIMIT`，不是缺少 multipart 预算 |
| #150 B | Settings、Compose、env 示例默认 false；文件/Lease/GC 机制已存在，默认修改不等于生命周期证明 |
| #157 | 服务端只验证名称、类型枚举及部分 builtin 合同；PATCH 逐项修改 ORM 对象，必须先算最终组合后校验 |
| #159 | `dlr_docs.py` 写成 Java config()/secrets()/logger() 且 logger.warning；`java_runtime.py::SOURCE` 实际为字段及 info/warn/error |

主规格里仍有旧协议/Wave 迁移叙述，本组只增补 JSON 保真和修改当前默认开关要求，不借本组清理历史迁移合同。第一组代码、状态和证据不改写。

## Goals / Non-Goals

**Goals:** 五项主 Issue 和三个已归档来源的全部独立验收可追溯；优先在现有领域写入口、组件与代理路由做小范围修正；用数据库新 Session、真实浏览器、实际代理和业务 oracle 证明结果。

**Non-Goals:** 不建立全局 JSON 类型库、新运行状态仓库或新事件总线；不改变 Worker wire protocol、调度/重试/取消/Lease 责任；不新增 schema 或填造历史输出；不扩建 Runtime API。产品默认开启不等于在现有部署擅自改写显式环境配置。

## Decisions

### D1. 输入更新在已有事务中显式标记 JSON 列

先在现有 PostgreSQL 测试 fixture 中 RED 复现：保存数字、提交、关闭 Session，保存布尔、提交，再开新 Session，联合 Python `type`、值和 SQL `jsonb_typeof` 判断当前配置与镜像。不能把 `0 == False` 单独作为根因证据。设计期间已对起点 SHA 完成独立 PostgreSQL/API RED：四个顶层方向及嵌套对象/数组均保留旧值、revision 仍递增；当前 JSON 和 Schedule 镜像的 ORM history 均未识别修改。该结果仅为修复前证据，不计 GREEN 或最终验收。

在 `_apply_input_config_update_locked` 赋值后，对 JSON 来源的 `config.json_value` 显式 `flag_modified`，同时对已赋值的 Schedule JSON 镜像执行对应标记；`JSON.NULL` 与 `null()` 分支保持原样，非 JSON 的 SQL 表达式不套用不必要的标记。先验证主字段，再验证镜像，避免只修一侧。此入口也承接旧 Schedule 兼容写入。

选择显式标记的原因是所有有效保存本就 revision+1，明确要求本次值持久化，不需要新建去重规则。全局 TypeDecorator/JSONB comparator 会波及无关 JSON 列；仅 deep copy 仍可被 Python 等值比较折叠；只改 UI 不解决持久化。若 RED 证明还有独立写入口，补最小入口覆盖并说明实际证据，不先扩大到所有 JSONB 字段。

不更改 schema、约束、revision、乐观锁或事务锁序。JSON null 与 source=none 的 SQL NULL 必须用 `IS NULL`/`jsonb_typeof` 分开验证。保存时旧 Execution 只读，后续创建才使用新配置。普通值与真正未变值的成功保存仍按原规则递增 revision。

### D2. 输出采用保守且统一的分类，历史不回填

在 OutputView 周边加入小型纯分类函数，由实时和历史共用，不新增状态存储。优先级如下：

| 条件 | 展示/操作 |
| --- | --- |
| output_truncated 为真 | 原截断大小与 preview 分支优先 |
| 存在非 null/undefined JSON 正文 | 按原值展示，缺少旧 size 不隐藏 false/0/空字符串/数组/对象 |
| null、size 为有限整数 4、未截断、无矛盾 preview/存在性信息，且不同时满足下面的从未执行组合 | 完整 JSON null |
| 一致的显式 size=0 且无正文/preview，或下面定义的服务端未开始执行组合证据 | 无输出 |
| null/undefined 且大小缺失、负值、非整数、错误大小或其他矛盾 | 中性“输出信息不足，无法确认” |

Worker 的正常完整输出以规范 JSON 序列化后计算大小，故 4 具有实际来源；不把任意正数当 null。不使用 Execution status 判别正文，已有复制/下载若存在须使用同一分类，不给 unknown 造字符串 null。当前 OutputView 本身没有新操作需求。

**无正文证据定案：不写入新的 size=0，不改后端终结路径。** 现有 ExecutionResponse 公开 attempt_count、started_at 和 dispatch_backend。组合证明限定为这些字段均显式存在、`dispatch_backend=rabbitmq`、`attempt_count=0`、`started_at=null`、状态为 queued/cancelled/expired，且 output=null、size=null/undefined、无截断与 preview：可靠执行机制要求先有 Attempt 才可执行代码，该组合表示从未开始产出正文。`0033` 迁移拒绝旧机制数据，不将任意旧行回填成此执行机制；但 response schema 有默认值，UI 仍必须检查实际字段存在，不能通过 `?? 0`/`?? "rabbitmq"` 对缺字段 fixture 或旧响应制造证明。实现前用真实创建→Claim 前取消例子查库确认无 Attempt，GET 与两处 UI 均显示无输出；状态本身不是证据。字段缺失、不合法或有任何输出元数据冲突时不使用该分支，曾有 Attempt 的 null+缺失 size 保持 unknown。

冲突优先级明确：实际非 null 正文仍按原值展示，即使其他元数据陈旧；截断标记始终第一。null+size4 同时具备从未执行组合则元数据矛盾，显示 unknown；null+非零错误大小/preview 同理，不能落到无输出分支。

新旧报告的 null + 缺失 size 不能统一补0；准备失败、Worker report、恢复终态不因本项新增元数据写入或重置历史输出。历史 fixture 必须包括字段缺失、旧默认值与矛盾组合，读前后哈希证明未改写。独立 Review 核对 API/迁移对 attempt_count 的默认处理，禁止仅为显示为空更改状态机。

### D3. 历史详情取消捕获意图和 epoch

操作入口放在现有历史详情，权限使用已有 canOperate/Adapter 权限，不新建授权规则。点击时捕获 `{executionId, detailEpoch}`，调用已有取消 API；取消 pending 按执行 ID 去重，状态变化由后端最终裁定。

响应只在 epoch 和 ID 仍匹配时写详情、错误和 watcher；切换/关闭后允许列表正常刷新 B，但不能覆盖 C 或启动错误 watcher。409/终态/权限失败分别呈现现有安全错误并重新读取目标，重复点击不产生重复 UI 请求。第一次返回和 watcher 状态的先后次序都测试。复用第一组 cancellation error code，不重写服务端释放 Slot/Admission/Lease 的代码。

### D4. 权威运行锁共享刷新，草稿与已保存状态分开

`adapter.runtime_locked` 继续是所有受保护控件的主权威，后端已经包含 Schedule/Webhook 启用与 active Attempt。统一使用已同步的 Adapter snapshot，不让 Task 面板用 scheduleEnabled 单独决定部分控件、Header 用另一份过时 Adapter。

在 App 当前选中 Adapter 的现有刷新链路收敛以下触发：页面可见时按既有 3 秒策略有界轮询，包括 unlocked/idle；恢复窗口焦点或可见性时立即刷新；计划开启/关闭、取消/终态和 runtime 409 后刷新。每次只有一个在途读取，依 Adapter ID/请求 epoch 忽略迟到结果，卸载清理定时器/listener；后台隐藏暂停常规轮询，恢复时补读。复用现有 watcher，不新增通知流、不强行切换历史选择。

计划 GET 与 Adapter GET 需要协调：本页计划变更成功后刷新两者；显式刷新计划同样更新权威 Adapter。后台刷新只更新计划 enabled 等已保存状态，在 scheduleTouched/schedulePolicyTouched 时不重置 cron/timezone/policy 草稿。把 Task 面板当前会清空 Worker/run mode/timeout overrides 的 `refreshAdapter` 拆清用途：保存成功且当前 epoch 匹配时接受新基线，纯状态刷新/冲突刷新不清草稿。代码 snapshot、requirements、runtimeConfig、输入 JSON/文件/retention 均不能被运行状态刷新替换。

锁定后禁用保存、Monaco、依赖、运行配置、绑定、Worker、超时、当前输入等受保护操作，原因可聚焦查看；停用计划、停止执行仍按现有各自资格可用。queued/retry_wait 不等于 active 锁。后端 409 保持最终防线，前端显示已同步原因且保留 dirty。新增文案双语，对涉及 Button/Form/Tooltip 的实现先查询项目固定 Ant Design 5.29.3 快照，不升级 manifest。

### D5. 两套托管上传专用路由使用精确有限总量

新增匹配 `/api/adapters/<数字 ID>/input-artifacts` 的独立路由，不放宽通用 `/api/`。Token 原样代理；账号保留 `__dlr_account` 私有 rewrite、认证/CSRF/安全 header 传递。

当前 `MAX_FILE_BYTES=2*1024^3=2,147,483,648`；`REQUEST_OVERHEAD_LIMIT=256*1024=262,144`；采用精确十进制 `client_max_body_size 2147745792;`，即二者之和，不误把 `2g` 当完整请求上限。不写死一次样例的 185 字节开销。确定性测试从应用常量读取预算，解析两个路由中的值和范围，不复制第三份应用最大值。

托管路由沿用上传的流式代理习惯：关闭 request buffering、使用 HTTP/1.1，并保留有界 body/read timeout。后端继续以当前数据库 L + 开销限制实际请求，同时维持单文件、字段预算、quota reservation、磁盘低水位、失败清理。代理总量放大不修改后端限制，不需要数据库策略自动生成配置。

真实代理验证两种 L（1 MiB 与大于 1 MiB 的策略），每种 L/L+1；请求大小明确位于代理上限内，从状态码、JSON code 与代理/Control 受控证据区分拒绝层。用超过固定上限的声明长度请求证明代理 413，无需真的制造/传输超大文件；最大 2 GiB 的配置关系用常量一致性和 Nginx 配置加载验证，不伪称实际传输了 2 GiB。成功文件保存后由脚本读取并与独立 SHA-256 比对。

### D6. 开关默认值与既有关闭行为一起验证

Settings default、Compose `${DLR_MANAGED_FILES_ENABLED:-true}`、`.env.example` 一致改 true。更新过时 Wave 默认关闭说明与测试断言，同时保留明确关闭的测试；不能把全部测试 fixture 切 true 后删掉关闭用例。两个默认值分开测：Python 配置无变量/true/false，Compose 无变量/true/false 渲染；不借真实用户 env 文件跑测试。

不改 ArtifactStore 路径/挂载或对象格式，不清空数据。显式 false 的旧部署保持 false，运维文档说明显式打开、重新创建受影响服务、能力与真实读取检查；关闭前停止新增并等待已有执行/Lease 按原合同收敛，保留数据和治理。

业务矩阵至少有五语言各一条 managed file 读取执行并核对文件名/大小/哈希/内容；格式维度覆盖 XLSX、CSV、LOG、JSON，实际Excel等相关模板及现有日志Adapter示例分别独立验证；随产品发布的日志模板在精确候选catalog中不存在时，保留目录身份和完整场景清单，记为NOT_APPLICABLE_CURRENT_CATALOG，不计PASS。若现场有实际用户日志模板，按其真实来源和版本追加验收，不能由catalog缺项豁免。格式与语言采取覆盖矩阵，不声称只有一条文本读取就证明所有组合。受支持但需要依赖的模板必须准备好合法来源再运行。ArtifactStore 重启持久、配额拒绝、活跃 Lease 保护、到期/删除后回收与历史摘要保持分别留证；GC 自然等待可利用既有允许策略，不能伪造时间或直接清理保留数据。

实际 Worker 验收发现既有 Python harness 直接执行 `module_from_spec` 的结果，没有先登记 `sys.modules`，导致使用延迟注解和 `dataclass` 的现有 Excel 模板在加载时失败。修复限于标准模块导入语义：在 `exec_module` 前登记本次模块，成功后保留登记供类型解析使用；加载抛出异常时清理本次登记或恢复先前同名模块，并继续抛出原异常。参考 [Python importlib 直接导入源文件的官方示例](https://docs.python.org/3.13/library/importlib.html#importing-a-source-file-directly)。通过真实 harness 子进程验证注解/dataclass 的输出与错误恢复，再以未改动的正式 Excel 模板和独立 XLSX 预期执行新候选 Worker；原失败单独保留。这是上述模板验收的必要兼容修复，不改变 wire protocol、隔离、依赖安装、调度/取消/Lease 责任或 Runtime API；最终部署范围必须重新绑定新增文件和精确候选。

### D7. 依赖源校验先合并，再变更，保存不访问网络

在现有源 service 或同包小型帮助函数中建立唯一 `(kind, index_url)` 语法校验，创建与 PATCH 同用。PATCH 先读取记录、算 next_kind/next_url/next_credential，验证整个组合和现有 builtin/credential 规则，再改 ORM 属性或默认源标记，避免失败留下部分更新。省略字段保留；显式 null 延续既有 schema 合同，不借此改变 PATCH 语义。

| 类型 | 保留的合法形式 | 必须拒绝 |
| --- | --- | --- |
| PyPI | 单个 http(s) host/可选端口/仓库路径；精确 dlr-builtin://pypi | 无 scheme/host、非法端口、控制字符/空白、错配 builtin |
| npm | 单个 http(s) registry 路径；精确 dlr-builtin://npm，现有 password/token 规则 | 同上及 builtin 凭据冲突 |
| Maven | 单个 http(s) repository 路径；精确 dlr-builtin://maven | 同上，不新增 file/其他 transport |
| Go Proxy | 当前单个 http(s) proxy；精确 dlr-builtin://goproxy | 同上，不新增 direct/off、逗号/竖线 proxy list 或 file 协议 |

使用标准 URL parser 并显式验证 scheme、host、合法端口、控制字符和原始字符串中的非法空白；保留正常 http、localhost/IP、合法 IPv6、路径/尾斜杠与已有支持的查询语义，不强制 HTTPS、公共 DNS、`/simple/` 后缀或即时可达。不能依 URL parser 自动规范化吞掉控制字符。凭据绑定不受误伤，错误文本/detail 只含字段名与稳定原因，不包含原始 URL/userinfo/query 值。

内置源保持已有创建限制、类型/地址/凭据不可变约束，普通源不能通过 PATCH 转成内置源。旧非法普通源不在启动/list 上强制校验，可修成合法地址或删除；其他修改若最终组合仍非法则返回字段错误。既有测试用 malformed URL 建连通性失败源须改为合法但确定不可达的合成地址，旧非法记录兼容用隔离测试 fixture 种入。

前端 `SystemSettingsDrawer` 的地址 Form.Item 显示后端稳定字段错误，保存失败保持输入、支持修复；frontend 轻量语法提示不能替代服务端权威。四类源均完成创建/PATCH、合法保存重读、旧记录修复及 UI 安全错误矩阵。遵从 #157 的原始边界，对已具备条件的合法源使用可控源、新 Version 冷目录和可观测包请求，分别断言实际源访问、依赖准备及第三方业务输出；已知暂缓的外部 PyPI 准备故障单列原始失败与业务未到达，不要求在本组修复，也不计为 PASS 或 N/A。不清现有 cache，不以旧 ready 环境证明新源；新增校验造成的选源或保存回归仍须修复。

### D8. Java 文档示例直接对运行时代码编译

纠正 `dlr_docs.py` Java contract 与 runtime-config entry；搜索相关 Java 文档/示例同类调用。Java logger 是 `warn`，不保留 `warning` 误导。Python/JavaScript 等语言条目按各自实际 API，不做机械全库替换。

在可检索 Java 合同里保留短而可执行的代表性示例，测试提取同一示例与 `java_runtime.SOURCE` 编译，使用当前真实 harness、input/config 和合成 secret 环境执行；oracle 断言配置值、输入转换输出、info/warn/error 分流和仅布尔凭据存在性。正文及 stdout/stderr 扫描合成 secret，证明无真值泄漏。测试故意将字段改成方法时应编译失败，证明绑定的是 Runtime 合同而不是文档字符串快照。没有 JDK 时不能计 PASS，使用已有 JDK CI/验证环境，不为本项更改 Runtime。

### D10. 已批准的第二组一次性同 schema 保全部署

本组单独使用 `audited-group2-same-schema-v1` / manifest v4，保持 `0040_issue152_dispositions` 的完整迁移图与内容不变。普通模式、v2 和第一组 v3 的准入及拒绝规则不变；既有 manifest、探针或成功状态不得重绑新 head。批准仅覆盖本组产品修复和 proposal 明列的六个 controller/doc 文件及四个 OpenSpec 文件，不扩到后续组、安装器、部署配置或新 helper。

独立 review 后的私有范围记录冻结部署 base、最终 head/tree、完整 raw diff 摘要、每个路径的 status/mode/blob、迁移图、controller 摘要、CI run/attempt、镜像和本次批准来源。它由独立审查与 integration owner 冻结，不能由运行时 planner 从当前 diff/数据库自批。安装、plan、select、最终切换都在既有锁内重核真实对象；不把 commit 自身 SHA 写进自身源码。仅允许闭合集合中逐项已审的 A/M 普通文件且固定 mode/type；删除、重命名、复制、类型或 mode 变化及未知差异全部拒绝。

fresh 只读重复读快照先与第一组独立封存事实核对，再保护全部原责任行、完整处置审计、旧业务资产和 user_sessions 的实际列/PK/行摘要。此前合法探针留下的行、文件和空目录也属于旧资产。原 queued、cleanup、terminal 及零 Attempt 的 pending 占位不能被新流程终结或忽略。Token/Master Key、显式开关和持久策略、卷/mount/backing、原材料与 runtime/journal 的内容、mode/owner 保持。

正式次序是停 Control 后同事务重核、停其余应用、证明真实 idle kernel、制作可由 `pg_restore --list` 列出的新备份并核资产、同 schema Alembic no-op、再次保全核对、启动候选、真实新 RabbitMQ→Worker 探针、自然 cleanup、完整后置保全，最后才写 receipt/current/ready 并消费一次 manifest。探针增量通过当次对象、请求、窗口及内容来源精确归属；原行不变，只有证明属于本次且已经 completed 的新 cleanup、计数/字节回到原值的唯一 `global_execution_admission.updated_at` 时间戳（旧 Adapter admission 全字段不变）和明确新文件/目录元数据可接受。不得复用第一组固定 ID/时间或全局忽略 mtime；未知变化保持 attention，不回退数据库或重置原责任。

仅新模式启动 `control worker web account-web`。account-web 使用同一个冻结候选 Web image，原 loopback 绑定、command、network、mount 来自现有私有显式 env、Compose 与实际旧容器的三方一致核验，缺值不能猜测或 fallback；较旧且 stopped 的原 account 镜像可被更新。部署后验证实际两容器 image ID、健康及真实代理认证/CSRF/隔离边界；首页 200 或内部 Control 探针不算通过。原日志只允许可归属的追加，旧内容前缀、mode/owner 保留。

有效 v4 ready receipt 与已消费 manifest、transaction、current SHA 一致时，同 SHA recover 恢复并重新核验双入口，不产生第二次业务探针；旧模式仍维持原三应用行为。没有 ready 证据的未完成切换不能被 recover 洗成成功。

账号业务验收优先使用适用且已授权的普通会话；缺失时，专项批准包含通过正常 API 创建本轮唯一普通测试用户、仅该用户首次改密/重登录、自有新对象及独立新对象的 ACL 负例。结束正常清理对象、登出、停用并保留新用户行，旧用户/密码/权限/会话均不变。该账号业务增量与正式部署探针分开登记；不得以一套 cleanup 解释另一套对象。

### D11. 首次启动后失败事务的专项软件恢复

该入口仅处理本组已绑定的 `starting` 失败事务，不能把失败候选变成成功前驱。原 D10 的失败停留和成功版本 recover 规则保持不变。实施代码、离线测试、独立审查与精确 CI 可以先准备；隔离工具传输、旧软件恢复和控制面提交必须另获绑定具体请求与工具 SHA 的专项批准。

唯一宿主入口为 `reconcile-group2-starting --incident-request <private.json> --incident-approval <approval.json>`，无任意命令、目标 SHA、force、retry 或 resume 参数。闭合请求绑定失败 manifest/首次启动原件、最后成功 receipt/consumed/镜像、完整 64 路径及 54 个冻结产品 blob、宿主六工具及 VM 四个实际安装文件的独立摘要、精确提交审查/CI、原卷及账号绑定。独立批准记录绑定请求摘要、工具提交、实际用户原文及三个精确动作：传事故工具、恢复旧软件、提交控制面对账。旧通用批准或自填布尔值不能代替。

官方安装器拒绝 attention 的边界不动。从审查通过的干净提交运行官方入口，先核实际批准/源码/CI、host/VM authority、正式安装字节、镜像/卷/PG 身份；之后只向私有 `incidents/<id>/tool/` 暂存同提交的 deploy/carry 两个工具并核对摘要。工具就位后，使用它完成下面的完整 fresh 保全核验，全部通过前不改 transaction phase 或停止服务；失败只留下 prepared 事故目录，原告警与运行状态不变。正式宿主及 VM 已安装控制器、installation 和旧成功 receipt 保持原字节。宿主 operation→config 锁与 VM deploy 锁排除并发，同 ID 目录已存在即拒绝重放。

先以 fresh 全量只读证据与原件核对 paused/attention/starting、镜像、卷、完整责任/审计/21 类旧资产（含 session）、日志前缀和首次 startup，证明尚无业务 probe。随后停 Control 并复核，停 Worker/Web/account-web，再证明真实 idle kernel、namespace 与 FD 归属。恢复前后 PostgreSQL 实际主版本、完整 RootFS、数据卷和 schema 必须一致；仅用已有的最后成功镜像重建 PostgreSQL 软件容器，不 restore dump、迁移、改标签或触碰业务旧行。

每阶段保存完整 DB、files、logs、kernel、container、storage 及采集窗口，并在下一次 phase 写入或服务操作之前完成纯比较；停 Control、停其余应用、恢复 PostgreSQL 任一阶段漂移均不能继续。运行态使用实际失败候选 Worker authority；停后使用它作为基线核对原始 namespace/FD 证据，不能用布尔值替代。完整 evidence 摘要纳入 receipt，宿主和后续保全验证器复算相同链。

只启动最后成功版本的 Control、Worker、Web。account-web 保留失败候选的原容器、镜像及绑定并保持停止，RabbitMQ 和全部持久卷不变；不能借 Compose 依赖启动账号入口。第二次独立 startup 窗口验证唯一 Worker/nonce、连续日志及既有两处目录 mtime 变化，其余文件、内容、权限和 DB 必须不变。复用 Worker 公共证明，不削弱普通 v4 的四应用及双入口健康要求。

先持久化 VM 原件和独立 `group2-software-reconcile-v1` receipt，宿主读回全量重算并保存后，才将 VM transaction 记为旧成功 SHA 的 `incident_software_restore`，绑定事故 receipt，其 backup/carry 引用从此前成功事务派生，不能沿用失败 manifest。VM current SHA 和宿主 state 原本即为旧成功值，逐字节验证且不写；旧成功 probe/receipt/consumed 不改。失败 manifest 原字节归档并附作废记录，不消费它；CAS 核对原配置选择后清 carry 引用，保持 paused，最后才清本次 attention。任一点中断均保留真实 phase 和原件，不自动续作；receipt 后中断也须先只读对账并形成具体收尾方案。

后续保全引用保留原 shape，仅在现有报告中增加专用 `group2_starting_reconcile_v1` 来源和唯一闭合事故链。事故链嵌入既有七份输入的原始字节及摘要，重跑请求绑定检查。纯验证器从失败 manifest 中的原参考逐边连接前五阶段、首次 startup、停止、恢复 startup 与最终保全，唯一导出原 selection、原 DB、恢复后 files 及追加 request/receipt/chain 摘要的 lineage；不能从 fresh 值自批新根。事故后由独立 reviewer 签署该引用，再走 trusted stage、新 scope、官方 install、fresh manifest 和正常 v4 once；账号入口到这时才随候选更新并验收。

### D12. 已恢复软件但历史原件不完整时的受限事后收尾

本例外只适用于本组已绑定、旧软件已运行且恢复操作在应用健康等待中退出的事故。用户已接受针对该事故如实保留历史证据缺口并准备工具；这不等于对未来工具 SHA 和控制面写入的批准。原 D11 完整链、普通 private snapshot、安装器和普通 recover 的规则保持不变。

新记录分别绑定原请求及实际批准、原工具源码及审查/CI、实际失败尝试/phase/日志、四阶段已落盘的 ids/DB/files 原字节和当前完整原件。历史 idle namespace/FD 采样、阶段日志端点与精确应用启动窗口缺失必须明确记载；执行顺序只能证明门禁曾通过，不能生成历史扫描值。恢复 startup 使用实际外层命令开始/结束时间及已保存容器/日志来源，必须标明替代来源并纳入专项批准，不伪称原相邻阶段窗口。

当前六块 DB、全部责任/审计/资产/session、文件内容/身份/权限、卷、镜像、PG/schema、连续日志、唯一 Worker 生命周期/nonce、keeper/PID/cgroup 和账号停止策略仍完整校验。只接受既有两处启动 mtime 变化；日志中的额外业务写入或第三次启动仍拒绝。kernel authority 的 Compose project/service 标签与完整容器标签按固定两键投影比较，完整 Worker profile 校验独立保留，不能修改采集原件以通过。

新工具只允许隔离暂存、只读核验和固定控制面对账，不执行 compose stop/up/recreate/restart、迁移、数据库还原、业务探针或业务写接口。先持久化并读回新的受限结果及保全记录，再提交独立标识的旧成功 SHA transaction，继续引用原成功 backup/carry；宿主 state/current SHA 和旧成功原件保持原字节。失败 manifest 原样 abandoned、配置 CAS 后仍 paused，attention 最后清除。原事故的失败 phase 和缺口保持，不重放、不改记成功；任何中断保留实际部分提交状态，不自动补动作。

后继正常 v4 只能通过独立 reviewer 核验的新专用来源接纳唯一 snapshot：原 selection/DB、经核验的恢复后 files，以及绑定新请求/受限结果/原件链的 lineage。不得将部分证据塞入 D11 旧链、以 null/布尔值填充缺失 raw，或改标普通 private snapshot。后续仍须重新 stage/install/plan/once 和完整四应用、双入口及业务验收。

### D13. 已完成受限收尾后本次 VM 重启的单次承接

本次新增范围仅覆盖已闭合 D12 收尾之后、当前 VM boot 下的一次旧软件启动。当前状态应为旧成功 SHA/schema/images/state/ready transaction 不变、原六容器均停止、keeper 不存在、paused 且无 carry/attention；这些是请求必须 fresh 核对的条件，不是从文档读取的运行事实。D11/D12 历史成功、失败、缺口及原件保持，不重做收尾，不将本次新生命周期塞入旧链。范围批准只允许实现、测试、独立审查和 exact CI；执行前仍须实际用户批准精确工具 SHA、请求摘要、当前 boot 和下述动作。

入口固定为 `recover-group2-post-finalize-reboot --reboot-request <private.json> --reboot-approval <approval.json>`，不接受任意命令、目标版本、force/retry/resume。闭合请求绑定完整父 D12 chain/receipt/snapshot 原字节及摘要、独立父来源 review、旧成功 receipt/consumed、host/VM authority 和安装字节、原六容器完整 inspect、镜像 RootFS、卷/backing、显式配置、当前 boot 及 missing keeper 原件，以及本次干净工具提交的差异/审查/CI。宿主 operation→config 锁和 VM deploy 锁不变；隔离工具仅为同提交 deploy/carry 两文件。以父事故/收尾和 boot 派生唯一私有目录，原子占位后任何重入或更换请求 ID 均拒绝，不能靠换目录重放。

数据比较与生命周期比较分开，固定顺序如下；每阶段先持久化操作意图，再执行单次动作、保存完整结果与窗口并纯比较，失败禁止进入下一阶段：

1. **stopped**：完整重算父 D12 链导出 F reference；只读采集原卷内 runtime/journal/materials、旧日志连续前缀、容器完整 State/静态 profile、PG 镜像 RootFS/数据目录版本、host/VM 原件，逐项对齐 F。所有五个旧业务容器必须保持父链 ID/image/config/mount，只有已绑定 reboot 导致的 stopped State 变化可被记录；account-web 的原 stopped State、ID/image/binding 也不变。此时不得调用要求活 Worker PID 的 authority 采集器，不伪造 DB 或 kernel；PG 停止时明确 DB 尚不可读，DB 预期只取父链。
2. **keeper-ready**：只调用已冻结旧 `prepare-sandbox-host.sh`，使用原私有 unit/CPU/memory 参数创建该唯一 keeper；该脚本不在父 F 的四 VM 工具摘要内，新 stopped-platform 必须显式保存其 raw/hash/mode/owner/非 symlink 来源并对照旧成功仓库同名源码，工具验证及动作紧前重核同一字节和参数。核当前 boot、unit/description/limits/Delegate、PID/starttime、cgroup、实际 mount/namespace/FD 和无残留。stopped 时的静态 Worker profile 与父链严格比较，新的 idle 扫描使用当前 boot 下核实的卷/mount 目标，不能把旧 boot 的 PID、namespace 编号或 authority 当作当前扫描来源。keeper 准备不得改变文件、日志、卷或控制面原件。
3. **database-ready**：只对请求冻结的原 PG/RabbitMQ 容器 ID 执行 `docker start`，不 recreate；等待二者真实健康后，用现有只读 DB/files 收集器取完整六块 DB 并与 F 严格相等，核 schema、PG 主版本/完整 RootFS/数据卷、全部责任/审计/资产/session、文件和连续日志。任何失败保留此阶段真实状态，Control/Worker/Web 不启动。应用启动前另取只读 SQL/队列元数据原件并绑定同次完整 DB：无 pending outbox、retry_wait、active Attempt/占用 slot/待执行 cleanup，原 queued 当前 generation 的 outbox 均已 published 且原保护摘要不变，全部相关 dispatch/DLQ 队列及内部在途总量为 0（包含 ready/unacked/delayed/DLX，缺字段不能当 0），无 enabled Schedule。按旧成功源码逐项覆盖 demo bootstrap、scheduler、retention、artifact GC、orphan audit、admission、attempt、DLQ、topology、outbox 启动循环及 Worker 启动恢复：分别保存实际只读行/时间/队列配置来源，证明请求冻结的完整有界窗口内不新增业务、不消费原责任、不删除或改写旧资产。具体包括 bootstrap admin/demo 已满足存在条件；admission counter 存在且实际聚合一致、terminal Rabbit execution 已有 admission_released_at；retention 年龄和 max_per_adapter 数量两分支及到期 terminal idempotency；attempt dead-letter hold 到期分支；GC upload reservation/binding/staged/deletion/retry lease 和磁盘 orphan；Worker journal recovery 和 adapter cleanup claim。retention/GC/admission 首次 tick 在 sleep 之前，不能用轮询间隔作保护，也不能用 open Incident 当 claim 阻断。Rabbit 检查只能 list 元数据，不能 get/ack/purge；topology 核全部可能承载旧消息的队列/路由及原配置，无未绑定生产者/消费者；正常幂等声明或按原配置创建已证明不存在旧消息的空队列不视为数据漂移，危险未知路由或无法覆盖的消息路径拒绝。未知循环、缺失证据、任何到期/数量清理候选、窗口余量不足或未绑定生产者均拒绝，不能改旧行/消息、先启动后回滚或增加例外白名单。
4. **applications-started**：仅一次启动原 Control/Worker/Web 三个 ID，不用 `compose up`、recreate、restart、依赖启动或候选镜像；account-web 始终停止。完整记录每次命令及实际容器 State，Worker 必须保持原 ID/image/完整 profile，新的 StartedAt 在本次唯一窗口内、restart_count 为 0、恰有一份新的完整 preflight receipt/nonce、gate 日志、cleanup/residue 证明。nonce 与父链全部已出现的 startup 不同；健康等待失败、重复 startup 或任何额外业务处理均失败，不能重试。
5. **verified**：用本次新 keeper/Worker 的完整 live authority 验证 PID/starttime、标签固定投影与独立完整 profile、namespace、真实 cgroup tree/限制、卷来源和预检 namespace 身份一致，再取完整 DB/files/logs/containers/storage/authority 复核。DB 与原责任、审计及排队记录全部不变；日志仅接受有来源的本次启动/健康追加，旧前缀/权限/内容不变。复用完整 startup 文件比较，只允许 `runtime` 根与 `journal/sandbox-recovery` 这两处目录的 mtime 在本次精确窗口中单调变化，两处都须有本次证明；其他目录、内容、权限、owner、材料和 journal facts 不变。

`compare_kernel`、`_validate_restored_running_kernel` 及 D12 transition 都要求旧 keeper/boot 身份连续，不能跨 boot 直接复用或删除其失败条件。新增纯校验仅把 F→本次的允许变化限定为上述已记录生命周期身份；同一新 boot 内 keeper 的 PID/starttime/parent identity 从 keeper-ready 到 verified 仍完全一致。新 cgroup/namespace inode 与 PID 不能要求数值必然不同，但必须绑定本次 boot、启动窗口和实际内核来源。原 `_worker_startup_proof` 的容器替换规则保留；提取共同的完整 preflight/log/window/profile 校验，本入口使用单独严格的 same-ID stopped→running 分支，不能将旧分支改成任意 ID 都接受。旧持久卷 device/inode/backing 和文件身份不因跨 boot 获得豁免，未知变化拒绝。

队列空证明先绑定原 Rabbit image 的实际版本、插件及配置能力；适用的 delayed/DLX 路径必须有覆盖内部在途的原始统计，或由该版本源码证明覆盖这些状态的总量，不适用必须有该原版本/配置不存在此路径的证据，字段缺失本身既不等于 0，也不能套用最新版本字段猜测。请求分别冻结本次恢复和紧接后继验证的具体截止时间，查询原行/策略导出最早可能自然变更时间及候选来源；必须覆盖整个约定验证期间，超窗不能产有效结果。正常远期保留策略不是本次失败条件，不承诺无限期不变；在约定期间内或无法覆盖后继验证时会实际消费保护集的候选才阻断应用启动。后继正式 plan 必须在证据有效期内 fresh 完整比较，过期即拒绝接续；不新增配置冻结、定时停止或自动续作。

VM 先保存独立 result/receipt/chain，宿主读回全量重算后保存同一结果及 snapshot。成功与失败都只追加本次目录中的真实 phase、命令结果及已取得原件；原 state/current/transaction/config/attention、正式安装、D12 成功原件、D11 失败、旧 receipt/consumed 和失败 manifest 均不写、不重新消费。中断只表示停留在已发生阶段，禁止自动续作、补造缺失 raw 或自动再启动；新结果不代表候选已部署。

后继来源新增且仅新增 `group2_post_finalize_reboot_v1`。验证器必须先用原严格验证器重算父 D12 链及其原参考，再逐边验证本次 stopped→keeper→DB→唯一 startup→verified 证据，唯一导出父 selection/DB、本次已证明 files 和追加 request/receipt/chain 摘要的 lineage。`preview.validate_group2_artifacts` 在该分支重算完整嵌套来源，绑定本次工具 head/controller 到后继 scope，严格检查父 F 原 tool/review 绑定；不把父 F 工具改绑为新 head，不放宽 `_partial_finalize_scope_binding` 或其他旧来源规则。独立 reviewer 冻结新引用后，仍须新 exact scope/install/plan/once、live DB compatibility 及正式四应用/双入口/真实 Chrome/合并门禁。此前纯构建缓存不替代任一门禁。

实现仍限 proposal 的六个 controller/doc 文件和四个 planning 文件；复用收集器、原始日志、纯 DB/files 比较及完整 startup 证明，只增加本次闭合编排与来源边，不复制完整事故框架、不新增 helper/安装器/配置字段。

## Verification Matrix

| 检查点 | 必须证明 | 方法/不能替代的证据 |
| --- | --- | --- |
| 153-A | 顶层/dict/list 的四个方向类型更新、null/SQL NULL、镜像、revision、历史不变 | 真实 PG RED/GREEN，commit 后新 Session + SQL 类型；真实 UI 保存/重载/API/执行/历史 |
| 153-B | null、无正文、false/0/空值、缺失/无效/矛盾、截断 | 分类表 + 实时/历史组件 + 真实 null 运行；无正文说明可靠来源；历史 fixture 读前后一致 |
| 155-A | 指定 queued、另一 active 不变、重复/迟到/权限/claim/terminal | UI 请求 ID 与选择一致，真实 API/资源核对，回归第一组取消状态机 |
| 155-B | 同页与双页面启停计划、active、dirty、409/失败/迟到 | 两个真实 Chrome 页面及交互/请求/Console，代码与多个表单草稿逐字段前后比较 |
| 150-A | 双代理 L/L+1/>1MiB、上限关系、改 L 无 reload、清理/其他路由/认证 | 真实代理与业务 SHA oracle；静态关系和 Nginx配置加载不能代替 L 成功 |
| 150-B | 未设置/显式开关，格式×五语言覆盖、模板、持久/配额/Lease/GC | 独立合成夹具，真实执行与重启、自然生命周期；保留旧 BLOCKED，不以 ready 当成功 |
| 157 | 四类 create/PATCH 全组合、离线保存、旧记录修复、安全错误 | PG/API + UI字段反馈；已具备条件的合法源使用新版本冷环境验证实际源访问及业务输出，已知排除的外部 PyPI 准备失败与业务未到达单列，不计 PASS |
| 159 | 检索内容实际公共字段与日志方法、示例可编译执行 | 提取文档代码与真实 Java SOURCE 编译运行，配置/输出/日志/secret不泄漏 |
| 最终组 | 新 head 无遗漏、第一组可靠性不退化 | Backend Ruff/format/Mypy/full pytest、Web ESLint/TS/Vitest/build、OpenSpec strict、适用CI/独立Review、最终head关键运行 |
| 专项事故恢复 | 完整正例与批准/来源/并发/漂移/日志/idle/PG/账号/持久化反例 | 离线纯函数与编排故障注入、精确代码 Review/CI；获专项批准后才采 fresh 原件并执行一次，原失败与恢复结果分别留证 |
| 本次 VM 重启承接 | 闭合 F 来源、stopped 文件和日志、PG 后完整 DB、新 boot/同 ID 唯一 startup、两处 mtime、旧责任不消费、来源冻结及不重放 | 真实形状离线正反例、各动作/持久化边界故障注入、隔离自有容器的同 ID start 验证；独立审查/exact CI 后另获具体执行批准，生产启动不得作为开发测试 |

复现与运行证据保留在私有交付目录，公共材料只记录合成预期、结果摘要、公开 SHA 和无敏感定位。不提交本机端口、路径、私有对象 ID、Token/Key、运行日志、完整请求头或截图中的账号信息。所有 NOT_RUN/BLOCKED/旧 SHA 证据明确标记。

## Risks / Trade-offs

- [ORM 赋值仍折叠类型差异或只修一侧] → PostgreSQL 新 Session 与两个 JSONB 字段逐项验证；不以返回对象或 Python `==` 通过。
- [历史输出无法可靠恢复] → unknown 显示保守保真；不填历史 size、不根据 succeeded 猜测、不新增无正文标记，只采用现有可靠字段。
- [增加 idle 状态轮询] → 复用 3 秒策略、单请求、可见性暂停及 epoch 清理；不引入另一套 store。
- [刷新清空草稿] → 已保存 snapshot 与 override 分开，后台/409 不调用清空草稿的保存成功分支，双页 dirty 场景强制验收。
- [代理上限较大] → 只扩托管路由，后端仍以实际 L/配额/磁盘限制流式读取，固定有限总量及认证不变。
- [默认开启把依赖问题暴露出来] → 五语言/格式/模板真实矩阵；明确失败归因，保持 D021/D027 排除范围。
- [旧非法源阻塞正常修复] → list/start 不校验历史，修复/delete 可行；PATCH 最终组合校验在写入前。
- [固定环境保全更新超出第一组合同] → 按已批准的 D10 独立模式实施并重新审查；最终 Git/CI/原责任及双入口证据缺失时仍禁止部署成功与合并。

## Migration Plan

1. 在第二组唯一集成分支按 153A→153B→155A→155B→150A→150B→157→159 串行推进，每项保留对应提交、目标测试与证据。Worker 子任务为 LOCAL_FAST，不 push/PR；整组远端交付由 integration owner 按已授权模式处理。
2. 预计 Alembic head 保持 `0040`，没有 schema/历史数据迁移；最终验证 migration diff、metadata 与 fresh head 一致，发现新增迁移需求时重新审查，不能假定同 schema 即可更新。
3. 第二组 backend/Nginx/Compose 等变更通过已批准 D10 的本组专用合同交付，不能扩大第一组模式。冻结 exact source/target SHA、完整路径集合、schema、持久卷/审计/queued 等保护清单与备份恢复方案；控制器 attention 不绕过，现行模式拒绝不能靠扩大通配路径解决。专项批准已经获得，最终配套实现审查、scope/CI 与正式保全证据仍是固定环境更新和合并前阻断门禁，不得进入第三组。
4. 远端交付顺序固定为：创建唯一开放且非 draft 的最终 PR（控制器接受的选择对象）→精确 PR HEAD 的 Hosted CI/独立 Review→在第3项合同及批准满足后选择该 PR，完成固定环境部署和关键运行门禁→所有门禁通过后按既有授权 merge→精确 merged-main CI 与实际部署 SHA 核对/必要回归。不得等全部部署 Gate 通过才创建 PR，也不得先 merge 再补候选部署验收。通用默认值变化不覆盖现有显式 false。
5. 回退只在授权的保全程序内处理应用版本；不降级数据库、不删卷、不撤销或重置历史/queued/Incident。托管开关可显式关闭以阻止新增，并按既有规则收敛在途 Lease，Blob 与历史保留。旧 UI 对 null 的展示退化不允许回填历史掩盖。
6. 主 Issue 保持开放，原归档 Issue 保持归档。机器检查、合并、部署和用户最终验收分别记录；不使用自动关闭关键字，不把剩余强制人工门禁变成非阻塞。组间推进以 #161 明确门禁为准。

## References

- [SQLAlchemy JSON null/SQL NULL 合同](https://docs.sqlalchemy.org/en/20/core/type_basics.html#sqlalchemy.types.JSON)：决定保留 JSON.NULL 与 null()，不改变现有模型映射。
- [SQLAlchemy flag_modified](https://docs.sqlalchemy.org/en/20/orm/session_api.html#sqlalchemy.orm.attributes.flag_modified) 与 [compare_values](https://docs.sqlalchemy.org/en/20/core/custom_types.html#sqlalchemy.types.TypeDecorator.compare_values)：显式属性变更可避开默认 Python 值比较；选择服务写入口而非全局类型替换。
- [Nginx client_max_body_size](https://nginx.org/en/docs/http/ngx_http_core_module.html#client_max_body_size)：限制整个请求体，超限 413，0 为禁用检查；据此采用专用路由有限值。
- [Nginx proxy_request_buffering](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_request_buffering)：流式代理复用现有上传模式，HTTP/1.1 与 chunked 行为一并核对。
- [Go Modules GOPROXY](https://go.dev/ref/mod#environment-variables)：上游支持的 proxy list 不自动成为 DLR 已支持的产品合同，本组仅验证当前单地址及内置源。
