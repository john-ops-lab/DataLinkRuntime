# AI 附件的 CPU 转换组件

本组件只生成临时 AI 附件表示，不执行 Adapter，不写业务 InputRevision，不调用 Provider。原件字节和 SHA-256 保持不变；Control 通过内部 HTTP 调用两个受限服务，不持有 Docker socket。

## 固定组件与职责

| 组件 | 固定版本与用途 |
|---|---|
| Docling CPU | `docling-serve-cpu:v1.37.0` 镜像索引 digest `sha256:6eda9c524992147c1b0b06a1a9be4894bd68b1f43c86d92c32f996c4c60d18a3`；Docling 2.138.0/core 2.101.1/parse 7.22.3，使用 SDK 的单次子进程，不启动 Serve Local 线程池 |
| Office | `gotenberg:8.37.0` 索引 digest `sha256:f29984bd1e226bf1b93ba90af06000afa8b315853e99d27b9aaa41b93f15c769`；直接监督镜像内 LibreOffice 26.8.0.3，使用现成 Noto CJK 字体，不启动 Gotenberg HTTP 转换入口 |
| 原件工作簿 | openpyxl 3.1.5、et-xmlfile 2.0.0、xlrd 2.0.2；只读公式、缓存、格式与合并，不计算公式 |
| PDF 目录与单页 | pypdf 6.16.1；原始 1-based 页码与真实 PDF outline 的 sheet 起始位置，pypdfium2 生成真实 PNG |

镜像中预置 layout、TableFormer、RapidOCR 等权重（模型目录约 871MiB），启动和普通转换均禁止在线下载。中文 OCR 明确使用 `RapidOcrOptions(backend="torch", lang=["ch"])`、CPU 和 2 个线程；该快照的默认 ONNX OCR 配置不能作为已验证中文路径。代码许可证为 MIT（Docling/Gotenberg/openpyxl）、BSD（pypdf/xlrd）；LibreOffice 为 MPL/LGPL，Noto 字体为 OFL。预置模型还有 Apache-2.0、MIT、CDLA-Permissive-2.0 等独立许可证，分发镜像时保留上游声明。

## 启用与资源

```bash
docker compose -f docker-compose.yml -f docker-compose.ai-documents.yml up -d --build
```

附加清单给 Control 配置 `DLR_AI_DOCUMENT_CONVERTER_URL=http://ai-documents:5001`。两个组件仅连接 `internal` 网络，不发布宿主端口，不挂业务卷或宿主目录。每个组件同时最多一个转换，忙时有明确错误；Docling 为 2 CPU/3GiB/256 PID，Office 为 1 CPU/768MiB/128 PID，无 swap，根文件系统只读，临时目录为有界 tmpfs，移除全部 capabilities。

实际子进程另外以 seccomp 拒绝 IPv4/IPv6 socket，保留 Office 需要的 Unix socket；原件不能向外部地址取内容。Office 使用每次独立、最高宏安全级别且禁止宏执行的空 profile。带宏或外部关系的 OOXML 不进入 Office 渲染，原件 metadata 仍可读取并返回覆盖限制。XLS 的外部访问同样被进程 socket 过滤禁止。

私有 HTTP 合同为 `prepare/read-page/read-sheet` 加 `DELETE /v1/jobs/{job_id}`。每个请求有独立 job_id 和剩余期限；取消/超时必须杀死并 wait 整个进程组、清理临时目录后才能确认 `terminated`。Compose `init: true` 负责收割被接管的后代；监督器在最多 2.25 秒内确认进程组已经消失，否则返回 `ai_attachment_cancel_failed`，不确认成功。Docling 父监督器将相同 job_id、期限和取消传给 Office 服务。网络错误也要求取消确认；不能用断开 HTTP 代替实际终止。服务与 Control 客户端均拒绝重定向，不回显上游自由错误文本。

当前硬边界：原件 6MiB、派生 64MiB、私有 wire 96MiB、200 页、单图 1600 万像素、单件媒体 10MiB、单次最长 60 秒、sheet/range 最多 200 cells。单件内存压力超过容器额度时失败并回收；真实样本的中文 OCR 在 2GiB 下曾 OOM，3GiB 下通过，不能声称 2GiB 足够。

## 表示与覆盖

prepare 先提供页数、目录和至多首一页；未读页保持原始编号，可在剩余总期限内按页生成。空白页不会使后页前移；单页原生 PDF 导出也在受限子进程里执行。工作表页映射只使用真实 PDF outline，缺失时标 unknown；隐藏 sheet 与打印范围外内容不假称已渲染。工作簿范围读取仍能按原件读取隐藏内容并标记 hidden。

XLSX 同时返回原始公式和原始缓存。缓存缺失保持 `null` 与 missing；LibreOffice 的派生 PDF 可能重算，不能覆盖原值。xlrd 不提供公式源码，XLS 明确标 unavailable，不将存储结果伪称公式。结构化 PDF 表格作为辅助信息，返回 `structure_partial` 与真实页图供交叉核对。

`DocumentAsset.data`、原件和派生 PDF 都是私有字节；公共 manifest 只含 opaque ID、hash、大小、页/工作表定位、覆盖和稳定 warning code，不能包含 base64、宿主路径或内部 URL。

## 验证与限制

2026-10-14 的同组独立 oracle 样本包含中文数字 PDF（彩色柱图、空白第2页、文本第3页）、图像扫描 PDF、带原生图表/公式/缓存缺失/格式/合并/打印范围/隐藏 sheet 的 XLSX，以及真正 BIFF XLS。

- Docling 单独处理 XLSX 没有真实图表像素，XLS 返回失败，因此单组件不足。
- 固定镜像内直接 LibreOffice 与 Gotenberg HTTP 的同样本图表首页像素差异为零、逐页文本一致、outline 一致；XLSX 4页，XLS 3页，分页不能按 sheet ordinal 推断。
- Serve Local 客户端约0.51秒超时后仍运行19.42秒；本实现使用独立进程组。真实私有 API 取消 Docling/Office 分别约51/45毫秒确认回收，随后新合法文件恢复成功。
- 10 项真实 client→隔离组件检查验证 OCR、页码、真实 PNG/单页 PDF、sheet 映射及原始缓存。组件峰值内存约2.23GiB/125MiB，无 OOM，完成和取消后无本功能临时目录。

这些结果不替代真实 Chrome、所选 Vision 模型、Candidate/Diff/Apply/Save/Run 或最终用户验收。完整原件、hash、oracle 和敏感运行记录保存在私有评估材料，不能复制进公开 PR。

## 回滚

先停止新的 AI 附件请求，取消活动 job 并释放本功能临时附件；移除附加 Compose 清单和 Control 转换 URL 后回退应用/组件。只清理本功能临时资产，不删除业务 PostgreSQL/Managed Input 卷、Token、Master Key 或历史日志。

文本与代码复用原附件 MIME/扩展名白名单，严格按 UTF-8 原件读取，每段最多 64Ki 字符并保留 1-based 段页目录。prepare 只返回首段，后续可按页读取到末段，不截断原件；字符偏移从 0 开始，结束偏移不包含在段中。`text/both` 返回 `source_origin=original_text` 的原文，不执行代码，不生成伪图/PDF；image/pdf 视图明确不支持。非法 UTF-8/NUL、空文本及原件超限继续返回明确错误。
