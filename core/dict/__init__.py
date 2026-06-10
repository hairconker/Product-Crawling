"""硬件字典集成（阶段 9）。

从 vendor/pc-part-dataset（docyx 北美 66k+ SKU × 25 品类）加载原始 JSON，
按项目品类归一化，过滤 2015+ 型号，输出 data/dict/YYYY-Www/ 与
data/skus/YYYY-Www/{category}/{brand}.csv。

设计红线：
  - 不修改 vendor/pc-part-dataset 下任何文件
  - 仅读 JSON，不 import 其 TS 代码
  - USD 价格不采入（阶段 12 走三平台取价）
"""

from __future__ import annotations

from core.dict.builder import run_build
from core.dict.categories import PROJECT_CATEGORIES

__all__ = ["run_build", "PROJECT_CATEGORIES"]
