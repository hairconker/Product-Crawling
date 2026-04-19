---
name: spider-coding
description: 写 spider 代码、Playwright 选择器、解析函数、爬虫异常、请求限流、cookie/state 加载时必读的编码规范。修改 run_cpu_crawl*.py、spiders/*.py、core/base_spider.py、core/exceptions.py 等爬虫文件前自动加载。
paths:
  - "run_cpu_crawl*.py"
  - "spiders/**/*.py"
  - "core/**/*.py"
  - "scripts/login_helper.py"
allowed-tools: Read, Edit, Grep, Glob, Bash
---

# 爬虫编码铁律（写 / 改任何 spider 代码前必读）

## 一、选择器稳定性（第一优先级）

**禁用**：随机生成的 CSS class（`.Card--xxx-AbC3dE`）、父子索引（`div > div:nth-child(3)`）、长类名串。
**首选**：
- URL 模式匹配：`a[href*='item.jd.com/']`、`a[href*='goods_id=']`、`a[href*='item?id=']`
- 稳定属性：`data-sku`、`data-item-id`、`data-spm`
- 语义结构：`aria-label`、`itemprop`
- 文本特征：`:has-text('¥')`（Playwright 独有）

选择器失败时，**先落盘 HTML 再改代码**，不要凭猜测。

## 二、异常必须分类

所有 spider 抛出的异常**必须**继承项目的 `SpiderError` 基类，且必须选对子类：

| 失败现象 | 异常类 | 是否自动重试 |
|---|---|---|
| HTTP 超时/5xx/代理失败 | `NetworkError` | 可重试（指数退避） |
| 跳登录页 / cookie 失效 | `LoginRequiredError` | **不可重试** |
| CAPTCHA / 滑块 / IP 封 | `AntiSpiderError` | 不可重试 |
| 429 / 业务限流 | `RateLimitError` | 可重试 |
| 字段缺失 / 选择器超时（已证实登录态正常） | `ParseError` | 不可重试 |

每个异常必须带 `platform=` 和 `url=` 上下文。

## 三、每个 parse 函数必须调用 dump_debug

失败分支里**必须**有：
```python
dump_debug(platform, page.content())   # Playwright
# 或
dump_debug(platform, response.text)    # requests
```
调试文件自动落到 `logs/debug/{platform}_{ts}.html`。不要 raise 后才想起落盘。

## 四、Playwright 强制项

1. `browser.launch(args=["--disable-blink-features=AutomationControlled"])`
2. `context.add_init_script(_STEALTH_JS)`（去掉 `navigator.webdriver`）
3. `storage_state=str(state_path)` 从 `state/{platform}_state.json` 载入登录态
4. 真实 UA（`UA_DESKTOP` / `UA_MOBILE`），别用默认
5. 搜索页之后必须 `wait_for_load_state("networkidle")`，再 `wait_for_selector`
6. 瀑布流需要多次 `_scroll_page` + **每次滚动后增量抽取**（不要滚完再一次 evaluate）

## 五、日志字段约定

用项目内置 logger（loguru 版或 stdlib 版）。每条日志必须带 `platform` 上下文。关键节点：
- 开始（`INFO`）：请求 URL + page/index
- 每页命中数（`INFO`）
- 超时/选择器失败（`ERROR`）并说明 dump 路径
- 反复用 `log.warning` 标记"可能失败但继续"的分支

## 六、配置优先级

代码里**不要硬编码** cookie、UA、路径。一律走：
1. `state/{platform}_cookie.txt` / `state/{platform}_state.json`
2. `config/config.yaml`（未来）
3. 环境变量

## 七、禁止做的事

- ❌ 在爬虫代码里 `time.sleep(random.randint(...))` 代替 `min_interval + jitter`——用项目的限流机制
- ❌ 吞掉异常 `except Exception: pass`——要么抛 `SpiderError`，要么 log 完再抛
- ❌ 写测试时 mock 真实平台返回——用 `tests/fixtures/*.html` 样本
- ❌ 在主流程加 `--no-verify` / 禁用 TLS 校验等临时绕过
- ❌ 凭记忆"改选择器试一试"——先 dump_debug，看 HTML，再改

## 八、改动前的自查清单

动笔前回答：
1. 我要改的选择器是稳定的吗？—— 跑 `Grep` 确认
2. 失败路径有 `dump_debug` 吗？
3. 抛的异常类型对吗？
4. 日志有 `platform` 字段吗？
5. cookie/UA 是否从配置读？

全部 ✅ 再写。
