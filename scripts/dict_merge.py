"""阶段 11 CLI：合并字典三源产出 `data/skus/{week}/_merged/`。

用法：
  python scripts/dict_merge.py                        # 当前 ISO 周
  python scripts/dict_merge.py --week 2026-W17
  python scripts/dict_merge.py --week 2026-W17 --dry-run      # 只算统计 + 冲突，不落盘

前置：
  先跑 scripts/dict_build.py（阶段 9）产出 data/dict/{week}/、data/skus/{week}/。
  阶段 10 产出可选（`{brand}_jd.csv`）。
  overrides 目录可选（data/dict/overrides/）。
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

from core.dict.merger import run_merge  # noqa: E402


def current_iso_week() -> str:
    y, w, _ = date.today().isocalendar()
    return f"{y}-W{w:02d}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="阶段 11：字典三源合并 + overrides")
    ap.add_argument("--week", default=None, help="ISO 周号，默认当前周")
    ap.add_argument("--out", default=None, help="输出根目录，默认 <project>/data")
    ap.add_argument("--dry-run", action="store_true", help="只统计 + 冲突，不写盘")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    week = args.week or current_iso_week()
    out_base = Path(args.out).resolve() if args.out else None

    summary = run_merge(week=week, out_base=out_base, dry_run=args.dry_run)

    print(f"[done] week={summary.week} dry_run={args.dry_run}")
    print(f"  total_chips={summary.total_chips}  total_skus={summary.total_skus}  "
          f"conflicts={summary.conflicts}")
    for cat, stat in sorted(summary.categories.items()):
        chips = stat.get("chips", 0)
        brands = stat.get("brands_merged", 0)
        skus = stat.get("skus_merged", 0)
        print(f"  {cat:14s}  chips={chips:4d}  brands={brands:4d}  skus={skus:6d}")
    if summary.conflicts:
        print(f"  冲突记录 → logs/dict_merge_conflicts_{week}.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
