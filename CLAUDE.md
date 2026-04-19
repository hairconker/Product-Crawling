# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

中国境内电商/二手商品价格爬虫，支持按关键词搜索 **京东 / 闲鱼 / 淘宝** 三个平台并归一化输出（拼多多 2026-04 已移除）。对外有三种调用形态：Python 模块、CLI、规划中的 HTTP API。

## 两条并行实现路径（关键）

本仓库刻意保留两套实现共存，不要误删其中一套：

- **`run_cpu_crawl.py`** —— 最小依赖版，**仅用 `requests` + 标准库**。环境没有 `pip`/`sudo` 时（受限 WSL / 服务器）仍能跑，但只适合 JD 的 SSR 页面；闲鱼/淘宝已全面 SPA 化，此版本对它们基本返回空。
- **`run_cpu_crawl_pw.py`** —— Playwright 版，**实际的主生产脚本**。处理 SPA、加载 `state/*_state.json` 登录态、注入 stealth JS、多页去重、截图+风控检测（"睁眼"）。默认用这个。
- **`core/` 目录是"未来版"脚手架**，用 `loguru / pydantic / tenacity / fastapi` 等第三方库，依赖 `pip install -r requirements.txt` 才能跑。当前 `run_cpu_crawl*.py` **不**从 `core/` import 任何东西（自包含），但 `core/` 定义的异常体系和模型是全项目契约，新代码应以其为准。

## 常用命令（Windows PowerShell 为准）

```powershell
# 一次性准备
pip install playwright
python -m playwright install chromium

# 扫码登录（cookie 自动检测，不用按回车）
python scripts\login_helper.py all                     # 京东 + 闲鱼 + 淘宝
python scripts\login_helper.py jd                      # 只登一个

# 爬取（默认每平台 3 页，去重）
python run_cpu_crawl_pw.py "i5-12400F" --pages 3
python run_cpu_crawl_pw.py "i9-14900K" --only jd --pages 5
python run_cpu_crawl_pw.py "i5-12400F" --headed        # 显示浏览器（过淘宝滑块必须）

# 最小依赖版（仅 JD 有效）
python run_cpu_crawl.py "i5-12400F" --only jd

# 编译自检
python -m py_compile run_cpu_crawl_pw.py run_cpu_crawl.py scripts\login_helper.py
```

**没有 `make` / `pytest` / lint 脚本**。CI 未配置。

## 架构要点

### 登录态双轨保存（`state/` 目录）

`scripts/login_helper.py` 登录成功后**同时**写两种文件：
- `{platform}_state.json` —— Playwright `storage_state`，给 `run_cpu_crawl_pw.py` 用
- `{platform}_cookie.txt` —— 纯 Cookie 字符串，给 `run_cpu_crawl.py` 用

登录检测机制是**轮询 `context.cookies()`**，命中 `login_cookie_markers` 里任一字段且非空即判定登录完成（不是 DOM 选择器，避免前端改版失效）。各平台标记 cookie：
- JD: `thor` / `pin` / `pinId`
- 闲鱼: `unb` / `_nk_` / `tracknick`（属阿里体系）
- 淘宝: `unb` / `tracknick` / `_nk_`

### 异常体系 = 用户操作决策树

`SpiderError` 的子类不是装饰，而是**明确告诉用户下一步该做什么**。改代码时选错类会误导用户：

| 异常 | 根因 | 用户该做 | 是否自动重试 |
|---|---|---|---|
| `NetworkError` | 超时/代理失败 | 检查网络 | 是（tenacity 指数退避） |
| `LoginRequiredError` | cookie 失效或跳登录页 | 重跑 `login_helper.py` | 否 |
| `AntiSpiderError` | 风控/滑块/CAPTCHA | 改 `--headed` / 换 IP | 否 |
| `RateLimitError` | 429 / "大促异常火爆" | 加大 `min_interval` | 是（退避） |
| `ParseError` | 已登录也无滑块但解析失败 | **平台 DOM 变了**，需更新选择器 | 否 |

### "睁眼"诊断机制（`run_cpu_crawl_pw.py`）

每次 `page.goto()` 后强制调用 `_snapshot_page()` + `_detect_risk()`，目的是让用户**不靠猜**就知道爬虫看到了什么：
- `_snapshot_page` 落盘 `logs/debug/{platform}_{tag}_{ts}.png` + 同名 `.meta.json`（URL/title/viewport）
- `_detect_risk` 识别 `rgv587_flag` / `deny_h5` / `risk_handler` / 纯 JSON 响应等特征，命中直接抛 `AntiSpiderError` 而不是继续等选择器超时

新增平台时**必须**保留这两个调用，否则排错回到盲猜。

