"""阶段 13：数据归档。滚动保留最近 N 周（默认 12），更早的目录打 zip 到 data/archive/。

用法：
  python scripts/archive.py                          # 默认保留 12 周
  python scripts/archive.py --keep-weeks 8
  python scripts/archive.py --dry-run                # 只列出要归档/删除的目录

扫描范围：
  data/dict/{YYYY-Www}/
  data/skus/{YYYY-Www}/
  data/prices/{YYYY-Www}/

归档原则（原子安全）：
  1. 按 ISO 周排序
  2. 保留最近 keep_weeks 周
  3. 对更早的每个 {layer}/{week}/ 先打包 data/archive/{layer}_{week}.zip.tmp
  4. zip 成功后 rename 为正式 .zip
  5. 最后删除原目录
  任何一步失败则不删原目录（保守），写日志。
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_DATA = _ROOT / "data"
_ARCHIVE = _DATA / "archive"
_LAYERS = ("dict", "skus", "prices")
_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")


def _iter_week_dirs(layer_dir: Path) -> list[tuple[tuple[int, int], Path]]:
    """返回 [((year, week), path)] 按 (year, week) 升序。非 ISO 周目录跳过。"""
    if not layer_dir.exists():
        return []
    out: list[tuple[tuple[int, int], Path]] = []
    for d in layer_dir.iterdir():
        if not d.is_dir():
            continue
        m = _WEEK_RE.match(d.name)
        if not m:
            continue
        out.append(((int(m.group(1)), int(m.group(2))), d))
    out.sort(key=lambda x: x[0])
    return out


def _zip_dir(src: Path, dst_zip: Path) -> None:
    """打包目录为 zip，使用 PurePosixPath 兜住跨平台。原子写：.tmp → rename。"""
    dst_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_zip.with_suffix(dst_zip.suffix + ".tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in src.rglob("*"):
                if path.is_file():
                    arcname = path.relative_to(src.parent).as_posix()
                    zf.write(path, arcname=arcname)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    tmp.replace(dst_zip)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="阶段 13：滚动归档旧周数据")
    ap.add_argument("--keep-weeks", type=int, default=12,
                    help="保留最近 N 周，默认 12")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列出要归档/删除的目录，不实际操作")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s - %(message)s",
    )

    if args.keep_weeks < 1:
        logging.error("--keep-weeks 必须 ≥ 1")
        return 2

    run_log = _ROOT / "logs" / f"archive_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    run_log.parent.mkdir(parents=True, exist_ok=True)
    # 附加 FileHandler，让归档动作真正落到 logs/archive_*.log（Codex 审 S1 修）
    fh = logging.FileHandler(run_log, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s - %(message)s"))
    logging.getLogger().addHandler(fh)

    total_archived = 0
    total_errors = 0
    for layer in _LAYERS:
        layer_dir = _DATA / layer
        weeks = _iter_week_dirs(layer_dir)
        if len(weeks) <= args.keep_weeks:
            logging.info(f"[{layer}] 总 {len(weeks)} 周，≤ 保留阈值 {args.keep_weeks}，跳过")
            continue
        to_archive = weeks[:-args.keep_weeks]
        kept = weeks[-args.keep_weeks:]
        logging.info(
            f"[{layer}] 待归档 {len(to_archive)}，保留 {len(kept)}  "
            f"(最早保留 {kept[0][1].name}, 最新归档 {to_archive[-1][1].name})"
        )
        for (year, wk), path in to_archive:
            week_tag = f"{year}-W{wk:02d}"
            dst_zip = _ARCHIVE / f"{layer}_{week_tag}.zip"
            if args.dry_run:
                print(f"  [dry-run] would archive {path} → {dst_zip}")
                continue
            try:
                _zip_dir(path, dst_zip)
                shutil.rmtree(path)
                logging.info(f"[{layer}] ✓ {week_tag}: {path} → {dst_zip.name}")
                total_archived += 1
            except Exception as e:
                logging.error(f"[{layer}] ✗ {week_tag}: {e}")
                total_errors += 1

    if args.dry_run:
        print(f"\n[dry-run] 无修改")
        return 0

    print(f"\n[archive] 完成 {total_archived} 个周目录，错误 {total_errors} 个")
    print(f"[archive] 日志 → {run_log}")
    return 0 if total_errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
