"""阶段 11 字典合并器。

三源合并：**docyx（阶段 9）+ jd_category（阶段 10）+ overrides（用户手填）**。
优先级：override > jd_category > docyx。合并产物是阶段 12 价格采集器的唯一搜索词来源。

输入：
  data/dict/{week}/{category}_chips.csv              # phase 9 芯片字典
  data/skus/{week}/{category}/{brand}.csv            # phase 9 docyx SKU
  data/skus/{week}/{category}/{brand}_jd.csv         # phase 10 JD 分类页 SKU
  data/dict/overrides/chips/{category}_chips.csv     # 用户手填，跨周常驻
  data/dict/overrides/skus/{category}/{brand}.csv    # 用户手填，跨周常驻

输出：
  data/dict/{week}/_merged/{category}_chips.csv      # 合并后芯片
  data/skus/{week}/_merged/{category}/{brand}.csv    # 合并后 SKU（带 alt_titles）
  logs/dict_merge_conflicts_{week}.csv               # 冲突记录
  logs/dict_merge_summary_{week}.json                # 统计摘要

sku_key 归一化：小写 + 去 whitespace/hyphen/underscore/dot/slash。仅用于**去重匹配**，
不改写 sku_title（阶段 12 搜索词要保留原样，含 alt_titles 双路搜索）。
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.dict.categories import DEFAULT_OUT_DIR, ROOT

logger = logging.getLogger(__name__)

_SKU_KEY_STRIP = re.compile(r"[\s\-_./\\]+")
_SOURCE_PRIORITY: dict[str, int] = {"override": 3, "jd_category": 2, "docyx": 1}

_LOGS_DIR = ROOT / "logs"
_OVERRIDES_ROOT = DEFAULT_OUT_DIR / "dict" / "overrides"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class MergedSkuRow:
    sku_title: str       # 按优先级挑出的 canonical title
    chip: str
    spec_json: str
    source: str          # '+' 连接的来源集合，如 'docyx+jd_category'
    origin_count: int    # 出现在多少个源
    alt_titles: str      # json list，其他源的不同 title 变体


@dataclass
class MergedChipRow:
    chip: str
    generation: str
    vendor: str
    release_year_est: str
    source: str


@dataclass
class MergeSummary:
    week: str
    out_dir: str
    categories: dict[str, dict[str, int]] = field(default_factory=dict)
    total_skus: int = 0
    total_chips: int = 0
    conflicts: int = 0


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def sku_key(title: str, chip: str = "") -> str:
    """sku 去重归一化键：title_norm + '|' + chip_norm。

    为什么不光用 title：docyx 同一 title 可能对应不同 chip 的 SKU（典型：Corsair
    Vengeance LPX 16GB 既有 DDR4-3200 也有 DDR5-6000 同名 SKU）。复合键保留差异，
    避免这些合法变体被错误合并成一条。

    使用 `casefold()` 而非 `.lower()` 以正确处理 Unicode 拉丁/希腊/西里尔等变体
    （如德语 ß → ss；中文、日文 casefold == lower，不受影响）。
    """
    t = _SKU_KEY_STRIP.sub("", (title or "").casefold())
    c = _SKU_KEY_STRIP.sub("", (chip or "").casefold())
    return f"{t}|{c}"


def _read_csv_safe(path: Path) -> list[dict[str, str]]:
    """读 CSV；文件不存在返回空列表。

    兼容 overrides 格式约定：跳过开头以 `#` 起始的注释行（阶段 11 md 约定的
    `# manual overrides, priority highest`）。utf-8-sig 自动吞 BOM。
    """
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8-sig", newline="") as fp:
        lines = fp.readlines()
    # 跳过开头连续的 # 注释行
    start = 0
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#"):
            start = i + 1
        else:
            break
    reader = csv.DictReader(lines[start:])
    return list(reader)


def _write_csv_atomic(
    path: Path,
    header: list[str],
    rows: list[dict[str, Any]],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    tmp.replace(path)


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# 合并：chips 层
# ---------------------------------------------------------------------------


def _merge_chips_one_category(
    category: str,
    week_dict_dir: Path,
    overrides_root: Path,
    conflicts: list[dict[str, str]],
) -> list[MergedChipRow]:
    base = _read_csv_safe(week_dict_dir / f"{category}_chips.csv")
    override = _read_csv_safe(overrides_root / "chips" / f"{category}_chips.csv")

    merged: dict[str, dict[str, str]] = {}
    for r in base:
        r = {**r, "source": "docyx"}
        merged[r.get("chip", "")] = r
    for r in override:
        key = r.get("chip", "")
        if not key:
            continue
        prev = merged.get(key)
        r = {**r, "source": "override"}
        if prev and any(prev.get(k) != r.get(k) for k in ("generation", "vendor", "release_year_est")):
            conflicts.append({
                "type": "chip",
                "category": category,
                "brand": "",
                "key": key,
                "source_a": prev.get("source", "docyx"),
                "payload_a": json.dumps(prev, ensure_ascii=False),
                "source_b": "override",
                "payload_b": json.dumps(r, ensure_ascii=False),
            })
        merged[key] = r

    out: list[MergedChipRow] = []
    for row in merged.values():
        out.append(MergedChipRow(
            chip=row.get("chip", ""),
            generation=row.get("generation", ""),
            vendor=row.get("vendor", ""),
            release_year_est=row.get("release_year_est", ""),
            source=row.get("source", "docyx"),
        ))
    return out


# ---------------------------------------------------------------------------
# 合并：skus 层
# ---------------------------------------------------------------------------


def _collect_brand_sources(
    category: str,
    week_skus_dir: Path,
    overrides_root: Path,
) -> dict[str, list[tuple[str, list[dict[str, str]]]]]:
    """扫目录，返回 {brand: [(source, rows), ...]}。

    override-only brand（docyx/jd 无数据，仅 override 提供）正确被识别。
    """
    out: dict[str, list[tuple[str, list[dict[str, str]]]]] = defaultdict(list)
    cat_dir = week_skus_dir / category
    if cat_dir.exists():
        for file in sorted(cat_dir.glob("*.csv")):
            stem = file.stem
            if stem.startswith("_"):
                continue  # 跳过 _merged 等元目录产物（理论不会在 category/ 下，防御）
            if stem.endswith("_jd"):
                brand = stem[:-3]
                out[brand].append(("jd_category", _read_csv_safe(file)))
            else:
                out[stem].append(("docyx", _read_csv_safe(file)))
    override_dir = overrides_root / "skus" / category
    if override_dir.exists():
        for file in sorted(override_dir.glob("*.csv")):
            out[file.stem].append(("override", _read_csv_safe(file)))
    return out


def _inherit_missing_chip(
    sources: list[tuple[str, list[dict[str, str]]]],
) -> None:
    """原地修正：override 行若 chip 为空，从非-override 源同 title 推断。

    避免 Codex 审 Critical #3：空 chip 会让 sku_key 变成 'title|'，产生孤立桶，
    实质是数据破坏。推断失败时保留空值并 WARN（不中断）。
    """
    lookup: dict[str, str] = {}
    for src, rows in sources:
        if src == "override":
            continue
        for r in rows:
            title_norm = _SKU_KEY_STRIP.sub("", (r.get("sku_title") or "").casefold())
            chip = (r.get("chip") or "").strip()
            if title_norm and chip and title_norm not in lookup:
                lookup[title_norm] = chip
    for src, rows in sources:
        if src != "override":
            continue
        for r in rows:
            if (r.get("chip") or "").strip():
                continue
            title_norm = _SKU_KEY_STRIP.sub("", (r.get("sku_title") or "").casefold())
            inherited = lookup.get(title_norm)
            if inherited:
                r["chip"] = inherited
                logger.info(
                    "override 继承 chip: title=%r chip=%s",
                    r.get("sku_title"), inherited,
                )
            else:
                logger.warning(
                    "override 无 chip 且无匹配源: title=%r（将产生独立桶）",
                    r.get("sku_title"),
                )


def _merge_brand(
    category: str,
    brand: str,
    sources: list[tuple[str, list[dict[str, str]]]],
    conflicts: list[dict[str, str]],
) -> list[MergedSkuRow]:
    """对单品牌合并。按 (title + chip) 复合 sku_key 分桶。

    只有真正的 override 与非-override 源不一致时才报 conflict；docyx/jd 间的
    title/chip 语法差异不算冲突（它们是正常的 alt_titles 合并候选）。
    """
    _inherit_missing_chip(sources)
    buckets: dict[str, list[tuple[str, dict[str, str]]]] = defaultdict(list)
    for src, rows in sources:
        for r in rows:
            title = (r.get("sku_title") or "").strip()
            if not title:
                continue
            k = sku_key(title, r.get("chip", ""))
            buckets[k].append((src, r))

    out: list[MergedSkuRow] = []
    for key, items in buckets.items():
        items.sort(key=lambda x: -_SOURCE_PRIORITY.get(x[0], 0))
        canon_src, canon_row = items[0]
        seen_titles: set[str] = {canon_row.get("sku_title") or ""}
        alt_titles: list[str] = []
        sources_set: set[str] = set()
        canon_chip = (canon_row.get("chip") or "").strip()
        canon_spec = canon_row.get("spec_json") or ""

        for src, row in items:
            sources_set.add(src)
            title = (row.get("sku_title") or "").strip()
            if title and title not in seen_titles:
                alt_titles.append(title)
                seen_titles.add(title)
            # 只在"override 与其它源" spec_json 显著差异时报 conflict
            if canon_src != src and "override" in (canon_src, src):
                other_spec = row.get("spec_json") or ""
                if canon_spec and other_spec and canon_spec != other_spec:
                    conflicts.append({
                        "type": "sku",
                        "category": category,
                        "brand": brand,
                        "key": key,
                        "source_a": canon_src,
                        "payload_a": json.dumps(canon_row, ensure_ascii=False),
                        "source_b": src,
                        "payload_b": json.dumps(row, ensure_ascii=False),
                    })

        out.append(MergedSkuRow(
            sku_title=canon_row.get("sku_title") or "",
            chip=canon_chip,
            spec_json=canon_spec,
            source="+".join(sorted(sources_set)),
            origin_count=len(sources_set),    # 去重后源数（几种源出现过），非原始行数
            alt_titles=json.dumps(alt_titles, ensure_ascii=False) if alt_titles else "[]",
        ))

    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


_SKU_HEADER = ["sku_title", "chip", "spec_json", "source", "origin_count", "alt_titles"]
_CHIP_HEADER = ["chip", "generation", "vendor", "release_year_est", "source"]
_CONFLICT_HEADER = [
    "type", "category", "brand", "key",
    "source_a", "payload_a",
    "source_b", "payload_b",
]


def run_merge(
    week: str,
    out_base: Path | None = None,
    *,
    dry_run: bool = False,
    overrides_root: Path | None = None,
) -> MergeSummary:
    out_base = out_base or DEFAULT_OUT_DIR
    overrides_root = overrides_root or _OVERRIDES_ROOT
    week_dict_dir = out_base / "dict" / week
    week_skus_dir = out_base / "skus" / week
    merged_dict_dir = week_dict_dir / "_merged"
    merged_skus_dir = week_skus_dir / "_merged"

    if not week_dict_dir.exists():
        raise FileNotFoundError(
            f"week 字典目录不存在 {week_dict_dir}；请先跑 `python scripts/dict_build.py --week {week}`"
        )

    summary = MergeSummary(week=week, out_dir=str(out_base))
    conflicts: list[dict[str, str]] = []

    # --- 品类发现：week 字典/SKU + overrides，UNION（Codex 审 C2 修复）---
    chip_categories: set[str] = set()
    sku_categories: set[str] = set()
    for f in week_dict_dir.glob("*_chips.csv"):
        # 防御性：glob 理论上不递归，但如果未来加 `**`，这里显式过滤 _merged/
        if "_merged" in f.parts:
            continue
        chip_categories.add(f.stem.removesuffix("_chips"))
    if week_skus_dir.exists():
        for d in week_skus_dir.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                sku_categories.add(d.name)
    # overrides 侧
    override_chips_dir = overrides_root / "chips"
    if override_chips_dir.exists():
        for f in override_chips_dir.glob("*_chips.csv"):
            chip_categories.add(f.stem.removesuffix("_chips"))
    override_skus_dir = overrides_root / "skus"
    if override_skus_dir.exists():
        for d in override_skus_dir.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                sku_categories.add(d.name)

    # --- chips ---
    for cat in sorted(chip_categories):
        rows = _merge_chips_one_category(cat, week_dict_dir, overrides_root, conflicts)
        if not dry_run and rows:
            _write_csv_atomic(
                merged_dict_dir / f"{cat}_chips.csv",
                header=_CHIP_HEADER,
                rows=[asdict(r) for r in rows],
            )
        stat = summary.categories.setdefault(cat, {})
        stat["chips"] = len(rows)
        summary.total_chips += len(rows)

    # --- skus ---
    for cat in sorted(sku_categories):
        brand_srcs = _collect_brand_sources(cat, week_skus_dir, overrides_root)
        cat_sku_total = 0
        cat_brand_total = 0
        for brand, srcs in sorted(brand_srcs.items()):
            merged_rows = _merge_brand(cat, brand, srcs, conflicts)
            if not merged_rows:
                continue
            if not dry_run:
                _write_csv_atomic(
                    merged_skus_dir / cat / f"{brand}.csv",
                    header=_SKU_HEADER,
                    rows=[asdict(r) for r in merged_rows],
                )
            cat_sku_total += len(merged_rows)
            cat_brand_total += 1
        stat = summary.categories.setdefault(cat, {})
        stat["brands_merged"] = cat_brand_total
        stat["skus_merged"] = cat_sku_total
        summary.total_skus += cat_sku_total

    summary.conflicts = len(conflicts)

    if not dry_run:
        if conflicts:
            _write_csv_atomic(
                _LOGS_DIR / f"dict_merge_conflicts_{week}.csv",
                header=_CONFLICT_HEADER,
                rows=conflicts,
            )
        _write_json_atomic(
            _LOGS_DIR / f"dict_merge_summary_{week}.json",
            asdict(summary),
        )

    return summary
