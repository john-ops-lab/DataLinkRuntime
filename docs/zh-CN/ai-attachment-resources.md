# AI 附件临时资源

临时资源独立于 Managed Input、业务数据库和历史会话。所有 API 要求 Adapter 的 edit 权限；资源同时绑定当前认证 Principal、Adapter 和浏览器页面 UUID scope，引用不能单独授权，也不能跨账号、Adapter 或页面读取。

| API | 合同 |
|---|---|
| `POST /api/adapters/{id}/ai/attachments` | `{scope_id, attachment}`，沿用 inline 附件的 MIME、签名、ZIP、6MiB 门禁；返回 opaque ref、原件 hash、表示 revision、目录与覆盖 |
| `GET /api/adapters/{id}/ai/attachments/{ref}?scope_id=…` | 获取本范围的公开 manifest，不返回提取正文、二进制、内部地址或路径 |
| `DELETE /api/adapters/{id}/ai/attachments/{ref}?scope_id=…` | 幂等关闭资源，实际取消活动转换，最后一个请求 pin 释放后回收 |
| `DELETE /api/adapters/{id}/ai/attachments/scopes/{scope_id}` | 关闭整范围并取消活动转换；同一 generation 的迟到上传不能重新打开范围 |
| `GET /api/adapters/{id}/ai/attachment-capabilities` | Adapter 编辑者可读的准备与所选模型能力；全局旧接口保留管理员权限 |

准备请求断开或被取消时，Control 传递取消信号并等待转换客户端确认，转换服务必须实际终止进程。API 只返回稳定错误码，格式验证不会回显 base64 或任意上游错误文本。非法附件、错误引用或取消不影响下一次合法的新范围请求。

默认 TTL 为30分钟、同范围8件、原件总48MiB、全库512MiB、单件派生64MiB。原件、派生文件、目录和读取缓存均计入容量；最多2个活动转换、4个活动加等候操作、32个缓存读取、128个范围。Assist 只 pin 本次冻结的 refs/hash/revision，每次读取重新检查原件哈希与权限，混合 inline/refs 共同受数量与原件总量门禁。显式关闭立即阻止后续读取，TTL 清理不删除活动 pin。

携带 prepared refs 的请求在完成前，必须在本轮至少成功执行一次 `read_attachment_page` 或 `read_attachment_sheet`；目录、准备状态、旧轮次读取和失败的工具调用不计入。零读取尝试时，Control 最多追加一次纠正，沿用原冻结引用、上下文和执行预算；纠正后仍无成功读取，或读取失败且没有成功结果时，返回服务端的未读取说明、`candidate=null` 和 `task_status=incomplete`，不展示模型未取证的完成答复。开启知识库检索时，原检索顺序和证据要求也须独立满足，附件读取不会推进知识库状态。旧 inline 与无附件请求不受此门禁影响。仅询问元数据但携带 refs 的请求同样需要一次读取；一次读取只证明取得了部分原件内容，不证明所有引用、范围或答复事实均已核准。

浏览器在同一页面范围内串行准备附件，最多保留8个活动或排队任务。删除排队附件立即移出队列，关闭范围清空等待任务；活动取消还需由服务端确认进程已终止。其他页面的并发转换或尚未完成的取消可能返回可重试的忙状态。Retry/Regenerate 使用发送时冻结的 refs/hash/revision，不用后来加入的文件替换旧轮次。pagehide 关闭当前范围；从 BFCache 恢复时新建页面范围并清空旧临时会话，普通 pageshow 不重置会话。

设置为 `DLR_AI_ATTACHMENT_ROOT`、`DLR_AI_ATTACHMENT_TTL_SECONDS`、`DLR_AI_ATTACHMENT_STORE_MAX_BYTES`、`DLR_AI_ATTACHMENT_DERIVED_MAX_BYTES`、`DLR_AI_ATTACHMENT_PREPARE_TIMEOUT_SECONDS`，详见配置中的有界取值。库目录700、文件600，用户文件名不用于磁盘路径；单 Control 实例用 owner lock 防止并行进程误清理，重启仅回收本功能有标记的孤儿目录。跨重启引用过期，需要重新附加文件。

第一批交付准备 API 与转换组件时，`prepared_attachments_enabled=false`，refs Assist 返回 `ai_attachment_reader_unavailable`，旧 inline 请求仍可用。第二批接入请求 reader 后才启用能力，第三批界面根据能力使用准备流程。回滚先取消并释放本功能资源，保留业务卷和认证密钥。固定组件和真实组件验证范围见 [CPU 转换组件](issue202-document-conversion.md)。
