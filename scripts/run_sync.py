"""一键流水线: 爬虫 SQLite → CSV → XiaoBai MySQL。

依次串行执行:
  1. align_schema  --target both       --apply      (两边 schema 对齐)
  2. fill_hardware_image --target sqlite --apply    (爬虫库 chip 图回填)
  3. csv_export    --target sqlite                  (导出 t_hardware + history 到 data/csv_export/)
  4. csv_import    --target mysql       --apply     (灌 t_hardware,默认 xiaobai-wins)
  5. csv_import    --target mysql       --apply     (灌 t_hardware_price_history,skip-existing)
  6. fill_hardware_image --target mysql --apply     (SKU 借父 chip 闲鱼图)
  7. 在 MySQL 上 CREATE OR REPLACE VIEW v_hardware_xianyu  (供查看)

用法:
    python scripts/run_sync.py --dry-run     # 只跑 align/export 不写 MySQL
    python scripts/run_sync.py --apply       # 完整流水线
    python scripts/run_sync.py --apply --merge-mode overwrite   # 激进模式:CSV 全量盖 MySQL
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from _db_common import backend, force_utf8_stdout  # noqa: E402

log = logging.getLogger("run_sync")
PY = sys.executable
CSV_DIR = ROOT / "data" / "csv_export"

VIEW_SQL = """
CREATE OR REPLACE VIEW v_hardware_xianyu AS
SELECT
    h.id                       AS hardware_id,
    h.category,
    h.brand,
    h.model,
    h.name_full,
    h.tier,
    h.image                    AS hardware_image,
    h.price_ref,
    h.price_jd,
    h.price_tb,
    h.price_pdd,
    h.price_xianyu,
    h.release_year,
    h.release_date,
    h.generation,
    h.source_tag,
    h.status,
    h.create_time,
    agg.xianyu_count,
    agg.xianyu_min,
    agg.xianyu_max,
    agg.xianyu_avg,
    agg.latest_image_url,
    agg.latest_url,
    agg.latest_title,
    agg.latest_record_date
FROM t_hardware h
LEFT JOIN (
    SELECT
        ph.keyword,
        COUNT(*)            AS xianyu_count,
        MIN(ph.price)       AS xianyu_min,
        MAX(ph.price)       AS xianyu_max,
        ROUND(AVG(ph.price), 2) AS xianyu_avg,
        SUBSTRING_INDEX(
            GROUP_CONCAT(ph.image_url ORDER BY ph.crawled_at DESC SEPARATOR '||'),
            '||', 1
        ) AS latest_image_url,
        SUBSTRING_INDEX(
            GROUP_CONCAT(ph.url ORDER BY ph.crawled_at DESC SEPARATOR '||'),
            '||', 1
        ) AS latest_url,
        SUBSTRING_INDEX(
            GROUP_CONCAT(ph.title ORDER BY ph.crawled_at DESC SEPARATOR '||'),
            '||', 1
        ) AS latest_title,
        MAX(ph.record_date) AS latest_record_date
    FROM t_hardware_price_history ph
    WHERE ph.deleted = 0
      AND ph.platform = 'xianyu'
      AND ph.keyword IS NOT NULL
    GROUP BY ph.keyword
) agg
    ON REPLACE(REPLACE(LOWER(agg.keyword), '_', ''), '-', '')
     = REPLACE(REPLACE(LOWER(h.model),     ' ', ''), '-', '')
