---
description: 强制加载 Python 通用编码规范（类型标注、异常分类、I/O 编码、导入顺序、禁用反模式）
---

请严格按 `.claude/skills/python-coding/SKILL.md` 的全部规范工作。

先读该文件，再动任何 .py 代码。核心铁律：

- 函数签名必须带类型（`str | None` 风格，Python 3.10+ 内置泛型）
- 禁用可变默认参数、裸 except、通配符 import、无 encoding 的 open
- 所有 I/O 用 pathlib.Path；所有异常 `raise X from e` 保栈
- 中文 JSON 必须 `ensure_ascii=False`
- 不用 print 当日志

若同时动爬虫代码，叠加 `spider-coding` 的铁律（两者互补不冲突）。

$ARGUMENTS
