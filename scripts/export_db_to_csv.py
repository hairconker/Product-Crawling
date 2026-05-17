"""把 data/prices.db 全部表导出为 CSV 文件(UTF-8-sig 编码,Excel 兼容)。

设计:
- 输出目录: data/export_csv_YYYYMMDD_HHMM/(按时间戳防覆盖)
- 每个表一个 .csv,文件名 = {表名}.csv
- 编码 utf-8-sig(带 BOM),Excel 双击直接显示中文不乱码
- 默认导全部表,可用 --tables 限定
- products 表巨大时可加 --split-by-platform,按平台拆 csv

用法:
    python scripts/export_db_to_csv.py
    python scripts/export_db_to_csv.py --tables products,t_hardware
    python scripts/export_db_to_csv.py --split-by-platform
    python scripts/export_db_to_csv.py --out data/my_export
"""
from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "prices.db"
DEFAULT_OUT_PARENT = ROOT / "data"

log = logging.getLogger("export_csv")


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def list_user_tables(conn: sqlite3.Connection) -> list[str]:
    """返回所有用户表(排除 sqlite_* 系统表)。"""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def export_table(
    conn: sqlite3.Connection,
    table: str,
    out_path: Path,
    where: str = "",
) -> int:
    """把单表(可带 WHERE 过滤)dump 为 utf-8-sig CSV。返回写入行数。"""
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" WHERE {where}"
    try:
        cur = conn.execute(sql)
    except sqlite3.Error as e:
        raise RuntimeError(f"query {table} failed") from e
    cols = [d[0] for d in cur.description]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    try:
        with out_path.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            for row in cur:
                w.writerow(["" if v is None else v for v in row])
                n += 1
    except OSError as e:
        raise RuntimeError(f"write {out_path} failed") from e
    return n


def run(
    db_path: Path,
    out_dir: Path,
    *,
    tables: list[str] | None,
    split_by_platform: bool,
) -> int:
    if not db_path.exists():
        log.error("db not found: %s", db_path)
        return 2

    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
    except sqlite3.Error as e:
        log.error("connect failed: %s", e)
        return 2

    try:
        all_tables = list_user_tables(conn)
        targets = tables if tables else all_tables
        unknown = [t for t in targets if t not in all_tables]
        if unknown:
            log.error("unknown tables: %s (available: %s)", unknown, all_tables)
            return 2

        out_dir.mkdir(parents=True, exist_ok=True)
        log.info("output dir: %s", out_dir)

        total_rows = 0
        for t in targets:
            if t == "products" and split_by_platform:
                # 按平台拆分,products 那么大 Excel 会卡
                platforms = [r[0] for r in conn.execute(
                    "SELECT DISTINCT platform FROM products ORDER BY platform"
                ).fetchall()]
                for plat in platforms:
                    safe_plat = plat or "unknown"
                    out = out_dir / f"products_{safe_plat}.csv"
                    n = export_table(conn, "products", out, where=f"platform='{plat}'")
                    log.info("  %s: %d rows", out.name, n)
                    total_rows += n
            else:
                out = out_dir / f"{t}.csv"
                n = export_table(conn, t, out)
                log.info("  %s: %d rows", out.name, n)
                total_rows += n

        log.info("DONE: %d tables, %d rows total -> %s", len(targets), total_rows, out_dir)
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--out", type=Path, default=None,
        help="输出目录(默认 data/export_csv_时间戳)",
    )
    parser.add_argument(
        "--tables", type=str, default=None,
        help="逗号分隔的表名,如 products,t_hardware(默认全部)",
    )
    parser.add_argument(
        "--split-by-platform", action="store_true",
        help="products 表按 platform 列拆 csv(便于 Excel 打开)",
    )
    args = parser.parse_args()

    _configure_logging()

    if args.out is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        out_dir = DEFAULT_OUT_PARENT / f"export_csv_{stamp}"
    else:
        out_dir = args.out

    tables_list: list[str] | None = None
    if args.tables:
        tables_list = [t.strip() for t in args.tables.split(",") if t.strip()]

    return run(
        args.db,
        out_dir,
        tables=tables_list,
        split_by_platform=args.split_by_platform,
    )


if __name__ == "__main__":
    raise SystemExit(main())
