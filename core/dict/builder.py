"""硬件字典构建主流水线。

流程：load_docyx → normalize → filter_scope → aggregate_chips → write_csv

不做的事：
  - 不取价（docyx price 是 USD，价格走阶段 12）
  - 不访问 vendor 代码（仅读 data/json/ 下 JSON）
  - 不从 core/ 之外反向依赖
"""

from __future__ import annotations

import csv
import json
import logging
import re
import string
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from core.dict.categories import (
    ALLOWED_CPU_MICROARCH,
    ALLOWED_GPU_CHIP_PATTERNS,
    ALLOWED_MB_SOCKETS,
    ALLOWED_RAM_DDR_VERSIONS,
    BRAND_ALIASES,
    CATEGORY_MAP,
    DEFAULT_OUT_DIR,
    MB_CHIPSETS_2015_PLUS,
    MULTI_WORD_BRANDS,
    PROJECT_CATEGORIES,
    VENDOR_DOCYX_JSON_DIR,
)
from core.dict.parse_model import drives_release_year, release_year_from_spec_json

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class SkuRow:
    sku_title: str
    chip: str
    spec_json: str      # json.dumps 后的规格字段，便于下游 pd 读
    source: str         # "docyx" | "jd_category" | "override"


@dataclass
class ChipRow:
    chip: str
    generation: str
    vendor: str
    release_year_est: int | None
    source: str


@dataclass
class BuildSummary:
    week: str
    out_dir: str
    categories: dict[str, dict[str, int]] = field(default_factory=dict)
    unfiltered_count: int = 0


# ---------------------------------------------------------------------------
# 品牌 & 芯片抽取
# ---------------------------------------------------------------------------

_PUNCT_STRIP = str.maketrans("", "", string.punctuation.replace("-", "").replace(".", ""))
_SLUG_RE = re.compile(r"[^a-z0-9_-]")


def _safe_brand_slug(brand: str) -> str:
    """文件名安全化：仅保留 a-z / 0-9 / _ / -；非 ASCII 字符被丢弃。"""
    cleaned = _SLUG_RE.sub("", brand.lower())
    return (cleaned[:40] or "unknown")


def extract_brand(name: str) -> str:
    """从产品 name 抽品牌 slug。流程：
      1. 整串 lower 与 MULTI_WORD_BRANDS 逐条前缀匹配（覆盖 'In Win' 等）
      2. 否则取首 token，经 BRAND_ALIASES 别名归一
      3. 最后 _safe_brand_slug 清理字符（防 bidi 控制符等注入文件名）
    """
    if not name:
        return "unknown"
    low = name.strip().lower()
    for prefix, slug in MULTI_WORD_BRANDS:
        if low.startswith(prefix):
            return _safe_brand_slug(slug)
    first = low.split()[0].translate(_PUNCT_STRIP)
    raw = BRAND_ALIASES.get(first, first) or "unknown"
    return _safe_brand_slug(raw)


_MB_CHIPSET_CAND_RE = re.compile(r"\b([A-Z]\d{2,3}[A-Z]?)\b")


def extract_mb_chipset(name: str) -> str | None:
    """从主板 name 抽芯片组（B760/X670E/Z97...）。未命中返回 None。"""
    if not name:
        return None
    for tok in _MB_CHIPSET_CAND_RE.findall(name.upper()):
        if tok in MB_CHIPSETS_2015_PLUS:
            return tok
    return None


def extract_gpu_generation(chipset: str) -> str:
    """从 GPU chipset 文本抽代际标签（用于 chips.csv）。"""
    if not chipset:
        return "unknown"
    m = re.search(r"(GTX|RTX)\s*(\d0)\d+", chipset)
    if m:
        return f"NV {m.group(1)} {m.group(2)}0 series"
    m = re.search(r"RX\s*(\d)\d{3}", chipset)
    if m:
        return f"AMD RX {m.group(1)}000 series"
    if "Arc" in chipset:
        return "Intel Arc"
    if "R9" in chipset or "R7" in chipset or "Fury" in chipset or "VII" in chipset:
        return "AMD R/VII legacy"
    return "other"


# ---------------------------------------------------------------------------
# 2015+ 过滤
# ---------------------------------------------------------------------------

ScopeResult = tuple[bool, str]  # (是否在范围内, 命中原因或淘汰原因)


def scope_gpu(row: dict[str, Any]) -> ScopeResult:
    """用 regex 白名单匹配 chipset 字段。覆盖大小写（PRO）/ 词边界（RTX A4000）/ 新家族（Ada）。"""
    chipset = (row.get("chipset") or "").strip()
    if not chipset:
        return False, "no_chipset"
    for pat in ALLOWED_GPU_CHIP_PATTERNS:
        m = pat.search(chipset)
        if m:
            return True, f"pattern:{pat.pattern}"
    return False, f"chipset_out_of_scope:{chipset}"


