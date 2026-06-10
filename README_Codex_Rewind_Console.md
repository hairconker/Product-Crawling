# Codex 回溯台

Codex 回溯台是一个本地会话恢复工具，用来处理 Codex Desktop 切换账号、切换 API/custom provider、升级或项目索引异常后，旧聊天记录还在磁盘里但左侧不显示的问题。

它包含命令行脚本、网页控制台和 Codex Skill。

## 功能

- 扫描当前 Codex Desktop 会话数据库和历史 JSONL 会话文件。
- 将隐藏或缺失的会话导出为 Markdown。
- 生成 API/custom 模式可见副本，同时保留原始 `openai` 记录。
- 修复已经创建但处于半转换状态的 API 可见副本。
- 按项目选择要修复的范围，避免全局误操作。
- 一键备份当前 Codex 状态文件。
- 提供本地网页控制台：`http://127.0.0.1:8765/`。
- 只使用 Python 标准库，不需要安装第三方依赖。

## 安全设计

- 历史会话源文件按只读处理。
- 不修改原始旧 provider 记录。
- 不覆盖当前 API/custom 模式中新建的对话。
- 所有写入操作前都会备份到：

```text
%USERPROFILE%\.codex\restore_backups\
```

- 网页服务默认只监听 `127.0.0.1`。
- 网页按钮只调用白名单脚本，不接受任意命令执行。
- 导出的 raw JSONL 可能包含本地路径、命令输出、工具日志或敏感信息，不建议提交到 GitHub。

## 快速开始

克隆仓库：

```powershell
git clone git@github.com:hairconker/Codex-Rewind-Console.git
cd Codex-Rewind-Console
```

启动网页控制台：

```powershell
python scripts\codex_recovery_web.py --port 8765
```

打开：

```text
http://127.0.0.1:8765/
```

## 网页控制台能做什么

网页会显示：

- `openai` 原始记录数量。
- `custom/API` 当前记录数量。
- 待修复 API 副本数量。
- 待创建 API 副本数量。
- 每个项目的普通路径和扩展路径数量。
- 项目名称、项目路径，以及它是否对应 Codex 左侧项目。

网页按钮支持：

- 备份当前状态。
- 预检副本修复。
- 应用副本修复。
- 预检选中项目修复。
- 应用选中项目修复。
- 创建 API 可见副本。
- 导出全部会话。
- 导出单个项目会话。

## 命令行用法

查看当前状态：

```powershell
python scripts\restore_codex_sessions.py --report
```

导出全部会话为 Markdown，并复制 raw JSONL：

```powershell
python scripts\export_codex_sessions.py --output codex-session-export --copy-raw
```

只创建备份，不修改任何数据：

```powershell
python scripts\restore_codex_sessions.py --backup-only
```

预检 API 可见副本修复：

```powershell
python scripts\restore_codex_sessions.py --repair-api-visible-copies
```

应用修复：

```powershell
python scripts\restore_codex_sessions.py --repair-api-visible-copies --apply
```

只修复某个项目：

```powershell
python scripts\restore_codex_sessions.py --repair-api-visible-copies --project-exact "E:\biji"
python scripts\restore_codex_sessions.py --repair-api-visible-copies --project-exact "E:\biji" --apply
```

## 重要使用流程

Codex Desktop 正在运行时，可能会把内存里的旧状态重新写回 `state_5.sqlite`。因此最终应用修复时建议：

1. 先在网页或命令行里查看状态。
2. 点击“备份当前状态”或运行 `--backup-only`。
3. 完全退出 Codex Desktop。
4. 再执行应用修复。
5. 重新打开 Codex Desktop 查看左侧记录。

如果应用后再次扫描又变回待修复状态，通常说明 Codex Desktop 还没有完全退出。

## Codex Skill

Skill 位于：

```text
.agents/skills/codex-session-export/
```

安装到全局 Skill 目录：

```text
%USERPROFILE%\.codex\skills\codex-session-export\
```

安装后，Codex 在遇到“聊天记录消失、API 模式隐藏记录、导出/恢复会话”等任务时可以自动加载该 Skill。

## 仓库结构

```text
codex_recovery_dashboard.html
scripts/
  codex_recovery_web.py
  export_codex_sessions.py
  restore_codex_sessions.py
.agents/
  skills/
    codex-session-export/
docs/
  codex_session_recovery.md
```

## 不要提交的内容

不要提交导出的会话内容或 raw JSONL。它们可能包含：

- 本地路径。
- shell 命令输出。
- 工具调用日志。
- 用户粘贴过的账号、密钥、cookie 或服务器信息。

仓库中的 `.gitignore` 已经排除了常见导出目录和备份文件。
