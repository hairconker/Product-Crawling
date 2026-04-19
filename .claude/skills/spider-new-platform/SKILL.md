---
name: spider-new-platform
description: 新增一个电商/二手平台爬虫（苏宁、淘特、得物、转转、1688 等）时的完整接入清单。用户说"加一个平台"、"接入新站点"、"支持 xxx 网"时加载。
allowed-tools: Read, Edit, Write, Grep, Glob, Bash
---

# 新平台接入清单

按顺序完成。跳步会踩坑。

## Step 1 — 调研（不写代码）

1. 用浏览器手动搜索目标关键词，记录：
   - 搜索 URL 模板：`https://xxx.com/search?q={kw}&page={p}`
   - 翻页参数是 `page` 还是 `offset` / `s`？每页多少条？
   - **是否要登录才能搜**？
   - 商品详情 URL 格式（提取 `item_id` 的正则）
2. 打开 F12 Network，找商品列表的**数据源**：
   - SSR（HTML 里直接有）——最好，用 requests + 正则即可
   - XHR JSON 接口——次之，可能带签名
   - CSR 异步渲染——最差，必须 Playwright
3. 判断反爬强度：滑块、签名、加密参数
4. 把结论写到 `阶段任务/阶段X_<平台>.md`（参考阶段 1~4 的模板）

## Step 2 — 开源项目盘点

**优先复用现成的**。在项目里新建 `vendor/<platform>/`（`.gitignore` 已忽略）：

1. GitHub 搜 `<平台名> spider python`，筛选最近 6 个月内有提交的
2. 至少找 3 个候选，读 README 和核心文件
3. 把参考链接 + 技术选型表写到阶段文件
4. 不要直接 copy 代码，只抽关键思路（选择器、签名算法、接口路径）

## Step 3 — 登录流程

若需登录，在 `scripts/login_helper.py` 的 `PLATFORMS` 字典里加一项：

```python
"<platform>": {
    "name": "<中文名>",
    "login_url": "<登录页 URL>",
    "post_login_url": "<登录后业务页，触发业务 cookie>",
    "login_cookie_markers": ["<关键 cookie 名>", ...],  # 轮询检测用
    "cookie_domain_filter": [".xxx.com"],
    "mobile_ua": False,  # 需要手机 UA 的（如 PDD）设 True
},
```

**`login_cookie_markers` 的选法**：浏览器登录前后对比 F12 → Application → Cookies，新增且非空的关键字段就是它。

## Step 4 — Spider 实现

在 `run_cpu_crawl_pw.py` 里（或 `spiders/<platform>.py` 如果架构已拆分）：

1. 加一个 `crawl_<platform>_pw(context, keyword, max_pages) -> list[Product]` 函数
2. 模板骨架：

```python
def crawl_xxx_pw(context, keyword, max_pages):
    page = context.new_page()
    seen: dict[str, Product] = {}
    try:
        for i in range(max_pages):
            url = f"<搜索 URL>".format(kw=keyword, page=i+1)
            log.info(f"[xxx] page {i+1}/{max_pages} → {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except PWTimeout:
                log.warning(f"[xxx] goto 超时")

            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                pass

            try:
                page.wait_for_selector("<稳定选择器>", timeout=20000)
            except PWTimeout:
                dump_debug("xxx", page.content())
                if "login" in (page.url or "").lower():
                    raise LoginRequiredError(...)
                raise AntiSpiderError(...)

            _scroll_page(page, times=3, delay_ms=900)

            raw = page.evaluate("<提取 JS>")
            for it in raw:
                if it["id"] not in seen:
                    seen[it["id"]] = Product(
                        platform="xxx",
                        item_id=it["id"],
                        title=...,
                        url=...,
                        current_price=_parse_price(...),
                        image_url=it["img"],
                    )
    finally:
        page.close()
    return list(seen.values())
```

3. 在 `CRAWLERS` 字典注册：`"xxx": (crawl_xxx_pw, "xxx_state.json", <mobile_bool>)`

**必须遵守 `spider-coding` skill 的所有铁律**（选择器稳定、异常分类、dump_debug、stealth JS）。

## Step 5 — 验证

```powershell
python scripts\login_helper.py <platform>
python run_cpu_crawl_pw.py "i5-12400F" --only <platform> --pages 2 --headed
```

**必须观察到的信号**：
- 浏览器不跳登录（或成功自动登录）
- 商品选择器命中 > 0
- 终端打印至少 5 条商品且价格非 None

三项都 OK 才算接入成功。

## Step 6 — 收尾

1. `阶段任务.md` 主索引加新平台链接
2. `登录流程.md` 加对应章节
3. `.gitignore` 确认 `state/<platform>_*` 被忽略
4. 如果平台有独特反爬（如 PDD 的 anti_content），把解密脚本/依赖单独放 `spiders/_<platform>_assets/`

## 不要做的事

- ❌ 不做调研就抄别人代码（版本不对基本跑不起来）
- ❌ 选择器用随机 CSS class
- ❌ 登录流程靠手动复制 cookie（不稳，应走 login_helper.py）
- ❌ 异常不分类全部 `raise SpiderError`（排错工具直接失灵）
- ❌ 跳过 `--headed` 验证，直接无头跑
