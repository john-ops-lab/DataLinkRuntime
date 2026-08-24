## Purpose

移除 Adapter Catalog 标题下的冗余常驻说明，使搜索筛选区域上移并减少空白，同时保留 Catalog 的主要导航和操作入口。

## ADDED Requirements

### Requirement: Catalog 不展示常驻 overview 说明

Adapter Catalog MUST 保留“适配器”标题、新建、刷新和帮助入口，但 MUST NOT 渲染 `catalog.overview` 常驻说明文案。说明移除后搜索、筛选和列表区域 SHALL 直接占据释放的布局空间，不得产生由该说明留下的异常空白或额外间距。

#### Scenario: Catalog 首次展示
- **WHEN** 用户打开 Adapter Catalog
- **THEN** 标题和既有操作入口可见，`catalog.overview` 文案不可见，搜索/筛选区域紧接标题区布局

#### Scenario: Catalog 操作不受影响
- **WHEN** 用户执行搜索、筛选、新建、刷新或帮助操作
- **THEN** 对应操作和 Adapter 列表行为与改动前一致，只有常驻说明被移除
