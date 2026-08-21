# Direct Thread Delete

[English](../README.md)

Direct Thread Delete 是一个本地 Codex 插件，用于按项目浏览、筛选、批量选择并永久删除 Codex 任务，同时让 Codex Desktop 侧边栏保持同步。

## 功能

- 按项目将任务组织成可展开、收起的目录树。
- 支持搜索、状态筛选、项目筛选、分页和每页数量设置。
- 每行提供独立删除按钮。
- 支持多选、项目内全选和跨页面批量删除。
- 同时列出活动任务和已归档任务。
- 支持按 UUID 精确删除。
- 删除后复查目标是否已经从本地任务存储中消失。
- 管理器和后台进程均在本机运行，页面只绑定回环地址。

## 删除与侧边栏同步原理

用户只需在管理器中确认一次。对于活动任务，插件会先通过 Codex Desktop 的任务归档能力同步移除侧边栏条目，再通过受支持的 `thread/delete` app-server 方法永久删除任务。已经归档的任务会直接进入永久删除阶段。

这个内部归档步骤只是同一次删除操作中的侧边栏同步阶段，不需要用户手动点击“归档”。如果同步阶段失败，后端不会继续永久删除。

## 安装

在目标设备上执行：

```powershell
codex plugin marketplace add Lucency09/direct-thread-delete
codex plugin add direct-thread-delete@lucency09
```

安装后重启 Codex Desktop，新建一个任务，然后输入 `@Direct Thread Delete`。插件会直接启动并打开会话管理器。

## 更新

仓库更新后，在目标设备执行：

```powershell
codex plugin marketplace upgrade lucency09
codex plugin add direct-thread-delete@lucency09
```

随后重启 Codex Desktop，并在新任务中使用新版插件。

## 使用说明

1. 在一个专门用于托管管理器的 Codex 任务中输入 `@Direct Thread Delete`。
2. 等待页面显示“侧栏同步已连接”。协调器未连接时，删除按钮会保持禁用，防止产生侧边栏残留。
3. 搜索或展开项目，选择需要删除的任务。
4. 核对确认窗口中的目标，再执行永久删除。
5. 完成后点击“关闭管理器”，同时关闭页面、协调器和本地后端。

不要删除当前正在托管管理器的任务。插件会保护启动管理器的任务，但仍建议使用单独任务进行管理。

## 运行环境与依赖

- 面向 Codex Desktop 与其本地 `codex` CLI。
- 当前版本主要在 Windows Codex Desktop 上验证。
- Python 实现仅使用标准库，不依赖 PyYAML 或其他第三方 Python 包。
- 如果系统没有注册 `python` 或 `py -3`，插件可使用 Codex Desktop 提供的 Python 运行时。
- 同时支持 ChatGPT 账号登录和 API Key 登录模式；插件不会读取或修改 `auth.json`。

首次运行时，Codex 可能要求授权插件访问当前 Windows 用户的原生 `.codex` 状态目录和临时目录。该权限用于受支持的任务列表与删除协议，不包含读取凭据。

## 安全设计

- 永久删除前必须在管理器中进行精确目标确认。
- 批量删除请求中的目标列表与确认列表必须完全一致。
- 侧边栏同步失败时终止删除，不留下半完成状态。
- 当前管理任务受到保护。
- 不直接删除 JSONL 文件，也不修改 Codex SQLite 数据库。
- 不读取、复制或修改身份验证凭据。
- 后端只监听本机回环地址，并使用临时 bearer token 协调管理页面。
- Windows 后台进程使用无控制台窗口模式运行。

## 卸载

```powershell
codex plugin remove direct-thread-delete@lucency09
codex plugin marketplace remove lucency09
```

卸载插件不会恢复此前已经永久删除的任务。

## 仓库结构

```text
direct-thread-delete/
├─ .agents/plugins/marketplace.json
├─ docs/README.zh-CN.md
└─ plugins/direct-thread-delete/
   ├─ .codex-plugin/plugin.json
   └─ skills/direct-thread-delete/
      ├─ SKILL.md
      ├─ assets/
      └─ scripts/
```