def scope_cpu(row: dict[str, Any]) -> ScopeResult:
    """CPU 过滤：白名单精确匹配 microarchitecture。未知 uarch 保留并标 unknown 前缀，
    run_build 据此把行同步落 _unfiltered.csv（阶段 9 md 契约：保留 + 可 review）。"""
    uarch = (row.get("microarchitecture") or "").strip()
    if not uarch:
        return True, "unknown_uarch_missing"
    if uarch in ALLOWED_CPU_MICROARCH:
        return True, f"uarch:{uarch}"
    return False, f"uarch_out_of_scope:{uarch}"


def scope_mb(row: dict[str, Any]) -> ScopeResult:
    """主板 socket 字段存在复合写法（"AM3+/AM3"、"2 x LGA2011-3"），用子串匹配。"""
    socket = (row.get("socket") or "").strip()
    name = row.get("name", "")
    chipset = extract_mb_chipset(name)
    socket_upper = socket.upper()
    for allowed in ALLOWED_MB_SOCKETS:
        if allowed.upper() in socket_upper:
            return True, f"socket:{allowed}"
    if chipset is not None:
        return True, f"chipset:{chipset}"
    return False, f"mb_out_of_scope:socket={socket!r}"


def scope_ram(row: dict[str, Any]) -> ScopeResult:
    speed = row.get("speed")
    if not (isinstance(speed, list) and len(speed) == 2):
        return False, "bad_speed"
    ddr_ver = speed[0]
    if ddr_ver in ALLOWED_RAM_DDR_VERSIONS:
        return True, f"ddr{ddr_ver}"
    return False, f"ddr_out_of_scope:{ddr_ver}"


def scope_storage(row: dict[str, Any]) -> ScopeResult:
    """存储 scope：int→HDD（docyx quirk，RPM 存 type 字段），str SSD/HDD 进，其余丢。
    HYBRID 从 scope 层丢弃，与 _split_storage 保持契约一致（Codex 审后）。"""
    t = row.get("type")
    if isinstance(t, int):
        return True, f"hdd_rpm:{t}"
    s = (t or "").strip().upper() if isinstance(t, str) else ""
    if s in ("SSD", "HDD"):
        return True, f"type:{s}"
    return False, f"storage_unknown_type:{t!r}"


def scope_always_in(_: dict[str, Any]) -> ScopeResult:
    return True, "always_in"


SCOPE_BY_CATEGORY: dict[str, Callable[[dict[str, Any]], ScopeResult]] = {
    "gpu": scope_gpu,
    "cpu": scope_cpu,
    "mb": scope_mb,
    "ram": scope_ram,
    "storage": scope_storage,
    "psu": scope_always_in,
    "cooler": scope_always_in,
    "case": scope_always_in,
    "nic_wired": scope_always_in,
    "nic_wireless": scope_always_in,
}


# ---------------------------------------------------------------------------
# 芯片字段抽取（用于 SKU 行的 chip 列）
# ---------------------------------------------------------------------------


def chip_for_row(project_category: str, row: dict[str, Any]) -> str:
    """返回该 SKU 所属的 chip / spec 字符串（后续按此聚合 chips.csv）。"""
    name = row.get("name", "")
    if project_category == "gpu":
        return (row.get("chipset") or "unknown").strip()
    if project_category == "cpu":
        return name.strip()
    if project_category == "mb":
        return extract_mb_chipset(name) or (row.get("socket") or "unknown").strip()
    if project_category == "ram":
        speed = row.get("speed") or []
        modules = row.get("modules") or []
        ddr_v = speed[0] if len(speed) == 2 else "?"
        mhz = speed[1] if len(speed) == 2 else "?"
        total = ""
        if len(modules) == 2:
            try:
                total = f"-{int(modules[0]) * int(modules[1])}GB"
            except (TypeError, ValueError):
                total = ""
        return f"DDR{ddr_v}-{mhz}{total}"
    if project_category in ("storage_ssd", "storage_hdd"):
        cap = row.get("capacity")
        iface = (row.get("interface") or "").strip()
        return f"{iface} {cap}GB".strip() if cap else iface or "unknown"
    if project_category == "psu":
        return f"{row.get('wattage', '?')}W {row.get('efficiency', '?')}"
    if project_category == "cooler":
        return (row.get("size") or row.get("rpm") or "air").__str__()
    if project_category == "case":
        return (row.get("type") or "case").strip()
    if project_category.startswith("nic"):
        return (row.get("protocol") or row.get("interface") or "nic").strip()
    return "unknown"


