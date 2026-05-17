"""把 autoPCBulid SQLite 的闲鱼价格融合到 XiaoBai MySQL 的 t_hardware_price_history。

设计:
- **绝不动 t_hardware 本身**(不 UPDATE / 不 INSERT 新硬件)
- **只往 t_hardware_price_history 追加新行**:source='xianyu', record_date=today
- 不破坏既有数据(用 INSERT OR IGNORE 等价的 NOT EXISTS 子查询防重)
- 匹配规则:autoPCBulid keyword(REPLACE _ 为空格)与 MySQL CONCAT(brand, ' ', model)
  做"token 包含"匹配 — keyword 包含所有 brand 词且包含 model 第一个词

流程:
1. 读 MySQL t_hardware,在内存建索引 {(category, brand_lower, model_first_token_lower): id}
2. 读 SQLite products,按 keyword 聚合:median_price, n
3. 对每个 keyword,尝试匹配到一个 hardware_id
4. 生成 INSERT SQL 文件 + 跑 mysql.exe

用法:
    python scripts/merge_xianyu_to_xiaobai_mysql.py --dry-run
    python scripts/merge_xianyu_to_xiaobai_mysql.py
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import statistics
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQLITE_DB = ROOT / "data" / "prices.db"
MYSQL_BIN = Path("C:/Program Files/MySQL/MySQL Server 8.0/bin/mysql.exe")
XIAOBAI_DB = "xiao_bai_zhuang_ji_zhu_shou"
DEFAULT_MYSQL_PWD = "123456"

# autoPCBulid SQLite t_hardware.category(大写) → XiaoBai MySQL t_hardware.category(小写)
# XiaoBai 没有 hdd / nic_wired / nic_wireless 这几个类,这些不参与匹配
_CAT_MAP: dict[str, str] = {
    "cpu": "cpu",
    "gpu": "gpu",
    "motherboard": "motherboard",
    "memory": "memory",
    "ssd": "ssd",
    "psu": "psu",
    "chassis": "chassis",
    "cooler": "cooler",
}

log = logging.getLogger("merge_xb")


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO)


# ─── MySQL 读 ─────────────────────────────────────────────────


def fetch_mysql_hardware(pwd: str) -> list[dict[str, str | int]]:
    """SELECT id, category, brand, model FROM t_hardware,只取启用的。"""
    cmd = [
        str(MYSQL_BIN), "-u", "root", f"-p{pwd}",
        "--default-character-set=utf8mb4",
        "-B", "-N",
        "-D", XIAOBAI_DB,
        "-e",
        "SELECT id, category, COALESCE(brand,''), COALESCE(model,'') "
        "FROM t_hardware WHERE status=1 AND deleted=0;",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", check=True
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"mysql query failed: {e.stderr}") from e

    rows: list[dict[str, str | int]] = []
    for line in result.stdout.splitlines():
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
    """切 token + 小写。"""
    return [t.lower() for t in _TOKEN_SPLIT_RE.split(s) if t]


def build_index(hw_rows: list[dict[str, str | int]]) -> dict[str, list[dict]]:
    """按 category 分组,每组列出所有硬件(包含预处理的 tokens)。"""
    grouped: dict[str, list[dict]] = {}
    for h in hw_rows:
        cat = str(h["category"])
        brand_tokens = _tokenize(str(h["brand"]))
        model_tokens = _tokenize(str(h["model"]))
        entry = {
            "id": h["id"],
            "brand_tokens": brand_tokens,
            "model_tokens": model_tokens,
            "raw": f"{h['brand']} {h['model']}".strip(),
        }
        grouped.setdefault(cat, []).append(entry)
    return grouped


def guess_category(keyword: str) -> list[str]:
    """从 keyword 猜测可能的 category(用于减少匹配搜索空间)。"""
    kw = keyword.lower()
    cats: list[str] = []
    # GPU 特征
    if any(t in kw for t in ("rtx", "gtx", "radeon", "rx ", "rx_", "arc ", "arc_", "quadro", "titan")):
        cats.append("gpu")
    # CPU 特征
    if any(t in kw for t in ("i3-", "i5-", "i7-", "i9-", "ryzen", "core i", "epyc", "xeon",
                              "athlon", "celeron", "pentium", "threadripper", "ultra")):
        cats.append("cpu")
    # RAM 特征
    if "ddr" in kw or any(t in kw for t in ("fury", "vengeance", "ripjaws", "xpg", "trident")):
        cats.append("memory")
    # MB 特征
    if any(t in kw for t in ("h610", "b660", "b760", "z690", "z790", "b550", "b650", "x670", "a620", "x870",
                              "tuf", "mortar", "aorus", "steel legend", "cvn")):
        cats.append("motherboard")
    # PSU 特征
    if re.search(r"\d+w\b", kw):
        cats.append("psu")
    # SSD 特征
    if any(t in kw for t in ("tiplus", "sn770", "sn850", "990 ", "990_", "gm7", "nv2", "kc3000")):
        cats.append("ssd")
    # cooler 特征
    if any(t in kw for t in ("ak ", "ak_", "as ", "as_", "pa120", "frost", "kraken", "peerless")):
        cats.append("cooler")
    # chassis 特征
    if any(t in kw for t in ("趣造", "包豪斯", "lancool", "p360", "o11", "ch560")):
        cats.append("chassis")
    return cats if cats else list(_CAT_MAP.values())


def match_hardware(
    keyword: str,
    grouped: dict[str, list[dict]],
) -> int | None:
    """对 keyword 找最佳匹配的 hardware id。

    规则:**model 所有 token 必须全部在 keyword 里**(精确包含),品牌只是加分项。
    打分:model 总 token 数(基础分) + brand 命中加 0.5 倾向(避免 brand 误差)。
    多匹配时优先打分高的(更具体的胜出)。
    """
    kw_tokens = set(_tokenize(keyword))
    if not kw_tokens:
        return None
    candidate_cats = guess_category(keyword)
    best_score: float = 0.0
    best_id: int | None = None
    for cat in candidate_cats:
        for h in grouped.get(cat, []):
            brand_t = h["brand_tokens"]
            model_t = h["model_tokens"]
            if not model_t:
                continue
            # model 所有 token 必须在 keyword(严格)
            if not all(t in kw_tokens for t in model_t):
                continue
            # 基础分 = model token 数(model 越长越具体)
            score: float = float(len(model_t))
            # brand 在 keyword 加 0.5(优先 brand 也对的)
            if brand_t and all(t in kw_tokens for t in brand_t):
                score += 0.5
            if score > best_score:
                best_score = score
                best_id = int(h["id"])
    return best_id


# ─── 聚合 + 输出 ──────────────────────────────────────────────


def aggregate_xianyu_items(
    sqlite_path: Path,
) -> dict[str, list[tuple[float, str]]]:
    """从 SQLite 读闲鱼商品 (price, title),按 keyword 聚合。

    title 保留用于后续按 hardware model token 二次过滤(跟 csv 版一致)。
    返回 {keyword_unslug: [(price, title), ...]}
    """
    conn = sqlite3.connect(str(sqlite_path))
    try:
        rows = conn.execute(
            "SELECT keyword, current_price, title FROM products "
            "WHERE platform='xianyu' AND current_price IS NOT NULL AND current_price > 0"
        ).fetchall()
    finally:
        conn.close()

    bucket: dict[str, list[tuple[float, str]]] = {}
    for kw, price, title in rows:
        if not kw:
            continue
        unslug = kw.replace("_", " ")
        bucket.setdefault(unslug, []).append((float(price), title or ""))
    return bucket


def filter_by_title(
    items: list[tuple[float, str]],
    model_tokens: list[str],
) -> list[float]:
    """只保留 title 包含 model 关键 token 的商品价(跟 csv 版同算法)。

    规则:title token 必须包含 model_tokens 至少 ⌈len/2⌉ 个;model 仅 1 token 时必须出现。
    """
    if not model_tokens:
        return [p for p, _ in items]
    required = max(1, (len(model_tokens) + 1) // 2)
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
    """去掉两端 trim_ratio 比例(默认 10%)后算中位数。样本 <5 直接全体取中位数。"""
    if not prices:
        return None
    if len(prices) < 5:
        return statistics.median(prices)
    sorted_ps = sorted(prices)
    k = int(len(sorted_ps) * trim_ratio)
    trimmed = sorted_ps[k:len(sorted_ps) - k] or sorted_ps
    return statistics.median(trimmed)


def _sql_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "''")


def build_insert_sql(
    matched: list[tuple[int, str, float, int]],
    record_date: date,
) -> str:
    """生成 INSERT SQL,用 NOT EXISTS 防重复(同 hardware_id + 同源 + 同天只插一次)。

    每行参数:(hardware_id, keyword_for_log, median_price, sample_count)
    """
    if not matched:
        return ""
    lines: list[str] = [
        "-- 自动生成,合并 autoPCBulid 闲鱼价格到 XiaoBai MySQL",
        "-- 只 INSERT,不 UPDATE,跳过同 (hardware_id, source, record_date) 已存在",
        "SET NAMES utf8mb4;",
        "USE xiao_bai_zhuang_ji_zhu_shou;",
        "START TRANSACTION;",
    ]
    for hid, kw, price, n in matched:
        kw_safe = _sql_escape(kw[:200])
        lines.append(
            f"-- {kw_safe} | {int(n)} samples\n"
            f"INSERT INTO t_hardware_price_history "
            f"(hardware_id, source, price, record_date) "
            f"SELECT {hid}, 'xianyu', {price:.2f}, '{record_date.isoformat()}' "
            f"FROM DUAL WHERE NOT EXISTS ("
            f"  SELECT 1 FROM t_hardware_price_history "
            f"  WHERE hardware_id={hid} AND source='xianyu' "
            f"  AND record_date='{record_date.isoformat()}'"
            f");"
        )
    lines.append("COMMIT;")
    return "\n".join(lines)


# ─── 主流程 ──────────────────────────────────────────────────


def run(pwd: str, *, dry_run: bool, out_sql: Path) -> int:
    if not SQLITE_DB.exists():
        log.error("sqlite db not found: %s", SQLITE_DB)
        return 2

    log.info("fetching MySQL t_hardware...")
    try:
        hw = fetch_mysql_hardware(pwd)
    except RuntimeError as e:
        log.error("%s", e)
        return 2
    log.info("MySQL t_hardware: %d active rows", len(hw))

    grouped = build_index(hw)

    log.info("aggregating xianyu items (price+title) from SQLite...")
    items_per_kw = aggregate_xianyu_items(SQLITE_DB)
    log.info("autoPCBulid xianyu keywords: %d", len(items_per_kw))

    # 摊平 grouped 方便按 hid 反查 model_tokens
    by_hid_entry: dict[int, dict] = {}
    for entries in grouped.values():
        for e in entries:
            by_hid_entry[int(e["id"])] = e

    log.info("matching keywords -> hardware, title-filter, trimmed median...")
    matched: list[tuple[int, str, float, int]] = []
    unmatched: list[str] = []
    rejected_by_title = 0
    for kw, items in items_per_kw.items():
        hid = match_hardware(kw, grouped)
        if hid is None:
            unmatched.append(kw)
            continue
        hw = by_hid_entry.get(hid)
        if hw is None:
            continue
        clean = filter_by_title(items, hw["model_tokens"])
        if len(clean) < 3:
            rejected_by_title += 1
            continue
        price = trimmed_median(clean)
        if price is None or price <= 0:
            rejected_by_title += 1
            continue
        matched.append((hid, kw, price, len(clean)))

    # 同 hardware 多 keyword 时,取样本数最多的(跟 csv 版一致)
    by_hid: dict[int, tuple[int, str, float, int]] = {}
    for m in matched:
        if m[0] not in by_hid or m[3] > by_hid[m[0]][3]:
            by_hid[m[0]] = m
    deduped = list(by_hid.values())
    log.info("rejected by title-filter: %d keywords", rejected_by_title)

    log.info(
        "match result: matched=%d (deduped %d unique hardware), unmatched=%d keywords",
        len(matched), len(deduped), len(unmatched),
    )
    if unmatched and dry_run:
        log.info("unmatched samples (first 10):")
        for kw in unmatched[:10]:
            log.info("  %s", kw)

    sql = build_insert_sql(deduped, date.today())
    out_sql.parent.mkdir(parents=True, exist_ok=True)
    out_sql.write_text(sql, encoding="utf-8")
    log.info("SQL written: %s (%d INSERT)", out_sql, len(deduped))

    if dry_run:
        log.info("dry-run done, MySQL untouched")
        return 0

    log.info("executing SQL on MySQL...")
    cmd = [
        str(MYSQL_BIN), "-u", "root", f"-p{pwd}",
        "--default-character-set=utf8mb4",
        "-D", XIAOBAI_DB,
    ]
    try:
        with out_sql.open("r", encoding="utf-8") as f:
            r = subprocess.run(
                cmd, stdin=f, capture_output=True, text=True,
                encoding="utf-8", check=False,
            )
        if r.returncode != 0:
            log.error("mysql exec failed: %s", r.stderr[:500])
            return 2
    except OSError as e:
        log.error("subprocess error: %s", e)
        return 2

    # 验证
    verify_cmd = [
        str(MYSQL_BIN), "-u", "root", f"-p{pwd}",
        "--default-character-set=utf8mb4", "-B", "-N",
        "-D", XIAOBAI_DB,
        "-e",
        f"SELECT COUNT(*) FROM t_hardware_price_history "
        f"WHERE source='xianyu' AND record_date='{date.today().isoformat()}';",
    ]
    r2 = subprocess.run(verify_cmd, capture_output=True, text=True, encoding="utf-8")
    today_count = r2.stdout.strip().splitlines()[-1] if r2.stdout else "?"
    log.info("verified: today xianyu rows in t_hardware_price_history = %s", today_count)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--password", default=DEFAULT_MYSQL_PWD)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--out", type=Path,
        default=ROOT / "data" / "merge_xianyu_to_xiaobai.sql",
    )
    args = parser.parse_args()

    _configure_logging()
    return run(args.password, dry_run=args.dry_run, out_sql=args.out)


if __name__ == "__main__":
    raise SystemExit(main())
