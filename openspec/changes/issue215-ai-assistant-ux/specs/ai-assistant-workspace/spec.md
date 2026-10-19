# Spec Delta

## Purpose

为管理员提供以对话和代码审查为中心的 AI 助手工作区，使附件、输入和发送操作清晰可见，并允许按需检查发送内容及操作说明，同时保持现有临时对话与人工应用的产品边界。

## ADDED Requirements

### Requirement: Compact composer and on-demand details

AI 助手 SHALL 在输入工具栏仅显示附件与发送（等待时为停止等待），通过顶部更多菜单提供查看发送内容和使用说明。帮助与预览 SHALL 在独立浮层呈现，不改变对话和输入区尺寸。发送内容预览 SHALL 跟随当前代码、配置、片段、近期对话与待发送附件，且不暴露凭据真值。

#### Scenario: Review current outgoing contents
- **WHEN** 用户在更多菜单选择查看发送内容并修改当前编辑草稿或附件
- **THEN** 预览反映当前待发送内容，关闭后返回更多按钮，输入草稿不丢失且不触发请求

#### Scenario: Read help
- **WHEN** 用户打开使用说明
- **THEN** 可读到人类可读的附件类型与实时限制、模型能力限制、临时聊天生命周期和人工应用流程，不显示 MIME 字符串

### Requirement: Responsive readable assistant

AI 助手 SHALL 在中英文 1280、1440、1920 宽度的普通、最大化及恢复状态保持可阅读可操作。侧栏 SHALL 为代码编辑保留主要空间，最大化 SHALL 限制对话与输入的阅读宽度。模型及临时对话状态 SHALL 在非空对话中保持可见。长输入与附件 SHALL 独立限高滚动，不挤走发送工具栏。

#### Scenario: Keep primary operations visible
- **WHEN** 用户添加多个附件、输入长文本、打开更多或详情并切换最大化
- **THEN** 发送操作保持可见，关闭详情后仍保留原始草稿、附件和消息

### Requirement: Preserve review and temporary conversation

系统 SHALL 保留片段添加与删除、附件准备与错误恢复、重试、重新生成、代码预览和人工 Diff／Apply。布局切换和收起重开 SHALL 保留临时状态；刷新、离开页面及切换账号或适配器 SHALL 按现有规则清空。代码建议 SHALL 以独立卡片呈现审查入口，不能自动应用、保存或运行。

#### Scenario: Review generated code
- **WHEN** 模型返回代码建议
- **THEN** 用户可预览代码及差异，只有明确应用才更新当前编辑草稿，保存和运行继续单独操作