# ---------------------------------------------------------------------------
# 主流水线
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, list):
        raise ValueError(f"{path}: top-level must be list")
    # 校验每条是 dict，非 dict 行跳过避免后续 row.get(...) 崩溃
    out: list[dict[str, Any]] = []
    for i, item in enumerate(data):
        if isinstance(item, dict):
            out.append(item)
        else:
            logger.warning("skip non-dict row #%d in %s (type=%s)", i, path.name, type(item).__name__)
    return out


def _split_storage(rows: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """docyx internal-hard-drive 按 type 分桶。docyx quirk：HDD 行 type 字段是转速 int（7200 等）。"""
    out: dict[str, list[dict[str, Any]]] = {"storage_ssd": [], "storage_hdd": []}
    for r in rows:
        t = r.get("type")
        if isinstance(t, int):
            out["storage_hdd"].append(r)
            continue
        s = t.strip().upper() if isinstance(t, str) else ""
        if s == "SSD":
            out["storage_ssd"].append(r)
        elif s == "HDD":
            out["storage_hdd"].append(r)
        # 其他（HYBRID / 空）丢弃，阶段 11 overrides 可补回
    return out


def _spec_json(row: dict[str, Any], category: str | None = None) -> str:
    """把非 name/price 的原始字段打包成 spec_json 列。price 不入库（走阶段 12 三平台取价）。

    storage_ssd / storage_hdd 额外注入 release_year_est / tier / form_factor
    （来自 drives_release.yaml），因为这两个品类 ChipRow 粒度是 (iface + capacity)
    无法表达型号发布年，必须落在 SKU 级 spec_json。
    """
    keep = {k: v for k, v in row.items() if k not in ("name", "price")}
    if category in ("storage_ssd", "storage_hdd"):
        hit = drives_release_year(row.get("name", "") or "")
        if hit is not None:
            year, tier, form = hit
            keep["release_year_est"] = year
            keep["tier"] = tier
            if form:
                keep["form_factor"] = form
    return json.dumps(keep, ensure_ascii=False, sort_keys=True)


def _write_csv(path: Path, header: list[str], rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)
            n += 1
    tmp.replace(path)
    return n


def _aggregate_chips(project_category: str, sku_rows: list[SkuRow]) -> list[ChipRow]:
    """把 skus 聚合成 chips 层（chips.csv）。"""
    seen: dict[str, ChipRow] = {}
    for s in sku_rows:
        chip = s.chip
        if chip in seen:
            continue
        if project_category == "gpu":
            cl = chip.lower()
            if cl.startswith(("geforce", "nvidia", "rtx ", "gtx ", "titan ", "quadro")):
                vendor = "nvidia"
            elif cl.startswith(("radeon", "amd", "rx ", "r9 ", "r7 ")):
                vendor = "amd"
            elif cl.startswith(("intel", "arc ")):
                vendor = "intel"
            else:
                vendor = "unknown"
            gen = extract_gpu_generation(chip)
        elif project_category == "cpu":
            vendor = "amd" if chip.lower().startswith("amd") else ("intel" if chip.lower().startswith("intel") else "unknown")
            gen = vendor.upper()
        elif project_category == "mb":
            vendor = _mb_chip_vendor(chip)
            gen = chip
        else:
            vendor = "unknown"
            gen = chip
        release_year = release_year_from_spec_json(project_category, chip, s.spec_json)
        seen[chip] = ChipRow(chip=chip, generation=gen, vendor=vendor, release_year_est=release_year, source="docyx")
    return list(seen.values())


_AMD_MB_CHIPSETS = frozenset({
    "A320", "B350", "X370", "B450", "X470",
    "A520", "B550", "X570", "A620", "B650", "B650E", "X670", "X670E",
    "B840", "B850", "X870", "X870E",           # AM5 refresh（Codex 审确认全 AM5）
    "X399", "TRX40", "WRX80", "WRX90", "TRX50",
})
_AMD_MB_SOCKETS = frozenset({
    "AM3+", "FM2+", "AM4", "AM5", "sTR4", "TR4", "sTRX4", "sWRX8", "sTR5", "SP3", "SP5",
})


def _mb_chip_vendor(chip: str) -> str:
    """mb chip/socket 字符串 → vendor slug（amd / intel / unknown）。"""
    if chip in _AMD_MB_CHIPSETS or chip in _AMD_MB_SOCKETS:
        return "amd"
    if chip.startswith("LGA") or chip in MB_CHIPSETS_2015_PLUS:
        return "intel"
    return "unknown"


def _iter_categorized_rows(docyx_name: str, rows: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """返回 [(project_category, row), ...]。storage 会拆成 ssd / hdd。"""
    proj = CATEGORY_MAP.get(docyx_name)
    if proj is None:
        return []
    if proj == "storage":
        out: list[tuple[str, dict[str, Any]]] = []
        for k, v in _split_storage(rows).items():
            out.extend((k, r) for r in v)
        return out
    return [(proj, r) for r in rows]


def run_build(
    week: str,
    out_base: Path | None = None,
    categories: list[str] | None = None,
    vendor_dir: Path | None = None,
) -> BuildSummary:
    """主入口：读 docyx → 过滤 → 输出 CSV。"""
    out_base = out_base or DEFAULT_OUT_DIR
    vendor_dir = vendor_dir or VENDOR_DOCYX_JSON_DIR

    if not vendor_dir.exists():
        raise FileNotFoundError(
            f"vendor/pc-part-dataset 未 clone 到 {vendor_dir}；请先按 docs/vendored.md 操作"
        )

    selected = set(categories) if categories else set(PROJECT_CATEGORIES)
    unknown = selected - set(PROJECT_CATEGORIES)
    if unknown:
        raise ValueError(f"未知品类：{sorted(unknown)}；合法值 {PROJECT_CATEGORIES}")

    bucket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for docyx_name, proj in CATEGORY_MAP.items():
        if proj is None:
            continue
        json_path = vendor_dir / f"{docyx_name}.json"
        if not json_path.exists():
            logger.warning("docyx 缺失品类文件 %s，跳过", json_path.name)
            continue
        rows = _load_json(json_path)
        for cat, row in _iter_categorized_rows(docyx_name, rows):
            if cat in selected:
                bucket[cat].append(row)

    summary = BuildSummary(week=week, out_dir=str(out_base))
    dict_dir = out_base / "dict" / week
    skus_dir = out_base / "skus" / week
    unfiltered_path = out_base / "dict" / week / "_unfiltered.csv"
    unfiltered_rows: list[dict[str, Any]] = []

    for category, rows in bucket.items():
        scope_fn = SCOPE_BY_CATEGORY.get(_scope_key(category), scope_always_in)
        kept: list[SkuRow] = []
        dropped = 0
        brand_buckets: dict[str, list[SkuRow]] = defaultdict(list)
        for row in rows:
            in_scope, reason = scope_fn(row)
            if not in_scope:
                dropped += 1
                unfiltered_rows.append({
                    "category": category,
                    "name": row.get("name", ""),
                    "status": "dropped",
                    "reason": reason,
                })
                continue
            # kept_uncertain：contract 规定保留 + 同步记 unfiltered（阶段 9 md 要求 review）
            if reason.startswith("unknown_"):
                unfiltered_rows.append({
                    "category": category,
                    "name": row.get("name", ""),
                    "status": "kept_uncertain",
                    "reason": reason,
                })
            sku = SkuRow(
                sku_title=(row.get("name") or "").strip(),
                chip=chip_for_row(category, row),
                spec_json=_spec_json(row, category=category),
                source="docyx",
            )
            if not sku.sku_title:
                dropped += 1
                continue
            kept.append(sku)
            brand_buckets[extract_brand(sku.sku_title)].append(sku)

        chips = _aggregate_chips(category, kept)
        chips_csv = dict_dir / f"{category}_chips.csv"
        _write_csv(
            chips_csv,
            header=["chip", "generation", "vendor", "release_year_est", "source"],
            rows=(asdict(c) for c in chips),
        )

        cat_skus_dir = skus_dir / category
        brand_written = 0
        for brand, items in brand_buckets.items():
            path = cat_skus_dir / f"{brand}.csv"
            _write_csv(
                path,
                header=["sku_title", "chip", "spec_json", "source"],
                rows=(asdict(s) for s in items),
            )
            brand_written += 1

        summary.categories[category] = {
            "kept": len(kept),
            "dropped": dropped,
            "chips": len(chips),
            "brands": brand_written,
        }

    if unfiltered_rows:
        _write_csv(
            unfiltered_path,
            header=["category", "name", "status", "reason"],
            rows=unfiltered_rows,
        )
        summary.unfiltered_count = len(unfiltered_rows)

    summary_path = dict_dir / "_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_summary = summary_path.with_suffix(summary_path.suffix + ".tmp")
    with open(tmp_summary, "w", encoding="utf-8") as fp:
        json.dump(asdict(summary), fp, ensure_ascii=False, indent=2)
    tmp_summary.replace(summary_path)

    return summary


def _scope_key(project_category: str) -> str:
    """把 storage_ssd / storage_hdd 映射回 scope_storage。"""
    if project_category.startswith("storage_"):
        return "storage"
    return project_category
