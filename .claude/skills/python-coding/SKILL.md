---
name: python-coding
description: 写 / 改任何 Python 文件（*.py）时的通用编码规范：类型标注、异常处理、I/O、日志、测试、导入、性能反模式。修改 .py 文件前自动加载。
paths:
  - "**/*.py"
allowed-tools: Read, Edit, Grep, Glob, Bash
---

# Python 编码规范（通用）

> 本 skill 只管"Python 语言本身的写法"。爬虫业务规范看 `spider-coding`。

## 一、类型标注（必须）

Python 3.10+ 项目，**新增/修改函数签名必须带类型**。

- 用 `str | None` 而非 `Optional[str]`、用 `list[str]` 而非 `List[str]`（内置泛型，别 `from typing import List`）
- 返回 `None` 也要显式 `-> None`
- `Any` 是逃生口，不要滥用；能写具体类型就写具体类型
- 复杂数据用 `@dataclass` 或 `pydantic.BaseModel`，别用裸 dict/tuple

```python
# ❌
def search(kw, page=1): ...

# ✅
def search(kw: str, page: int = 1) -> SearchResult: ...
```

## 二、异常处理

**禁止**：
- `except:` 裸捕获
- `except Exception: pass` —— 吞异常
- `except Exception as e: print(e)` —— 丢栈
- 在底层库随意 `raise Exception("xxx")` —— 没分类

**必须**：
- 捕获**具体异常类**
- 需要重抛时 `raise XxxError(...) from e` 保留原始栈
- 自定义异常继承层级明确（本项目走 `SpiderError` 家族）

```python
# ❌
try:
    r = requests.get(url)
except:
    return None

# ✅
try:
    r = requests.get(url, timeout=15)
except requests.Timeout as e:
    raise NetworkError(f"timeout {url}", platform=p) from e
except requests.RequestException as e:
    raise NetworkError(str(e), platform=p) from e
```

## 三、可变默认参数（绝对禁止）

```python
# ❌ 灾难
def add(item, bucket=[]): bucket.append(item); return bucket

# ✅
def add(item, bucket: list | None = None) -> list:
    bucket = bucket if bucket is not None else []
    bucket.append(item)
    return bucket
```
`dict`, `set`, 自定义可变对象同理。

## 四、I/O 与路径

- **禁止** `open(...)` 不加 `encoding=` 读写文本（Windows 默认不是 UTF-8，必炸）
- **禁止** `os.path.join` + 字符串拼接；全部用 `pathlib.Path`
- **必须**用 `with` 上下文管理器；禁止裸 `open` 后 `.close()`
- JSON：`json.dump(obj, f, ensure_ascii=False, indent=2)`，中文场景一律加 `ensure_ascii=False`

```python
# ✅
from pathlib import Path
path = Path("state") / f"{platform}_cookie.txt"
path.write_text(content, encoding="utf-8")
```

## 五、字符串格式

- **首选** f-string；不要 `%` 格式化；不要 `str + str +` 长串
- 日志里**用 lazy 格式**：`log.info("got %s items", n)` 而非 `log.info(f"got {n} items")`（loguru 用 f-string 可接受，stdlib logging 用 `%s` 占位更好）

## 六、导入

顺序（空行分组）：
1. 标准库
2. 第三方
3. 本项目

```python
import json
import re
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

from core.exceptions import SpiderError
from core.models import Product
```

**禁止** `from xxx import *`（唯一例外：明确维护的 `__init__.py` 再导出）。

## 七、逻辑与数据

- **短路优于 try**：`if x:` 比 `try: x[0]` 更直白
- 列表 in 查询频繁 → 转 `set`
- 拼接多段字符串 → `"".join(parts)` 而非循环 `+=`
- 数字除法注意：`/` 浮点、`//` 整除；价格分转元用 `/ 100` 明示
- 避免 `is` 比较字符串/数字（用 `==`）；`is None` / `is True` 例外

## 八、日志

- 用项目统一 logger（本项目：loguru 或 stdlib 都有），**不要** `print()` 生产代码
- 每条日志带上下文：`platform / spider / keyword / request_id`（本项目已约定）
- 异常路径用 `log.exception(...)` 或 `log.error(..., exc_info=True)`，**保留 traceback**
- DEBUG 等级用于入参/出参，INFO 用于业务节点，ERROR 用于真异常

## 九、测试

- 用 `pytest`；函数名 `test_xxx`；断言直接写 `assert`
- 集成测试（要联网/cookie）加 `@pytest.mark.integration`，CI 默认 skip
- Fixtures：平台样本 HTML 放 `tests/fixtures/`，不要每次去真实站点抓
- 一个测试只测一件事；测试名说明意图（`test_jd_parse_handles_missing_price`）

## 十、并发

- 同步 requests / sync_playwright，**禁止** 混 `asyncio`
- 需要并发时用 `concurrent.futures.ThreadPoolExecutor`（I/O bound）或 `asyncio` 全栈（就别再 requests）
- 共享状态必须加锁，或改用队列

## 十一、禁止清单（快速自查）

- ❌ `except Exception: pass`
- ❌ `except:`
- ❌ 可变默认参数
- ❌ `open()` 不带 `encoding`
- ❌ `from x import *`
- ❌ 裸 `print()` 用作日志
- ❌ 硬编码路径 `"E:/xxx"`、硬编码 cookie / API key
- ❌ `time.sleep(random.randint(1, 5))` 临时限流（用项目限流机制）
- ❌ 注释掉的死代码（删掉，git 里有历史）
- ❌ `TODO` 不写责任人和日期

## 十二、改动前三连问

1. 我动的函数有类型标注吗？
2. 我的 `try` 捕获的是具体异常吗？失败分支抛的是本项目异常家族吗？
3. 我的 I/O 都加 `encoding="utf-8"` 了吗？

全 ✅ 再落盘。
