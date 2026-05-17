"""字典 → DB 导入器(T2)。

把 `data/dict/{week}/_merged/*_chips.csv` 中的零件型号灌进
`data/prices.db.model_info` 表,空字段(release_date)留给后续 T3+ 补全。

冲突策略:INSERT OR IGNORE — 字典只填缺口,不抹掉 `enrich_model_info.py`
已手工补好的 release_year。

首次运行会自动给 model_info 加三个新列(generation / release_date /
source_tag),操作幂等。

用法:
    python scripts/dict_to_db.py                    # 自动找最新周
    python scripts/dict_to_db.py --week 2026-W17
    python scripts/dict_to_db.py --dry-run          # 只统计不写入
"""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def _read_csv_safe(path: Path) -> list[dict[str, str]]:
    """读 CSV;不存在返回空列表;跳过开头 `#` 注释行;utf-8-sig 吞 BOM。

    内联自 `core/dict/merger.py::_read_csv_safe`(commit 8b21a63 时的版本)。
    不直接 import 是因为 `core/__init__.py` 会拉 pydantic 等 requirements.txt
    依赖,而 scripts/ 下脚本约定自包含(见 CLAUDE.md)。
    """
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as fp:
        lines = fp.readlines()
    start = 0
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            start = i + 1
        else:
            break
    reader = csv.DictReader(lines[start:])
    return list(reader)

# ─── 常量 ────────────────────────────────────────────────────────

DICT_ROOT = Path("data/dict")
DEFAULT_DB = Path("data/prices.db")

# CSV 文件名 stem → DB 中的 category 值
CATEGORY_BY_STEM: dict[str, str] = {
    "cpu_chips": "CPU",
    "gpu_chips": "GPU",
    "mb_chips": "MB",
    "psu_chips": "PSU",
    "cooler_chips": "COOLER",
    "case_chips": "CASE",
    "ram_chips": "RAM",
    "storage_ssd_chips": "STORAGE_SSD",
    "storage_hdd_chips": "STORAGE_HDD",
    "nic_wired_chips": "NIC_WIRED",
    "nic_wireless_chips": "NIC_WIRELESS",
}

# 周目录命名格式:2026-W17
_WEEK_RE = re.compile(r"^\d{4}-W\d{2}$")

# CPU 型号品牌前缀(规范化用);其他类别保留原样
_CPU_PREFIX_RE = re.compile(r"^(AMD|Intel)\s+", re.IGNORECASE)

# release_year 合理区间
_YEAR_MIN = 1990
_YEAR_MAX = 2100

# 视为"无值"的 vendor 标记
_UNKNOWN_VENDORS = {"", "unknown", "n/a", "na", "none"}

log = logging.getLogger("dict_to_db")


# ─── 工具函数 ────────────────────────────────────────────────────


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
        )
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def _find_latest_week(root: Path) -> str:
    if not root.is_dir():
        raise FileNotFoundError(f"dict root not found: {root}")
    weeks = sorted(
        p.name for p in root.iterdir() if p.is_dir() and _WEEK_RE.match(p.name)
    )
    if not weeks:
        raise FileNotFoundError(f"no week directory under {root}")
    return weeks[-1]


def _ensure_schema(conn: sqlite3.Connection) -> list[str]:
    """幂等地给 model_info 加三个新列,返回本次实际新增的列名列表。"""
    cur = conn.execute("PRAGMA table_info(model_info)")
    existing = {row[1] for row in cur.fetchall()}
    added: list[str] = []
    for col, sqltype in (
        ("generation", "TEXT"),
        ("release_date", "TEXT"),
        ("source_tag", "TEXT"),
    ):
        if col not in existing:
            conn.execute(f"ALTER TABLE model_info ADD COLUMN {col} {sqltype}")
            added.append(col)
    if added:
        conn.commit()
    return added


def _normalize_model(category: str, chip: str) -> str:
    """CPU 类别剥掉 `AMD ` / `Intel ` 前缀以匹配现有 371 行命名约定。"""
    chip = chip.strip()
    if category == "CPU":
        return _CPU_PREFIX_RE.sub("", chip)
    return chip


def _normalize_brand(vendor: str) -> str | None:
    vendor = (vendor or "").strip()
    if vendor.lower() in _UNKNOWN_VENDORS:
        return None
    return vendor.upper()


