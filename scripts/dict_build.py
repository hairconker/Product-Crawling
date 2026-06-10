"""阶段 9 CLI：从 vendor/pc-part-dataset 构建一级字典。

用法：
  python scripts/dict_build.py                        # 默认当前 ISO 周，全品类
  python scripts/dict_build.py --week 2026-W17
  python scripts/dict_build.py --categories gpu,cpu
  python scripts/dict_build.py --out data --verbose
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.dict.builder import run_build  # noqa: E402
from core.dict.categories import PROJECT_CATEGORIES  # noqa: E402


def current_iso_week() -> str:
    y, w, _ = date.today().isocalendar()
    return f"{y}-W{w:02d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="阶段 9：构建一级字典（docyx → dict/ + skus/）")
    ap.add_argument("--week", default=None, help="ISO 周号，默认当前周（如 2026-W17）")
    ap.add_argument("--categories", default=None, help=f"逗号分隔，默认全部：{','.join(PROJECT_CATEGORIES)}")
    ap.add_argument("--out", default=None, help="输出根目录，默认 <project>/data")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    week = args.week or current_iso_week()
    categories = [s.strip() for s in args.categories.split(",")] if args.categories else None
    out_base = Path(args.out).resolve() if args.out else None

    summary = run_build(week=week, out_base=out_base, categories=categories)

    print(f"[done] week={summary.week} out={summary.out_dir}")
    for cat, stat in sorted(summary.categories.items()):
        print(
            f"  {cat:14s}  kept={stat['kept']:6d}  dropped={stat['dropped']:5d}  "
            f"chips={stat['chips']:4d}  brands={stat['brands']:4d}"
        )
    if summary.unfiltered_count:
        unfiltered_full = Path(summary.out_dir) / "dict" / summary.week / "_unfiltered.csv"
        print(f"[unfiltered] {summary.unfiltered_count} rows → {unfiltered_full}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
