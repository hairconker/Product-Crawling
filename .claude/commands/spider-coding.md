---
description: 强制加载爬虫编码规范（选择器稳定性、异常分类、dump_debug、stealth JS、日志字段）
---

请严格按照 `.claude/skills/spider-coding/SKILL.md` 的全部铁律工作。

立即读取并在本轮对话中执行其中所有规定：
- 选择器必须用稳定模式（URL 匹配 / data-属性 / 语义），禁用随机 class
- 所有爬虫异常必须继承 SpiderError，且按 Network/LoginRequired/AntiSpider/RateLimit/Parse/Platform 正确分类
- 失败分支必须 `dump_debug(platform, content)`
- Playwright 必须注入 `_STEALTH_JS` + `--disable-blink-features=AutomationControlled` + storage_state
- 日志每条都带 `platform` 字段；禁止吞异常；禁止硬编码 cookie/UA

先读 `.claude/skills/spider-coding/SKILL.md`，再动代码。
