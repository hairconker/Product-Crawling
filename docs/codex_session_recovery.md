# Codex Desktop 会话导出与恢复

本工具用于处理 Codex Desktop 切换账号、切换 API/custom provider、升级或索引异常后，旧聊天记录仍在磁盘但侧边栏不显示的问题。

## 文件

- `scripts/export_codex_sessions.py`：只读导出 `.codex` 会话为 Markdown。
- `scripts/restore_codex_sessions.py`：把缺失会话重新合并到当前 Codex Desktop 状态库。
- `.agents/skills/codex-session-export/`：同样功能的 Codex Skill，内置两份脚本。

两份脚本只使用 Python 标准库。

## 只读导出

列出可导出的 projects：

```powershell
python scripts\export_codex_sessions.py --list-only
```

导出所有当前和备份会话：

```powershell
python scripts\export_codex_sessions.py --output codex-session-export --copy-raw
```

只导出某个 project：

```powershell
python scripts\export_codex_sessions.py --project-filter "E:\work\商店小程序" --output codex-session-export-shop --copy-raw
```

输出结构：

- `index.md`：总索引。
- `index.json`：机器可读索引。
- `projects/<project-slug>/index.md`：单个 project 的索引。
- `projects/<project-slug>/*.md`：可读聊天记录。
- `projects/<project-slug>/raw/*.jsonl`：原始 JSONL 副本，仅在 `--copy-raw` 时生成。

## 恢复到 Codex Desktop 侧边栏

恢复脚本默认 dry-run，不写入：

```powershell
python scripts\restore_codex_sessions.py
```

确认 dry-run 的缺失记录和 projects 正确后再应用：

```powershell
python scripts\restore_codex_sessions.py --apply
```

应用后再次确认没有缺失记录：

```powershell
python scripts\restore_codex_sessions.py
```

如果 Codex Desktop 侧边栏没有刷新，完全退出并重新打开 Codex Desktop。

## API/custom 模式可见副本

Codex Desktop 在 custom/API provider 模式下可能只显示 `model_provider = custom` 的线程。为了不破坏切回原 provider 后的旧记录，可以生成 API 可见副本：

```powershell
python scripts\restore_codex_sessions.py --api-visible-copy
```

确认 dry-run 后应用：

```powershell
python scripts\restore_codex_sessions.py --api-visible-copy --apply
```

该模式不会修改原始 `openai` 线程，而是：

- 为每条旧线程生成一个合法 UUID 形态的新线程 id。
- 复制原 JSONL，并把副本 JSONL 内部的 `session_meta.id`、`thread_id` 等字段改为新 id。
- 将副本写为 `model_provider = custom`。
- 在副本标题后追加 `[API可见副本]`。
- 在 `thread_source` 记录 `api-visible-copy:<原始id>`，方便审计和清理。

这样 API/custom 模式能看到副本；切回原 provider 时，原始旧记录仍然存在。

如果 `--api-visible-copy` 显示 `0 missing sessions selected`，但左侧仍然缺一部分旧记录，说明副本已经创建过，问题通常是副本的 `cwd`、标题、`thread_source` 或 JSONL 元数据还停在半转换状态。此时运行修复模式：

```powershell
python scripts\restore_codex_sessions.py --repair-api-visible-copies
```

确认 dry-run 输出后应用：

```powershell
python scripts\restore_codex_sessions.py --repair-api-visible-copies --apply
```

该模式只修复由原始 `openai` 记录推导出的对应 `custom` 副本，不修改原始 `openai` 记录，也不会动 API/custom 模式下新建的其他对话。

## 恢复行为

`restore_codex_sessions.py --apply` 会：

- 读取当前 `%USERPROFILE%\.codex\state_5.sqlite`。
- 读取历史来源：`sessions/`、`sessions.bak/`、`archived_sessions.bak/`、`session_index.jsonl.bak`、`state_5.sqlite.bak`。
- 只插入当前 `threads.id` 不存在的记录。
- 把缺失 JSONL 复制到当前 `sessions/YYYY/MM/DD/`。
- 只追加缺失 id 到 `session_index.jsonl`。
- 写入前备份当前文件到 `.codex/restore_backups/<timestamp>/`。

它不会覆盖当前 API/custom provider 模式已经创建的对话。

## 常用参数

```powershell
python scripts\restore_codex_sessions.py --project-filter "E:\AI\code"
python scripts\restore_codex_sessions.py --title-filter "无盘启动"
python scripts\restore_codex_sessions.py --limit 5
python scripts\restore_codex_sessions.py --codex-home "D:\backup\.codex"
```

## Skill 使用

Skill 路径：

```text
.agents/skills/codex-session-export/
```

安装到全局 Codex Skill 目录时，复制整个目录到：

```text
%USERPROFILE%\.codex\skills\codex-session-export\
```

Skill 内脚本也可直接运行：

```powershell
python .agents\skills\codex-session-export\scripts\export_codex_sessions.py --list-only
python .agents\skills\codex-session-export\scripts\restore_codex_sessions.py
```

## 本地网页控制台

启动本地控制台：

```powershell
python scripts\codex_recovery_web.py --port 8765
```

然后打开：

```text
http://127.0.0.1:8765/
```

控制台会显示当前识别到的 provider 数量、项目列表、普通路径/扩展路径数量、待创建 API 副本数量、待修复副本数量，并提供导出、预检和应用按钮。服务只监听 `127.0.0.1`，按钮只调用白名单脚本，不接受任意命令。

如果应用修复后再次扫描又变回待修复状态，通常是 Codex Desktop 仍在运行并把内存旧状态写回了 `state_5.sqlite`。此时应完全退出 Codex Desktop，再执行网页里的应用按钮或命令行脚本。

## 验证

```powershell
python -m py_compile scripts\export_codex_sessions.py scripts\restore_codex_sessions.py
python scripts\export_codex_sessions.py --list-only
python scripts\restore_codex_sessions.py
```

## 安全注意

导出的 Markdown 和 raw JSONL 可能包含：

- 本机路径和项目名。
- shell 命令输出。
- 工具调用日志。
- 用户粘贴过的账号、密钥、cookie 或服务器信息。

`codex-session-export*/` 已加入 `.gitignore`，不要提交导出结果。
