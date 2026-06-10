"""把 CSV UPSERT 到 SQLite 或 MySQL 的 t_hardware / t_hardware_price_history。

合并策略(--merge-mode):
- skip-existing : 命中唯一键时不动,只 INSERT 新行(默认,最保守)
- xiaobai-wins  : 命中唯一键时,只填目标表的 NULL/0/空字段(目标已有真实值时不覆盖)
- overwrite     : 命中唯一键时,CSV 全量覆盖目标行(慎用)

UPSERT 唯一键:
- t_hardware                : (category, model)
- t_hardware_price_history  : (platform, item_id, record_date)

字段映射规则:
- image / image_url:CSV 里两个字段都会原样落库,前端读 image;import 时若目标 image 为空且
  CSV 提供了 image_url(history 表才有)且当前是 hardware 表导入,自动从 CSV image 列取
- release_date / release_year:DATE/INT 列,空串视为 NULL
- price_ref/jd/tb/pdd/xianyu:空串视为 NULL,数字串转 float
- is_second_hand / is_outlier / status / deleted:空串视为 NULL/0,数字串转 int

用法:
    # CSV → SQLite (导回本机字典库)
    python scripts/csv_import.py --target sqlite --table t_hardware --csv data/csv_export/mysql_t_hardware.csv --merge-mode skip-existing

    # CSV → MySQL (灌 XiaoBai 装机库,常用)
    python scripts/csv_import.py --target mysql --table t_hardware --csv data/csv_export/sqlite_t_hardware.csv --merge-mode xiaobai-wins --dry-run
    python scripts/csv_import.py --target mysql --table t_hardware --csv data/csv_export/sqlite_t_hardware.csv --merge-mode xiaobai-wins --apply

    # 闲鱼成交记录灌 MySQL
    python scripts/csv_import.py --target mysql --table t_hardware_price_history --csv data/csv_export/sqlite_t_hardware_price_history.csv --merge-mode skip-existing --apply
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from _db_common import (  # noqa: E402
    UNION_SCHEMAS, UPSERT_KEYS, backend, csv_columns, force_utf8_stdout,
)

DEFAULT_SQLITE = ROOT / "data" / "prices.db"

log = logging.getLogger("csv_import")

# 各列的类型转换(用于把 CSV str 转回 DB 值)
_INT_COLS = {"release_year", "is_second_hand", "is_outlier", "status",
             "deleted", "sort", "hardware_id"}
_FLOAT_COLS = {"price_ref", "price_jd", "price_tb", "price_pdd", "price_xianyu",
               "price", "origin_price"}
# 空串 → None 的列;其余空串保留为 ''(后端 NOT NULL TEXT 类型才不报错)
_NULLABLE_COLS = {
    "brand", "name_full", "image", "link_jd", "link_tb", "link_pdd",
    "params_json", "tier", "release_year", "release_date", "generation", "source_tag",
    "hardware_id", "source", "keyword", "platform", "item_id", "title",
    "shop_name", "location", "is_second_hand", "is_outlier",
    "url", "image_url", "image_local_path",
    "crawled_at", "origin_price", "price_xianyu",
}


def _setup_log() -> None:
    force_utf8_stdout()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                     datefmt="%H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def _coerce(value: str, col: str) -> Any:
    if value == "":
        if col in _NULLABLE_COLS:
            return None
        if col in _INT_COLS:
            return 0
        if col in _FLOAT_COLS:
            return 0.0
        return None
    if col in _INT_COLS:
        try:
            return int(float(value))
        except ValueError:
            return None
    if col in _FLOAT_COLS:
        try:
            return float(value)
        except ValueError:
            return None
    return value


def _read_csv(path: Path, cols: list[str]) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            log.error("CSV 无表头: %s", path)
            return []
        # 跳过额外列,补齐缺失列为空
        rows: list[dict[str, Any]] = []
        missing = set(cols) - set(reader.fieldnames)
        extra = set(reader.fieldnames) - set(cols)
        if missing:
            log.warning("  CSV 缺列(将补 None): %s", sorted(missing))
        if extra:
            log.warning("  CSV 多列(将忽略): %s", sorted(extra))
        for r in reader:
            row = {c: _coerce(r.get(c, "") or "", c) for c in cols}
            rows.append(row)
    return rows


def _key_canonical(v: Any) -> Any:
    """把 key 列值统一成 csv-friendly 形式(MySQL DATE/DATETIME → str),
    避免 'xianyu','123','2026-04-24'(str) 和 'xianyu','123',date(2026,4,24)(date) 不相等。"""
    if v is None:
        return None
    # date/datetime → ISO 字符串
    import datetime as _dt
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()[:10] if not isinstance(v, _dt.datetime) else v.isoformat(sep=" ")
    # int/float 的 item_id 也归一化为 str(CSV 来源就是 str)
    if isinstance(v, (int, float)):
        return str(v)
    return v


def _load_existing_keys(db, table: str, keys: tuple[str, ...]) -> dict[tuple, dict[str, Any]]:
    """读目标表里已有的 UPSERT 键 → 全行字典,供合并决策。"""
    cur = db.cursor()
    cols = csv_columns(table)
    try:
        cur.execute(f"SELECT {', '.join(cols)} FROM {table}")
        out: dict[tuple, dict[str, Any]] = {}
        for row in cur.fetchall():
            row_dict = dict(zip(cols, row))
            key = tuple(_key_canonical(row_dict[k]) for k in keys)
            if any(v is None or v == "" for v in key):
                continue  # 唯一键含 NULL 跳过
            out[key] = row_dict
        return out
    finally:
        cur.close()


def _merge_row(target: dict[str, Any], source: dict[str, Any],
               mode: str) -> dict[str, Any] | None:
    """根据 mode 返回新行(供 UPDATE)。返回 None 表示无需更新。"""
    if mode == "skip-existing":
        return None
    if mode == "overwrite":
        # CSV 提供的非键字段全量替换
        return dict(source)
    if mode == "xiaobai-wins":
        # 仅填目标 NULL/0/'' 的字段
        out = dict(target)
        changed = False
        for k, v in source.items():
            if v is None or v == "":
                continue
            cur = target.get(k)
            empty_target = cur is None or cur == "" or cur == 0 or cur == 0.0
            if empty_target:
                out[k] = v
                changed = True
        return out if changed else None
    raise ValueError(f"未知 merge-mode: {mode}")


def _row_to_args(row: dict[str, Any], cols: list[str]) -> tuple:
    return tuple(row[c] for c in cols)


def import_csv(target: str, table: str, csv_path: Path, mode: str,
               sqlite_path: str | None, dry_run: bool) -> dict[str, int]:
    cols = csv_columns(table)
    keys = UPSERT_KEYS[table]
    log.info("CSV 列数: %d, 唯一键: %s, mode=%s", len(cols), keys, mode)

    rows = _read_csv(csv_path, cols)
    log.info("读取 CSV: %d 行", len(rows))

    kw = {"sqlite_path": sqlite_path} if target == "sqlite" else {}
    stats: dict[str, int] = defaultdict(int)
    with backend(target, **kw) as db:
        existing = _load_existing_keys(db, table, keys)
        log.info("目标库 %s 已有 %d 个唯一键", table, len(existing))

        cur = db.cursor()
        ph = db.placeholder()
        insert_cols = [c for c in cols if c not in {"create_time", "update_time"}]
        # 避免覆盖 DEFAULT CURRENT_TIMESTAMP
        insert_sql = (
            f"INSERT INTO {table} ({', '.join(insert_cols)}) "
            f"VALUES ({', '.join([ph] * len(insert_cols))})"
        )
        update_cols = [c for c in cols if c not in {"id"} and c not in keys
                       and c not in {"create_time"}]

        try:
            for row in rows:
                key = tuple(_key_canonical(row.get(k)) for k in keys)
                if any(v is None or v == "" for v in key):
                    stats["skip_null_key"] += 1
                    continue
                if key in existing:
                    new_row = _merge_row(existing[key], row, mode)
                    if new_row is None:
                        stats["skip_existing"] += 1
                        continue
                    sets = ", ".join(f"{c}={ph}" for c in update_cols)
                    where = " AND ".join(f"{k}={ph}" for k in keys)
                    sql = f"UPDATE {table} SET {sets} WHERE {where}"
                    args = tuple(new_row.get(c) for c in update_cols) + key
                    stats["updated"] += 1
                    if not dry_run:
                        cur.execute(sql, args)
                else:
                    args = tuple(row.get(c) for c in insert_cols)
                    if dry_run:
                        stats["inserted"] += 1
                        continue
                    try:
                        cur.execute(insert_sql, args)
                        stats["inserted"] += 1
                    except Exception as e:
                        stats["insert_error"] += 1
                        if stats["insert_error"] <= 3:
                            log.warning("    insert 失败示例: %s | row=%s", e,
                                        {k: row.get(k) for k in keys})
            if dry_run:
                db.rollback()
                log.info("DRY-RUN 完成,未提交")
            else:
                db.commit()
                log.info("✓ 提交完成")
        finally:
            cur.close()
    return dict(stats)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", required=True, choices=("sqlite", "mysql"))
    p.add_argument("--sqlite-path", default=str(DEFAULT_SQLITE))
    p.add_argument("--table", required=True, choices=tuple(UNION_SCHEMAS))
    p.add_argument("--csv", required=True, help="CSV 文件路径")
    p.add_argument("--merge-mode", choices=("skip-existing", "xiaobai-wins", "overwrite"),
                   default="skip-existing")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.apply:
        p.error("--dry-run 或 --apply 必选其一")
    if args.dry_run and args.apply:
        p.error("互斥")

    _setup_log()
    stats = import_csv(args.target, args.table, Path(args.csv),
                       args.merge_mode, args.sqlite_path, args.dry_run)
    log.info("─── 统计 ───")
    for k, v in sorted(stats.items()):
        log.info("  %s = %d", k, v)
    return 0


if __name__ == "__main__":
    sys.exit(main())
