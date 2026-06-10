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
import os
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
DEFAULT_MYSQL_PWD = os.environ.get("XIAOBAI_MYSQL_PASSWORD")
PROGRESS_FILE = ROOT / "data" / "merge_progress.json"

# XiaoBai category 子集(只有这些类别参与匹配,字典里其他类别 XiaoBai 没有)
_VALID_CATS: tuple[str, ...] = (
    "cpu", "gpu", "motherboard", "memory", "ssd", "psu", "chassis", "cooler",
)

# 无意义的关键词黑名单 — 早期字典生成器留下的规格抽象名,搜出来全是噪声
# 黑名单匹配方式:精确(exact)或前缀(prefix)
_KEYWORD_BLACKLIST: set[str] = {
    "am3", "am3 am3", "am3+", "am3+ am3",
    "lga2011-3", "2 x lga2011-3", "2 x lga2011-3 narrow",
    "lga1155", "lga1151", "lga1200",
    "ddr4", "ddr5", "ddr3",
}
# 前缀黑名单:`1000W bronze` / `DDR3-1066-12GB` 这种规格抽象,带功率/容量后缀
_KEYWORD_BLACKLIST_PREFIX: tuple[str, ...] = (
    # PSU 功率 + 认证档(早期字典抽象,不是品牌型号)
    # 形如 "1000W None / bronze / gold / platinum / plus / silver / titanium"
    # 简单规则:数字+W 开头且包含认证关键词
)
# 这些前缀匹配在 _is_blacklisted 里用正则做
_PSU_SPEC_RE = re.compile(
    r"^\d+w\s+(none|bronze|gold|platinum|plus|silver|titanium)\s*$",
    re.IGNORECASE,
)
# DDR 规格:DDR3-1066-12GB / DDR4-3200-16GB 等纯规格描述,没品牌
_DDR_SPEC_RE = re.compile(r"^ddr[3-5]-\d+-\d+gb\s*$", re.IGNORECASE)


