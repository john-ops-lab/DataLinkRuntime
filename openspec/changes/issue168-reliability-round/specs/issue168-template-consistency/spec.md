## Purpose

本合同确保模板广场原样复制后的静态模板按实际发布依赖和 Runtime 编译、运行，并明确 JSON 映射的五语言共享规则，使相同输入获得同一完整业务结果，避免以执行状态成功或分语言预期掩盖数据差异。

## ADDED Requirements

### Requirement: Java S3 template compiles against declared SDK
Java S3 模板 SHALL 使用其声明 SDK 的真实公共 API，原样复制后冷编译并完成合成只读目标的列举/读取；SDK 安装、编译和业务内容验证 MUST 独立记录。

#### Scenario: The declared builder API is not nested
- **WHEN** 固定基线模板引用不存在的嵌套 Builder 类型
- **THEN** 修复 SHALL 仅采用对应 SDK 提供的构造器 API，不通过新增 Runtime 方法或无必要升级 SDK 消除编译失败

### Requirement: Mapping semantics are declared once for five languages
五语言 JSON 映射 SHALL 使用一份共享规则：排序区分大小写、按 Unicode 码点比较并保持稳定性，不依赖 locale；null/boolean 转字符串为 `null`、`true`、`false`。非 ASCII 及补充平面字符 MUST 遵循同一码点顺序。未变更的输入校验、过滤、去重和资源限制保持，不能修改用例为分语言预期。

#### Scenario: Sorting and string conversion diverge
- **WHEN** 相同输入在五语言中产生不同顺序或 null/boolean 字符串
- **THEN** SHALL 先形成共享规则和独立 oracle，再分别执行原样模板核对完整 JSON，不能仅断言 succeeded
