# JavaScript Excel 依赖评审

核对日期：2026-09-05。

## 决定

JavaScript Excel Variant 暂定精确依赖：

```text
@e965/xlsx@0.20.3
```

不使用旧的 `xlsx@0.18.5`。选择项仍保持 `reference-generated`，不能因为完成本页评审就升级成熟度。

## Registry 事实

只读查询：

```bash
npm view @e965/xlsx@0.20.3 name version license repository.url dist.tarball dist.integrity time engines --json
npm view @e965/xlsx@0.20.3 description keywords dependencies optionalDependencies --json
```

返回的关键事实：

| 字段 | 值 |
|---|---|
| name / version | `@e965/xlsx` / `0.20.3` |
| registry license | `Apache-2.0` |
| repository | `https://github.com/e965/sheetjs-npm-publisher.git` |
| publish time | `2024-07-19T00:56:04.420Z` |
| tarball | npm registry 的 scoped package tarball |
| integrity | `sha512-703RN/3OdsRD5mtse2HBX7Um7xwaP9tlswEG6svOtjqokXoX7rJdQj7DyabD2I+xk22RgaIIU+R6BHgkpZGB/w==` |
| description | SheetJS Spreadsheet data parser and writer |
| advertised keywords | 包含 `xls` 与 `xlsx` |
| registry dependencies | 未声明 dependencies 或 optionalDependencies |

这些信息说明同一个包公开声明能够处理 XLS 与 XLSX，并提供固定 tarball 完整性值；它们不能替代实际安装、安全扫描或格式安全证明。首版 Recipe 同时开放 XLSX 与 XLS，但只有 XLSX 能获得独立 OOXML package 预检；XLS 始终保持 data-only、实验边界。

## 来源与维护判断

该 scoped 包指向第三方 npm publisher 仓库，不是 SheetJS 上游当前官方 npm 发布通道。以核对日计算，0.20.3 已超过两年没有新发布。即使 registry 声明 Apache-2.0，也需要在发布门禁中核对 tarball 内许可证、NOTICE、源码对应关系和完整性。

因此：

- 只允许精确版本，不允许 `latest`、范围或未锁定版本；
- 不把“无外部 dependencies”解释为无安全风险，主要解析代码可能打包在 tarball 内；
- 不把 registry keywords 解释为真实双格式兼容证明；
- 安全公告、维护活跃度或许可证证据无法确认时，保持实验标签或更换实现；
- 未经新的 OpenSpec 决策，不自动回退到旧 `xlsx@0.18.5`。

## 计划 API 与活动内容边界

Variant 在独立 OOXML ZIP 预检通过后使用库入口读取 XLSX；旧版 XLS 则直接进入相同库的离线 data-only 读取路径：

```javascript
read(buffer, {
  type: "buffer",
  cellFormula: true, // detect formula cells so Recipe can replace them with null
  cellHTML: false,
  cellNF: false,
  bookVBA: true,
  bookFiles: true
})
```

这些选项用于识别公式并减少 HTML 和格式元数据暴露，但不会执行公式、宏或网络访问，也不能单独证明所有活动内容都安全。Recipe 仍须执行以下门禁：

- 仅接受明确的 `.xlsx` 与 `.xls`，其他扩展名在解析器调用前拒绝；
- 文件字节、行、列、字段和总输出有正整数上限；
- XLSX ZIP member 数、单 member 与膨胀总量受限，发现 VBA、宏启用格式、外部 relationship 或加密标记时拒绝；
- XLS 使用解析器的离线 data-only 路径；不创建公式求值器，不执行 VBA，也不跟随外部关系；
- 不调用公式计算器，不跟随外部链接，不访问网络；
- 不把公式文本、外部路径或原始解析异常写入普通日志；
- XLS 无法获得与 OOXML 等价的完整活动内容预检，因此必须保持 `reference-generated`，不能把“未执行”写成“已检测并拒绝”。

旧二进制 XLS 的宏、嵌入对象、外部关系和加密识别尚无三语言一致且可验证的解析前门禁。当前支持面只承诺离线读取存储值且不调用执行引擎；它不是活动内容扫描器，也不据此升级成熟度。

## 发布前强制门禁

只有以下条件全部完成后，才可考虑提升为 `syntax-verified` 或 `fixture-verified`：

1. Worker 的 JavaScript requirements parser 接受精确 scoped package；
2. 从 registry 安装固定版本并核对 lockfile、tarball integrity、包内 LICENSE 和 NOTICE；
3. 运行项目采用的依赖安全扫描，并记录工具、数据库时间与结果；
4. `node --check` 或等价 Runtime 编译门禁直接检查发布 Variant；
5. 同一发布源码成功读取真实的最小 XLSX 与 XLS；
6. fixtures 覆盖 sheet、range、header、空值、文件/行/列/输出上限；
7. fixtures 证明公式不计算、宏不执行、外部关系不跟随；
8. 覆盖加密工作簿、损坏文件、ZIP 膨胀、OOXML 活动内容，以及旧版 XLS 不创建公式/宏执行路径和不访问外链的行为；
9. Receipt 绑定当前 `source_sha256`，不能复用其他实现或其他语言的结果。

当前尚未完整执行上述 1–9 全部门禁，也没有完成匹配当前发布源码的真实 XLSX/XLS 双格式 fixture，因此 JavaScript Excel Variant 必须继续显示“实验 / 未验证”。已执行的静态检查或窄安全 canary 不构成升级证据。
