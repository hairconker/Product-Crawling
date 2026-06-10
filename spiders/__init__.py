"""项目 spiders 包（阶段 10 起使用）。

与 core/ 不同，spiders/ 下各文件设计为**自包含**（可独立运行），但允许从
`run_cpu_crawl_pw.py` 导入已验证过的 Playwright 基础设施（日志、截图、
风控检测、异常类、节流常量）。禁止反向 import core/ 中的 pydantic / loguru 内容。
"""
