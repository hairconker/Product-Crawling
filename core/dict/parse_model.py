"""型号 → 发布年份解析（阶段 9 ChipRow.release_year_est 填充用）。

设计思路
--------
pc-part-dataset 已经给出了结构化字段：
  - CPU 行有 ``microarchitecture``（"Zen 4" / "Raptor Lake Refresh" ...）
  - GPU 行有 ``chipset``（"GeForce RTX 4070" / "Radeon RX 9070 XT" ...）
  - RAM 行我们在 builder.chip_for_row 合成了 ``DDR{v}-{mhz}-{total}GB``

因此本模块**不**解析原始 SKU 名称（"Intel Core i5-12400F" → 12 代）——那一层工作
已经由数据源做完；我们只做"家族 → 年份"的查表，粒度到桌面首发年即可。

数据来源
--------
- painebenjamin/dbgpu (MIT) —— TechPowerUp 数据镜像，含 release_date
  https://github.com/painebenjamin/dbgpu
  本项目只要家族年份，35 条映射直接内联到 categories.py，不引入依赖。
- NVIDIA / AMD / Intel 官方产品页 + Wikipedia 代号表做二次校对。

为什么不用 cpumodel / dbgpu / gpu-info-api
------------------------------------------
- djjudas21/cpumodel：无 license（skill 铁律禁用），且设计目标是解析 /proc/cpuinfo，
  不返回发布年
- painebenjamin/dbgpu：粒度对不上（我们只要"Zen 4 → 2022"，不要 "7800X3D → 2023-04"）
- voidful/gpu-info-api：无 license
- archspec/archspec-json：只分类微架构，无年份字段
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

import yaml

from core.dict.categories import (
    CPU_MICROARCH_RELEASE_YEAR,
    GPU_CHIPSET_FAMILY_PATTERNS,
    RAM_DDR_RELEASE_YEAR,
)

logger = logging.getLogger(__name__)

_DRIVES_YAML = Path(__file__).parent / "drives_release.yaml"
_TIER_YAML = Path(__file__).parent / "tier_whitelist.yaml"


@lru_cache(maxsize=1)
def _load_drives_table() -> list[tuple[str, int, str, str]]:
    """读 drives_release.yaml，平铺成 [(match_lower, year, tier, form), ...]，按 match 长度降序。

    长度降序是为了让"990 Evo Plus"在"990 Evo"之前被匹配；同品牌顺序在 yaml 内维护。
    跨品牌的跨污染（如 WD HDD 的 "WD Blue" vs Samsung 850 Evo）由具体子串差异隔开——
    "WD Blue" 不会匹配 "Samsung 850 Evo"，反之亦然。
    """
    if not _DRIVES_YAML.exists():
        logger.warning("drives_release.yaml 不存在：%s", _DRIVES_YAML)
        return []
    try:
        data = yaml.safe_load(_DRIVES_YAML.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        logger.error("drives_release.yaml 解析失败：%s", e)
        return []
    rows: list[tuple[str, int, str, str]] = []
    for brand, items in data.items():
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            m = it.get("match")
            y = it.get("release_year")
            t = it.get("tier") or "unknown"
            f = it.get("form") or ""
            if not m or not isinstance(y, int):
                continue
            rows.append((str(m).lower(), y, str(t), str(f)))
    # 长度降序：更具体优先命中
    rows.sort(key=lambda r: -len(r[0]))
    return rows


@lru_cache(maxsize=1)
def _load_tier_whitelist() -> dict[str, list[dict[str, object]]]:
    """读 tier_whitelist.yaml → {category: [entry, ...]}。entry 含 model/tier/release_year/brand。

    三级结构（category → brand → items）被摊平到二级（category → items with brand），
    便于下游"给我所有 fan 白名单"或"给我 psu 的 high 档"之类的按品类扫描。
    """
    if not _TIER_YAML.exists():
        logger.warning("tier_whitelist.yaml 不存在：%s", _TIER_YAML)
        return {}
    try:
        data = yaml.safe_load(_TIER_YAML.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        logger.error("tier_whitelist.yaml 解析失败：%s", e)
        return {}
    out: dict[str, list[dict[str, object]]] = {}
    for category, brands in data.items():
        if not isinstance(brands, dict):
            continue
        flat: list[dict[str, object]] = []
        for brand, items in brands.items():
            if not isinstance(items, list):
                continue
            for it in items:
                if not isinstance(it, dict):
                    continue
                entry = dict(it)
                entry["brand"] = brand
                entry["category"] = category
                flat.append(entry)
        out[category] = flat
    return out


def tier_whitelist_models(
    category: str, tiers: tuple[str, ...] | None = None,
) -> list[dict[str, object]]:
    """返回某品类的白名单型号列表（可选按档位过滤）。

    Parameters
    ----------
    category : str
        "fan" / "case" / "psu"
    tiers : tuple[str, ...] | None
        None 全部；否则只返回匹配档位的（"high" / "mid" / "entry"）。
        电源白名单原生不含 entry，传 ("entry",) 会返回空列表。

    Returns
    -------
    list[dict]
        每条含 model / tier / release_year / brand / category + 该品类特有字段（size/form/cert 等）
    """
    rows = _load_tier_whitelist().get(category, [])
    if tiers is None:
        return list(rows)
    allowed = set(tiers)
    return [r for r in rows if r.get("tier") in allowed]


def drives_release_year(sku_name: str) -> tuple[int, str, str] | None:
    """硬盘 SKU 名称 → (release_year, tier, form_factor)。未命中返回 None。

    匹配策略：case-insensitive 子串匹配，同长度多候选按 yaml 顺序取第一个。
    """
    if not sku_name:
        return None
    s = sku_name.lower()
    for match, year, tier, form in _load_drives_table():
        if match in s:
            return (year, tier, form)
    return None


def release_year_from_spec(category: str, chip: str, spec: dict[str, object]) -> int | None:
    """返回 chip 的估计发布年。

    Parameters
    ----------
    category : str
        项目 slug（"cpu" / "gpu" / "ram" / "storage_ssd" / ...）
    chip : str
        该品类下 chip_for_row() 的产物（builder.py:219）。CPU 是 SKU 全名、
        GPU 是 chipset 家族名、RAM 是 "DDR{v}-{mhz}-{total}GB" 格式。
    spec : dict
        原始 row 去掉 name/price 后的 dict（即 spec_json 反序列化结果）。
        CPU 路径需要 spec["microarchitecture"]；其他路径不依赖。

    Returns
    -------
    int | None
        估计发布年；查不到返回 None（builder 写 CSV 时落空字段）。
    """
    if category == "cpu":
        microarch = spec.get("microarchitecture")
        if isinstance(microarch, str):
            return CPU_MICROARCH_RELEASE_YEAR.get(microarch.strip())
        return None

    if category == "gpu":
        # chip 直接就是 chipset（"GeForce RTX 4070" 等），按族正则首匹配命中
        for pattern, year in GPU_CHIPSET_FAMILY_PATTERNS:
            if pattern.search(chip):
                return year
        return None

    if category == "ram":
        # chip 形如 "DDR5-6000-32GB" / "DDR4-3200"，取开头 DDR 版本段
        head = chip.split("-", 1)[0].strip().upper()
        return RAM_DDR_RELEASE_YEAR.get(head)

    # storage_ssd / storage_hdd / mb / psu / cooler / case / nic_*：
    # 字典字段不含 release_year，靠后续阶段的静态 YAML 或官网爬覆盖
    return None


def release_year_from_spec_json(category: str, chip: str, spec_json: str) -> int | None:
    """便利包装：接受序列化的 spec_json 字符串，替调用方做 json.loads。

    builder.py 里 SkuRow 只持有 spec_json 字符串，用这个入口省一次手工 parse。
    spec_json 解析失败返回 None（builder 记日志不崩溃）。
    """
    try:
        spec = json.loads(spec_json) if spec_json else {}
    except (ValueError, TypeError) as e:
        logger.warning("spec_json 解析失败 category=%s chip=%s err=%s", category, chip, e)
        return None
    if not isinstance(spec, dict):
        return None
    return release_year_from_spec(category, chip, spec)
