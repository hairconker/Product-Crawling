"""把 autoPCBulid SQLite (data/prices.db) 合并到 XiaoBai_Computer MySQL。

合并契约（与用户确认）:
  1. XiaoBai 为底,SKU 变体(盒装/散片/原厂散热套装/主板套装拆分/高频优选批次)保留不动
  2. chip-level 行:autoPCBulid 全量 INSERT,即使无价格;命中已存在 chip 则 UPDATE
  3. AI 假行的 price_jd/tb/pdd/ref 用闲鱼中位价覆盖
  4. 占位图(dummyimage / /imgs/coreIcon/ / OSS pczj 默认图)替换为爬到的真实 image_url
  5. release_year / generation / source_tag 从 model_info 注入
  6. 15553 条闲鱼成交记录全部 APPEND 到 t_hardware_price_history,FK 重新映射

用法:
    python scripts/merge_to_xiaobai_mysql.py --dry-run   # 仅打印 diff,不写库
    python scripts/merge_to_xiaobai_mysql.py --apply     # 真写库(已确认 dry-run 后)
    python scripts/merge_to_xiaobai_mysql.py --apply --skip-history  # 只合并 t_hardware,跳过 15553 条历史
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any

import pymysql

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE = ROOT / "data" / "prices.db"

log = logging.getLogger("merge_xb")

MYSQL_CFG: dict[str, Any] = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": os.environ.get("XIAOBAI_MYSQL_PASSWORD", ""),
    "database": "xiao_bai_zhuang_ji_zhu_shou",
    "charset": "utf8mb4",
    "autocommit": False,
}

# SKU 变体后缀（XiaoBai curated_budget_catalog 命名规律）—— 命中即视为 SKU,跳过 UPDATE
_SKU_SUFFIXES = ("盒装", "散片", "原厂散热套装", "主板套装拆分", "高频优选批次", "下架样例")

# 这些类别 XiaoBai 没有,但用户同意一并写入
_EXTRA_CATEGORIES = {"hdd", "nic_wired", "nic_wireless"}

# t_hardware 新增列
_T_HARDWARE_NEW_COLS = (
    ("release_year", "INT NULL"),
    ("release_date", "DATE NULL"),
    ("generation", "VARCHAR(64) NULL"),
    ("source_tag", "VARCHAR(64) NULL"),
    ("price_xianyu", "DECIMAL(10,2) DEFAULT NULL"),
)

# t_hardware_price_history 新增列（对齐 autoPCBulid 爬虫输出）
_T_PHISTORY_NEW_COLS = (
    ("keyword", "VARCHAR(128) NULL"),
    ("platform", "VARCHAR(32) NULL"),
    ("item_id", "VARCHAR(64) NULL"),
    ("title", "TEXT NULL"),
    ("origin_price", "DECIMAL(10,2) NULL"),
    ("shop_name", "VARCHAR(255) NULL"),
    ("location", "VARCHAR(64) NULL"),
    ("is_second_hand", "TINYINT NULL"),
    ("url", "VARCHAR(1000) NULL"),
    ("image_url", "VARCHAR(1000) NULL"),
    ("image_local_path", "VARCHAR(500) NULL"),
    ("crawled_at", "DATETIME NULL"),
    ("is_outlier", "TINYINT NULL DEFAULT 0"),
)


def _configure_logging() -> None:
    # Windows 控制台默认 cp936,中文 ALTER 输出会乱码 → 强制 utf-8
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def _is_sku_variant(model: str) -> bool:
    return any(model.endswith(suf) for suf in _SKU_SUFFIXES)


def _is_placeholder_image(img: str | None) -> bool:
    if not img:
        return True
    s = img.strip().lower()
    return (
        "dummyimage.com" in s
        or "/imgs/coreicon/" in s
        or s.endswith("/files/images/cpu.jpg")
        or s.endswith("/files/images/gpu.jpg")
        or s.endswith("/files/images/motherboard.jpg")
        or s.endswith("/files/images/memory.jpg")
        or s.endswith("/files/images/ssd.jpg")
        or s.endswith("/files/images/psu.jpg")
        or s.endswith("/files/images/chassis.png")
        or s.endswith("/files/images/cooler.jpg")
        or s.endswith("/files/images/monitor.png")
        or s.endswith("/files/images/peripheral.png")
        or s.endswith("/files/images/gaming_pc.png")
        or s.endswith("/files/images/office.jpg")
        or s.endswith("/files/images/creator.jpg")
        or s.endswith("/files/images/live.png")
        or s.endswith("/files/images/dev.jpg")
    )


def _normalize_model_for_match(s: str | None) -> str:
    if not s:
        return ""
    # 小写化 + 先把 _ / - 统一成空格,再剥前缀品牌词,最后压紧
    t = s.lower().strip()
    t = re.sub(r"[_/\-]+", " ", t)
    for noise in ("intel ", "amd ", "nvidia ", "geforce ", "core ", "ryzen ",
                  "锐龙", "酷睿", "radeon "):
        t = t.replace(noise, "")
    t = re.sub(r"\s+", "", t)
    return t


# ─── ALTER TABLE ─────────────────────────────────────────────────


def _alter_tables(cur, dry_run: bool) -> None:
    """添加缺失列;已存在的列直接跳过(不依赖 INFORMATION_SCHEMA 二次查询,直接 try/except)。"""
    plans: list[str] = []
    for table, cols in (
        ("t_hardware", _T_HARDWARE_NEW_COLS),
        ("t_hardware_price_history", _T_PHISTORY_NEW_COLS),
    ):
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s",
            (table,),
        )
        existing = {r[0] for r in cur.fetchall()}
        for col, ddl in cols:
            if col in existing:
                continue
            sql = f"ALTER TABLE `{table}` ADD COLUMN `{col}` {ddl}"
            plans.append(sql)
    # 幂等性兜底索引(便于幂等再跑)
    cur.execute(
        "SELECT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='t_hardware_price_history' "
        "AND INDEX_NAME='uk_xianyu_record'"
    )
    if not cur.fetchone():
        plans.append(
            "CREATE UNIQUE INDEX uk_xianyu_record ON t_hardware_price_history "
            "(platform, item_id, record_date)"
        )
    cur.execute(
        "SELECT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='t_hardware' "
        "AND INDEX_NAME='uk_category_model'"
    )
    if not cur.fetchone():
        plans.append(
            "CREATE UNIQUE INDEX uk_category_model ON t_hardware (category, model(64))"
        )
    if not plans:
        log.info("schema 已是最新,跳过 ALTER")
        return
    log.info("即将执行 %d 条 ALTER/INDEX:", len(plans))
    for s in plans:
        log.info("  %s", s)
    if dry_run:
        return
    for s in plans:
        try:
            cur.execute(s)
        except pymysql.err.OperationalError as e:
            # 重复索引/列等问题幂等忽略
            if e.args[0] in (1060, 1061, 1068):
                log.warning("  忽略幂等错误: %s", e)
            else:
                raise


# ─── 数据聚合 ────────────────────────────────────────────────────


def _load_xianyu_stats(
    sconn: sqlite3.Connection,
) -> dict[str, dict[str, Any]]:
    """对每个 keyword(=chip model) 聚合闲鱼成交价中位数 + 一张代表图。"""
    cur = sconn.cursor()
    cur.execute(
        """
        SELECT keyword, price, image_url, title
        FROM t_hardware_price_history
        WHERE price > 0 AND is_outlier = 0 AND keyword IS NOT NULL
        """
    )
    by_kw: dict[str, list[tuple[float, str | None, str | None]]] = defaultdict(list)
    for kw, price, img, title in cur.fetchall():
        by_kw[kw].append((price, img, title))
    out: dict[str, dict[str, Any]] = {}
    for kw, rows in by_kw.items():
        prices = [r[0] for r in rows]
        rows_sorted = sorted(rows, key=lambda r: r[0], reverse=True)
        # 取价格中位附近、有 image_url 的一条做代表图
        rep_img = next((r[1] for r in rows_sorted if r[1]), None)
        rep_title = next((r[2] for r in rows_sorted if r[2]), None)
        out[kw] = {
            "median": round(median(prices), 2),
            "min": round(min(prices), 2),
            "max": round(max(prices), 2),
            "count": len(prices),
            "image_url": rep_img,
            "rep_title": rep_title,
        }
    log.info("闲鱼聚合: %d 个 keyword 有真实价格", len(out))
    return out


def _load_model_meta(sconn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    """(category, model_normalized) → release_year / generation / source_tag。"""
    # autoPCBulid 的 SQLite t_hardware 已经持有这些字段(category 小写,model 原始)
    cur = sconn.cursor()
    cur.execute(
        """
        SELECT category, brand, model, name_full, image, price_ref,
               release_year, release_date, generation, source_tag
        FROM t_hardware
        """
    )
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for row in cur.fetchall():
        (category, brand, model, name_full, image, price_ref,
         ry, rd, gen, src) = row
        key = (category, _normalize_model_for_match(model))
        out[key] = {
            "category": category,
            "brand": brand,
            "model": model,
            "name_full": name_full,
            "image": image,
            "price_ref": price_ref,
            "release_year": ry,
            "release_date": rd,
            "generation": gen,
            "source_tag": src,
        }
    log.info("autoPCBulid chip 字典: %d 条", len(out))
    return out


# ─── t_hardware 合并 ─────────────────────────────────────────────


def _merge_hardware(
    mcur,
    sconn: sqlite3.Connection,
    xianyu_stats: dict[str, dict[str, Any]],
    sqlite_chips: dict[tuple[str, str], dict[str, Any]],
    dry_run: bool,
) -> dict[str, int]:
    """返回 stats: {updated, inserted_chip, skipped_sku, image_replaced, price_replaced}。"""
    stats = defaultdict(int)

    # 1. 拉 MySQL 现有所有 row
    mcur.execute(
        "SELECT id, category, model, image, price_ref, params_json, status, deleted "
        "FROM t_hardware"
    )
    mysql_rows = mcur.fetchall()
    # 组装索引(category, normalized_model) → 选 chip-level 而非 SKU
    mysql_idx: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    for r in mysql_rows:
        key = (r[1], _normalize_model_for_match(r[2]))
        mysql_idx[key].append(r)

    consumed_sqlite_keys: set[tuple[str, str]] = set()
    # 在 dry-run 模式下也要让 history 阶段看到我们"将要"插入的 chip,所以挂在 self 上
    _merge_hardware.planned_inserts = []  # type: ignore[attr-defined]

    # 2. 遍历 MySQL row,根据 model 匹配 SQLite chip,UPDATE
    for (mid, mcat, mmodel, mimg, mprice_ref, mparams, mstatus, mdel) in mysql_rows:
        if mdel:
            continue
        if _is_sku_variant(mmodel):
            stats["skipped_sku"] += 1
            # SKU 也要补 release_year (从父 chip)
            parent_chip = mmodel
            for suf in _SKU_SUFFIXES:
                if parent_chip.endswith(suf):
                    parent_chip = parent_chip[: -len(suf)].strip()
                    break
            key = (mcat, _normalize_model_for_match(parent_chip))
            meta = sqlite_chips.get(key)
            if meta and meta.get("release_year"):
                sql = (
                    "UPDATE t_hardware SET release_year=%s, generation=%s, "
                    "source_tag=COALESCE(source_tag,%s) WHERE id=%s"
                )
                args = (meta["release_year"], meta.get("generation"),
                        meta.get("source_tag"), mid)
                stats["sku_meta_filled"] += 1
                if not dry_run:
                    mcur.execute(sql, args)
            continue

        key = (mcat, _normalize_model_for_match(mmodel))
        meta = sqlite_chips.get(key)
        consumed_sqlite_keys.add(key)

        sets: list[str] = []
        args: list[Any] = []

        if meta:
            for col in ("release_year", "release_date", "generation", "source_tag"):
                v = meta.get(col)
                if v is not None:
                    sets.append(f"{col}=%s")
                    args.append(v)

        # 用 keyword 试找闲鱼价(model 直接当 keyword;再 fallback 到 normalized)
        xy = xianyu_stats.get(mmodel)
        if xy is None:
            # 试 normalized 匹配
            norm = _normalize_model_for_match(mmodel)
            for kw, v in xianyu_stats.items():
                if _normalize_model_for_match(kw) == norm:
                    xy = v
                    break
        if xy and xy["count"] >= 1:
            stats["price_replaced"] += 1
            median_p = xy["median"]
            sets += [
                "price_ref=%s", "price_jd=%s", "price_tb=%s",
                "price_pdd=%s", "price_xianyu=%s",
            ]
            args += [median_p, median_p, median_p, median_p, median_p]
            if _is_placeholder_image(mimg) and xy["image_url"]:
                sets.append("image=%s")
                args.append(xy["image_url"])
                stats["image_replaced"] += 1

        if sets:
            args.append(mid)
            sql = "UPDATE t_hardware SET " + ", ".join(sets) + " WHERE id=%s"
            stats["updated"] += 1
            if not dry_run:
                mcur.execute(sql, args)

    # 3. SQLite chip 不存在于 MySQL → INSERT
    for key, meta in sqlite_chips.items():
        if key in consumed_sqlite_keys:
            continue
        if not meta["model"]:
            continue
        category = meta["category"]
        if category in _EXTRA_CATEGORIES:
            stats[f"inserted_{category}"] += 1
        # 取闲鱼价
        xy = xianyu_stats.get(meta["model"])
        if xy is None:
            norm = _normalize_model_for_match(meta["model"])
            for kw, v in xianyu_stats.items():
                if _normalize_model_for_match(kw) == norm:
                    xy = v
                    break
        median_p = xy["median"] if xy and xy["count"] >= 1 else 0.00
        img = (
            xy["image_url"] if xy and xy["image_url"]
            else (meta.get("image") if not _is_placeholder_image(meta.get("image")) else None)
        )
        name_full = meta.get("name_full") or meta["model"]
        params_obj = {"importedFrom": "autoPCBulid", "source_tag": meta.get("source_tag")}
        if xy:
            params_obj["xianyu"] = {
                "median": xy["median"], "min": xy["min"], "max": xy["max"],
                "count": xy["count"],
            }
        sql = (
            "INSERT INTO t_hardware (category, brand, model, name_full, image, "
            "price_ref, price_jd, price_tb, price_pdd, price_xianyu, params_json, "
            "tier, status, sort, deleted, release_year, release_date, generation, "
            "source_tag) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        )
        args = (
            category, meta.get("brand"), meta["model"], name_full, img,
            median_p, median_p, median_p, median_p, (median_p or None),
            json.dumps(params_obj, ensure_ascii=False),
            None, 1, 9999, 0,
            meta.get("release_year"), meta.get("release_date"),
            meta.get("generation"), meta.get("source_tag"),
        )
        stats["inserted_chip"] += 1
        _merge_hardware.planned_inserts.append((category, _normalize_model_for_match(meta["model"])))  # type: ignore[attr-defined]
        if not dry_run:
            try:
                mcur.execute(sql, args)
            except pymysql.err.IntegrityError:
                # uk_category_model 命中,跳过
                stats["insert_dup_skip"] += 1
    return dict(stats)


# ─── t_hardware_price_history 合并 ───────────────────────────────


def _merge_price_history(
    mcur,
    sconn: sqlite3.Connection,
    dry_run: bool,
    batch_size: int = 500,
) -> dict[str, int]:
    """把 SQLite 的 15553 条闲鱼成交记录 APPEND 到 MySQL,FK 通过 model 反查。"""
    stats = defaultdict(int)

    # 重建 MySQL t_hardware 索引:(category, normalized_model) → id
    mcur.execute("SELECT id, category, model FROM t_hardware WHERE deleted=0")
    mysql_idx: dict[tuple[str, str], int] = {}
    for hid, cat, model in mcur.fetchall():
        mysql_idx[(cat, _normalize_model_for_match(model))] = hid
        # SKU 变体也要参与 FK 匹配(剥掉后缀回退到父 chip)
        if _is_sku_variant(model):
            parent = model
            for suf in _SKU_SUFFIXES:
                if parent.endswith(suf):
                    parent = parent[: -len(suf)].strip()
                    break
            mysql_idx.setdefault((cat, _normalize_model_for_match(parent)), hid)
    # 在 dry-run 模式下,把"将要"插入的 chip 也算进 idx(用 0 占位 id,实际不会写入)
    planned = getattr(_merge_hardware, "planned_inserts", [])  # type: ignore[attr-defined]
    for key in planned:
        mysql_idx.setdefault(key, -1)  # -1 表示 dry-run 占位

    # 从 SQLite t_hardware 拿 chip → category 映射(autoPCBulid)
    scur = sconn.cursor()
    scur.execute("SELECT category, model FROM t_hardware")
    chip_cat: dict[str, str] = {}
    for cat, model in scur.fetchall():
        chip_cat[_normalize_model_for_match(model)] = cat

    scur.execute(
        """
        SELECT keyword, platform, item_id, price, record_date, title,
               origin_price, shop_name, location, is_second_hand,
               url, image_url, image_local_path, crawled_at, is_outlier
        FROM t_hardware_price_history
        WHERE deleted = 0
        """
    )

    sql_insert = (
        "INSERT IGNORE INTO t_hardware_price_history "
        "(hardware_id, source, price, record_date, deleted, "
        "keyword, platform, item_id, title, origin_price, shop_name, "
        "location, is_second_hand, url, image_url, image_local_path, "
        "crawled_at, is_outlier) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    )

    batch: list[tuple] = []
    total = 0
    for row in scur.fetchall():
        (keyword, platform, item_id, price, record_date, title,
         origin_price, shop_name, location, is_second_hand,
         url, image_url, image_local_path, crawled_at, is_outlier) = row
        total += 1
        norm = _normalize_model_for_match(keyword)
        cat = chip_cat.get(norm)
        hid = mysql_idx.get((cat, norm)) if cat else None
        if hid is None:
            stats["no_fk_match"] += 1
        elif hid == -1:
            # dry-run 占位:把它视为"会有 FK"
            stats["fk_via_planned_insert"] += 1
            hid = None  # 实际入库时此分支不会出现(--apply 下 idx 已含真 id)
        record = (
            hid, platform, float(price) if price else 0.0, record_date, 0,
            keyword, platform, str(item_id) if item_id else None,
            title[:65000] if title else None,
            float(origin_price) if origin_price else None,
            shop_name, location,
            int(is_second_hand) if is_second_hand is not None else None,
            url[:1000] if url else None,
            image_url[:1000] if image_url else None,
            image_local_path[:500] if image_local_path else None,
            crawled_at,
            int(is_outlier) if is_outlier is not None else 0,
        )
        batch.append(record)
        if len(batch) >= batch_size:
            stats["batched"] += len(batch)
            if not dry_run:
                mcur.executemany(sql_insert, batch)
                stats["inserted"] += mcur.rowcount
            batch.clear()
    if batch:
        stats["batched"] += len(batch)
        if not dry_run:
            mcur.executemany(sql_insert, batch)
            stats["inserted"] += mcur.rowcount
    stats["total_source_rows"] = total
    return dict(stats)


# ─── 入口 ────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只打印 diff,不写库")
    parser.add_argument("--apply", action="store_true", help="真写库")
    parser.add_argument("--sqlite", default=str(DEFAULT_SQLITE), help="SQLite 路径")
    parser.add_argument(
        "--password",
        default=os.environ.get("XIAOBAI_MYSQL_PASSWORD"),
        help="MySQL 密码；也可用环境变量 XIAOBAI_MYSQL_PASSWORD",
    )
    parser.add_argument("--skip-history", action="store_true", help="跳过 15553 条历史合并")
    args = parser.parse_args()

    _configure_logging()

    if not args.dry_run and not args.apply:
        log.error("必须指定 --dry-run 或 --apply 之一")
        return 2
    if args.dry_run and args.apply:
        log.error("--dry-run 和 --apply 互斥")
        return 2
    if args.password is None:
        log.error("缺少 MySQL 密码：请传 --password 或设置 XIAOBAI_MYSQL_PASSWORD")
        return 2
    MYSQL_CFG["password"] = args.password

    dry = args.dry_run
    log.info("模式: %s | SQLite: %s | skip-history=%s",
             "DRY-RUN" if dry else "APPLY", args.sqlite, args.skip_history)

    sconn = sqlite3.connect(args.sqlite)
    mconn = pymysql.connect(**MYSQL_CFG)
    mcur = mconn.cursor()

    try:
        _alter_tables(mcur, dry)
        if not dry:
            mconn.commit()

        xianyu_stats = _load_xianyu_stats(sconn)
        sqlite_chips = _load_model_meta(sconn)

        log.info("── 合并 t_hardware ──")
        hw_stats = _merge_hardware(mcur, sconn, xianyu_stats, sqlite_chips, dry)
        for k, v in sorted(hw_stats.items()):
            log.info("  %s = %d", k, v)
        if not dry:
            mconn.commit()
            log.info("t_hardware 提交完成")

        if not args.skip_history:
            log.info("── 合并 t_hardware_price_history ──")
            ph_stats = _merge_price_history(mcur, sconn, dry)
            for k, v in sorted(ph_stats.items()):
                log.info("  %s = %d", k, v)
            if not dry:
                mconn.commit()
                log.info("t_hardware_price_history 提交完成")
        else:
            log.info("跳过 t_hardware_price_history")

        if dry:
            log.info("DRY-RUN 完成,未写库。确认无误后改用 --apply。")
            mconn.rollback()
    except Exception as e:
        log.exception("发生异常,回滚")
        mconn.rollback()
        return 1
    finally:
        mcur.close()
        mconn.close()
        sconn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
