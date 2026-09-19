## 1. 范围冻结与交付准备

- [ ] 1.1 核对第二组起点、唯一集成分支、原始 Issue/归档关系和工作区，建立八个 A/B 子项的私有证据索引；以 git 状态、公开 SHA 和全部 NOT_RUN/历史记录分离证明未覆盖已有工作。
- [ ] 1.2 独立审查 proposal/specs/design，重点核对输出存在性、草稿保留和无迁移边界；`openspec validate issue161-product-correctness --strict` 通过并记录审查结论后进入对应实施。
- [ ] 1.3 为真实 PG/浏览器/代理/冷源/五语言准备隔离合成夹具和独立预期，确认测试数据库、缓存和对象不属于保留业务数据；以资源清单和工具可用性记录证明可执行，缺失项明确 BLOCKED。

## 2. #153 A：JSON 类型持久化

- [ ] 2.1 在真实 PostgreSQL 经 API 重现顶层 `0↔false`、`1↔true`、嵌套对象和数组的旧类型问题；每次 commit 后新 Session 验证类型/值/jsonb_typeof，并分别记录当前配置与 Schedule 镜像的 RED。
- [ ] 2.2 在统一输入写入口显式标记 JSON 当前值及镜像变更，保留 JSON.NULL/null()、revision、锁序和事务；以 2.1 全部转 GREEN 及对应静态检查证明修复。
- [ ] 2.3 回归 JSON null/SQL NULL、普通数字/字符串、真不变保存、legacy Schedule 写入和失败事务；新 Session 验证 revision 与镜像，旧 Execution 输入/输出前后相同，新 Execution 使用新值。
- [ ] 2.4 真实 Chrome 完成页面保存→重载→API读取→执行→历史链路，类型和值一致并有独立预期；保留交互/请求/Console 和对应修复 SHA，不能用 PG 自动回归替代。

## 3. #153 B：输出存在性

- [ ] 3.1 核对 Worker/Control/ExecutionResponse 的 null、size、preview、attempt_count、started_at 来源，建立设计 D2 真值表；真实未 Claim 取消例子证明无 Attempt/无正文，历史缺失或默认元数据无法证明者归 unknown，不补写0。
- [ ] 3.2 实现实时与历史共用的输出分类和双语中性提示；组件回归覆盖 null、false、0、空字符串/数组/对象、显式空、缺失/负数/非整数/矛盾元数据和截断优先，已有复制/下载不造 null。
- [ ] 3.3 真实运行 JSON null 并在实时/历史验证正文；读取明确未开始执行的取消记录与历史不完整 fixture，核对 API/数据库来源和前后哈希不变，完成大输出/截断已有操作回归。

## 4. #155 A：指定 queued 取消

- [ ] 4.1 在既有历史详情加入有权限的 queued 取消入口，捕获发起 ID/epoch、按执行去重并复用 API；测试断言请求 ID、pending、终态及权限反馈。
- [ ] 4.2 覆盖取消期间切换/关闭详情、迟到响应、重复点击、claim/terminal 竞态；Web回归和第一组相关取消后端回归证明不误取消、不重复释放资源、不把旧结果覆盖新详情。
- [ ] 4.3 真实 Chrome 保持 active A、取消所选 queued B，保存请求 ID、界面、Console 与后端状态/资源证据；A 继续运行，B 刷新后状态与历史一致。

## 5. #155 B：运行锁与草稿同步

- [ ] 5.1 统一 App 已选 Adapter 的权威 runtime refresh，覆盖 unlocked/idle、可见性/焦点、计划操作及409，复用3秒策略并处理单在途/epoch/卸载；自动测试验证没有重复轮询或旧Adapter覆盖。
- [ ] 5.2 分开后台状态刷新与保存成功基线更新，保留代码、依赖、runtime_config、Worker/timeout/run mode override、Schedule与输入草稿；dirty字段逐项前后比较及读取失败测试通过。
- [ ] 5.3 受保护控件统一使用权威运行锁和可访问的禁用原因，保留停用/停止操作与后端409；查询固定Ant Design快照，完成组件/权限/locale/静态检查。
- [ ] 5.4 使用两个真实 Chrome 页面验证同页/跨页计划启停、active解锁、dirty保留及保存竞争409；记录交互、请求、Console和实际禁用恢复，不以静态测试替代。

## 6. #150 A：双代理上传边界

