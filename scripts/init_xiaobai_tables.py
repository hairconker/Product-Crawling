"""初始化 t_hardware / t_hardware_price_history 两张新表(对齐 XiaoBai_Computer schema)。

设计原则:
- 完整照搬 XiaoBai E:\\XiaoBai_Computer/SpringBootTemplate/src/main/resources/sql/db.sql
  里 t_hardware (18 列) 和 t_hardware_price_history (8 列) 的所有原列、约束、索引,**一字不改**
- 在每张表末尾追加 autoPCBulid 项目需要的列(release_year/date/generation/source_tag 等),
  这些追加列对 XiaoBai 那边的 MyBatis Mapper 是"看不见但不报错"的,完全兼容
- MySQL → SQLite 类型映射:
    bigint → INTEGER
    varchar(N) / text → TEXT
    decimal(10,2) → REAL
    tinyint / int → INTEGER
    datetime / date → TEXT (ISO 8601)
- 使用 CREATE TABLE IF NOT EXISTS,完全幂等;不会破坏既有 model_info / products 表

用法:
    python scripts/init_xiaobai_tables.py
    python scripts/init_xiaobai_tables.py --db data/prices.db --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "prices.db"

log = logging.getLogger("init_xb")


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO)


# ─── DDL ─────────────────────────────────────────────────────────

DDL_T_HARDWARE = """
CREATE TABLE IF NOT EXISTS t_hardware (
    -- ┌─── 以下 19 列完全照搬 XiaoBai_Computer t_hardware (id 起到 update_time) ───┐
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL,                            -- cpu/gpu/motherboard/memory/ssd/psu/chassis/cooler/monitor/keyboardmouse
    brand TEXT,
    model TEXT NOT NULL,
    name_full TEXT,
    image TEXT,
    price_ref REAL DEFAULT 0.00,
    price_jd REAL DEFAULT 0.00,
    price_tb REAL DEFAULT 0.00,
    price_pdd REAL DEFAULT 0.00,
    link_jd TEXT,
    link_tb TEXT,
    link_pdd TEXT,
    params_json TEXT,                                  -- 参数JSON
    tier TEXT,                                         -- low/mid/high
    status INTEGER NOT NULL DEFAULT 1,                 -- 0下架 1上架
    sort INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    create_time TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    update_time TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    -- └─── 以下 4 列是 autoPCBulid 项目追加(发布日期补全管道用) ───┐
    release_year INTEGER,                              -- 发布年(整数)
    release_date TEXT,                                 -- 发布日期月/季度精度,如 '2024-10' / '2024Q4'
    generation TEXT,                                   -- 字典代际描述,如 'NV RTX 500 series'
    source_tag TEXT                                    -- 数据来源标识,如 docyx/nvidia_official/bing
);
"""

DDL_T_HARDWARE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_hw_cat ON t_hardware(category);",
    "CREATE INDEX IF NOT EXISTS idx_hw_model ON t_hardware(model);",
]

DDL_T_HARDWARE_PRICE_HISTORY = """
CREATE TABLE IF NOT EXISTS t_hardware_price_history (
    -- ┌─── 以下 8 列完全照搬 XiaoBai_Computer t_hardware_price_history ───┐
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hardware_id INTEGER NOT NULL,                      -- FK → t_hardware.id
    source TEXT,                                       -- jd/tb/pdd(autoPCBulid 扩展到 jd/xianyu/taobao)
    price REAL NOT NULL,                               -- 单价(autoPCBulid 的 current_price 落这里)
    record_date TEXT NOT NULL,                         -- 日期(autoPCBulid 的 DATE(crawled_at))
    deleted INTEGER NOT NULL DEFAULT 0,
    create_time TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    update_time TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    -- └─── 以下 12 列是 autoPCBulid 项目追加(多卖家观察值特有) ───┐
    keyword TEXT,                                      -- 搜索关键词 slug,兼容历史 products.keyword
    platform TEXT,                                     -- 同 source 冗余,保留 autoPCBulid 习惯
    item_id TEXT,                                      -- 卖家商品 ID(同型号多卖家,各有 item_id)
    title TEXT,                                        -- 卖家自起的商品标题
    origin_price REAL,                                 -- 原价(打折前)
    shop_name TEXT,
    location TEXT,
    is_second_hand INTEGER DEFAULT 1,                  -- 1=二手 0=全新
    url TEXT,
    image_url TEXT,
    image_local_path TEXT,                             -- 本地图片相对路径
    crawled_at TEXT,                                   -- 完整 ISO 时间戳(record_date 只到天)
    is_outlier INTEGER DEFAULT 0                       -- 价格离群标记
);
"""

DDL_T_HARDWARE_PRICE_HISTORY_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_hph_hid_date ON t_hardware_price_history(hardware_id, record_date);",
    "CREATE INDEX IF NOT EXISTS idx_hph_kw ON t_hardware_price_history(keyword);",
    "CREATE INDEX IF NOT EXISTS idx_hph_platform ON t_hardware_price_history(platform);",
    # UNIQUE 索引防止同一卖家同一天重复入库
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_hph_observation "
    "ON t_hardware_price_history(keyword, platform, item_id, record_date);",
]


# ─── 主流程 ──────────────────────────────────────────────────────


def run(db_path: Path, *, dry_run: bool) -> int:
    if not db_path.exists():
        log.error("db not found: %s", db_path)
        return 2

    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
    except sqlite3.Error as e:
        log.error("connect db failed: %s", e)
        return 2

    try:
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()

        # 检查已存在
        existing = {
            row[0]
            for row in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('t_hardware', 't_hardware_price_history')"
            ).fetchall()
        }
        log.info(
            "before: t_hardware=%s, t_hardware_price_history=%s",
            "EXISTS" if "t_hardware" in existing else "missing",
            "EXISTS" if "t_hardware_price_history" in existing else "missing",
        )

        if dry_run:
            log.info("dry-run: 将执行以下 DDL:")
            log.info("  - CREATE TABLE IF NOT EXISTS t_hardware (23 列)")
            for s in DDL_T_HARDWARE_INDEXES:
                log.info("  - %s", s.strip())
            log.info(
                "  - CREATE TABLE IF NOT EXISTS t_hardware_price_history (20 列)"
            )
            for s in DDL_T_HARDWARE_PRICE_HISTORY_INDEXES:
                log.info("  - %s", s.strip())
            return 0

        try:
            cur.executescript(DDL_T_HARDWARE)
            for stmt in DDL_T_HARDWARE_INDEXES:
                cur.execute(stmt)
            cur.executescript(DDL_T_HARDWARE_PRICE_HISTORY)
            for stmt in DDL_T_HARDWARE_PRICE_HISTORY_INDEXES:
                cur.execute(stmt)
            conn.commit()
        except sqlite3.Error as e:
            raise RuntimeError("DDL 执行失败") from e

        # 验证
        t_hw_cols = cur.execute("PRAGMA table_info(t_hardware)").fetchall()
        t_hph_cols = cur.execute(
            "PRAGMA table_info(t_hardware_price_history)"
        ).fetchall()
        t_hw_indexes = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='t_hardware'"
        ).fetchall()
        t_hph_indexes = cur.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='t_hardware_price_history'"
        ).fetchall()

        log.info(
            "OK: t_hardware -> %d columns, %d indexes",
            len(t_hw_cols),
            len(t_hw_indexes),
        )
        log.info(
            "OK: t_hardware_price_history -> %d columns, %d indexes",
            len(t_hph_cols),
            len(t_hph_indexes),
        )
        # 确认旧表无损
        old = cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('model_info', 'products')"
        ).fetchall()
        log.info("legacy tables intact: %s", [r[0] for r in old])

        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    _configure_logging()
    return run(args.db, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
