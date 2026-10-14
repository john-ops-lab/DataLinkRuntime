# 工作区导航与表单可访问性（#199–#201）

Task 当前输入尚未保存（JSON、输入来源、托管文件、保留期限或 STAGED 上传）时，进入模板广场前会沿用离开提醒。取消后草稿与已保存输入保持；确认进入后不自动保存、绑定或删除文件，返回仍遵循暂存恢复合同。已保存且无输入草稿时正常导航。模板广场保留代码工作区挂载，因此单纯代码草稿仍可直接浏览模板；真正离开工作区的系统设置、系统状态与浏览器历史导航复用完整 guard，不重复确认。

新建适配器使用现有 DrawerForm：打开时名称字段获得初始焦点，Tab/Shift+Tab 在可见控件首尾循环，Escape、关闭和取消返回新建入口。库本身的焦点约束与恢复继续保留；取消不创建适配器。

凭据名称或当前类型的必填字段为空时，错误说明显示在弹层内，并通过稳定 ID 与输入的 aria-invalid、aria-describedby 和 aria-errormessage 关联。修正、类型切换、关闭或重开会清除相关过期状态。服务端校验和创建前确认保留；取消不提交草稿。

回归入口：Node 24 下运行 Web unit tests；`npm run test:browser -- --grep 'create Adapter initial focus|credential required field association'` 使用合成 API fixtures 检查中英文真实键盘和计算后的可访问描述，不连接部署、不创建持久对象。业务部署上的 Chrome/AX 与独立状态验收由主 Agent 在集成 SHA 上执行；单元测试和隔离浏览器回归不替代该验收。