### 节流常量（保护账号）

`run_cpu_crawl_pw.py` 顶部定义 `SCROLL_DELAY_MS=2200`、`PAGE_DELAY_MS=4500`、`PLATFORM_DELAY_SEC=6.0`、`CAPTCHA_WAIT_SEC=300`。**业务代码一律引用常量**，不要散写数字。淘宝滑块时 `_wait_for_captcha_pass()` 会等用户手动拖（仅 `--headed` 模式有效）。

### 闲鱼特殊：瀑布流 + 增量抽取

闲鱼没有传统翻页，是无限滚动。`crawl_xianyu_pw` 的循环是"滚一次 → evaluate 一次 → 累计到 `seen` dict"，而非全部滚完再抽一次——滚完再抽会丢失被懒加载替换掉的早期元素。

## 项目级 Skill / Command 系统（重要）

`.claude/skills/` 和 `.claude/commands/` 是这个项目的**强约束**（不是可选参考）：

| 命令 | 何时自动加载 | 强制内容 |
|---|---|---|
| `/python-coding` | 改任何 `*.py` | 类型标注、禁可变默认参数、禁裸 `except`、I/O 必带 `encoding`、`ensure_ascii=False` |
| `/spider-coding` | 改爬虫相关 `.py` | 选择器稳定性（禁随机 class）、异常按上表分类、失败必 `dump_debug`、Playwright 必注入 stealth JS |
| `/spider-debug` | 说"爬取失败/风控/滑块/返回空" | 先看 `logs/debug/` 和 `error.log`，未看证据不许改代码 |
| `/spider-new-platform` | 说"加一个平台/接入 xxx" | 6 步清单：调研 → 开源盘点 → login_helper 配置 → crawler 实现 → 验证 → 文档同步 |
| `/github-reuse` ⭐ | 说"实现/写一个/做一个 xxx" | **禁止直接写代码**。必须先 3+ 次不同关键词 WebSearch，输出候选打分表，选"直接用/摘函数/自写（带理由）"之一 |

违背 skill 的改动视为**不符合项目规范**，应拒绝落盘。这是项目从"写代码踩坑→补规范→再踩坑→再补"迭代出来的，不是可有可无。

## `vendor/` 规范

- 所有外部复用代码 clone 到 `vendor/<name>/`，`--depth 1` + 固定 commit SHA
- SHA + 用途登记到 `docs/vendored.md`（审计真源）
- **禁止**修改 `vendor/` 下文件；**禁止**直接 copy 其代码进主树
- 摘核心函数须在主代码写出处注释（repo URL + commit + 行号 + license）

当前已 vendored：`Usagi-org/ai-goofish-monitor @ 9923efac`（闲鱼搜索参考，1290 行 async `scrape_xianyu` 在 `vendor/ai-goofish-monitor/src/scraper.py`；重构 `crawl_xianyu_pw` 时从它抽）。

## 文档结构

- `阶段任务.md` + `阶段任务/阶段{0..7}_*.md` —— 开发路线图，阶段 4（PDD）已划线标注移除
- `登录流程.md` —— 用户操作手册（扫码流程、cookie 兜底手动方案）
- `诊断报告.md` —— 首次运行的环境诊断（WSL IP 被 JD 风控、各平台 SPA 化结论），历史记录勿删

## 已知红线

- **拼多多已永久移除**：`run_cpu_crawl*.py` / `login_helper.py` / 阶段 4 文件均已清；PR 里再加回来前先问用户。
- **JD 在机房 IP 会被软拦截**（搜索页只渲染页脚）：WSL 直接跑可能空结果，Windows 家宽正常——改代码前确认是环境问题还是代码问题，看 `logs/debug/jd_*.png` 截图。
- **淘宝服务端硬封**：命中 `rgv587_flag` 时连 HTML 都没有，只有 JSON 拒绝页。此时换选择器无用，对策在 `AntiSpiderError` 的消息里。
- `run_cpu_crawl.py`（requests 版）对闲鱼/淘宝几乎必然空结果——不是 bug，是平台 SPA 化的客观结果，不要修。

## 未来接入 pip 后的路径

`requirements.txt` 已列好全量依赖（loguru/pydantic/tenacity/fastapi/sqlmodel 等）。装齐后：
1. 逐步把 `run_cpu_crawl_pw.py` 的实现搬到 `spiders/{platform}.py`
2. 继承 `core/base_spider.py::BaseSpider`
3. 用 `core/exceptions.py` 替换本地重复定义的 `SpiderError` 家族
4. 按阶段 5 文档起 FastAPI 服务

在那之前，**`run_cpu_crawl_pw.py` 是唯一的实际运行入口**，修改时保持自包含（别 `from core import ...`）。
