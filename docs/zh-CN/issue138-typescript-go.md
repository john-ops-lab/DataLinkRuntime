# TypeScript 与 Go 适配器

工作台、模板广场、Clone 和 ZIP 导入导出支持 `typescript`、`go`。现有 17 个内置场景新增两种语言变体；模板是可编辑参考代码，复制后配置目标地址和凭据，再验证实际业务。

## 入口与工具链

TypeScript 保存原始源码，Worker 使用 TypeScript **5.8.3** 严格编译（`strict`、`noEmitOnError`），再由 Node 22 执行 ESM。错误日志通过 source map 指向 `adapter.mts`。可以使用全局 `Context`，或 `import type { Context } from "dlr"`：

```typescript
export function handle(context: Context, input: unknown) {
  context.logger.info("开始处理");
  return { input, configured: context.config.mode ?? null };
}
```

编辑器提供 TypeScript 和 DLR 基础类型；第三方包的完整类型与最终编译结果以 Worker 环境检查为准。npm 依赖每行填写 `package@1.2.3`，有独立声明包时同时填写对应 `@types/package@版本`。内置 npm 材料与 JavaScript 共用。

Go Worker 固定 **Go 1.27.1**，关闭自动工具链下载及 CGO。用户代码为单个 `package main` 源文件，平台提供入口程序，不编写 `main()`：

```go
package main

func Handle(ctx *Context, input any) (any, error) {
    var request struct { Name string `json:"name"` }
    if err := DecodeInput(input, &request); err != nil { return nil, err }
    ctx.Logger.Info("开始处理")
    return map[string]any{"name": request.Name}, nil
}
```

`ctx.Config` 为 `map[string]any`；`ctx.Secrets.Get("绑定键")` 返回字符串，缺少凭据返回空串；日志使用 `Info/Warn/Error`。`ctx.InputFiles` 的字段为 `Ordinal`、`Path`、`OriginalName`、`ContentType`、`SizeBytes`、`SHA256`。只从平台提供的路径读取文件。原始输入数字采用 `json.Number`，业务结构体可用 `DecodeInput` 解码。

Go 不提供 LSP、多文件编辑或 CGO。标准库之外的模块每行填写固定版本，如 `github.com/lib/pq@v1.10.9`，不接受 `latest`、本地路径或下载 URL。模块必须兼容当前固定工具链。

## Go 依赖源和离线材料

系统设置新增 Go Modules 类别，预置 `https://goproxy.cn`、`https://proxy.golang.org`，国内源初始为默认。源地址可包含认证信息；日志走现有凭据脱敏。兼容环境变量为 `DLR_GO_PROXY_URL`，系统中所选源优先。工具链不依赖所选模块源。

内置源接受标准 Go proxy 材料：

```text
example.com/sdk/@v/v1.2.3.mod
example.com/sdk/@v/v1.2.3.info
example.com/sdk/@v/v1.2.3.zip
```

保留完整相对路径；模块名大写字符使用 Go proxy 的 `!` 转义。`.mod` 模块声明、`.info` 版本和 ZIP 顶层目录必须与路径一致。可选目录上传，或以 `example.com/sdk/@v` 为前缀上传三个文件。

在联网准备机使用与 Adapter 相同的模块及工具链，运行 `go mod tidy`、`go mod download all`；从 `GOMODCACHE/cache/download` 收集所需直接和传递模块的 `.mod/.info/.zip`。上传时选中该目录作为根目录，保留其下模块路径。不要上传缓存锁、`list` 等其他文件；本平台只接收上述三类材料。

内置源在 Worker 上形成只读材料快照并使用本地 `file://` proxy。禁止公网模块回退、VCS、校验库和工具链下载；材料不足明确失败。材料的配额、摘要校验、占用保护及执行授权沿用内置依赖库。

## 构建与升级

两种语言均先构建再执行。成功构建原子写入版本缓存；源码、依赖、来源、工具链或平台类型变化会改变缓存身份，Go 还包含系统和架构。失败不生成可复用缓存。Go 依赖构建暂存上限为 1 GiB，编译并行数为 1，仍受 Attempt 的内存、CPU、进程和磁盘限制；大型 SDK 需要按实际开销配置资源和超时。编译日志、取消、超时与清理通过现有执行结果展示。

按数据库 → Control → Worker → Web 顺序升级，运行 `alembic upgrade head` 到 `0038_issue138_languages`。旧 Worker 不具备新语言能力，不会被选来运行新语言。降级前停止并排空新语言任务，备份并显式处理 TypeScript/Go 资产以及 Go 源和材料；迁移拒绝不兼容数据，不自动删除用户内容。

代表性 Kubernetes SDK 冷编译实测占用约 700 MiB，测试部署因此配置 1 GiB Attempt 内存，详见[验证记录](../issue138-validation.md)。
