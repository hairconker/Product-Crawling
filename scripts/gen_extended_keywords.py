"""为 5 大类(GPU/CPU/RAM/MB/PSU)生成"品牌×型号/规格"扩展关键词文件。

设计原则:
- 只覆盖市场主流型号/规格,不爆炸式 cross product
- 同账号一次跑不要超过 ~500 总关键词(节流 + 风控)
- 输出文件直接给 run_cpu_crawl_pw.py --keywords-file 用

用法:
    python scripts/gen_extended_keywords.py
    输出:
        keywords_gpu_ext.txt
        keywords_cpu_ext.txt
        keywords_ram_ext.txt
        keywords_mb_ext.txt
        keywords_psu_ext.txt
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ─── GPU:NVIDIA / AMD 主流型号 × 各家品牌 ───────────────────────

NV_BRANDS: list[str] = [
    "影驰", "华硕", "技嘉", "微星", "七彩虹", "索泰", "耕升", "铭瑄", "映众",
]
AMD_BRANDS: list[str] = ["蓝宝石", "迪兰恒进", "华擎"]

# NVIDIA 主流型号(50 系 + 40 系 + 30 系仍流通)
NV_MODELS: list[str] = [
    # RTX 50 系
    "RTX 5090", "RTX 5080", "RTX 5070 Ti", "RTX 5070", "RTX 5060 Ti", "RTX 5060",
    # RTX 40 系
    "RTX 4090", "RTX 4080 Super", "RTX 4080", "RTX 4070 Ti Super", "RTX 4070 Super",
    "RTX 4070", "RTX 4060 Ti", "RTX 4060",
    # RTX 30 系(还有二手流通)
    "RTX 3060",
]
# AMD 主流型号
AMD_MODELS: list[str] = [
    "RX 9070 XT", "RX 9070",
    "RX 7900 XTX", "RX 7900 XT", "RX 7800 XT", "RX 7700 XT", "RX 7600",
]


def gen_gpu() -> list[str]:
    out: list[str] = []
    for b in NV_BRANDS:
        for m in NV_MODELS:
            out.append(f"{b} {m}")
    for b in AMD_BRANDS:
        for m in AMD_MODELS:
            out.append(f"{b} {m}")
    return out


# ─── CPU:Intel / AMD 主流型号(近 3 代 + X3D 系列) ───────────────

CPU_MODELS: list[str] = [
    # Intel 12 代 Alder Lake
    "i3-12100F", "i5-12400F", "i5-12600KF", "i7-12700KF", "i9-12900K",
    # Intel 13 代 Raptor Lake
    "i3-13100F", "i5-13400F", "i5-13490F", "i5-13600KF", "i7-13700KF", "i9-13900K",
    # Intel 14 代 Raptor Lake Refresh
    "i3-14100F", "i5-14400F", "i5-14600KF", "i7-14700KF", "i9-14900K",
    # Intel Core Ultra 200 系列 (Arrow Lake)
    "Ultra 5 245K", "Ultra 7 265K", "Ultra 9 285K",
    # AMD Ryzen 5000 系列
    "R5 5600", "R5 5600X", "R5 5700X", "R7 5700X3D", "R7 5800X3D", "R9 5900X",
    # AMD Ryzen 7000 系列
    "R5 7500F", "R5 7600", "R5 7600X", "R7 7700", "R7 7700X", "R7 7800X3D", "R9 7950X",
    # AMD Ryzen 8000 APU
    "R5 8400F", "R5 8500G", "R7 8700F", "R7 8700G",
    # AMD Ryzen 9000 系列
    "R5 9600X", "R7 9700X", "R7 9800X3D", "R9 9900X", "R9 9950X",
]


def gen_cpu() -> list[str]:
    # CPU 不需要品牌前缀,闲鱼上 i5-13400F 这种直接搜
    return list(CPU_MODELS)


# ─── RAM:主流品牌 × 主流规格 ─────────────────────────────────

RAM_BRANDS: list[str] = [
    "金士顿", "芝奇", "威刚", "海盗船", "光威",
    "影驰", "宇瞻", "十铨", "英睿达", "致态", "玖合",
]
RAM_SPECS: list[str] = [
    "DDR4 3200 16GB",
    "DDR4 3200 32GB",
    "DDR4 3600 16GB",
    "DDR5 6000 32GB",
    "DDR5 6400 32GB",
]


def gen_ram() -> list[str]:
    return [f"{b} {s}" for b in RAM_BRANDS for s in RAM_SPECS]


# ─── MB:主流品牌 × 主流芯片组 ───────────────────────────────

MB_BRANDS: list[str] = [
    "华硕", "微星", "技嘉", "华擎", "七彩虹",
    "铭瑄", "映泰", "昂达", "精粤",
]
MB_CHIPSETS: list[str] = [
    # Intel
    "H610M", "B660M", "B760M", "Z690", "Z790",
    # AMD
    "A620M", "B550M", "B650M", "X670", "X870",
]


def gen_mb() -> list[str]:
    return [f"{b} {c}" for b in MB_BRANDS for c in MB_CHIPSETS]


# ─── PSU:主流品牌 × 主流功率档 ──────────────────────────────

PSU_BRANDS: list[str] = [
    "海韵", "振华", "酷冷至尊", "海盗船", "航嘉",
    "长城", "安钛克", "鑫谷", "先马", "爱国者", "九州风神", "银欣",
]
PSU_WATTS: list[str] = ["550W", "650W", "750W", "850W", "1000W", "1200W"]


def gen_psu() -> list[str]:
    return [f"{b} {w}" for b in PSU_BRANDS for w in PSU_WATTS]


# ─── 输出 ────────────────────────────────────────────────────


def write_kws(name: str, kws: list[str]) -> None:
    path = ROOT / f"keywords_{name}_ext.txt"
    path.write_text("\n".join(kws) + "\n", encoding="utf-8")
    print(f"{path.name}: {len(kws)} 个关键词")


def main() -> int:
    write_kws("gpu", gen_gpu())
    write_kws("cpu", gen_cpu())
    write_kws("ram", gen_ram())
    write_kws("mb", gen_mb())
    write_kws("psu", gen_psu())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