def _is_blacklisted(kw: str) -> bool:
    """判断 keyword 是否在黑名单(精确名 + 规格前缀两种规则)。"""
    low = kw.lower().strip()
    if low in _KEYWORD_BLACKLIST:
        return True
    if _PSU_SPEC_RE.match(low):
        return True
    if _DDR_SPEC_RE.match(low):
        return True
    return False

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
    """tmp + rename 原子写,带 retry 抗 Windows 反病毒/Defender 实时扫描冲突。"""
    import time
    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=".merge_progress_", suffix=".json", dir=PROGRESS_FILE.parent
    )
    tmp = Path(tmp_str)
    try:
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Windows 上反病毒可能短暂锁住目标文件,retry 3 次
        for attempt in range(3):
            try:
                tmp.replace(PROGRESS_FILE)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.3)
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
    """读 MySQL t_hardware,**不过滤 status**(被下架的硬件如 i5-13490F 也要参与匹配,
    避免我们再给它新建重复条目);只过滤 deleted=1 这种真删除的。"""
    cmd = [
        str(MYSQL_BIN), "-u", "root", f"-p{pwd}",
        "--default-character-set=utf8mb4", "-B", "-N",
        "-D", XIAOBAI_DB,
        "-e",
        "SELECT id, category, COALESCE(brand,''), COALESCE(model,'') "
        "FROM t_hardware WHERE deleted=0;",
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
# R5/R7/R9 → Ryzen 5/7/9
# 前需词边界,后允许:空格 / 词尾 / 后接数字(R5 5700X 这种,X 不在 \b 里所以原 \b 失败)
# 用 (?=\s|$|\d) 替代尾部 \b:R5 5600 ✓, R5 5700X ✓, R5500 ✗(被前面 \b 拦)
_RYZEN_SHORT_RE = re.compile(r"\bR([579])(?=\s|$)", re.IGNORECASE)
# i3-/i5-/i7-/i9- 加 Core 前缀(MySQL 的 model 是 'Core i5-12400F' 带 Core 前缀)
_INTEL_CORE_SHORT_RE = re.compile(r"\b(i[3579]-)", re.IGNORECASE)


def _normalize_keyword(s: str) -> str:
    """归一化 keyword 的常见别名,提升匹配率。

    R5/R7/R9 → Ryzen 5/7/9
    i3-/i5-/i7-/i9- → Core i3-/i5-/i7-/i9-(MySQL 那边 model 带 Core 前缀)
    """
    s = _RYZEN_SHORT_RE.sub(r"Ryzen \1", s)
    s = _INTEL_CORE_SHORT_RE.sub(r"Core \1", s)
    return s


def _tokenize(s: str) -> list[str]:
    return [t.lower() for t in _TOKEN_SPLIT_RE.split(_normalize_keyword(s)) if t]


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
    # GPU 显卡(独立显卡型号)
    if any(t in kw for t in ("rtx", "gtx", "radeon", "rx ", "rx_", "arc ", "arc_", "quadro", "titan")):
        cats.append("gpu")
    # CPU(明确处理器型号,避免误把 SSD/cooler/网卡当 cpu)
    if any(t in kw for t in ("i3-", "i5-", "i7-", "i9-", "ryzen", "core i", "epyc", "xeon",
                              "athlon", "celeron", "pentium", "threadripper", "ultra ")):
        cats.append("cpu")
    # 内存
    if "ddr" in kw or any(t in kw for t in ("fury", "vengeance", "ripjaws", "xpg lancer", "trident")):
        cats.append("memory")
    # 主板
    if any(t in kw for t in ("h610", "b660", "b760", "z690", "z790", "b550", "b650", "x670",
                              "a620", "x870", "tuf gaming", "mortar", "aorus", "steel legend", "cvn")):
        cats.append("motherboard")
    # SSD(具体型号关键词)
    if any(t in kw for t in (
        "tiplus", "tipro", "ti600", "sn770", "sn850", "sn580",
        "990 pro", "990_pro", "990 evo", "990_evo", "980 pro", "980_pro",
        "970 evo", "870 evo", "kc3000", "nv2", "a2000",
        "t700", "t705", "t500", "p5 plus", "mx500",
        "nm710", "nm790", "c4000", "e3000", "g4000", "rc20",
        "exceria", "sx8200", "sx6000", "m10p", "n930e", "n7000",
        "p3000", "p5000", "gm7000", "gm7", "se10",
    )):
        cats.append("ssd")
    # HDD(机械硬盘)
    if any(t in kw for t in ("蓝盘", "黑盘", "红盘", "紫盘", "酷鱼", "酷狼", "酷鹰",
                              "barracuda", "ironwolf", "skyhawk", "p300", "x300", "n300", "hgst")):
        cats.append("hdd")
    # 散热器
    if any(t in kw for t in ("peerless", "pa120", "frost commander", "ak400", "ak500", "ak620",
                              "kraken", "frozen", "masterliquid", "暴雪",
                              "ak ", "ak_", "hx 120", "hx 240", "hx 280", "hx 360",
                              "argb", "下压式风冷", "双塔风冷", "塔式风冷", "水冷", "风冷")):
        cats.append("cooler")
    # 机箱
    if any(t in kw for t in ("趣造", "包豪斯", "lancool", "o11", "p360", "p400", "ch560",
                              "td500", "td600", "海景房", "高风道", "玻璃侧透", "atx", "m-atx", "matx",
                              "迫击炮 case", "flux ", "gx680")):
        cats.append("chassis")
    # 电源(单独 Wattage + 品牌前缀)
    if re.search(r"\b\d{3,4}w\b", kw):
        cats.append("psu")
    # 网卡(XiaoBai 没收,空 list 让 _infer_category 返回 None)
    if any(t in kw for t in ("ax200", "ax210", "ax211", "be200", "i225", "i226", "i350",
                              "x550", "tg-3", "tg3", "archer tx")):
        cats.append("__nic__")  # 哨兵值,_infer_category 会跳过
    # 注意:**没识别返回空 list**,_infer_category 会跳过 auto-add
    return cats


_GPU_VENDOR_PREFIXES = {"geforce", "radeon"}


def match_hardware(keyword: str, grouped: dict[str, list[dict]]) -> int | None:
    """token 匹配 keyword 到 MySQL hardware id。

    匹配时 guess_category 空 list 兜底用全集(尽力找匹配);
    auto-add 时反之:空 list 应拒绝(避免乱分类)。
    """
    kw_tokens = set(_tokenize(keyword))
    if not kw_tokens:
        return None
    best_score = 0.0
    best_id: int | None = None
    cats = guess_category(keyword)
    if not cats:
        cats = list(_VALID_CATS)  # 匹配阶段兜底全集
    # 过滤哨兵值 __nic__
    cats = [c for c in cats if c != "__nic__"]
    for cat in cats:
        is_gpu = (cat == "gpu")
        for h in grouped.get(cat, []):
            mt_raw = h["model_tokens"]
            # GPU 类去掉 geforce/radeon 前缀 token,弱化匹配
            if is_gpu:
                mt = [t for t in mt_raw if t not in _GPU_VENDOR_PREFIXES]
            else:
                mt = mt_raw
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

    **核心 token 子拆分**:`i5-13490f` 这种带连字符的型号,title 里大概率写成
    `i5 13490f`(空格)。所以再用 `-`/`_`/`/` 拆 model_token 一次,只要拆出的
    sub-token 都在 title 里就算命中。这让 `i5-13490f` 等价 `i5 + 13490f`。
    """
    if not model_tokens:
        return [p for p, _ in items]
    # 把 model_tokens 拆成 sub-tokens(避免连字符锁死)
    expanded: list[list[str]] = []
    for t in model_tokens:
        subs = [s for s in re.split(r"[-_/]+", t) if s]
        expanded.append(subs)
    required = max(1, (len(model_tokens) + 1) // 2)
    out: list[float] = []
    for price, title in items:
        if not title:
            continue
        title_tokens = set(_tokenize(title))
        # 也按 - / _ 切一次 title token(覆盖 title 里的 "i5 13490F" 形式)
        for tt in list(title_tokens):
            for s in re.split(r"[-_/]+", tt):
                if s:
                    title_tokens.add(s)
        hit = sum(
            1 for subs in expanded
            if subs and all(s in title_tokens for s in subs)
        )
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


_AUTO_ADD_MIN_SAMPLES = 10  # 触发"新产品自动添加"的最少商品数门槛


def _infer_category_from_keyword(kw: str) -> str | None:
    """从 keyword 推断 XiaoBai category(用于新产品添加)。

    auto-add 严格:guess_category 返回空 → None(跳过,避免乱分类)
    含 __nic__ 哨兵 → None(XiaoBai 不收网卡类)
    """
    cats = guess_category(kw)
    if not cats:
        return None
    if "__nic__" in cats:
        return None  # 网卡跳过(XiaoBai 没此 category)
    # XiaoBai 没有 hdd 分类 — 跳过 HDD 不 auto-add(避免污染 ssd 类)
    if cats == ["hdd"] or (len(cats) == 1 and cats[0] == "hdd"):
        return None
    # 优先级,排除 hdd
    priority = ["gpu", "cpu", "ssd", "memory", "motherboard", "cooler", "chassis", "psu"]
    for p in priority:
        if p in cats:
            return p
    return None


def _infer_brand_model(kw: str) -> tuple[str | None, str]:
    """从 keyword 拆 brand 和 model。规则:首 token 当 brand(如果是已知品牌),其余 model。"""
    parts = kw.strip().split(maxsplit=1)
    if len(parts) < 2:
        return None, kw.strip()
    return parts[0], parts[1]


def _new_hardware_sql(category: str, brand: str | None, model: str) -> str:
    """生成 INSERT t_hardware 的 SQL(单行)。返回 INSERT 语句。"""
    brand_sql = "NULL" if not brand else f"'{_sql_escape(brand[:64])}'"
    model_safe = _sql_escape(model[:128])
    return (
        f"INSERT INTO t_hardware (category, brand, model, status, sort, deleted) "
        f"VALUES ('{category}', {brand_sql}, '{model_safe}', 1, 9999, 0);"
    )


def process_one_csv(
    csv_path: Path,
    grouped: dict[str, list[dict]],
    pwd: str,
    *,
    dry_run: bool,
    auto_add: bool = False,
) -> dict:
    """处理一个 csv 文件,返回统计 dict。

    流程:read → match → title 二次过滤 → trimmed median → INSERT
    auto_add=True 时,unmatched + samples >= 阈值 的 keyword 触发"新产品自动添加":
        INSERT t_hardware 一行 → 拿 LAST_INSERT_ID 写 price_history
    """
    items_per_kw = read_csv_priced_items(csv_path)
    if not items_per_kw:
        return {"rows_in": 0, "kws": 0, "matched": 0, "inserted": 0,
                "rejected_by_title": 0, "auto_added": 0, "status": "empty"}
    # 为快速查 hardware 详情,把 grouped 摊平成 {hid: entry}
    by_hid_entry: dict[int, dict] = {}
    for entries in grouped.values():
        for e in entries:
            by_hid_entry[int(e["id"])] = e

    matched: list[tuple[int, str, float, int]] = []
    auto_add_list: list[tuple[str, list[tuple[float, str]]]] = []  # (kw, items) 待新增
    rejected_kws = 0  # 匹配到 hardware 但 title 过滤后样本不足
    blacklisted = 0
    for kw, items in items_per_kw.items():
        # 黑名单跳过(AM3/规格抽象等无意义关键词)
        if _is_blacklisted(kw):
            blacklisted += 1
            continue
        hid = match_hardware(kw, grouped)
        if hid is None:
            # 未匹配 → 看是否触发自动添加
            if auto_add and len(items) >= _AUTO_ADD_MIN_SAMPLES:
                auto_add_list.append((kw, items))
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
    if dry_run:
        return {
            "rows_in": rows_in,
            "kws": kws_count,
            "matched": len(matched),
            "rejected_by_title": rejected_kws,
            "would_auto_add": len(auto_add_list),
            "inserted": 0,
            "status": "dry-run",
        }
    if not deduped and not auto_add_list:
        return {
            "rows_in": rows_in, "kws": kws_count, "matched": 0,
            "rejected_by_title": rejected_kws, "auto_added": 0, "inserted": 0,
            "status": "no-match",
        }

    # 1. 先写已匹配的 price_history
    if deduped:
        sql = build_insert_sql(deduped, date.today())
        tmp_sql = ROOT / "data" / f".merge_tmp_{csv_path.stem[:30]}.sql"
        tmp_sql.write_text(sql, encoding="utf-8")
        try:
            exec_mysql_sql(pwd, tmp_sql)
        finally:
            tmp_sql.unlink(missing_ok=True)

    # 2. 处理"新产品自动添加":逐个 INSERT t_hardware → LAST_INSERT_ID → INSERT price_history
    auto_added = 0
    if auto_add_list:
        auto_added = _execute_auto_add(auto_add_list, pwd)

    return {
        "rows_in": rows_in,
        "kws": kws_count,
        "matched": len(matched),
        "rejected_by_title": rejected_kws,
        "auto_added": auto_added,
        "inserted": len(deduped),
        "status": "done",
    }


def _execute_auto_add(
    auto_add_list: list[tuple[str, list[tuple[float, str]]]],
    pwd: str,
) -> int:
    """对 unmatched keyword 新增 t_hardware 行,然后写 price_history。

    用一个事务里的多个 SQL 块,每个 keyword 单独 INSERT + LAST_INSERT_ID + INSERT。
    """
    sql_lines: list[str] = [
        "-- 新产品自动添加 (auto-add)",
        "SET NAMES utf8mb4;",
        f"USE {XIAOBAI_DB};",
        "START TRANSACTION;",
    ]
    today = date.today().isoformat()
    n = 0
    for kw, items in auto_add_list:
        category = _infer_category_from_keyword(kw)
        if not category:
            continue
        brand, model = _infer_brand_model(kw)
        # 算价格(无 title 二次过滤,因 hardware 是新的,model_tokens 就是 keyword)
        prices = [p for p, _ in items]
        price = trimmed_median(prices)
        if price is None or price <= 0:
            continue
        sql_lines.append(_new_hardware_sql(category, brand, model))
        sql_lines.append(
            f"INSERT INTO t_hardware_price_history "
            f"(hardware_id, source, price, record_date) "
            f"VALUES (LAST_INSERT_ID(), 'xianyu', {price:.2f}, '{today}');"
        )
        n += 1
    sql_lines.append("COMMIT;")

    if n == 0:
        return 0
    tmp_sql = ROOT / "data" / ".merge_auto_add.sql"
    tmp_sql.write_text("\n".join(sql_lines), encoding="utf-8")
    try:
        exec_mysql_sql(pwd, tmp_sql)
    finally:
        tmp_sql.unlink(missing_ok=True)
    return n


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
    auto_add: bool = False,
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
            stat = process_one_csv(csv_path, grouped, pwd, dry_run=dry_run, auto_add=auto_add)
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
    parser.add_argument(
        "--password",
        default=DEFAULT_MYSQL_PWD,
        help="MySQL 密码；也可用环境变量 XIAOBAI_MYSQL_PASSWORD",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true",
                       help="忽略进度文件,所有 csv 重新处理")
    parser.add_argument("--list-progress", action="store_true",
                       help="列出已处理的 csv")
    parser.add_argument("--auto-add", action="store_true",
                       help="把 unmatched 且样本数 >= 10 的 keyword 自动新增到 t_hardware 表"
                            "(然后写 price_history)。⚠️ 会动 t_hardware 表结构外的数据")
    args = parser.parse_args()

    _configure_logging()

    if args.list_progress:
        return cmd_list_progress()
    if not args.inputs:
        parser.error("需要至少一个输入 csv/目录,或用 --list-progress")
    if args.password is None:
        parser.error("缺少 MySQL 密码：请传 --password 或设置 XIAOBAI_MYSQL_PASSWORD")
    return run(args.inputs, args.password, dry_run=args.dry_run, force=args.force,
               auto_add=args.auto_add)


if __name__ == "__main__":
    raise SystemExit(main())
