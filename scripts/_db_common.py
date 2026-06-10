"""SQLite + MySQL 双后端通用基础设施。

把 align_schema / csv_export / csv_import 三个脚本都需要的「连接 + 类型映射 + 列字典」抽出来,
避免三处复制。
"""
from __future__ import annotations

import sqlite3
import sys
import os
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

try:
    import pymysql  # type: ignore
except ImportError:
    pymysql = None  # 仅 SQLite 模式时允许不装


# 默认配置(与 application.yml 一致)
MYSQL_CFG_DEFAULT: dict[str, Any] = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": os.environ.get("XIAOBAI_MYSQL_PASSWORD", ""),
    "database": "xiao_bai_zhuang_ji_zhu_shou",
    "charset": "utf8mb4",
    "autocommit": False,
}


# 统一后的「合集模式」—— SQLite 和 MySQL ALTER 完后都应有这些列
UNION_T_HARDWARE: list[tuple[str, str, str]] = [
    # (列名, MySQL DDL, SQLite DDL)
    ("id", "BIGINT NOT NULL AUTO_INCREMENT", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("category", "VARCHAR(32) NOT NULL", "TEXT NOT NULL"),
    ("brand", "VARCHAR(64)", "TEXT"),
    ("model", "VARCHAR(128) NOT NULL", "TEXT NOT NULL"),
    ("name_full", "VARCHAR(255)", "TEXT"),
    ("image", "VARCHAR(500)", "TEXT"),
    ("price_ref", "DECIMAL(10,2) DEFAULT 0.00", "REAL DEFAULT 0.00"),
    ("price_jd", "DECIMAL(10,2) DEFAULT 0.00", "REAL DEFAULT 0.00"),
    ("price_tb", "DECIMAL(10,2) DEFAULT 0.00", "REAL DEFAULT 0.00"),
    ("price_pdd", "DECIMAL(10,2) DEFAULT 0.00", "REAL DEFAULT 0.00"),
    ("price_xianyu", "DECIMAL(10,2) DEFAULT NULL", "REAL"),
    ("link_jd", "VARCHAR(500)", "TEXT"),
    ("link_tb", "VARCHAR(500)", "TEXT"),
    ("link_pdd", "VARCHAR(500)", "TEXT"),
    ("params_json", "TEXT", "TEXT"),
    ("tier", "VARCHAR(16)", "TEXT"),
    ("status", "TINYINT NOT NULL DEFAULT 1", "INTEGER NOT NULL DEFAULT 1"),
    ("sort", "INT NOT NULL DEFAULT 0", "INTEGER NOT NULL DEFAULT 0"),
    ("deleted", "TINYINT NOT NULL DEFAULT 0", "INTEGER NOT NULL DEFAULT 0"),
    ("release_year", "INT NULL", "INTEGER"),
    ("release_date", "DATE NULL", "TEXT"),
    ("generation", "VARCHAR(64) NULL", "TEXT"),
    ("source_tag", "VARCHAR(64) NULL", "TEXT"),
    ("create_time", "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
     "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"),
    ("update_time",
     "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
     "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"),
]

UNION_T_PRICE_HISTORY: list[tuple[str, str, str]] = [
    ("id", "BIGINT NOT NULL AUTO_INCREMENT", "INTEGER PRIMARY KEY AUTOINCREMENT"),
    ("hardware_id", "BIGINT", "INTEGER"),
    ("source", "VARCHAR(32)", "TEXT"),
    ("price", "DECIMAL(10,2) NOT NULL", "REAL NOT NULL"),
    ("record_date", "DATE NOT NULL", "TEXT NOT NULL"),
    ("deleted", "TINYINT NOT NULL DEFAULT 0", "INTEGER NOT NULL DEFAULT 0"),
    ("keyword", "VARCHAR(128) NULL", "TEXT"),
    ("platform", "VARCHAR(32) NULL", "TEXT"),
    ("item_id", "VARCHAR(64) NULL", "TEXT"),
    ("title", "TEXT NULL", "TEXT"),
    ("origin_price", "DECIMAL(10,2) NULL", "REAL"),
    ("shop_name", "VARCHAR(255) NULL", "TEXT"),
    ("location", "VARCHAR(64) NULL", "TEXT"),
    ("is_second_hand", "TINYINT NULL", "INTEGER"),
    ("url", "VARCHAR(1000) NULL", "TEXT"),
    ("image_url", "VARCHAR(1000) NULL", "TEXT"),
    ("image_local_path", "VARCHAR(500) NULL", "TEXT"),
    ("crawled_at", "DATETIME NULL", "TEXT"),
    ("is_outlier", "TINYINT NULL DEFAULT 0", "INTEGER DEFAULT 0"),
    ("create_time", "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
     "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"),
    ("update_time",
     "DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
     "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"),
]

UNION_SCHEMAS: dict[str, list[tuple[str, str, str]]] = {
    "t_hardware": UNION_T_HARDWARE,
    "t_hardware_price_history": UNION_T_PRICE_HISTORY,
}

# 业务唯一键(用于 UPSERT)
UPSERT_KEYS: dict[str, tuple[str, ...]] = {
    "t_hardware": ("category", "model"),
    "t_hardware_price_history": ("platform", "item_id", "record_date"),
}


def force_utf8_stdout() -> None:
    """Windows 终端默认 cp936,日志带中文会乱码。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass


# ─── 后端无关的连接抽象 ──────────────────────────────────────────


class DBBackend:
    """SQLite / MySQL 双后端通用抽象。"""

    def __init__(self, kind: str, conn: Any) -> None:
        assert kind in ("sqlite", "mysql")
        self.kind = kind
        self.conn = conn

    def cursor(self) -> Any:
        return self.conn.cursor()

    def placeholder(self) -> str:
        return "?" if self.kind == "sqlite" else "%s"

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    def close(self) -> None:
        self.conn.close()

    def existing_columns(self, table: str) -> set[str]:
        cur = self.cursor()
        if self.kind == "sqlite":
            try:
                cur.execute(f"PRAGMA table_info({table})")
                return {r[1] for r in cur.fetchall()}
            finally:
                cur.close()
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
            (table,),
        )
        try:
            return {r[0] for r in cur.fetchall()}
        finally:
            cur.close()

    def existing_indexes(self, table: str) -> set[str]:
        cur = self.cursor()
        if self.kind == "sqlite":
            try:
                cur.execute(f"PRAGMA index_list({table})")
                return {r[1] for r in cur.fetchall()}
            finally:
                cur.close()
        cur.execute(
            "SELECT DISTINCT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
            (table,),
        )
        try:
            return {r[0] for r in cur.fetchall()}
        finally:
            cur.close()


def open_backend(target: str, mysql_cfg: dict[str, Any] | None = None,
                 sqlite_path: str | None = None) -> DBBackend:
    """target ∈ {'sqlite', 'mysql'}。"""
    if target == "sqlite":
        if not sqlite_path:
            raise ValueError("--sqlite-path 必填")
        return DBBackend("sqlite", sqlite3.connect(sqlite_path))
    if target == "mysql":
        if pymysql is None:
            raise RuntimeError("未安装 pymysql,无法访问 MySQL")
        cfg = dict(MYSQL_CFG_DEFAULT)
        if mysql_cfg:
            cfg.update(mysql_cfg)
        return DBBackend("mysql", pymysql.connect(**cfg))
    raise ValueError(f"未知后端: {target}")


@contextmanager
def backend(target: str, **kw: Any) -> Iterator[DBBackend]:
    b = open_backend(target, **kw)
    try:
        yield b
    finally:
        b.close()


# ─── 列定义查询 ────────────────────────────────────────────────


def union_columns(table: str) -> list[str]:
    """合集模式下,该表应有的有序列名。"""
    return [name for name, _, _ in UNION_SCHEMAS[table]]


def csv_columns(table: str) -> list[str]:
    """导出/导入 CSV 时使用的列。剔除自增 id(由目标库决定),保留业务列。"""
    return [c for c in union_columns(table) if c not in {"id"}]
