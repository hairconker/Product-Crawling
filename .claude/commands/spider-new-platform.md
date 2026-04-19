---
description: 按清单接入一个新的电商/二手平台爬虫（调研 → 登录 → 实现 → 验证 6 步）
---

请按 `.claude/skills/spider-new-platform/SKILL.md` 的 6 步清单接入新平台：$ARGUMENTS

必做：
1. Step 1 调研：先手动用浏览器搜一次，确认 URL 模板、是否 SSR、是否要登录、反爬强度；结论写入 `阶段任务/阶段X_<平台>.md`
2. Step 2 盘点开源项目（最近 6 个月内有提交的 ≥3 个候选）
3. Step 3 在 `scripts/login_helper.py` 的 `PLATFORMS` 加配置（含 login_cookie_markers）
4. Step 4 在 `run_cpu_crawl_pw.py` 加 `crawl_<platform>_pw()` 函数并注册到 CRAWLERS
5. Step 5 用 `--only <platform> --pages 2 --headed` 亲自验证
6. Step 6 同步更新 `阶段任务.md` 索引与 `登录流程.md`

**同时遵守 `spider-coding` 的全部铁律。**
