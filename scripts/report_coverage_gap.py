"""生成两份覆盖差距报表:

1. **mysql_missing_xianyu.csv** — XiaoBai MySQL t_hardware 里没有闲鱼价的硬件
   (其实就是查 v_hardware_missing_xianyu 视图导出 csv)

2. **crawled_but_not_imported.csv** — autoPCBulid SQLite 爬到了 但未匹配 MySQL
   (我们爬到闲鱼数据,但 token 匹配规则下找不到 MySQL 对应硬件)

输出:data/coverage_report_YYYYMMDD_HHMM/
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQLITE_DB = ROOT / "data" / "prices.db"
MYSQL_BIN = Path("C:/Program Files/MySQL/MySQL Server 8.0/bin/mysql.exe")
XIAOBAI_DB = "xiao_bai_zhuang_ji_zhu_shou"
DEFAULT_PWD = os.environ.get("XIAOBAI_MYSQL_PASSWORD")

log = logging.getLogger("coverage")


def _configure_logging() -> None:
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def export_mysql_missing(out_path: Path, pwd: str) -> int:
    """从 v_hardware_missing_xianyu 视图导出 csv。"""
    cmd = [
        str(MYSQL_BIN), "-u", "root", f"-p{pwd}",
        "--default-character-set=utf8mb4", "-B",
        "-D", XIAOBAI_DB,
        "-e", "SELECT id, category, brand, model, name_full, price_jd_ai FROM v_hardware_missing_xianyu;",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True)
    lines = [ln for ln in r.stdout.splitlines() if "[Warning]" not in ln]
    n = 0
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        for i, ln in enumerate(lines):
            if not ln.strip():
                continue
            fields = ln.split("\t")
            w.writerow(fields)
            if i > 0:
                n += 1
    return n


def export_crawled_not_imported(out_path: Path, pwd: str) -> int:
    """SQLite 爬到的 keyword 减去 能匹配 MySQL 的 keyword,导出 csv。"""
    # 复用 merge_csv 的匹配逻辑
    sys.path.insert(0, str(ROOT))
    from scripts.merge_csv_to_xiaobai_mysql import (
        fetch_mysql_hardware, build_index, match_hardware,
    )
    hw = fetch_mysql_hardware(pwd)
    grouped = build_index(hw)

    conn = sqlite3.connect(str(SQLITE_DB))
    rows = conn.execute(
        "SELECT keyword, COUNT(*) AS n, AVG(current_price) AS avg_p, "
        "MIN(current_price) AS min_p, MAX(current_price) AS max_p "
        "FROM products WHERE platform='xianyu' AND current_price > 0 "
        "GROUP BY keyword"
    ).fetchall()
    conn.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    n_total = 0
    n_unmatched = 0
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["keyword", "products_count", "avg_price", "min_price", "max_price", "reason"])
        for kw, cnt, avg_p, min_p, max_p in rows:
            n_total += 1
            if not kw:
                continue
            unslug = kw.replace("_", " ")
            # 跳过 hash 后缀脏数据
            last = unslug.split()[-1] if unslug.split() else ""
            if len(last) == 10 and all(c in "0123456789abcdef" for c in last):
                continue
            hid = match_hardware(unslug, grouped)
            if hid is not None:
                continue
            # 分析未匹配原因
            kw_lower = unslug.lower()
            if any(t in kw_lower for t in ("ax200", "ax210", "be200", "i225", "i226", "i350", "x550", "tp-link", "tg-3", "archer")):
                reason = "网卡 — XiaoBai 没 nic 类别"
            elif "hgst" in kw_lower or "8tb" in kw_lower and "7200" in kw_lower:
                reason = "HDD — XiaoBai hdd 是规格描述"
            elif "aigo" in kw_lower:
                reason = "SSD — MySQL 缺这型号"
            elif unslug.strip() in ("AM3", "AM3 AM3"):
                reason = "主板规格抽象,非品牌+型号"
            elif "i5-13490f" in kw_lower:
                reason = "命名差异 (MySQL 叫 Core i5-13490F)"
            else:
                reason = "未识别(品牌缺失/型号细分)"
            w.writerow([unslug, cnt, f"{avg_p:.2f}", f"{min_p:.2f}", f"{max_p:.2f}", reason])
            n_unmatched += 1
    log.info("SQLite 爬过 keyword 总数: %d,未匹配 MySQL: %d", n_total, n_unmatched)
    return n_unmatched


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--password",
        default=DEFAULT_PWD,
        help="MySQL 密码；也可用环境变量 XIAOBAI_MYSQL_PASSWORD",
    )
    args = parser.parse_args()

    _configure_logging()
    if args.password is None:
        parser.error("缺少 MySQL 密码：请传 --password 或设置 XIAOBAI_MYSQL_PASSWORD")

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_dir = ROOT / "data" / f"coverage_report_{stamp}"

    mysql_missing_csv = out_dir / "mysql_missing_xianyu.csv"
    n1 = export_mysql_missing(mysql_missing_csv, args.password)
    log.info("写: %s (%d 行)", mysql_missing_csv, n1)

    crawled_not_imported_csv = out_dir / "crawled_but_not_imported.csv"
    n2 = export_crawled_not_imported(crawled_not_imported_csv, args.password)
    log.info("写: %s (%d 行)", crawled_not_imported_csv, n2)

    log.info("报表目录: %s", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
