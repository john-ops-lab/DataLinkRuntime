## Purpose

定义实时与历史 Execution 输出展示的共同判别语义，使合法 JSON null 与明确没有正文、历史信息不足分别可见，同时保留非空 JSON、截断预览和已有输出操作，避免界面为缺失记录虚构输出内容。

## ADDED Requirements

### Requirement: 输出展示依据正文和可靠元数据
系统 SHALL 按截断、实际非 null 正文、可靠完整 null、明确无输出和信息不足区分输出；MUST NOT 使用 succeeded、其他状态或 truthy/falsy 替代存在性证据。

#### Scenario: 完整 JSON null
- **WHEN** `output=null` 且可靠的完整输出大小等于 JSON null 的字节数、未截断且无矛盾元数据
- **THEN** 实时和历史均显示 `null`

#### Scenario: 非 null 正文没有历史大小
- **WHEN** 已有正文为 false、0、空字符串、空数组、空对象或其他 JSON，历史大小字段缺失
- **THEN** 页面按实际值展示，不因缺少大小隐藏正文

#### Scenario: 明确不存在正文
- **WHEN** 记录具有可验证且相互一致的无正文依据
- **THEN** 页面显示无输出，不生成字符串 null

#### Scenario: 历史信息不足或矛盾
- **WHEN** output 为 null/undefined，存在性或大小缺失、无效、矛盾且没有其他可靠证据
- **THEN** 页面显示中性的信息不足提示，不把它认定为合法 null 或确定的无输出，不改写该记录

#### Scenario: 截断输出优先
- **WHEN** 记录带截断标记
- **THEN** 页面保留原大小/预览分支，不被 null 或历史兼容分支吞掉

### Requirement: 输出操作与显示共享语义
已有复制/下载能力 SHALL 与真实可用正文一致；本修复 MUST NOT 新增无关操作或批量制造历史大小、存在性标志或正文。

#### Scenario: 未知输出复制下载
- **WHEN** 用户查看信息不足的历史输出并使用已有相关操作
- **THEN** 操作不凭空生成 null 内容，历史记录保持原值

#### Scenario: 合法值操作
- **WHEN** 已有操作处理合法 null 或其他完整 JSON
- **THEN** 结果与原始 JSON 值一致，截断数据仍遵守原有能力边界