def _parse_year(value: str) -> int | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        n = int(value)
    except ValueError:
        return None
    if _YEAR_MIN <= n <= _YEAR_MAX:
        return n
    return None


# ─── 主逻辑 ──────────────────────────────────────────────────────


def _import_csv(
    conn: sqlite3.Connection,
    csv_path: Path,
    category: str,
    *,
    dry_run: bool,
) -> tuple[int, int, int]:
    """导入单个 csv,返回 (inserted, skipped_already_exists, malformed)。"""
    rows = _read_csv_safe(csv_path)
    if not rows:
        return (0, 0, 0)

    inserted = 0
    skipped = 0
    malformed = 0
    now = datetime.now().isoformat(timespec="seconds")
    cur = conn.cursor()

    for row in rows:
        chip = (row.get("chip") or "").strip()
        if not chip:
            malformed += 1
            continue

        model = _normalize_model(category, chip)
        brand = _normalize_brand(row.get("vendor") or "")
        generation = (row.get("generation") or "").strip() or None
        release_year = _parse_year(row.get("release_year_est") or "")
        source_tag = (row.get("source") or "").strip() or None

        if dry_run:
            r = cur.execute(
                "SELECT 1 FROM model_info WHERE model = ?", (model,)
            ).fetchone()
            if r is None:
                inserted += 1
            else:
                skipped += 1
            continue

        try:
            cur.execute(
                """INSERT OR IGNORE INTO model_info
                   (model, category, brand, generation, release_year,
                    release_date, source_tag, source_url, updated_at)
                   VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?)""",
                (model, category, brand, generation, release_year, source_tag, now),
            )
        except sqlite3.Error as e:
            raise RuntimeError(
                f"insert failed for model={model!r} category={category}"
            ) from e

        if cur.rowcount == 1:
            inserted += 1
        else:
            skipped += 1

    if not dry_run:
        conn.commit()
    return inserted, skipped, malformed


def run(week: str | None, db_path: Path, *, dry_run: bool) -> int:
    if not db_path.exists():
        log.error("db not found: %s", db_path)
        return 2

    try:
        resolved_week = week or _find_latest_week(DICT_ROOT)
    except FileNotFoundError as e:
        log.error("%s", e)
        return 2

    merged_dir = DICT_ROOT / resolved_week / "_merged"
    if not merged_dir.is_dir():
        log.error("merged dir not found: %s", merged_dir)
        return 2

    log.info(
        "week=%s db=%s dry_run=%s", resolved_week, db_path, dry_run
    )

    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.Error as e:
        raise RuntimeError(f"cannot open db {db_path}") from e

    try:
        if not dry_run:
            added = _ensure_schema(conn)
            if added:
                log.info("schema: added columns %s", added)
            else:
                log.info("schema: already up to date")
        else:
            log.info("schema: skipped (dry-run)")

        total_inserted = 0
        total_skipped = 0
        total_malformed = 0
        missing: list[str] = []

        for stem, category in CATEGORY_BY_STEM.items():
            csv_path = merged_dir / f"{stem}.csv"
            if not csv_path.exists():
                missing.append(csv_path.name)
                continue
            ins, skp, bad = _import_csv(
                conn, csv_path, category, dry_run=dry_run
            )
            verb = "would_insert" if dry_run else "inserted"
            log.info(
                "[%-12s] %s=%4d skipped=%4d malformed=%d",
                category,
                verb,
                ins,
                skp,
                bad,
            )
            total_inserted += ins
            total_skipped += skp
            total_malformed += bad

        if missing:
            log.warning("missing csv files: %s", missing)

        log.info(
            "TOTAL %s=%d skipped=%d malformed=%d",
            "would_insert" if dry_run else "inserted",
            total_inserted,
            total_skipped,
            total_malformed,
        )
    finally:
        conn.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import dictionary chips csv into prices.db model_info table.",
    )
    parser.add_argument(
        "--week",
        type=str,
        default=None,
        help="Week directory under data/dict/ (e.g. 2026-W17). Default: latest.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"Path to sqlite db. Default: {DEFAULT_DB}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count only, do not write.",
    )
    args = parser.parse_args()

    _configure_logging()
    return run(args.week, args.db, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
