## Purpose

让编辑页默认优先展示代码，并提供不影响 Working Copy 语义的编辑器最大化能力，使依赖和凭据配置在需要时可展开而不持续挤占代码编辑空间。

## ADDED Requirements

### Requirement: 编辑页底部配置默认折叠且可独立切换

编辑页首次展示时，`Python 依赖` 与 `凭据绑定` 两个区域 MUST 默认处于折叠状态。用户点击各自标题后 SHALL 只切换对应区域的展开状态，再次点击 SHALL 折回；折叠状态不得删除、重置或改写任何配置值。

#### Scenario: 首次进入编辑页优先显示代码
- **WHEN** 用户打开一个 Adapter 的编辑页且没有本次页面会话的展开状态
- **THEN** Python 依赖和凭据绑定内容均不展开，代码编辑区获得主要可视垂直空间

#### Scenario: 用户独立展开和折叠配置
- **WHEN** 用户依次点击 Python 依赖或凭据绑定标题
- **THEN** 只有被点击的区域切换显示，另一分区状态和其中的值保持不变

### Requirement: 代码编辑区支持最大化与恢复

代码编辑区 MUST 提供带可访问名称的最大化/恢复图标控制。最大化 SHALL 扩展代码编辑区到当前工作台可用主区域，恢复 SHALL 返回原有编辑页布局；两者都 MUST 保持当前代码、dirty 状态、保存语义和当前 Adapter 不变，并在布局完成后恢复切换前的编辑器 selection/cursor 起止位置（line/column）和可见顶部行。像素级 `scrollTop` 因容器尺寸变化而改变时，顶部可见行仍 MUST 保持不变；该位置恢复 MUST 由自动化断言覆盖。

#### Scenario: 最大化和恢复保留编辑位置且不触发生命周期操作
- **WHEN** 用户记录当前 selection/cursor 的起止 line/column 与顶部可见行后点击最大化按钮，再点击恢复按钮
- **THEN** 布局完成后 selection/cursor 起止 line/column 和顶部可见行与记录值相同，且不创建 Revision、不保存、不运行、不修改运行状态或 Credential binding

#### Scenario: 最大化期间继续编辑
- **WHEN** 用户在最大化状态修改代码后恢复布局
- **THEN** 修改仍保留在同一个浏览器 Working Copy 中，dirty 标记和后续显式保存行为与最大化前一致
