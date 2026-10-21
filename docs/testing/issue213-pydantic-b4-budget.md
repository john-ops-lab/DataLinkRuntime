# Issue #213：B4 私有 continuation 与预估载荷修正

## 发现与处理

切默认前的代码审查发现：B3 的逻辑预算视图计入了 Thinking 内容，却遗漏 Gemini 文本/工具 part 上的 `thought_signature` 和 OpenAI 工具 part 的 `extra_content`。实际 SDK 请求的 32 MiB 硬限制仍生效，预估值不完整。

在冻结 `936fddd16277054539419d2ed33e89e7eae5511e` 的原函数上重现：25,336 字节的合成 Google 工具签名没有进入预算，私有字节计数为 0。修正后计入 25,383 字节（含 JSON 包装），同一逻辑消息的估算从 10,334 增至 23,026 tokens。原失败记录保留，没有将旧结果改写为通过。

专用消息 bridge 现在逐 part 临时计量 Thinking content/signature 和 Text/ToolCall/Thinking 的 provider details，仅把 UTF-8 字节、ASCII JSON 字节与原始 part 数三个整数传给领域预算。相同签名按每次出现累加；原 SDK parts、签名和顺序不变。用户响应、候选和审计不接收私有原文。

## 载荷估算边界

预算不再调用三协议旧编码器。协议无关的 canonical JSON 包含模型/有效推理配置、普通消息、工具及与 SDK 共用的实际输出 schema；采用 ASCII 转义，媒体使用空 blob 占位并逐次加入 base64 长度，私有 continuation 直接加入测得的 ASCII 字节数。工具的非字符串内容计入可能发生的嵌套 JSON 转义。

根/行/part/工具裕量分别为 1,024/64/256/256 字节，用于当前 DLR 生成的 histories，不能宣称覆盖任意 SDK 扩展的数学上界。估算可能在边界附近提前裁掉一个完整可选项。原裁剪顺序仍是旧历史组、完整参考项、当前请求完整可选图片；问题、WorkingCopy 和工具调用/结果组不拆分。

`estimated_payload_bytes` 是预估；`payload_bytes` 在实际序列化前为 0，SDK transport 的回调记录真实 body 长度，原 legacy 审计在过渡期间仍用原 codec 记录实际字节。预估与实际请求分别受原 byte limit 约束，实际 transport 的 32 MiB 硬限制保持。模型请求数、D6 计数、纠正次数和 deadline 均不增加。

纯 schema 函数移到无 SDK 类型的 `output_schema.py`，预算和 SDK 使用同一个 inlined transport schema。每次返回新对象，仅缓存 schema 的整数长度。

## 验证

| 范围 | 结果 |
|---|---|
| 三协议 × 三输出模式的中文、转义、schema、真实图片 body | 原 SDK 请求 bytes 独立核对，估算覆盖实际长度 |
| Google 工具/文本/Thinking 签名，OpenAI 嵌套 extra_content，Anthropic redacted continuation | 真实 SDK 回送内容和顺序保持；两种字节及 token 预算增长；view/diagnostics/审计不含合成 canary |
| 重复签名/媒体、仅 byte 超限的参考项裁剪与必需上下文拒绝 | 逐次计数，保留完整 WorkingCopy；无 Provider 额外请求 |
| 相关预算、附件读取、审计合同和 SDK 网络回归 | **156 passed**；另补大量引号/反斜线/中文的非字符串 ToolReturn 实际 SDK 嵌套 JSON 字节 oracle，**1 passed** |
| 静态检查 | Ruff/format、strict Mypy（201 source files）、diff 检查通过 |

首次回归的两个 reader 用例因 legacy 审计 spy 仍接收旧函数签名失败，适配 spy 后原 deadline/结果断言通过。独立审查还发现验收脚本未把新增预估字段纳入 prepared/rejected 推导；先复现两个合同失败，再补齐推导及 bool/负数/NaN/超限断言，相关 32 项合同通过。以上首败保留。

预算修正提交 `b24e2feadcffec911e0d21675cc4fceb2e66ebd4` 的五项精确 HEAD CI 全部通过。80 个真实核心对照继续使用独立冻结的 `936fddd`；其 prompt、工具内容和模型参数不受本地工作区改动影响。在 B4 切默认前仍须补受影响部署/业务烟测；不得用该记录提前宣称 B4/B5 完成。后续发现并修正的收尾边界见[兼容记录](issue213-pydantic-b4-finalization.md)。

独立只读复核确认上述 validator 修正后没有剩余阻断项，可提交并等待精确 HEAD CI；结论仅覆盖该预算修正。
