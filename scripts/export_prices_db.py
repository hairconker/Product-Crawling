#!/usr/bin/env python3
"""Export dictionary-mode crawl results to SQLite and a MySQL import script."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
_SKU_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class SkuRow:
    week: str
    category: str
    brand: str
    sku_slug: str
    sku_title: str
    chip: str | None
    spec_json: str | None
    source: str | None
    origin_count: int | None
    alt_titles_json: str
    price_json_path: str | None
    crawled_at: str | None


@dataclass(frozen=True)
class StatusRow:
    week: str
    category: str
    brand: str
    sku_slug: str
    platform: str
    status: str
    error: str | None
    crawled_at: str | None
    search_terms_json: str
    source_file: str


@dataclass(frozen=True)
class PriceRow:
    observation_key: str
    week: str
    category: str
    brand: str
    sku_slug: str
    sku_title: str
    platform: str
    keyword: str
    item_id: str
    title: str
    url: str
    current_price: float | None
    origin_price: float | None
    shop_name: str | None
    location: str | None
    image_url: str | None
    is_second_hand: int
    crawled_at: str | None
    source_file: str


@dataclass(frozen=True)
class ExportRows:
    skus: dict[tuple[str, str, str, str], SkuRow]
    statuses: list[StatusRow]
    prices: dict[str, PriceRow]


def _sku_slug(title: str, max_len: int = 80) -> str:
    raw = _SKU_SLUG_RE.sub("-", title.lower()).strip("-") or "unknown"
    tail = hashlib.md5(title.encode("utf-8")).hexdigest()[:6]
    if len(raw) + 7 <= max_len:
        return f"{raw}-{tail}"
    return f"{raw[:max_len - 7]}-{tail}"


def _json_text(value: Any, *, default: str | None = None) -> str | None:
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return default
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default
        return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return default


def _to_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _observation_key(
    week: str,
    category: str,
    brand: str,
    sku_slug: str,
    platform: str,
    item_id: str,
    url: str,
    title: str,
) -> str:
    raw = "\0".join([week, category, brand, sku_slug, platform, item_id, url, title])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _iter_sku_csv_files(
    week: str,
    data_dir: Path,
    categories: set[str] | None,
) -> Iterable[tuple[str, str, Path]]:
    merged_root = data_dir / "skus" / week / "_merged"
    if not merged_root.exists():
        raise FileNotFoundError(f"missing SKU dictionary: {merged_root}")
    for category_dir in sorted(path for path in merged_root.iterdir() if path.is_dir()):
        category = category_dir.name
        if categories is not None and category not in categories:
            continue
        for brand_file in sorted(category_dir.glob("*.csv")):
            yield category, brand_file.stem, brand_file


def _read_skus(
    week: str,
    data_dir: Path,
    categories: set[str] | None,
) -> dict[tuple[str, str, str, str], SkuRow]:
    rows: dict[tuple[str, str, str, str], SkuRow] = {}
    for category, brand, path in _iter_sku_csv_files(week, data_dir, categories):
        with path.open("r", encoding="utf-8-sig", newline="") as fp:
            reader = csv.DictReader(fp)
            for raw in reader:
                title = (raw.get("sku_title") or "").strip()
                if not title:
                    continue
                sku_slug = _sku_slug(title)
                key = (week, category, brand, sku_slug)
                spec_json = _json_text(raw.get("spec_json"), default=None)
                alt_titles_json = _json_text(raw.get("alt_titles"), default="[]") or "[]"
                rows[key] = SkuRow(
                    week=week,
                    category=category,
                    brand=brand,
                    sku_slug=sku_slug,
                    sku_title=title,
                    chip=_as_text(raw.get("chip")),
                    spec_json=spec_json,
                    source=_as_text(raw.get("source")),
                    origin_count=_to_int(raw.get("origin_count")),
                    alt_titles_json=alt_titles_json,
                    price_json_path=None,
                    crawled_at=None,
                )
    return rows


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning("skip bad price JSON %s: %s", path, exc)
        return None
    if not isinstance(payload, dict):
        logging.warning("skip non-object price JSON %s", path)
        return None
    return payload


def _overlay_sku_from_price_json(
    rows: dict[tuple[str, str, str, str], SkuRow],
    path: Path,
    payload: dict[str, Any],
) -> tuple[str, str, str, str]:
    week = str(payload.get("week") or "")
    category = str(payload.get("category") or path.parent.parent.name)
    brand = str(payload.get("brand") or path.parent.name)
    sku_slug = path.stem
    sku_title = str(payload.get("sku_title") or sku_slug)
    key = (week, category, brand, sku_slug)
    existing = rows.get(key)
    rows[key] = SkuRow(
        week=week,
        category=category,
        brand=brand,
        sku_slug=sku_slug,
        sku_title=existing.sku_title if existing else sku_title,
        chip=existing.chip if existing else _as_text(payload.get("chip")),
        spec_json=existing.spec_json if existing else None,
        source=existing.source if existing else None,
        origin_count=existing.origin_count if existing else None,
        alt_titles_json=existing.alt_titles_json if existing else "[]",
        price_json_path=str(path),
        crawled_at=_as_text(payload.get("crawled_at")),
    )
    return key


def _read_prices(
    week: str,
    data_dir: Path,
    rows: dict[tuple[str, str, str, str], SkuRow],
    categories: set[str] | None,
) -> tuple[list[StatusRow], dict[str, PriceRow]]:
    prices_root = data_dir / "prices" / week
    statuses: list[StatusRow] = []
    prices: dict[str, PriceRow] = {}
    if not prices_root.exists():
        return statuses, prices
    for path in sorted(prices_root.rglob("*.json")):
        payload = _read_json_file(path)
        if payload is None:
            continue
        category = str(payload.get("category") or path.parent.parent.name)
        if categories is not None and category not in categories:
            continue
        key = _overlay_sku_from_price_json(rows, path, payload)
        sku = rows[key]
        search_terms_json = _json_text(payload.get("search_terms_used"), default="[]") or "[]"
        crawled_at = _as_text(payload.get("crawled_at"))
        platforms = payload.get("platforms")
        if not isinstance(platforms, dict):
            continue
        for platform, platform_payload in sorted(platforms.items()):
            if not isinstance(platform_payload, dict):
                continue
            status = str(platform_payload.get("status") or "unknown")
            statuses.append(
                StatusRow(
                    week=sku.week,
                    category=sku.category,
                    brand=sku.brand,
                    sku_slug=sku.sku_slug,
                    platform=str(platform),
                    status=status,
                    error=_as_text(platform_payload.get("error")),
                    crawled_at=crawled_at,
                    search_terms_json=search_terms_json,
                    source_file=str(path),
                )
            )
            items = platform_payload.get("items")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_platform = str(item.get("platform") or platform)
                item_id = str(item.get("item_id") or "")
                title = str(item.get("title") or "")
                url = str(item.get("url") or "")
                obs_key = _observation_key(
                    sku.week, sku.category, sku.brand, sku.sku_slug,
                    item_platform, item_id, url, title,
                )
                prices[obs_key] = PriceRow(
                    observation_key=obs_key,
                    week=sku.week,
                    category=sku.category,
                    brand=sku.brand,
                    sku_slug=sku.sku_slug,
                    sku_title=sku.sku_title,
                    platform=item_platform,
                    keyword=sku.sku_title,
                    item_id=item_id,
                    title=title,
                    url=url,
                    current_price=_to_float(item.get("current_price")),
                    origin_price=_to_float(item.get("origin_price")),
                    shop_name=_as_text(item.get("shop_name")),
                    location=_as_text(item.get("location")),
                    image_url=_as_text(item.get("image_url")),
                    is_second_hand=1 if bool(item.get("is_second_hand")) else 0,
                    crawled_at=crawled_at,
                    source_file=str(path),
                )
    return statuses, prices


def collect_rows(week: str, data_dir: Path, categories: set[str] | None) -> ExportRows:
    skus = _read_skus(week, data_dir, categories)
    statuses, prices = _read_prices(week, data_dir, skus, categories)
    return ExportRows(skus=skus, statuses=statuses, prices=prices)


SQLITE_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE sku (
  week TEXT NOT NULL,
  category TEXT NOT NULL,
  brand TEXT NOT NULL,
  sku_slug TEXT NOT NULL,
  sku_title TEXT NOT NULL,
  chip TEXT,
  spec_json TEXT,
  source TEXT,
  origin_count INTEGER,
  alt_titles_json TEXT NOT NULL,
  price_json_path TEXT,
  crawled_at TEXT,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (week, category, brand, sku_slug)
);

CREATE TABLE crawl_status (
  week TEXT NOT NULL,
  category TEXT NOT NULL,
  brand TEXT NOT NULL,
  sku_slug TEXT NOT NULL,
  platform TEXT NOT NULL,
  status TEXT NOT NULL,
  error TEXT,
  crawled_at TEXT,
  search_terms_json TEXT NOT NULL,
  source_file TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (week, category, brand, sku_slug, platform)
);

CREATE TABLE price_observation (
  observation_key TEXT PRIMARY KEY,
  week TEXT NOT NULL,
  category TEXT NOT NULL,
  brand TEXT NOT NULL,
  sku_slug TEXT NOT NULL,
  sku_title TEXT NOT NULL,
  platform TEXT NOT NULL,
  keyword TEXT NOT NULL,
  item_id TEXT,
  title TEXT NOT NULL,
  url TEXT NOT NULL,
  current_price REAL,
  origin_price REAL,
  shop_name TEXT,
  location TEXT,
  image_url TEXT,
  is_second_hand INTEGER NOT NULL,
  crawled_at TEXT,
  source_file TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE INDEX idx_price_lookup ON price_observation
  (week, category, brand, sku_slug, platform, current_price);
CREATE INDEX idx_price_item ON price_observation (platform, item_id);
CREATE INDEX idx_status_status ON crawl_status (week, platform, status);
"""


