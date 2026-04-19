---
name: spider-debug
description: 爬虫某个平台爬取失败、返回 0 条、选择器超时、跳登录、滑块、风控、cookie 过期、解析错误时的排错 playbook。用户说"爬取失败"、"返回空"、"报错 LoginRequired"、"AntiSpiderError"、"滑块"、"风控"时加载。
allowed-tools: Read, Grep, Glob, Bash
---

# 爬虫排错 Playbook

## 第 0 步：永远先看证据，不要猜

失败时按顺序查：

1. `logs/error.log`：最近 20 行，确认异常类型
2. `logs/debug/{platform}_*.html`：**最新那个**，看实际返回了什么
3. `logs/report_pw_*.json` / `logs/report_*.json`：看这次跑的全景

```bash
ls -lt logs/debug/ | head -5
ls -lt logs/report_*.json | head -3
```

**未看过 debug HTML 就不要动代码。**

---

## 第 1 步：按异常类型对症下药

### `LoginRequiredError`

症状：URL 跳到 `login.*` / `passport.*` / `qrlogin.*`，或 HTML 含"登录"按钮。

排查：
1. `state/{platform}_state.json` 是否存在？大小 > 1KB？
2. `state/{platform}_cookie.txt` 是否包含该平台的关键 cookie？
   - JD: `thor=` / `pin=`
   - 闲鱼: `unb=` / `_nk_=`
   - 淘宝: `unb=` / `tracknick=`
   - PDD: `PDDAccessToken=`
3. 有则 cookie **过期**，重跑 `python scripts\login_helper.py <platform>`
4. 没有则上次登录未完成，重跑扫码

### `AntiSpiderError`

症状：选择器超时但已登录；HTML 含 `nc_1_wrapper` / `nc-container`（阿里滑块）；JD 含 `risk_handler`；PDD 跳 `anti_content` 错误。

排查：
1. 看 debug HTML 搜索：`grep -oE "nc_wrapper|risk_handler|verify|captcha|滑块" logs/debug/{platform}_*.html`
2. 换 `--headed` 重跑，人工拖滑块后 cookie 会刷新
3. 还不行：加大 `min_interval`，换代理，或换账号

### `ParseError`

症状：已登录、没跳转、没滑块，但抽不到商品字段。

这是**平台 DOM 结构变化**。流程：
1. 用 `--headed` 跑一次，观察真实渲染后的 DOM
2. F12 复制几个商品节点的 `outerHTML`
3. 对比现在代码里的 selector，找出差异
4. **改 selector 优先用 URL 模式或稳定属性**（见 spider-coding 第一条）
5. 改完跑 `tests/` 里的 fixture 测试（如果有）

### `NetworkError`

症状：`RequestException` / 连接拒绝 / DNS 失败 / 超时。

排查：
1. `curl -sI --max-time 10 <url>` 确认连通性
2. WSL → 可能 NAT 出口被风控，试 Windows 原生 Python
3. 代理是否挂了？`config.yaml` 的 `proxy.pool` 是否可用
4. tenacity 已自动重试 3 次，仍失败说明稳定故障

### `RateLimitError`

症状：HTTP 429，或 JD `errorCode=601` "大促异常火爆"。

排查：
1. 查日志最近 1 分钟请求量
2. 增大 `min_interval`（x2 起步）
3. 换账号 cookie 或加代理

---

## 第 2 步：0 条结果 / 结果过少

不是 SpiderError，单纯数据少：

1. **瀑布流平台**（闲鱼、PDD）：增加 `--pages`，滚动次数是 `max_pages * N`，每次需要真实到底部触发 lazy load
2. **关键词匹配太严**：试"cpu"而非"i5-12400F"，看看是不是搜索匹配问题
3. **选择器抓到了但过滤太狠**：在 evaluate 里 `console.log` 出原始计数，和最终 `seen` 比较
4. **登录态弱**：PDD 的 `PDDAccessToken` 容易没抓到——重登

---

## 第 3 步：常见 HTML 指纹

用 Grep 自查：

```bash
# 滑块
grep -l "nc_wrapper\|nc-container" logs/debug/*.html

# 登录跳转
grep -l "passport\|login.taobao\|login.jd\|mobile.yangkeduo.com/login" logs/debug/*.html

# JD 风控页
grep -l "risk_handler\|cfe.m.jd.com/privatedomain" logs/debug/*.html

# PDD SSR 空壳
grep -L "rawData\|goods_id" logs/debug/pdd_*.html
```

---

## 第 4 步：复现最小化

发现问题后，用 `--only <platform> --pages 1 --headed` 缩小范围重跑，配合 F12 观察，比全量跑快 10 倍。

```powershell
python run_cpu_crawl_pw.py "cpu" --only taobao --pages 1 --headed
```

---

## 第 5 步：什么情况报给人

**不要自己乱改**并提 PR/commit 的场景：
- Selector 改了 3 次还是 ParseError
- 登录 cookie 看起来对但仍 LoginRequired
- AntiSpider 连续出现在多个账号/IP
- 发现平台可能有 WAF 或接口迁移

这些需要把 `logs/debug/*.html` 和 `logs/error.log` 贴出来一起分析，盲改只会越搞越复杂。
