"""数据迁移:model_info / products → t_hardware / t_hardware_price_history。

设计:
- **幂等**:重跑不会产生重复(t_hardware 用 UNIQUE(category, model) 兜底,t_hardware_price_history
  用 UNIQUE(keyword, platform, item_id, record_date) 兜底)
- **失败容错**:products 中 keyword 反查不到 t_hardware 时 hardware_id 设 NULL,记 log 不中断
- **批量**:executemany 单事务提交,2W 行预计 < 5 秒
- **保留语义**:
    * model_info 的稀疏列(architecture/socket/tdp_w/cores/...)打包进 t_hardware.params_json
    * model_info.summary 也并进 params_json,model_info.source_url → t_hardware.link_jd(暂用)
    * products.current_price → t_hardware_price_history.price
    * products.crawled_at → record_date(取日期部分)+ crawled_at(完整时间戳)
    * products.platform 同时填 source 和 platform 列(冗余,双口径)
- **category 映射**:autoPCBulid 大写枚举 → XiaoBai 小写枚举(见 _CATEGORY_MAP)

用法:
    python scripts/migrate_to_xiaobai_schema.py --dry-run
    python scripts/migrate_to_xiaobai_schema.py
    python scripts/migrate_to_xiaobai_schema.py --only hardware   # 只迁主表
    python scripts/migrate_to_xiaobai_schema.py --only history    # 只迁历史表
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "prices.db"

log = logging.getLogger("migrate_xb")

# autoPCBulid (大写枚举) → XiaoBai (小写,语义对齐)
_CATEGORY_MAP: dict[str, str] = {
    "CPU": "cpu",
    "GPU": "gpu",
    "MB": "motherboard",
    "PSU": "psu",
    "COOLER": "cooler",
    "CASE": "chassis",
    "RAM": "memory",
    "STORAGE_SSD": "ssd",
    "STORAGE_HDD": "hdd",          # XiaoBai 没此分类,autoPCBulid 扩展
    "NIC_WIRED": "nic_wired",      # XiaoBai 没此分类,autoPCBulid 扩展
    "NIC_WIRELESS": "nic_wireless",
}

# model_info 中要打包进 params_json 的稀疏列
_PARAMS_FIELDS: tuple[str, ...] = (
    "architecture",
    "socket",
    "tdp_w",
    "core_count",
    "thread_count",
    "base_freq_ghz",
    "boost_freq_ghz",
    "process_node",
    "summary",
)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO)


# ─── model_info → t_hardware ─────────────────────────────────────


def _build_params_json(row: sqlite3.Row) -> str | None:
    """把 model_info 的稀疏列打包成 JSON。全空返回 None。"""
    payload: dict[str, Any] = {}
    for col in _PARAMS_FIELDS:
        val = row[col] if col in row.keys() else None
        if val is None or val == "":
            continue
        payload[col] = val
    if not payload:
        return None
    return json.dumps(payload, ensure_ascii=False)


def migrate_hardware(conn: sqlite3.Connection, *, dry_run: bool) -> tuple[int, int, int]:
    """迁移 model_info → t_hardware。返回 (read, inserted, skipped)。"""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM model_info ORDER BY category, model"
    ).fetchall()
    total = len(rows)
    log.info("model_info: %d rows to process", total)

    batch: list[tuple] = []
    unknown_cats: set[str] = set()
    for r in rows:
        src_cat = (r["category"] or "").strip()
        cat = _CATEGORY_MAP.get(src_cat)
        if cat is None:
            unknown_cats.add(src_cat)
            cat = src_cat.lower() if src_cat else "unknown"
        model = (r["model"] or "").strip()
        if not model:
            continue
        params_json = _build_params_json(r)
        updated_at = (r["updated_at"] or "")[:19].replace("T", " ")  # ISO → "YYYY-MM-DD HH:MM:SS"
        if not updated_at:
            updated_at = None
        batch.append(
            (
                cat,                         # category
                r["brand"] or None,           # brand
                model,                        # model
                None,                         # name_full(model_info 没有,留空)
                None,                         # image
                params_json,                  # params_json
                None,                         # tier(没数据,留空)
                r["release_year"],            # 追加 4 列
                r["release_date"],
                r["generation"],
                r["source_tag"],
                updated_at,                   # create_time(用 updated_at 兜底)
                updated_at,                   # update_time
            )
        )

    if unknown_cats:
        log.warning("unmapped categories: %s (lowercase fallback used)", unknown_cats)

    if dry_run:
        log.info("dry-run: would insert %d t_hardware rows", len(batch))
        for sample in batch[:3]:
            log.info("  sample: cat=%s brand=%s model=%s", sample[0], sample[1], sample[2])
        return total, 0, 0

    # INSERT OR IGNORE on UNIQUE(category, model) — 但表里没建 UNIQUE,先建
    cur = conn.cursor()
    # 临时加 UNIQUE(category, model)?不能 ALTER 加唯一约束。换用 NOT EXISTS 判重。
    inserted = 0
    skipped = 0
    sql = """
        INSERT INTO t_hardware
            (category, brand, model, name_full, image,
             params_json, tier,
             release_year, release_date, generation, source_tag,
             create_time, update_time)
        SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
               COALESCE(?, datetime('now', 'localtime')),
               COALESCE(?, datetime('now', 'localtime'))
        WHERE NOT EXISTS (
            SELECT 1 FROM t_hardware WHERE category = ? AND model = ?
        )
    """
    for row in batch:
        cat, model = row[0], row[2]
        cur.execute(sql, (*row, cat, model))
        if cur.rowcount == 1:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    log.info("t_hardware: inserted=%d skipped=%d (already exists)", inserted, skipped)
    return total, inserted, skipped


# ─── products → t_hardware_price_history ─────────────────────────


def _build_hardware_lookup(conn: sqlite3.Connection) -> dict[str, int]:
    """构造 {model_normalized: hardware_id} 反查表。

    `model_normalized` = lower(replace(model, ' ', '_'))
    products.keyword 也做同样规整后查这个表。
    """
    rows = conn.execute("SELECT id, model FROM t_hardware").fetchall()
    lookup: dict[str, int] = {}
    for hid, model in rows:
        if not model:
            continue
        key = model.lower().replace(" ", "_").replace("/", "_")
        # 第一个赢家(category, model UNIQUE 一对一时不会撞,但跨 category 同 model 时取先)
        if key not in lookup:
            lookup[key] = hid
    return lookup


def _norm_kw(kw: str) -> str:
    """products.keyword 已是 slug 形式(Arc_A310),转成 lookup key。"""
    return (kw or "").lower().replace(" ", "_").replace("/", "_")


def _coerce(v: Any) -> Any:
    """空字符串 → None,其他原样。"""
    if v == "":
        return None
    return v


def migrate_history(
    conn: sqlite3.Connection, *, dry_run: bool
) -> tuple[int, int, int, int]:
    """迁移 products → t_hardware_price_history。

    返回 (read, inserted, skipped, orphan)。
    orphan = keyword 反查不到 hardware_id 的行数(hardware_id 写 NULL)。
    """
    lookup = _build_hardware_lookup(conn)
    log.info("hardware lookup: %d distinct (model→id) entries", len(lookup))

    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    total = len(rows)
    log.info("products: %d rows to process", total)

    batch: list[tuple] = []
    orphan = 0
    for r in rows:
        kw = (r["keyword"] or "").strip()
        if not kw:
            continue
        key = _norm_kw(kw)
        hid = lookup.get(key)
        if hid is None:
            orphan += 1
        platform = (r["platform"] or "xianyu").lower()
        # source 跟 platform 同步,jd→jd, xianyu→xianyu(XiaoBai 风格 tb→tb 这里走 taobao)
        source = platform
        crawled = r["crawled_at"] or ""
        record_date = crawled[:10] if crawled else None  # ISO 'YYYY-MM-DDTHH:MM:SS' → 'YYYY-MM-DD'
        price = _coerce(r["current_price"])
        if price is None:
            # price NOT NULL,跳过价格缺失行(虽 schema 允许填 NULL,但 XiaoBai 那边 NOT NULL)
            continue
        # 1=二手, 0=全新; products.is_second_hand
        ish = 1 if r["is_second_hand"] else 0
        batch.append(
            (
                hid,                                  # hardware_id
                source,                                # source
                price,                                 # price
                record_date,                           # record_date
                kw,                                    # keyword (autoPCBulid 追加)
                platform,                              # platform
                r["item_id"] or "",                    # item_id
                r["title"] or None,                    # title
                _coerce(r["origin_price"]),            # origin_price
                r["shop_name"] or None,                # shop_name
                r["location"] or None,                 # location
                ish,                                   # is_second_hand
                r["url"] or None,                      # url
                r["image_url"] or None,                # image_url
                r["image_local_path"] or None,         # image_local_path
                crawled or None,                       # crawled_at
                1 if r["is_outlier"] else 0,           # is_outlier
            )
        )

    log.info("staging: %d valid rows, %d orphan (hardware_id NULL)", len(batch), orphan)

    if dry_run:
        for sample in batch[:3]:
            log.info(
                "  sample: hid=%s source=%s price=%.2f kw=%s",
                sample[0],
                sample[1],
                sample[2],
                sample[4],
            )
        return total, 0, 0, orphan

    # INSERT OR IGNORE 依赖 uq_hph_observation(keyword, platform, item_id, record_date)
    sql = """
        INSERT OR IGNORE INTO t_hardware_price_history
            (hardware_id, source, price, record_date,
             keyword, platform, item_id, title, origin_price,
             shop_name, location, is_second_hand,
             url, image_url, image_local_path, crawled_at, is_outlier)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    cur = conn.cursor()
    cur.executemany(sql, batch)
    inserted = cur.rowcount  # executemany 后 rowcount = 总插入数
    conn.commit()
    skipped = len(batch) - inserted
    log.info(
        "t_hardware_price_history: inserted=%d skipped=%d (uq match) orphan=%d",
        inserted,
        skipped,
        orphan,
    )
    return total, inserted, skipped, orphan


