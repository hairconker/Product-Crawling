"""将 logs/by_keyword/*.csv 导入 SQLite 数据库，并生成待查询的型号列表。"""
from __future__ import annotations

import csv
import glob
import os
import re
import sqlite3
from pathlib import Path

DB_PATH = Path("data/prices.db")
CSV_DIR = Path("logs/by_keyword")


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT 'xianyu',
            item_id TEXT NOT NULL,
            title TEXT,
            current_price REAL,
            origin_price REAL,
            shop_name TEXT,
            location TEXT,
            is_second_hand INTEGER DEFAULT 1,
            url TEXT,
            image_url TEXT,
            crawled_at TEXT,
            UNIQUE(keyword, platform, item_id)
        );

        CREATE TABLE IF NOT EXISTS model_info (
            model TEXT PRIMARY KEY,
            category TEXT,
            brand TEXT,
            release_year INTEGER,
            architecture TEXT,
            socket TEXT,
            tdp_w REAL,
            core_count INTEGER,
            thread_count INTEGER,
            base_freq_ghz REAL,
            boost_freq_ghz REAL,
            process_node TEXT,
            source_url TEXT,
            summary TEXT,
            updated_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_products_keyword ON products(keyword);
        CREATE INDEX IF NOT EXISTS idx_products_platform ON products(platform);
        CREATE INDEX IF NOT EXISTS idx_products_price ON products(current_price);
    """)


def import_csvs(conn: sqlite3.Connection) -> int:
    files = sorted(glob.glob(str(CSV_DIR / "*.csv")))
    total = 0
    cursor = conn.cursor()
    for fp in files:
        kw = os.path.splitext(os.path.basename(fp))[0]
        with open(fp, "r", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                price_str = (row.get("current_price") or "").strip()
                origin_str = (row.get("origin_price") or "").strip()
                try:
                    price = float(price_str) if price_str else None
                except ValueError:
                    price = None
                try:
                    origin_price = float(origin_str) if origin_str else None
                except ValueError:
                    origin_price = None

                cursor.execute(
                    """INSERT OR IGNORE INTO products
                       (keyword, platform, item_id, title, current_price, origin_price,
                        shop_name, location, is_second_hand, url, image_url, crawled_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        kw,
                        (row.get("platform") or "xianyu").lower(),
                        row.get("item_id", ""),
                        (row.get("title") or "")[:500],
                        price,
                        origin_price,
                        row.get("shop_name", ""),
                        row.get("location", ""),
                        1 if str(row.get("is_second_hand", "True")).lower() in ("true", "1", "yes") else 0,
                        row.get("url", ""),
                        row.get("image_url", ""),
                        row.get("crawled_at", ""),
                    ),
                )
                total += 1
        if total % 2000 == 0:
            conn.commit()
    conn.commit()
    return total


def extract_models(conn: sqlite3.Connection) -> list[str]:
    """从 keyword 列提取所有型号名称。"""
    rows = conn.execute("SELECT DISTINCT keyword FROM products ORDER BY keyword").fetchall()
    return [r[0] for r in rows]


def print_stats(conn: sqlite3.Connection) -> None:
    """输出概览统计。"""
    total = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    keywords = conn.execute("SELECT COUNT(DISTINCT keyword) FROM products").fetchone()[0]
    print(f"  商品总数: {total}")
    print(f"  关键词数: {keywords}")

    rows = conn.execute(
        "SELECT keyword, COUNT(*) as cnt, AVG(current_price), MIN(current_price), MAX(current_price) "
        "FROM products GROUP BY keyword ORDER BY cnt DESC LIMIT 10"
    ).fetchall()
    print("\n  ── 记录最多的10个型号 ──")
    for r in rows:
        print(f"  {r[0]:30s}  {r[1]:3d}t  avg={r[2]:.0f}  min={r[3]:.0f}  max={r[4]:.0f}")


def main() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")

    create_schema(conn)
    count = import_csvs(conn)
    print(f"导入完成: {count} 条记录")
    print_stats(conn)

    models = extract_models(conn)
    print(f"\n待查询型号: {len(models)} 个")
    # 列出几个样本
    for m in models[:5]:
        print(f"  {m}")

    conn.close()
    print(f"\n数据库: {DB_PATH.resolve()}")


if __name__ == "__main__":
    main()
