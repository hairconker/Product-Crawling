# Product-Crawling

多平台电商/二手商品价格爬虫，按关键词同步搜索 **京东 / 闲鱼 / 淘宝** 三个平台并归一化输出。

## 快速开始

```bash
# 1) 一次性准备
pip install playwright
python -m playwright install chromium

# 2) 扫码登录（cookie 自动检测，不用按回车）
python scripts/login_helper.py all

# 3) 搜索某款 CPU
python run_cpu_crawl_pw.py "i5-12400F" --pages 3 --headed
```

- `--pages N`：每平台爬取页数
- `--only <platform>`：只跑单平台（`jd` / `xianyu` / `taobao`）
- `--headed`：显示浏览器（淘宝过滑块时必须）

结果打印在终端，完整 JSON 落 `logs/report_pw_*.json`，失败时 HTML + 全页截图落 `logs/debug/`。

## 两套实现并存

| 脚本 | 场景 |
|---|---|
| `run_cpu_crawl_pw.py` | **主力**：Playwright，处理 SPA，拦截 mtop 响应 / 抽 DOM |
| `run_cpu_crawl.py` | 最小依赖（仅 requests + 标准库），无 pip 环境可跑，只对 JD 可用 |

## 平台策略

- **京东**：从 HTML 骨架 + `api.m.jd.com` XHR 拦截商品 JSON
- **闲鱼**：`page.on('response')` 监听 `mtop.taobao.idlemtopsearch.pc.search` 返回，解析结构化字段
- **淘宝**：首页暖场建信任 → 直接 goto `s.taobao.com/search?q=...` → 等商品 DOM 就绪 → 抽 a 标签 + 父节点价格/标题/图

拼多多 2026-04 已永久移除（移动端登录复杂、pc 端商品极少）。

## 登录态 / 账号保活

- 登录助手 `scripts/login_helper.py` 通过**轮询关键 cookie**（`thor/pin` / `unb/tracknick`）自动识别登录完成，无需回车确认
- 每次爬取成功后自动把最新 cookies 回写 `state/{platform}_state.json`，延长登录态有效期
- 启动时自动读账号信息并报告是否可用（昵称 / 用户名 / UID）

## 反爬机制

- Stealth JS 注入（去 `navigator.webdriver`）
- `--disable-blink-features=AutomationControlled` 启动参数
- 节流常量：滚动 2.2s / 翻页 4.5s / 跨平台 6s
- 淘宝滑块检测 → headed 模式最多等 5 分钟人工过
- 风控识别：`rgv587_flag` / `deny_h5` / `risk_handler` 等命中后明确抛 `AntiSpiderError`

## 文档

- `CLAUDE.md` —— 项目架构概览（给 Claude Code 用）
- `阶段任务/` —— 按阶段拆分的开发路线图
- `登录流程.md` —— 扫码登录用户手册
- `.claude/skills/` —— 项目级 skill 规范（编码 / 排错 / 新平台接入 / GitHub 复用）
- `docs/vendored.md` —— 外部参考仓库清单（commit SHA 锁定）

## 许可证

MIT