- [ ] 6.1 两套Nginx新增精确托管上传路由和有界请求上限2,147,745,792字节，保留账号rewrite/认证及流式转发；确定性测试从应用常量核对字节关系，实际Nginx配置检查通过。
- [ ] 6.2 经Token与账号真实代理分别验证L=1MiB的L/L+1以及更大策略的合规>1MiB文件，调整L不reload代理；成功保存后脚本读取并匹配独立SHA，失败证明来自后端策略。
- [ ] 6.3 验证超过固定总上限的代理拒绝、multipart预算、配额/磁盘/中断失败清理及无错误绑定；其他上传/API限制、无权限和账号CSRF回归通过，拒绝层分别记录。

## 7. #150 B：托管文件默认开启

- [ ] 7.1 统一Settings、Compose和.env.example缺省true，更新默认断言及双语启停/升级说明；无变量/true/false配置矩阵和Compose渲染通过，显式false旧配置不被覆盖。
- [ ] 7.2 默认开启环境在真实页面完成上传、保存、运行，五语言各至少一次文件内容/元数据/哈希读取；格式覆盖XLSX/CSV/LOG/JSON，Excel与日志模板独立执行，矩阵逐格标注，不要求无依据的全笛卡尔积。
- [ ] 7.3 完成ArtifactStore重启持久化、配额拒绝、active Lease保护及自然到期/删除回收回归，检查历史摘要/输出保留；新增证据与原BLOCKED/失败分离，capability ready仅算配置检查。
- [ ] 7.4 显式false环境重跑原有允许/拒绝、none/json及治理分支，证明关闭不清文件/表/历史、不扩大行为，文档所述恢复步骤与实际配置一致。

## 8. #157：四类依赖源校验

- [ ] 8.1 建立单一类型相关语法校验，create/PATCH先合并最终kind/url/credential再写入；四类合法/非法矩阵及地址-only、类型-only、两者修改测试验证失败无部分更新。
- [ ] 8.2 覆盖匹配与错配builtin、现有不可变/凭据限制、http/https/端口/IPv6/路径/非法空白及不支持协议列表；合法但不可达地址保存成功，测试确保保存不发网络请求。
- [ ] 8.3 前端地址字段显示稳定安全错误并保留输入；旧非法记录可列出/修复/删除，包含userinfo等敏感输入的错误及日志不回显，API/组件和真实页面矩阵通过。
- [ ] 8.4 四类源分别以专用新Version冷环境和可控源验证实际访问/依赖准备/第三方业务输出；留证当前选源和独立oracle，不清保留cache，不以旧ready环境当证据。

## 9. #159：Java 文档合同

- [ ] 9.1 搜索并修正Java AI文档、配置说明及相关示例的字段/日志方法，保持Runtime API不变；检索/读取结果和源码对照检查通过，其他语言不被机械替换。
- [ ] 9.2 将文档代表性示例与真实java_runtime.SOURCE编译运行，核对config、JSON输出、info/warn/error及合成secret存在性且不泄漏；实际JDK通过并加入确定性契约回归，不能只测字符串。

## 10. 整组交付与状态分离

- [ ] 10.1 逐项完成针对性静态/单元/集成检查并记录提交检查点，最终head运行Backend Ruff/format/Mypy/full pytest、Web ESLint/TS/Vitest/build、OpenSpec strict及适用CI；实际结果和未执行项可追溯。
- [ ] 10.2 独立Review最终head和本组风险矩阵，回归第一组取消/可靠运行与本组关键真实路径；审查修复产生新head时重跑受影响Gate，不把旧SHA结果冒充当前。
- [ ] 10.3 由integration owner冻结第二组同schema保全部署的exact SHA、完整路径集合、schema与数据保护/备份恢复合同，完成适用授权；此项是固定环境更新和合并前阻断门禁，缺失不得部署、合并或进入第三组，独立产品实施及普通PR准备可继续，现行audited-web-same-schema拒绝不得绕过。
- [ ] 10.4 创建唯一开放非draft PR，依次完成精确PR HEAD Hosted CI/Review、在10.3通过后选择PR完成固定部署与关键运行Gate、合并、精确merged-main CI及实际部署/必要回归；记录各SHA，既有固定环境数据/卷/凭据/第一组证据保持，禁止先合并后补候选部署验收。
- [ ] 10.5 汇总八子项独立验收、修复SHA和剩余用户手工验收，主Issue保持开放、原归档Issue不重开；不使用自动关闭关键字，完成本次临时文件清理和公共材料敏感信息扫描。
