# Template Variant 成熟度与 Receipt

成熟度属于单个 `scenario_slug + version + language + source_sha256`，不是整个 Scenario 的营销标签。Python 通过验证不会自动提升 JavaScript 或 Java；同一语言旧版本的结果也不能证明新版本。

## 四级定义

| 值 | 用户可见含义 | 最低证据 |
|---|---|---|
| `reference-generated` | 实验 / 未验证 | 依据固定来源独立编写，但尚无满足下一等级全部门禁、且与当前源码哈希匹配的 Receipt；允许执行窄 smoke 或安全 canary，它们本身不构成升级证据 |
| `syntax-verified` | 语法已验证 | 精确 requirements 已被对应 Worker parser 接受，依赖可解析，并完成 Python compile、`node --check` 或 Java Runtime API compile |
| `fixture-verified` | Fixture 已验证 | 满足 syntax，并由固定响应或本地 fake service 直接执行目录中发布的同一份源码及主要成功、上限和失败路径 |
| `live-verified` | 真实只读环境已验证 | 满足 fixture，并在受控真实外部服务中使用最小只读权限执行，记录脱敏环境范围、时间和结果 |

“支持”的来源矩阵行表示公开操作或协议事实已核对，不等于 fixture 或 live 验证。

## Receipt 绑定

每个 Variant 固定一个资源：

```text
receipts/{scenario_slug}/{language}.json
```

Receipt 必须包含：

- `scenario_slug`；
- `version`；
- `language`；
- `source_sha256`；
- `behavior_contract_version`；
- `maturity`；
- `evidence`；
- `verified_at`。

这些字段必须与 metadata 完全一致。代码字节变化会改变 SHA-256，并使旧 Receipt 失效。

`reference-generated` 的 Receipt 必须：

```json
{
  "maturity": "reference-generated",
  "evidence": [],
  "verified_at": null
}
```

它是“没有满足升级门禁、且与当前源码哈希匹配的 evidence/Receipt”的机器可读声明，不是否认窄 smoke 已执行，也不是验证等级通过记录。

## 证据递进

- `syntax-verified`：至少一条 `kind=syntax` 且 `result=passed`；
- `fixture-verified`：同时包含 `syntax` 和 `fixture`；
- `live-verified`：同时包含 `syntax`、`fixture` 和 `live`；
- 非 `reference-generated` 必须有 `verified_at`；
- evidence 命令必须精确、可重现且不含 Secret、本机私有路径或真实账号；
- 测试跳过、缺少依赖、只检查另一份辅助实现或只做静态审阅，都不能作为 passed。

如果声明级别所需门禁失败或缺失，发布必须失败，或先把 metadata 与 Receipt 一并降到真实级别。

## Fixture 真实性

Fixture harness 必须直接加载发布资源：

```text
variants/{scenario_slug}/python.py
variants/{scenario_slug}/javascript.mjs
variants/{scenario_slug}/java.java
```

不得维护第二份“等价实现”来替代。日志应能关联 Scenario、版本、语言和 source hash，并让失败指向实际文件。

云或 CMDB sync 的 fixture 还必须证明：

- 相同 `scan_id` 与相同批次重复执行不产生第二套业务对象；
- 相同幂等键、不同 payload 返回冲突；
- 任何来源或批次失败均不调用 finish；
- preview 零目标写入；
- Output 只含有界摘要。

## Gallery 展示

详情按三种语言分别显示真实成熟度。Scenario 卡片取三语言中最低级别作为汇总：

```text
reference-generated < syntax-verified < fixture-verified < live-verified
```

`reference-generated` 必须显示“实验 / 未验证”等文字，不能只靠颜色。用户切换语言时，代码、requirements、安装说明、Runtime 建议、来源和成熟度必须一起切换。

## 当前状态

首批 51 个 Variant 在创建静态资产时全部标为 `reference-generated`。本次执行的若干窄 smoke 与安全 canary 不覆盖依赖安装、完整场景 fixture 或真实外部服务，因此不升级成熟度。只有后续对应等级的全部门禁实际成功并产生匹配当前源码哈希的 Receipt，才能逐语言升级；未进行真实外部服务调用时不得出现 `live-verified`。
