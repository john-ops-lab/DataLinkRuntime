# Proposal
## Why
#199 顶栏模板导航遗漏未保存输入提醒，#200 新建适配器初始键盘落点不可见，#201 凭据必填错误未关联字段。修复三个可观察 UI 缺陷，保留已保存输入与取消合同。
## What Changes
- 导航离开工作区复用现有 dirty/STAGED guard，取消保留状态，无草稿正常导航。
- 新建适配器初始焦点到名称，前向/反向 Tab 在可见控件循环，保留 Escape/关闭恢复。
- 凭据名称及类型必填字段错误在 dialog 内显示并通过 aria-invalid/description 关联，修正与关闭清除过期状态。
- 非目标：后台校验、输入持久化、自动保存/删除、运行锁、UI框架升级、#129。
## Capabilities
### New Capabilities
- `workbench-accessibility`: 新建表单的初始焦点、可见键盘循环、必填错误关联。
### Modified Capabilities
- `managed-input-web`: STAGED 草稿在顶栏与浏览器导航中复用工作区离开提醒。
## Impact
Web App、AdapterCatalog 和 CredentialsPanel，相关测试与说明。无 API、数据库、部署或依赖变化，代码回滚即可。
