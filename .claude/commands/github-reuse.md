---
description: 实现新功能 / 接入新平台前先搜 GitHub 找现成库，按候选表格汇报再决定复用或自写
---

按 `.claude/skills/github-reuse/SKILL.md` 的铁律工作：$ARGUMENTS

禁止事项（直到完成调研）：
- 禁止写第一行实现代码
- 禁止凭记忆说"有个库叫 xxx"
- 禁止只搜一次

必做事项：
1. WebSearch 至少 3 次不同关键词
2. 候选 ≥ 3 个，按 stars / 最近 commit / license / README 质量打分
3. 在对话里输出候选对比表格
4. 说明最终选择与理由（直接用 / 摘函数 / 自写）
5. 决定 clone 的话，`git clone --depth 1` 到 `vendor/<name>/`，记录 SHA
6. 摘函数必须在代码里写出处注释（repo + commit + 文件行号 + license）

完成调研后再动代码。