# ─── 主流程 ──────────────────────────────────────────────────────


def run(db_path: Path, *, only: str | None, dry_run: bool) -> int:
    if not db_path.exists():
        log.error("db not found: %s", db_path)
        return 2
    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
    except sqlite3.Error as e:
        log.error("connect failed: %s", e)
        return 2

    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=OFF")  # 允许 hardware_id 暂时 NULL(孤儿)

        # 先验证新表存在
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('t_hardware', 't_hardware_price_history')"
            ).fetchall()
        }
        for need in ("t_hardware", "t_hardware_price_history"):
            if need not in existing:
                log.error("table %s missing, run init_xiaobai_tables.py first", need)
                return 2

        if only in (None, "hardware"):
            migrate_hardware(conn, dry_run=dry_run)

        if only in (None, "history"):
            migrate_history(conn, dry_run=dry_run)

        if not dry_run:
            hw_count = conn.execute("SELECT COUNT(*) FROM t_hardware").fetchone()[0]
            hph_count = conn.execute(
                "SELECT COUNT(*) FROM t_hardware_price_history"
            ).fetchone()[0]
            hph_with_hid = conn.execute(
                "SELECT COUNT(*) FROM t_hardware_price_history "
                "WHERE hardware_id IS NOT NULL"
            ).fetchone()[0]
            log.info(
                "FINAL t_hardware=%d  t_hardware_price_history=%d (linked=%d)",
                hw_count,
                hph_count,
                hph_with_hid,
            )
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument(
        "--only", choices=("hardware", "history"), default=None,
        help="只迁某一张表",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    _configure_logging()
    return run(args.db, only=args.only, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
