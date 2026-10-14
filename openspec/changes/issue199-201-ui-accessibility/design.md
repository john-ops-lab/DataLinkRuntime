# Design
## Context
见 proposal.md。App 已有工作区与设置页 guard，Task 已把 STAGED 纳入 dirty；顶栏与 native browser blocker 未复用工作区。AdapterCatalog 使用 DrawerForm，rc-drawer 的隐藏 sentinel 可成为稳定 Tab 落点。凭据表单手动提交只显示外层错误。
## Goals / Non-Goals
仅改变三个 UI 入口行为；不扩展运行锁、不保存或删除暂存文件，不改变后端/API或全局弹层。
## Decisions
- 顶栏 Gallery 和 native Back/Forward 进入 Gallery 复用专用 Input Object guard（输入来源、JSON、文件/retention、STAGED），保留代码工作区挂载与代码草稿浏览无 confirm 的已有合同。真正卸载工作区的 system settings/status 及对应 native 导航使用完整工作区 guard，代码和输入合并只确认一次；设置页内切换不重复提示。
- 新建适配器用 InputRef 和 afterOpenChange 显式聚焦名称，保留 restoreFocus。仅在可见首尾控件的 Tab 边界拦截并转到相对端，其余顺序交给浏览器；不移除 rc-drawer 原生焦点约束或 sentinel。
- 凭据表单维护按字段的错误对象，稳定 ID、aria-invalid/describedby/errormessage 和 dialog 内 role=alert。修正字段、改类型、打开/关闭时清除相关错误；后台校验和现有创建确认继续保留。
- 使用 Ant Design 5.29.3 的 Drawer/Input 契约；不升级、不新增 UI 框架或依赖。
## Risks / Trade-offs
隐藏/禁用元素影响循环 → 仅选择可见可操作控件并以真实浏览器 Tab 检查；设置路径重复提示 → 按目标路由判断实际工作区离开；过期字段错误 → 输入修改与关闭时清除。
## Migration Plan
无迁移；回滚 UI 提交即可。
