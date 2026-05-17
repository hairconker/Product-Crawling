"""通用「csv → XiaoBai MySQL t_hardware_price_history」合并器,**带断点续传**。

设计:
- 输入:**单个 csv 或目录**(目录会递归找所有 *.csv),csv schema 见下
- 进度文件:`data/merge_progress.json` 记录已处理的 csv 文件
  - 中断后下次启动自动跳过已 done 的 csv
  - 用 atomic write(tmp + rename)防进度损坏
- 不动 XiaoBai t_hardware,只追加 t_hardware_price_history(source='xianyu')
- INSERT NOT EXISTS 防同 (hardware_id, source, record_date) 重复

CSV schema(autoPCBulid 标准,跟 logs/by_keyword/*.csv 一致):
    keyword, platform, item_id, title, current_price, origin_price,
    shop_name, location, is_second_hand, url, image_url, crawled_at

只 platform='xianyu' 的行会被处理,只取 current_price > 0 的。

用法:
    python scripts/merge_csv_to_xiaobai_mysql.py logs/by_keyword/A10-5700.csv
    python scripts/merge_csv_to_xiaobai_mysql.py logs/by_keyword/         # 整个目录
    python scripts/merge_csv_to_xiaobai_mysql.py data/export_csv_20260517_1005/
    python scripts/merge_csv_to_xiaobai_mysql.py path/ --dry-run         # 不真写 MySQL
    python scripts/merge_csv_to_xiaobai_mysql.py path/ --force           # 忽略进度文件,全部重跑
    python scripts/merge_csv_to_xiaobai_mysql.py --list-progress         # 查看已处理 csv
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import statistics
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MYSQL_BIN = Path("C:/Program Files/MySQL/MySQL Server 8.0/bin/mysql.exe")
XIAOBAI_DB = "xiao_bai_zhuang_ji_zhu_shou"
DEFAULT_MYSQL_PWD = "123456"
PROGRESS_FILE = ROOT / "data" / "merge_progress.json"

# XiaoBai category 子集(只有这些类别参与匹配,字典里其他类别 XiaoBai 没有)
_VALID_CATS: tuple[str, ...] = (
    "cpu", "gpu", "motherboard", "memory", "ssd", "psu", "chassis", "cooler",
)

log = logging.getLogger("merge_csv")


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO)


# ─── 进度文件 ────────────────────────────────────────────────


def load_progress() -> dict:
    if not PROGRESS_FILE.exists():
        return {"version": 1, "processed": {}}
    try:
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("进度文件读失败,当作空: %s", e)
        return {"version": 1, "processed": {}}


def save_progress_atomic(data: dict) -> None:
    """tmp + rename 原子写,避免半成品。"""
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=".merge_progress_", suffix=".json", dir=PROGRESS_FILE.parent
    )
    tmp = Path(tmp_str)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(PROGRESS_FILE)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"save progress failed: {e}") from e


def _csv_key(p: Path) -> str:
    """csv 在进度文件里的 key:相对项目根的 POSIX 路径。"""
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return p.resolve().as_posix()


# ─── MySQL 查 t_hardware ─────────────────────────────────────


def fetch_mysql_hardware(pwd: str) -> list[dict]:
    cmd = [
        str(MYSQL_BIN), "-u", "root", f"-p{pwd}",
        "--default-character-set=utf8mb4", "-B", "-N",
        "-D", XIAOBAI_DB,
        "-e",
        "SELECT id, category, COALESCE(brand,''), COALESCE(model,'') "
        "FROM t_hardware WHERE status=1 AND deleted=0;",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"mysql query failed: {e.stderr}") from e
    rows: list[dict] = []
    for line in r.stdout.splitlines():
        if not line.strip() or "[Warning]" in line:
            continue
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        try:
            hid = int(parts[0])
        except ValueError:
            continue
        rows.append({
            "id": hid,
            "category": parts[1].strip(),
            "brand": parts[2].strip(),
            "model": parts[3].strip(),
        })
    return rows


# ─── 匹配 ────────────────────────────────────────────────────


_TOKEN_SPLIT_RE = re.compile(r"[\s_\-/()]+")


def _tokenize(s: str) -> list[str]:
    return [t.lower() for t in _TOKEN_SPLIT_RE.split(s) if t]


def build_index(hw_rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for h in hw_rows:
        cat = h["category"]
        grouped.setdefault(cat, []).append({
            "id": h["id"],
            "brand_tokens": _tokenize(h["brand"]),
            "model_tokens": _tokenize(h["model"]),
        })
    return grouped


def guess_category(keyword: str) -> list[str]:
    kw = keyword.lower()
    cats: list[str] = []
    if any(t in kw for t in ("rtx", "gtx", "radeon", "rx ", "rx_", "arc ", "arc_", "quadro", "titan")):
        cats.append("gpu")
    if any(t in kw for t in ("i3-", "i5-", "i7-", "i9-", "ryzen", "core i", "epyc", "xeon",
                              "athlon", "celeron", "pentium", "threadripper", "ultra")):
        cats.append("cpu")
    if "ddr" in kw or any(t in kw for t in ("fury", "vengeance", "ripjaws", "xpg", "trident")):
        cats.append("memory")
    if any(t in kw for t in ("h610", "b660", "b760", "z690", "z790", "b550", "b650", "x670",
                              "a620", "x870", "tuf", "mortar", "aorus", "steel legend", "cvn")):
        cats.append("motherboard")
    if re.search(r"\d+w\b", kw):
        cats.append("psu")
    if any(t in kw for t in ("tiplus", "sn770", "sn850", "990 ", "990_", "gm7", "nv2", "kc3000",
                              "tipro", "ti600", "t700", "t500", "mx500", "nm710", "nm790")):
        cats.append("ssd")
    if any(t in kw for t in ("ak ", "ak_", "as ", "as_", "pa120", "frost", "kraken", "peerless")):
        cats.append("cooler")
    if any(t in kw for t in ("趣造", "包豪斯", "lancool", "p360", "o11", "ch560")):
        cats.append("chassis")
    return cats if cats else list(_VALID_CATS)


def match_hardware(keyword: str, grouped: dict[str, list[dict]]) -> int | None:
    kw_tokens = set(_tokenize(keyword))
    if not kw_tokens:
        return None
    best_score = 0.0
    best_id: int | None = None
    for cat in guess_category(keyword):
        for h in grouped.get(cat, []):
            mt = h["model_tokens"]
            if not mt or not all(t in kw_tokens for t in mt):
                continue
            score = float(len(mt))
            bt = h["brand_tokens"]
            if bt and all(t in kw_tokens for t in bt):
                score += 0.5
            if score > best_score:
                best_score = score
                best_id = h["id"]
    return best_id


# ─── csv 处理 ────────────────────────────────────────────────


def read_csv_priced_items(csv_path: Path) -> dict[str, list[tuple[float, str]]]:
    """读 csv,返回 {keyword: [(price, title), ...]},闲鱼 + 价格 > 0 的。

    title 保留用于后续按 hardware model token 二次过滤。
    """
    out: dict[str, list[tuple[float, str]]] = {}
    try:
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                plat = (row.get("platform") or "").strip().lower()
                if plat != "xianyu":
                    continue
                price_str = (row.get("current_price") or "").strip()
                if not price_str:
                    continue
                try:
                    price = float(price_str)
                except ValueError:
                    continue
                if price <= 0:
                    continue
                kw = (row.get("keyword") or "").strip()
                if not kw:
                    continue
                title = (row.get("title") or "").strip()
                out.setdefault(kw.replace("_", " "), []).append((price, title))
    except OSError as e:
        raise RuntimeError(f"read csv {csv_path} failed: {e}") from e
    return out


def filter_by_title(
    items: list[tuple[float, str]],
    model_tokens: list[str],
    brand_tokens: list[str],
) -> list[float]:
    """只保留 title 包含 model 关键 token 的商品价。

    规则:title token 中必须包含 model_tokens 的至少 ⌈len/2⌉ 个(向上取整)。
    若 model 仅 1 token,则该 token 必须出现在 title。brand 不强求(标题不一定带品牌)。
    """
    if not model_tokens:
        return [p for p, _ in items]
    required = max(1, (len(model_tokens) + 1) // 2)  # 至少一半 token 命中
    out: list[float] = []
    for price, title in items:
        if not title:
            continue
        title_tokens = set(_tokenize(title))
        hit = sum(1 for t in model_tokens if t in title_tokens)
        if hit >= required:
            out.append(price)
    return out


def trimmed_median(prices: list[float], trim_ratio: float = 0.1) -> float | None:
    """去掉两端 trim_ratio 比例(默认 10%)后算中位数。

    样本太少(<5)直接全部算中位数。
    """
    if not prices:
        return None
    if len(prices) < 5:
        return statistics.median(prices)
    sorted_ps = sorted(prices)
    k = int(len(sorted_ps) * trim_ratio)
    trimmed = sorted_ps[k:len(sorted_ps) - k]
    if not trimmed:
        trimmed = sorted_ps
    return statistics.median(trimmed)


# ─── SQL 生成 + 执行 ─────────────────────────────────────────


def _sql_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "''")


def build_insert_sql(rows: list[tuple[int, str, float, int]], record_date: date) -> str:
    if not rows:
        return ""
    lines: list[str] = [
        "-- merge_csv_to_xiaobai_mysql 生成",
        "SET NAMES utf8mb4;",
        f"USE {XIAOBAI_DB};",
        "START TRANSACTION;",
    ]
    for hid, kw, price, n in rows:
        kw_safe = _sql_escape(kw[:200])
        lines.append(
            f"-- {kw_safe} | {n} samples\n"
            f"INSERT INTO t_hardware_price_history (hardware_id, source, price, record_date) "
            f"SELECT {hid}, 'xianyu', {price:.2f}, '{record_date.isoformat()}' "
            f"FROM DUAL WHERE NOT EXISTS ("
            f"SELECT 1 FROM t_hardware_price_history "
            f"WHERE hardware_id={hid} AND source='xianyu' "
            f"AND record_date='{record_date.isoformat()}');"
        )
    lines.append("COMMIT;")
    return "\n".join(lines)


def exec_mysql_sql(pwd: str, sql_path: Path) -> None:
    cmd = [
        str(MYSQL_BIN), "-u", "root", f"-p{pwd}",
        "--default-character-set=utf8mb4", "-D", XIAOBAI_DB,
    ]
    with sql_path.open("r", encoding="utf-8") as f:
        r = subprocess.run(cmd, stdin=f, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise RuntimeError(f"mysql exec failed: {r.stderr[:500]}")


# ─── 单 csv 处理 ─────────────────────────────────────────────


def process_one_csv(
    csv_path: Path,
    grouped: dict[str, list[dict]],
    pwd: str,
    *,
    dry_run: bool,
) -> dict:
    """处理一个 csv 文件,返回统计 dict。

    流程:read → match → title 二次过滤 → trimmed median → INSERT
    """
    items_per_kw = read_csv_priced_items(csv_path)
    if not items_per_kw:
        return {"rows_in": 0, "kws": 0, "matched": 0, "inserted": 0,
                "rejected_by_title": 0, "status": "empty"}
    # 为快速查 hardware 详情,把 grouped 摊平成 {hid: entry}
    by_hid_entry: dict[int, dict] = {}
    for entries in grouped.values():
        for e in entries:
            by_hid_entry[int(e["id"])] = e

    matched: list[tuple[int, str, float, int]] = []
    rejected_kws = 0  # 匹配到 hardware 但 title 过滤后样本不足
    for kw, items in items_per_kw.items():
        hid = match_hardware(kw, grouped)
        if hid is None:
            continue
        hw = by_hid_entry.get(hid)
        if hw is None:
            continue
        clean_prices = filter_by_title(items, hw["model_tokens"], hw["brand_tokens"])
        # 至少 3 个有效样本才采纳(避免单条噪声主导)
        if len(clean_prices) < 3:
            rejected_kws += 1
            continue
        price = trimmed_median(clean_prices)
        if price is None or price <= 0:
            rejected_kws += 1
            continue
        matched.append((hid, kw, price, len(clean_prices)))

    # 同 hardware_id 多 keyword 时,取样本数最多的(更稳)
    by_hid: dict[int, tuple[int, str, float, int]] = {}
    for m in matched:
        if m[0] not in by_hid or m[3] > by_hid[m[0]][3]:
            by_hid[m[0]] = m
    deduped = list(by_hid.values())

    rows_in = sum(len(v) for v in items_per_kw.values())
    kws_count = len(items_per_kw)
    if dry_run or not deduped:
        return {
            "rows_in": rows_in,
            "kws": kws_count,
            "matched": len(matched),
            "rejected_by_title": rejected_kws,
            "inserted": 0 if dry_run else len(deduped),
            "status": "dry-run" if dry_run else "no-match",
        }

    sql = build_insert_sql(deduped, date.today())
    tmp_sql = ROOT / "data" / f".merge_tmp_{csv_path.stem[:30]}.sql"
    tmp_sql.write_text(sql, encoding="utf-8")
    try:
        exec_mysql_sql(pwd, tmp_sql)
    finally:
        tmp_sql.unlink(missing_ok=True)
    return {
        "rows_in": rows_in,
        "kws": kws_count,
        "matched": len(matched),
        "rejected_by_title": rejected_kws,
        "inserted": len(deduped),
        "status": "done",
    }


# ─── 主流程 ──────────────────────────────────────────────────


def collect_csv_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() == ".csv" else []
    if path.is_dir():
        return sorted(path.rglob("*.csv"))
    return []


def run(
    inputs: list[Path],
    pwd: str,
    *,
    dry_run: bool,
    force: bool,
) -> int:
    progress = load_progress()
    log.info("MySQL t_hardware 拉取中...")
    try:
        hw = fetch_mysql_hardware(pwd)
    except RuntimeError as e:
        log.error("%s", e)
        return 2
    log.info("MySQL t_hardware: %d 行", len(hw))
    grouped = build_index(hw)

    all_csv: list[Path] = []
    for inp in inputs:
        all_csv.extend(collect_csv_files(inp))
    if not all_csv:
        log.error("no csv files in inputs")
        return 2
    log.info("发现 %d 个 csv 文件", len(all_csv))

    skipped = 0
    new_processed: list[Path] = []
    total_inserted = 0
    total_matched = 0

    for csv_path in all_csv:
        key = _csv_key(csv_path)
        if not force and key in progress.get("processed", {}):
            skipped += 1
            continue
        try:
            stat = process_one_csv(csv_path, grouped, pwd, dry_run=dry_run)
        except RuntimeError as e:
            log.error("[FAIL] %s: %s", key, e)
            progress.setdefault("failed", {})[key] = {
                "error": str(e)[:200],
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            save_progress_atomic(progress)
            continue
        log.info(
            "[%s] %s rows=%d kws=%d matched=%d inserted=%d",
            stat["status"], key, stat["rows_in"], stat["kws"],
            stat["matched"], stat["inserted"],
        )
        if not dry_run:
            progress.setdefault("processed", {})[key] = {
                **stat,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            save_progress_atomic(progress)
        new_processed.append(csv_path)
        total_inserted += stat["inserted"]
        total_matched += stat["matched"]

    log.info(
        "DONE total_csv=%d skipped=%d processed=%d matched=%d inserted=%d",
        len(all_csv), skipped, len(new_processed), total_matched, total_inserted,
    )
    return 0


def cmd_list_progress() -> int:
    p = load_progress()
    n_done = len(p.get("processed", {}))
    n_fail = len(p.get("failed", {}))
    log.info("已处理 %d 个 csv,失败 %d 个", n_done, n_fail)
    log.info("进度文件: %s", PROGRESS_FILE)
    for k, v in list(p.get("processed", {}).items())[:5]:
        log.info("  ✓ %s | inserted=%s at=%s", k, v.get("inserted"), v.get("at"))
    if n_done > 5:
        log.info("  ... 共 %d 个", n_done)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", type=Path,
                       help="csv 文件或目录(可多个),目录会递归")
    parser.add_argument("--password", default=DEFAULT_MYSQL_PWD)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true",
                       help="忽略进度文件,所有 csv 重新处理")
    parser.add_argument("--list-progress", action="store_true",
                       help="列出已处理的 csv")
    args = parser.parse_args()

    _configure_logging()

    if args.list_progress:
        return cmd_list_progress()
    if not args.inputs:
        parser.error("需要至少一个输入 csv/目录,或用 --list-progress")
    return run(args.inputs, args.password, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
