---
name: github-reuse
description: 实现新功能、接入新平台、加新组件前，先在 GitHub 搜现成代码复用，拒绝从零重写。用户说"实现"、"加功能"、"接入 xxx 网站"、"写一个 xxx"、"做一个 xxx"、"支持 xxx" 时自动加载。
allowed-tools: WebSearch, WebFetch, Bash, Read, Grep, Glob, Edit, Write
---

# 从 GitHub 复用代码 · 铁律

> 背景：本项目过去踩过"明明 GitHub 有 11k stars 的成熟库，却从零重写"的坑。
> 该 skill 是为避免同类错误设立。**任何"实现/接入"类任务，先搜，再写。**

## 一、动手前必做（先搜，再写）

遇到"实现 / 加 / 接入 / 支持 / 做一个 …" 类需求，**禁止**直接落第一行代码。

### Step 1 搜索（至少 3 次不同查询）

用 `WebSearch` / `WebFetch` / `gh` CLI：

```
site:github.com <领域> <语言> <关键词>
<关键词> python library 2025
<关键词> 爬虫 github 最新
```

关键词变换：中英各搜一次，加年份过滤近 6 个月更新的。

### Step 2 候选筛选（最少 3 个）

用统一评分表，高的排前：

| 指标 | 说明 | 不及格线 |
|---|---|---|
| ⭐ Stars | 垂直领域相对值 | < 100 慎用 |
| 最近 commit | `git log` 或 GitHub 主页日期 | > 6 个月扣分 |
| Issues 活跃 | 近 30 天有回复 | 全无响应淘汰 |
| License | MIT/Apache2/BSD 可复用；GPL 谨慎 | 缺 license 淘汰 |
| README 质量 | 有安装/调用示例 | 空 README 淘汰 |
| 核心文件可读 | 入口文件 < 2000 行 | 混乱架构扣分 |

把**每个候选**的 `repo_url / stars / last_commit / license / 一句话评价` 表格写到对话里，**让用户看到你筛选过**。

### Step 3 决定

三种结论，选一个：
- ✅ **直接用**：把仓库 clone 到 `vendor/<name>/`（项目根的 `.gitignore` 已忽略 `vendor/`），作为依赖调用
- ⚠️ **摘核心函数**：仅借用 3-5 个关键函数，**必须保留出处注释**：
  ```python
  # 来源：https://github.com/<owner>/<repo>/blob/<sha>/<path>#L<line>
  # 原作者：<author>，License：<license>
  # 本项目内改造：<what changed>
  ```
- ❌ **从零写**：仅当搜了 ≥3 个都不合适才允许。必须写明：**搜过 A/B/C，分别因为 X/Y/Z 不合适**

## 二、clone 操作规范

```bash
# 到 vendor/（不进主仓 git）
git clone --depth 1 https://github.com/<owner>/<repo>.git vendor/<name>

# 锁死一个具体 commit，避免上游变动破坏复现
cd vendor/<name> && git rev-parse HEAD > ../../docs/vendored_refs/<name>.sha
```

**禁止**：
- 把 `vendor/*` 内容直接 copy 进主代码树（违背溯源）
- 修改 `vendor/*` 下的文件（改本项目侧的适配层）
- 不写 SHA 直接 clone（上游 force-push 会让复现失败）

## 三、引用规范（摘核心函数时）

在本项目的代码文件顶部加归属注释段：

```python
"""
本模块部分逻辑参考：
  - Usagi-org/ai-goofish-monitor (GPL-3.0) —— 闲鱼登录态加载 / storage_state 处理
    commit: 3f2a1b9c  文件: src/scraper.py#L445-L520

本项目许可证：<自己项目的 license>
"""
```

函数级引用用单行注释指向 vendor 路径：

```python
def xianyu_login_state_valid(context):
    # 参考 vendor/ai-goofish-monitor/src/scraper.py::scrape_xianyu（L445）
    ...
```

## 四、判断"能否直接调用 vs 必须改写"

| 场景 | 处理 |
|---|---|
| 目标是库（pip/npm 能装） | **直接装依赖**，别 clone |
| 是完整应用但有 API 入口 | 启为子进程，HTTP/stdio 调用 |
| 是 async 但我们是 sync | 摘核心函数用 `asyncio.run()` 包一层 |
| 有重型依赖（ai/llm/webui）我们不需要 | 摘核心函数，不整个 clone 到运行时 |
| 代码耦合严重无法抽出 | 这是"写得烂"信号，换一个候选 |

## 五、反模式（禁止）

- ❌ "我记得有个库叫 xxx" → **不准凭记忆**，必须现搜验证
- ❌ 只搜一个关键词 → 至少 3 次变换
- ❌ 只看第一页 → 看到至少 2-3 屏才能断定"没合适的"
- ❌ 搜到了但"我觉得自己写更快" → 自己写的往往更慢 + 更错
- ❌ 复用代码不写出处注释 → 违反 License 且维护时抓瞎
- ❌ clone vendor 后直接 copy 代码进主树 → 无法跟踪上游更新

## 六、调研完成的输出格式

对话里必须按此格式汇报（以表格形式）：

```
我搜索了：
  - "<关键词 1>"
  - "<关键词 2>"
  - "<关键词 3>"

候选对比：
| Repo | Stars | Last commit | License | 评价 |
|---|---|---|---|---|
| owner/a | 11k | 2026-03 | MIT | ✅ 选这个：有 HTTP API，文档全 |
| owner/b | 2k | 2025-09 | Apache2 | 次选：被 a 覆盖 |
| owner/c | 500 | 2024-01 | — | ❌ 已停止维护 |

决定：复用 a 的 xxx 函数 / clone 到 vendor/a
```

**没有这张表之前，不要写代码。**

## 七、本项目已 vendored 列表（维护）

每次 clone 新仓库后，把条目加到 `docs/vendored.md`：

```
| 仓库 | 本地路径 | Commit SHA | 用途 | 迁入日期 |
|---|---|---|---|---|
| Usagi-org/ai-goofish-monitor | vendor/ai-goofish-monitor | <sha> | 闲鱼搜索参考 | 2026-04-18 |
```

这份列表是项目依赖的真实画像，审计/换版/撤销时唯一可信来源。
