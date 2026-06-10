"""从 SQLite 或 MySQL 导出 t_hardware / t_hardware_price_history 为 CSV。

CSV 编码:utf-8-sig (Excel 友好),所有字段 QUOTE_ALL 避免转义歧义。
列顺序:严格按 _db_common.csv_columns(table),保证两边导入/导出一致。

用法:
    python scripts/csv_export.py --target sqlite --table t_hardware       --out data/csv_export/sqlite_hw.csv
    python scripts/csv_export.py --target mysql  --table t_hardware       --out data/csv_export/mysql_hw.csv
    python scripts/csv_export.py --target sqlite --table t_hardware_price_history --out data/csv_export/sqlite_ph.csv
    # 一次性双表双库:
    python scripts/csv_export.py --target both --out-dir data/csv_export
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from _db_common import (  # noqa: E402
    UNION_SCHEMAS, backend, csv_columns, force_utf8_stdout,
)

DEFAULT_SQLITE = ROOT / "data" / "prices.db"

log = logging.getLogger("csv_export")


def _setup_log() -> None:
    force_utf8_stdout()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                     datefmt="%H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def _serialize(v: object) -> str:
    """所有 None → 空串;datetime/date → ISO;其余 str()。"""
    if v is None:
        return ""
    return str(v)


def export_table(target: str, table: str, out_path: Path,
                 sqlite_path: str | None, where: str | None) -> int:
    cols = csv_columns(table)
    kw = {"sqlite_path": sqlite_path} if target == "sqlite" else {}
    with backend(target, **kw) as db:
        cur = db.cursor()
        try:
            select_cols = ", ".join(cols)
            sql = f"SELECT {select_cols} FROM {table}"
            if where:
                sql += f" WHERE {where}"
            cur.execute(sql)
            rows = cur.fetchall()
        finally:
            cur.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_ALL)
        w.writerow(cols)
        for row in rows:
            w.writerow(_serialize(v) for v in row)
    log.info("  ✓ %s → %s (%d 行,%d 列)", table, out_path, len(rows), len(cols))
    return len(rows)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", required=True, choices=("sqlite", "mysql", "both"))
    p.add_argument("--sqlite-path", default=str(DEFAULT_SQLITE))
    p.add_argument("--table", choices=tuple(UNION_SCHEMAS) + ("all",),
                   default="all", help="导哪张表")
    p.add_argument("--out", help="单表导出路径(指定 --table 时必填)")
    p.add_argument("--out-dir", help="多表导出目录(--table=all 时使用)")
    p.add_argument("--where", help="可选 WHERE 子句")
    args = p.parse_args()
    _setup_log()

    if args.table == "all":
        out_dir = Path(args.out_dir or "data/csv_export")
        out_dir.mkdir(parents=True, exist_ok=True)
        targets = ("sqlite", "mysql") if args.target == "both" else (args.target,)
        for t in targets:
            log.info("─── 后端 %s ───", t)
            for table in UNION_SCHEMAS:
                out = out_dir / f"{t}_{table}.csv"
                export_table(t, table, out, args.sqlite_path, args.where)
    else:
        if not args.out:
            p.error("--table 指定具体表时,--out 必填")
        targets = ("sqlite", "mysql") if args.target == "both" else (args.target,)
        for t in targets:
            log.info("─── 后端 %s ───", t)
            out_path = Path(args.out)
            if len(targets) > 1:
                # both 时输出名前缀区分
                out_path = out_path.with_name(f"{t}_{out_path.name}")
            export_table(t, args.table, out_path, args.sqlite_path, args.where)
    return 0


if __name__ == "__main__":
    sys.exit(main())
