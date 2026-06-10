"""统一两边数据库 schema —— 给 SQLite + MySQL 同时 ALTER 到合集模式。

合集来源:scripts/_db_common.py::UNION_SCHEMAS

用法:
    python scripts/align_schema.py --target sqlite --dry-run
    python scripts/align_schema.py --target sqlite --apply
    python scripts/align_schema.py --target mysql  --apply
    python scripts/align_schema.py --target both   --apply
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from _db_common import (  # noqa: E402
    UNION_SCHEMAS, UPSERT_KEYS, backend, force_utf8_stdout,
)

DEFAULT_SQLITE = ROOT / "data" / "prices.db"

log = logging.getLogger("align_schema")


def _setup_log() -> None:
    force_utf8_stdout()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                     datefmt="%H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def _plan_table(db, table: str) -> list[str]:
    """返回该表需要执行的 ALTER 列表。"""
    existing = db.existing_columns(table)
    if not existing:
        log.warning("  %s: 表不存在,跳过(请先用 init_xiaobai_tables.py 建表)", table)
        return []
    plans: list[str] = []
    for col, mysql_ddl, sqlite_ddl in UNION_SCHEMAS[table]:
        if col in existing:
            continue
        ddl = sqlite_ddl if db.kind == "sqlite" else mysql_ddl
        # AUTO_INCREMENT / PRIMARY KEY 不可后期 ALTER 添加,只在新表里出现
        if "AUTO_INCREMENT" in ddl or "PRIMARY KEY" in ddl:
            log.warning("  %s.%s: 主键列缺失但无法 ALTER,跳过(需要重建表)", table, col)
            continue
        plans.append(f"ALTER TABLE `{table}` ADD COLUMN `{col}` {ddl}")
    return plans


def _plan_indexes(db, table: str) -> list[str]:
    """为业务唯一键加 UNIQUE INDEX,便于 UPSERT 幂等。"""
    keys = UPSERT_KEYS.get(table)
    if not keys:
        return []
    idx_name = f"uk_{'_'.join(keys)}"
    if idx_name in db.existing_indexes(table):
        return []
    # 不加前缀:platform/item_id/category 都 <= 128 字符,加起来远低于 InnoDB 3072B 上限
    cols = ", ".join(f"`{c}`" if db.kind == "mysql" else c for c in keys)
    return [f"CREATE UNIQUE INDEX {idx_name} ON {table} ({cols})"]


def _dedupe_for_unique(db, cur, create_index_sql: str) -> None:
    """从 'CREATE UNIQUE INDEX name ON table (col, col, col)' 反解出表 + 列,
    保留每组最大 id,DELETE 其余,使 UNIQUE 索引创建成功。"""
    import re as _re
    m = _re.search(r"ON\s+(\w+)\s*\(([^)]+)\)", create_index_sql, _re.IGNORECASE)
    if not m:
        raise RuntimeError(f"无法解析: {create_index_sql}")
    table = m.group(1)
    cols_raw = m.group(2)
    cols = [c.strip().strip("`").split("(")[0] for c in cols_raw.split(",")]
    cols_csv = ", ".join(cols)
    ph_table = f"`{table}`" if db.kind == "mysql" else table
    # 找出每组的 max(id)
    cur.execute(
        f"SELECT {cols_csv}, COUNT(*) FROM {ph_table} GROUP BY {cols_csv} HAVING COUNT(*) > 1"
    )
    dup_groups = cur.fetchall()
    log.warning("    %s 重复键组: %d", table, len(dup_groups))
    if not dup_groups:
        return
    # 每组保留最大 id,删除其余
    where_clause = " AND ".join(f"{c} = " + db.placeholder() for c in cols)
    # SQLite/MySQL 都用「双绑定」:外层 WHERE + 内层 SELECT MAX(id) 各一份键
    delete_sql = (
        f"DELETE FROM {ph_table} WHERE {where_clause} AND id NOT IN "
        f"(SELECT max_id FROM (SELECT MAX(id) AS max_id FROM {ph_table} "
        f"WHERE {where_clause}) AS sub)"
    ) if db.kind == "mysql" else (
        f"DELETE FROM {ph_table} WHERE {where_clause} AND id < "
        f"(SELECT MAX(id) FROM {ph_table} WHERE {where_clause})"
    )
    total_deleted = 0
    for grp in dup_groups:
        key_vals = tuple(grp[:-1])
        cur.execute(delete_sql, key_vals + key_vals)
        total_deleted += cur.rowcount
    log.warning("    %s dedupe 删除 %d 行", table, total_deleted)


def align_one(target: str, sqlite_path: str | None, dry_run: bool) -> None:
    log.info("─── 后端: %s ───", target)
    kwargs = {"sqlite_path": sqlite_path} if target == "sqlite" else {}
    with backend(target, **kwargs) as db:
        cur = db.cursor()
        try:
            all_plans: list[str] = []
            for table in UNION_SCHEMAS:
                log.info("  检查表 %s", table)
                all_plans += _plan_table(db, table)
                all_plans += _plan_indexes(db, table)
            if not all_plans:
                log.info("  schema 已经对齐,无变更")
                return
            log.info("  即将执行 %d 条 DDL:", len(all_plans))
            for sql in all_plans:
                log.info("    %s", sql)
            if dry_run:
                log.info("  DRY-RUN 不写库")
                return
            for sql in all_plans:
                try:
                    cur.execute(sql)
                except Exception as e:
                    msg = str(e).lower()
                    # 1060(MySQL Duplicate column) / 1061 / 1068 / SQLite "already exists"
                    if any(k in msg for k in ("duplicate column",
                                               "already exists", "1060", "1061")):
                        log.warning("    幂等忽略: %s", e)
                        continue
                    # UNIQUE INDEX 失败 → 历史数据有 dup,先 dedupe 再重试
                    if "unique constraint failed" in msg or "duplicate entry" in msg:
                        log.warning("    UNIQUE 创建失败,有重复行,先 dedupe: %s", e)
                        _dedupe_for_unique(db, cur, sql)
                        cur.execute(sql)
                        continue
                    raise
            db.commit()
            log.info("  ✓ 写入完成")
        finally:
            cur.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", required=True, choices=("sqlite", "mysql", "both"))
    p.add_argument("--sqlite-path", default=str(DEFAULT_SQLITE))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.apply:
        p.error("必须指定 --dry-run 或 --apply")
    if args.dry_run and args.apply:
        p.error("--dry-run 和 --apply 互斥")

    _setup_log()
    targets = ("sqlite", "mysql") if args.target == "both" else (args.target,)
    for t in targets:
        align_one(t, args.sqlite_path, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