def write_sqlite(rows: ExportRows, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(SQLITE_SCHEMA)
        conn.executemany(
            """
            INSERT OR REPLACE INTO sku (
              week, category, brand, sku_slug, sku_title, chip, spec_json,
              source, origin_count, alt_titles_json, price_json_path,
              crawled_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    s.week, s.category, s.brand, s.sku_slug, s.sku_title, s.chip,
                    s.spec_json, s.source, s.origin_count, s.alt_titles_json,
                    s.price_json_path, s.crawled_at, now,
                )
                for s in rows.skus.values()
            ],
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO crawl_status (
              week, category, brand, sku_slug, platform, status, error,
              crawled_at, search_terms_json, source_file, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    s.week, s.category, s.brand, s.sku_slug, s.platform,
                    s.status, s.error, s.crawled_at, s.search_terms_json,
                    s.source_file, now,
                )
                for s in rows.statuses
            ],
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO price_observation (
              observation_key, week, category, brand, sku_slug, sku_title,
              platform, keyword, item_id, title, url, current_price,
              origin_price, shop_name, location, image_url, is_second_hand,
              crawled_at, source_file, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    p.observation_key, p.week, p.category, p.brand, p.sku_slug,
                    p.sku_title, p.platform, p.keyword, p.item_id, p.title,
                    p.url, p.current_price, p.origin_price, p.shop_name,
                    p.location, p.image_url, p.is_second_hand, p.crawled_at,
                    p.source_file, now,
                )
                for p in rows.prices.values()
            ],
        )
        conn.commit()
    finally:
        conn.close()
    tmp.replace(path)


MYSQL_SCHEMA = """
CREATE DATABASE IF NOT EXISTS `price_crawler`
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE `price_crawler`;

CREATE TABLE IF NOT EXISTS `sku` (
  `week` VARCHAR(10) NOT NULL,
  `category` VARCHAR(64) NOT NULL,
  `brand` VARCHAR(128) NOT NULL,
  `sku_slug` VARCHAR(96) NOT NULL,
  `sku_title` VARCHAR(512) NOT NULL,
  `chip` VARCHAR(255) NULL,
  `spec_json` JSON NULL,
  `source` VARCHAR(128) NULL,
  `origin_count` INT NULL,
  `alt_titles_json` JSON NOT NULL,
  `price_json_path` VARCHAR(1024) NULL,
  `crawled_at` VARCHAR(64) NULL,
  `updated_at` VARCHAR(64) NOT NULL,
  PRIMARY KEY (`week`, `category`, `brand`, `sku_slug`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `crawl_status` (
  `week` VARCHAR(10) NOT NULL,
  `category` VARCHAR(64) NOT NULL,
  `brand` VARCHAR(128) NOT NULL,
  `sku_slug` VARCHAR(96) NOT NULL,
  `platform` VARCHAR(32) NOT NULL,
  `status` VARCHAR(32) NOT NULL,
  `error` TEXT NULL,
  `crawled_at` VARCHAR(64) NULL,
  `search_terms_json` JSON NOT NULL,
  `source_file` VARCHAR(1024) NOT NULL,
  `updated_at` VARCHAR(64) NOT NULL,
  PRIMARY KEY (`week`, `category`, `brand`, `sku_slug`, `platform`),
  KEY `idx_status_status` (`week`, `platform`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS `price_observation` (
  `observation_key` CHAR(64) NOT NULL,
  `week` VARCHAR(10) NOT NULL,
  `category` VARCHAR(64) NOT NULL,
  `brand` VARCHAR(128) NOT NULL,
  `sku_slug` VARCHAR(96) NOT NULL,
  `sku_title` VARCHAR(512) NOT NULL,
  `platform` VARCHAR(32) NOT NULL,
  `keyword` VARCHAR(512) NOT NULL,
  `item_id` VARCHAR(128) NULL,
  `title` TEXT NOT NULL,
  `url` TEXT NOT NULL,
  `current_price` DECIMAL(12,2) NULL,
  `origin_price` DECIMAL(12,2) NULL,
  `shop_name` VARCHAR(255) NULL,
  `location` VARCHAR(255) NULL,
  `image_url` TEXT NULL,
  `is_second_hand` TINYINT(1) NOT NULL,
  `crawled_at` VARCHAR(64) NULL,
  `source_file` VARCHAR(1024) NOT NULL,
  `updated_at` VARCHAR(64) NOT NULL,
  PRIMARY KEY (`observation_key`),
  KEY `idx_price_lookup` (`week`, `category`, `brand`, `sku_slug`, `platform`, `current_price`),
  KEY `idx_price_item` (`platform`, `item_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


def _mysql_value(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return "NULL"
        return f"{value:.6f}".rstrip("0").rstrip(".")
    text = str(value).replace("\x00", "")
    text = text.replace("\\", "\\\\").replace("'", "''")
    return f"'{text}'"


def _write_mysql_insert(
    fp: Any,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence[Any]],
    update_columns: Sequence[str],
    *,
    chunk_size: int = 200,
) -> None:
    chunk: list[Sequence[Any]] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) >= chunk_size:
            _flush_mysql_insert(fp, table, columns, chunk, update_columns)
            chunk = []
    if chunk:
        _flush_mysql_insert(fp, table, columns, chunk, update_columns)


def _flush_mysql_insert(
    fp: Any,
    table: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    update_columns: Sequence[str],
) -> None:
    quoted_cols = ", ".join(f"`{col}`" for col in columns)
    fp.write(f"INSERT INTO `{table}` ({quoted_cols}) VALUES\n")
    values = []
    for row in rows:
        values.append("  (" + ", ".join(_mysql_value(value) for value in row) + ")")
    fp.write(",\n".join(values))
    if update_columns:
        updates = ", ".join(f"`{col}`=VALUES(`{col}`)" for col in update_columns)
        fp.write(f"\nON DUPLICATE KEY UPDATE {updates};\n\n")
    else:
        fp.write(";\n\n")


def write_mysql_sql(rows: ExportRows, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    with tmp.open("w", encoding="utf-8", newline="\n") as fp:
        fp.write("-- Generated by scripts/export_prices_db.py\n")
        fp.write("SET NAMES utf8mb4;\n")
        fp.write("SET time_zone = '+08:00';\n\n")
        fp.write(MYSQL_SCHEMA)
        fp.write("\n")
        _write_mysql_insert(
            fp,
            "sku",
            [
                "week", "category", "brand", "sku_slug", "sku_title", "chip",
                "spec_json", "source", "origin_count", "alt_titles_json",
                "price_json_path", "crawled_at", "updated_at",
            ],
            (
                (
                    s.week, s.category, s.brand, s.sku_slug, s.sku_title, s.chip,
                    s.spec_json, s.source, s.origin_count, s.alt_titles_json,
                    s.price_json_path, s.crawled_at, now,
                )
                for s in rows.skus.values()
            ),
            [
                "sku_title", "chip", "spec_json", "source", "origin_count",
                "alt_titles_json", "price_json_path", "crawled_at", "updated_at",
            ],
        )
        _write_mysql_insert(
            fp,
            "crawl_status",
            [
                "week", "category", "brand", "sku_slug", "platform", "status",
                "error", "crawled_at", "search_terms_json", "source_file",
                "updated_at",
            ],
            (
                (
                    s.week, s.category, s.brand, s.sku_slug, s.platform,
                    s.status, s.error, s.crawled_at, s.search_terms_json,
                    s.source_file, now,
                )
                for s in rows.statuses
            ),
            ["status", "error", "crawled_at", "search_terms_json", "source_file", "updated_at"],
        )
        _write_mysql_insert(
            fp,
            "price_observation",
            [
                "observation_key", "week", "category", "brand", "sku_slug",
                "sku_title", "platform", "keyword", "item_id", "title", "url",
                "current_price", "origin_price", "shop_name", "location",
                "image_url", "is_second_hand", "crawled_at", "source_file",
                "updated_at",
            ],
            (
                (
                    p.observation_key, p.week, p.category, p.brand, p.sku_slug,
                    p.sku_title, p.platform, p.keyword, p.item_id, p.title,
                    p.url, p.current_price, p.origin_price, p.shop_name,
                    p.location, p.image_url, p.is_second_hand, p.crawled_at,
                    p.source_file, now,
                )
                for p in rows.prices.values()
            ),
            [
                "sku_title", "platform", "keyword", "item_id", "title", "url",
                "current_price", "origin_price", "shop_name", "location",
                "image_url", "is_second_hand", "crawled_at", "source_file",
                "updated_at",
            ],
        )
    tmp.replace(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export data/prices/{week} to SQLite and MySQL SQL"
    )
    parser.add_argument("--week", required=True, help="Week tag, for example 2026-W17")
    parser.add_argument(
        "--data-dir",
        default=str(DATA_DIR),
        help="Project data directory",
    )
    parser.add_argument(
        "--category",
        help="Comma-separated category slugs; default exports every category",
    )
    parser.add_argument(
        "--sqlite",
        default=None,
        help="SQLite output path; default data/price_crawl_{week}.sqlite",
    )
    parser.add_argument(
        "--mysql-sql",
        default=None,
        help="MySQL import SQL path; default data/price_crawl_{week}_mysql.sql",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s - %(message)s",
    )
    data_dir = Path(args.data_dir)
    categories = (
        {item.strip().lower() for item in args.category.split(",") if item.strip()}
        if args.category else None
    )
    sqlite_path = Path(args.sqlite) if args.sqlite else data_dir / f"price_crawl_{args.week}.sqlite"
    mysql_sql_path = (
        Path(args.mysql_sql)
        if args.mysql_sql else data_dir / f"price_crawl_{args.week}_mysql.sql"
    )
    try:
        rows = collect_rows(args.week, data_dir, categories)
    except FileNotFoundError as exc:
        logging.error("%s", exc)
        return 2
    write_sqlite(rows, sqlite_path)
    write_mysql_sql(rows, mysql_sql_path)
    logging.info(
        "exported week=%s skus=%s statuses=%s observations=%s",
        args.week, len(rows.skus), len(rows.statuses), len(rows.prices),
    )
    logging.info("sqlite: %s", sqlite_path)
    logging.info("mysql sql: %s", mysql_sql_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
