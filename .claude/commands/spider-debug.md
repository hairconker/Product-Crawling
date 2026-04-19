---
description: 加载爬虫排错 playbook（按异常类型的诊断流程 + HTML 指纹识别 + 0 条结果处理）
---

请按照 `.claude/skills/spider-debug/SKILL.md` 的流程排错，**不要猜**。

必须按顺序：
1. 读 `logs/error.log` 最近记录
2. 列出并查看 `logs/debug/` 里最新的平台 HTML
3. 查看最近的 `logs/report_*.json`
4. 根据异常类型对症处理（LoginRequired / AntiSpider / Parse / Network / RateLimit）
5. 未看过 debug HTML 前禁止改代码

如果用户没贴错误，先 `ls -lt logs/debug/ | head` 和 `tail -30 logs/error.log` 看最新状态。

$ARGUMENTS