WHERE h.deleted = 0
"""


def _setup_log() -> None:
    force_utf8_stdout()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                     datefmt="%H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def _step(name: str, cmd: list[str], skip: bool = False) -> int:
    log.info("─── 步骤: %s ───", name)
    if skip:
        log.info("  (跳过)")
        return 0
    log.info("  $ %s", " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=False)
    if proc.returncode != 0:
        log.error("  ✗ 失败 (rc=%d): %s", proc.returncode, name)
    return proc.returncode


def _create_view() -> int:
    log.info("─── 步骤: 创建 / 刷新 v_hardware_xianyu 视图 ───")
    try:
        with backend("mysql") as db:
            cur = db.cursor()
            try:
                cur.execute(VIEW_SQL)
                db.commit()
                # 视图行数 sanity
                cur.execute("SELECT COUNT(*) FROM v_hardware_xianyu")
                n = cur.fetchone()[0]
                cur.execute(
                    "SELECT COUNT(*) FROM v_hardware_xianyu WHERE xianyu_count IS NOT NULL"
                )
                with_xy = cur.fetchone()[0]
                log.info("  ✓ 视图 v_hardware_xianyu 共 %d 行,其中 %d 行有闲鱼成交数据", n, with_xy)
            finally:
                cur.close()
    except Exception as e:
        log.exception("  ✗ 视图创建失败: %s", e)
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true",
                   help="只跑 align/export/fill_sqlite,不写 MySQL 数据")
    p.add_argument("--apply", action="store_true",
                   help="完整流水线 + 创建视图")
    p.add_argument("--merge-mode",
                   choices=("xiaobai-wins", "overwrite", "skip-existing"),
                   default="xiaobai-wins",
                   help="t_hardware 入库模式(history 总是 skip-existing)")
    p.add_argument("--skip-export", action="store_true",
                   help="复用 data/csv_export/ 已有文件")
    args = p.parse_args()
    if not args.dry_run and not args.apply:
        p.error("必须指定 --dry-run 或 --apply")

    _setup_log()
    dry = args.dry_run
    base = [PY, str(ROOT / "scripts")]

    # 1. align both
    rc = _step(
        "align_schema both",
        [PY, str(ROOT / "scripts" / "align_schema.py"),
         "--target", "both",
         "--apply" if not dry else "--dry-run"],
    )
    if rc:
        return rc

    # 2. SQLite 端图片回填(让导出 CSV 时 t_hardware.image 已经有数据)
    rc = _step(
        "fill_hardware_image (sqlite)",
        [PY, str(ROOT / "scripts" / "fill_hardware_image.py"),
         "--target", "sqlite",
         "--apply" if not dry else "--dry-run"],
    )
    if rc:
        return rc

    # 3. 从 SQLite 导出 CSV
    rc = _step(
        "csv_export sqlite",
        [PY, str(ROOT / "scripts" / "csv_export.py"),
         "--target", "sqlite",
         "--out-dir", str(CSV_DIR)],
        skip=args.skip_export,
    )
    if rc:
        return rc

    # 4. CSV → MySQL t_hardware
    rc = _step(
        f"csv_import → mysql.t_hardware (mode={args.merge_mode})",
        [PY, str(ROOT / "scripts" / "csv_import.py"),
         "--target", "mysql",
         "--table", "t_hardware",
         "--csv", str(CSV_DIR / "sqlite_t_hardware.csv"),
         "--merge-mode", args.merge_mode,
         "--apply" if not dry else "--dry-run"],
    )
    if rc:
        return rc

    # 5. CSV → MySQL t_hardware_price_history
    rc = _step(
        "csv_import → mysql.t_hardware_price_history (skip-existing)",
        [PY, str(ROOT / "scripts" / "csv_import.py"),
         "--target", "mysql",
         "--table", "t_hardware_price_history",
         "--csv", str(CSV_DIR / "sqlite_t_hardware_price_history.csv"),
         "--merge-mode", "skip-existing",
         "--apply" if not dry else "--dry-run"],
    )
    if rc:
        return rc

    # 6. MySQL 端图片回填(SKU 借父 chip 图,需要 history 已入库)
    rc = _step(
        "fill_hardware_image (mysql)",
        [PY, str(ROOT / "scripts" / "fill_hardware_image.py"),
         "--target", "mysql",
         "--apply" if not dry else "--dry-run"],
    )
    if rc:
        return rc

    # 7. 创建 / 刷新视图
    if not dry:
        rc = _create_view()
        if rc:
            return rc
    else:
        log.info("─── DRY-RUN 跳过视图创建 ───")

    log.info("\n✓ 流水线完成。在 MySQL 用以下查询查看:")
    log.info("    SELECT category, model, hardware_image, latest_image_url,")
    log.info("           xianyu_avg, xianyu_count, release_year, generation")
    log.info("    FROM v_hardware_xianyu")
    log.info("    WHERE xianyu_count > 0")
    log.info("    ORDER BY xianyu_count DESC LIMIT 50;")
    return 0


if __name__ == "__main__":
    sys.exit(main())
