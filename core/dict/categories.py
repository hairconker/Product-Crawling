"""品类映射表 + 2015+ 过滤白名单 + 品牌归一化别名。

所有常量集中在此文件，builder.py 只读不改。新增型号支持时优先改这里。
"""

from __future__ import annotations

import re
from pathlib import Path

# docyx 源品类 → 项目品类 slug。None 表示丢弃（非 DIY 主件或阶段 8 排除范围）
CATEGORY_MAP: dict[str, str | None] = {
    "video-card": "gpu",
    "cpu": "cpu",
    "motherboard": "mb",
    "memory": "ram",
    "internal-hard-drive": "storage",  # 后续按 type 字段分 ssd / hdd
    "power-supply": "psu",
    "cpu-cooler": "cooler",
    "case": "case",
    "wired-network-card": "nic_wired",
    "wireless-network-card": "nic_wireless",
    # 以下显式丢弃（范围外，阶段 8 已锁定）
    "case-accessory": None,
    "case-fan": None,
    "external-hard-drive": None,
    "fan-controller": None,
    "headphones": None,
    "keyboard": None,
    "monitor": None,
    "mouse": None,
    "optical-drive": None,
    "os": None,
    "sound-card": None,
    "speakers": None,
    "thermal-paste": None,
    "ups": None,
    "webcam": None,
}

PROJECT_CATEGORIES: tuple[str, ...] = (
    "gpu",
    "cpu",
    "mb",
    "ram",
    "storage_ssd",
    "storage_hdd",
    "psu",
    "cooler",
    "case",
    "nic_wired",
    "nic_wireless",
)

# ---------------------------------------------------------------------------
# 2015+ 过滤白名单
# ---------------------------------------------------------------------------

# GPU 芯片白名单（正则，case-insensitive）。scope_gpu 对 chipset 字段 re.search。
# 历次扩展：
#   2026-04-20 首版：前缀 startswith 列表
#   2026-04-20 Codex 审后：改 regex，覆盖 RTX A-series / Ada / TITAN RTX / Quadro / Intel Arc / Radeon PRO 大写
ALLOWED_GPU_CHIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    # NVIDIA GeForce 桌面消费级：GTX 900/1000/1600 + TITAN
    re.compile(r"\bgtx\s+(9\d{2}|1[06]\d{2}|titan)", re.IGNORECASE),
    # NVIDIA GeForce RTX 2000-5000
    re.compile(r"\brtx\s+[2-5]\d{3}", re.IGNORECASE),
    # NVIDIA TITAN 系列（Pascal/Volta/Turing 工作卡，2016+）
    re.compile(r"\btitan\s+(rtx|v|x|xp)\b", re.IGNORECASE),
    # NVIDIA RTX A-series 工作站（Ampere，2020+）
    re.compile(r"\brtx\s+a\d{3,4}\b", re.IGNORECASE),
    # NVIDIA RTX Ada Generation（2022+）
    re.compile(r"\brtx\s+\d+\s+ada", re.IGNORECASE),
    # NVIDIA Quadro 2015+（Pascal/Turing/Ampere 工作站）
    re.compile(r"\bquadro\s+(rtx|p[2-9]\d{3}|t\d{3,4}|gp|gv)", re.IGNORECASE),
    # AMD Radeon RX 400/500（3 位） + RX 5000/6000/7000/9000（4 位）
    re.compile(r"\brx\s+(4\d{2}|5\d{2,3}|[679]\d{3})", re.IGNORECASE),
    # AMD Radeon R9 200/300/Fury/Nano（2013-2016，二手市场 2026 仍活跃）
    re.compile(r"\br9\s+(fury|nano|\d{3})", re.IGNORECASE),
    # AMD Radeon R7 200/300 系列
    re.compile(r"\br7\s+[23]\d{2}", re.IGNORECASE),
    # AMD Radeon VII / Fury
    re.compile(r"\bradeon\s+(vii|fury)\b", re.IGNORECASE),
    # AMD Radeon Pro（含大写 PRO，覆盖 Codex 指出的 11 行漏网）
    re.compile(r"\bradeon\s+pro\s+", re.IGNORECASE),
    # Intel Arc A / B / Pro
    re.compile(r"\barc\s+(a\d{3}|b\d{3}|pro)", re.IGNORECASE),
)

# CPU 微架构白名单（2015+）
ALLOWED_CPU_MICROARCH: frozenset[str] = frozenset({
    # Intel（Haswell 2013 底发布，覆盖 2015 市场）
    "Haswell",
    "Haswell-E",
    "Haswell Refresh",    # 2014 发布，i7-4790K 等 2015 主销（Codex 审后补）
    "Broadwell",
    "Broadwell-E",
    "Skylake",
    "Skylake-X",
    "Kaby Lake",
    "Kaby Lake-X",
    "Coffee Lake",
    "Coffee Lake Refresh",
    "Comet Lake",
    "Rocket Lake",
    "Alder Lake",
    "Raptor Lake",
    "Raptor Lake Refresh",
    "Meteor Lake",
    "Arrow Lake",
    "Lunar Lake",
    "Panther Lake",
    "Cascade Lake",
    "Cascade Lake-X",
    "Emerald Rapids",
    "Sapphire Rapids",
    # AMD
    "Piledriver",      # FX 系列 2015 仍在售
    "Steamroller",
    "Excavator",       # A10/A12 APU
    "Zen",
    "Zen+",
    "Zen 2",
    "Zen 3",
    "Zen 3+",
    "Zen 4",
    "Zen 4c",
    "Zen 5",
    "Zen 5c",
})

# 主板芯片组白名单（用 socket + chipset 双路）
ALLOWED_MB_SOCKETS: frozenset[str] = frozenset({
    # Intel
    "LGA1150", "LGA1151", "LGA1151-2", "LGA1200", "LGA1700", "LGA1851",
    "LGA2011-3", "LGA2066", "LGA4677", "LGA3647",
    # AMD
    "AM3+", "FM2+", "AM4", "AM5",
    "sTR4", "TR4", "sTRX4", "sWRX8", "sTR5",
    "SP3", "SP5",
})

# 主板芯片组文本白名单（从 name 提取）
MB_CHIPSETS_2015_PLUS: frozenset[str] = frozenset({
    # Intel Haswell/Broadwell（LGA1150）
    "H81", "B85", "H87", "H97", "Z87", "Z97", "X99",
    # Skylake/Kaby/Coffee（LGA1151）
    "H110", "B150", "B250", "H170", "Q170", "Z170", "Z270",
    "H270", "B360", "B365", "H310", "H370", "Z370", "Z390", "Q370",
    # Comet Lake（LGA1200）
    "W480", "H410", "B460", "B465", "H470", "Q470", "Z490",
    # Rocket Lake
    "H510", "B560", "H570", "Z590",
    # Alder Lake（LGA1700）
    "H610", "B660", "H670", "Z690",
    # Raptor Lake
    "H710", "B760", "H770", "Z790",
    # Arrow Lake（LGA1851；B840/B850 实为 AMD AM5，已移至 builder._AMD_MB_CHIPSETS）
    "H810", "Z890", "W880", "B860",
    # HEDT/workstation
    "X299", "W580", "C246", "C252", "C422", "W680", "W790",
    # AMD AM4
    "A320", "B350", "X370", "B450", "X470",
    "A520", "B550", "X570",
    # AM5
    "A620", "B650", "B650E", "X670", "X670E",
    "B840", "B850", "X870", "X870E",      # AM5 中端 refresh（Codex 审确认全数据 110 行 AM5）
    # Threadripper / Workstation
    "X399", "TRX40", "WRX80", "WRX90", "TRX50",
})

# DDR 版本下界（2015 起 DDR3 仍大规模在售）
ALLOWED_RAM_DDR_VERSIONS: frozenset[int] = frozenset({3, 4, 5})

# ---------------------------------------------------------------------------
# 品牌归一化
# ---------------------------------------------------------------------------

# 从 name 抽取首个 token 后的特殊映射；key 必须小写已 strip 标点
BRAND_ALIASES: dict[str, str] = {
    "g.skill": "gskill",
    "gskill": "gskill",
    "be": "bequiet",            # "be quiet!"
    "team": "teamgroup",
    "teamgroup": "teamgroup",
    "t-force": "teamgroup",
    "pny": "pny",
    "xfx": "xfx",
    "asrock": "asrock",
    "msi": "msi",
    "asus": "asus",
    "gigabyte": "gigabyte",
    "evga": "evga",
    "zotac": "zotac",
    "sapphire": "sapphire",
    "powercolor": "powercolor",
    "colorful": "colorful",
    "galax": "galax",
    "galaxy": "galax",
    "manli": "manli",
    "maxsun": "maxsun",
    "onda": "onda",
    "nvidia": "nvidia",
    "amd": "amd",
    "intel": "intel",
    "samsung": "samsung",
    "crucial": "crucial",
    "corsair": "corsair",
    "kingston": "kingston",
    "patriot": "patriot",
    "mushkin": "mushkin",
    "western": "wd",            # "Western Digital"
    "wd": "wd",
    "seagate": "seagate",
    "toshiba": "toshiba",
    "sandisk": "sandisk",
    "sk": "skhynix",            # "SK Hynix"
    "hynix": "skhynix",
    "hitachi": "hgst",
    "hgst": "hgst",
    "lexar": "lexar",
    "silicon": "siliconpower",  # "Silicon Power"
    "adata": "adata",
    "inland": "inland",
    "teamgroup": "teamgroup",
    "phanteks": "phanteks",
    "fractal": "fractaldesign",
    "cooler": "coolermaster",   # "Cooler Master"
    "deepcool": "deepcool",
    "noctua": "noctua",
    "arctic": "arctic",
    "scythe": "scythe",
    "thermalright": "thermalright",
    "thermaltake": "thermaltake",
    "lian": "lianli",           # "Lian Li"
    "lianli": "lianli",
    "nzxt": "nzxt",
    "seasonic": "seasonic",
    "antec": "antec",
    "fsp": "fsp",
    "super": "superflower",     # "Super Flower"
    "superflower": "superflower",
    "montech": "montech",
    "hyte": "hyte",
    "enermax": "enermax",
    "chieftec": "chieftec",
    "silverstone": "silverstone",
    "tp-link": "tplink",
    "tplink": "tplink",
    "d-link": "dlink",
    "dlink": "dlink",
    "netgear": "netgear",
    "startech": "startech",
    "mellanox": "mellanox",
    "realtek": "realtek",
    "broadcom": "broadcom",
    "ubiquiti": "ubiquiti",
    # 阶段 10 Codex 审后回填：与 spiders/dict_jd_category.py::JD_BRAND_CN_TO_SLUG 保持
    # slug 一致，否则阶段 11 合并时会产生 docyx 侧 brand==xxx 与 jd 侧 brand==yyy 的碎片
    "gainward": "gainward",
    "inno3d": "inno3d",
    "yeston": "yeston",
    "dataland": "dataland",
    "kimtigo": "kimtigo",
    "asgard": "asgard",
    "gloway": "gloway",
    "apacer": "apacer",
    "klevv": "klevv",
    "zhitai": "zhitai",
    "kioxia": "kioxia",
    "aigo": "aigo",
    "great": "greatwall",          # docyx 若出现 "Great Wall ..." 首 token 归到 greatwall
    "greatwall": "greatwall",
    "huntkey": "huntkey",
    "segotep": "segotep",
    "delta": "delta",
    "jonsbo": "jonsbo",
    "jinhetian": "jinhetian",
    "mercury": "mercury",
    "fast": "fast",
    "h3c": "h3c",
    "dahua": "dahua",
    "unisoc": "unisoc",
    "gamdias": "gamdias",
}

# 多词品牌前缀表：name 开头按此表匹配（最长前缀优先，小写对比）
# 解决 Codex 指出的"首 token 过粗暴"问题：In Win → in、PC Cooler → pc 等
MULTI_WORD_BRANDS: tuple[tuple[str, str], ...] = (
    # 4 词
    ("pc power & cooling", "pcpower"),
    # 2-3 词
    ("western digital", "wd"),
    ("silicon power", "siliconpower"),
    ("cooler master", "coolermaster"),
    ("fractal design", "fractaldesign"),
    ("super flower", "superflower"),
    ("team group", "teamgroup"),
    ("pc cooler", "pccooler"),
    ("club 3d", "club3d"),
    ("lian li", "lianli"),
    ("sk hynix", "skhynix"),
    ("be quiet", "bequiet"),    # 覆盖 "be quiet!" 以及无感叹号变体
    ("in win", "inwin"),
    ("t-force", "teamgroup"),
    ("pny technologies", "pny"),
    ("kingston fury", "kingston"),
    ("patriot viper", "patriot"),
)


# ---------------------------------------------------------------------------
# 文件定位
# ---------------------------------------------------------------------------

ROOT: Path = Path(__file__).resolve().parent.parent.parent
VENDOR_DOCYX_JSON_DIR: Path = ROOT / "vendor" / "pc-part-dataset" / "data" / "json"
DEFAULT_OUT_DIR: Path = ROOT / "data"


# ---------------------------------------------------------------------------
# 发布年份映射（阶段 9 ChipRow.release_year_est 填充用）
#
# 数据源（用家族级粒度，不追具体 SKU 发布日）：
#   - 参考：painebenjamin/dbgpu (MIT) https://github.com/painebenjamin/dbgpu
#     dbgpu 每个 SKU 带 release_date；本项目取首发年即可，家族粒度足矣。
#   - 参考：Wikipedia 各代产品首发月份（Zen 系列 / NVIDIA 代号 / AMD 代号）。
#
# 为什么是常量表而不是依赖 dbgpu：
#   1) pc-part-dataset 已提供结构化 microarchitecture (CPU) / chipset (GPU) 字段，
#      我们只需家族→年映射；引入 dbgpu 需多一个 pip 依赖仅换来 35 条查表，不值
#   2) 这张表稳定性极高（历史不会变），变动仅在新代次发布时加一行
#   3) CPU 有 34 种微架构、GPU 十几个家族，全部能覆盖在本文件内
# ---------------------------------------------------------------------------

# CPU 微架构 → 发布年。取桌面首发版本的年份（非服务器 / 非移动端首发）。
# pc-part-dataset 中出现的全部 34 种 microarchitecture 值都已覆盖。
CPU_MICROARCH_RELEASE_YEAR: dict[str, int] = {
    # Intel
    "Core": 2006,
    "Wolfdale": 2008,
    "Yorkfield": 2008,
    "Nehalem": 2008,
    "Westmere": 2010,
    "Sandy Bridge": 2011,
    "Ivy Bridge": 2012,
    "Haswell": 2013,
    "Haswell Refresh": 2014,
    "Broadwell": 2015,
    "Skylake": 2015,
    "Kaby Lake": 2017,
    "Coffee Lake": 2017,
    "Coffee Lake Refresh": 2018,
    "Cascade Lake": 2019,
    "Comet Lake": 2020,
    "Rocket Lake": 2021,
    "Alder Lake": 2021,
    "Raptor Lake": 2022,
    "Raptor Lake Refresh": 2023,
    "Arrow Lake": 2024,
    # AMD
    "K10": 2007,
    "Lynx": 2011,             # APU A-series Llano 分支，用 Llano 对等年
    "Bulldozer": 2011,
    "Piledriver": 2012,
    "Jaguar": 2013,
    "Steamroller": 2014,
    "Puma+": 2014,
    "Excavator": 2015,
    "Zen": 2017,
    "Zen+": 2018,
    "Zen 2": 2019,
    "Zen 3": 2020,
    "Zen 4": 2022,
    "Zen 5": 2024,
}

# GPU 芯片家族 → 发布年。正则按出现顺序匹配，先特例后通配。
# 对 pc-part-dataset 中出现的 chipset 值做了覆盖率检查（见 parse_model 的自测）。
GPU_CHIPSET_FAMILY_PATTERNS: tuple[tuple[re.Pattern[str], int], ...] = (
    # NVIDIA 工作站（特例，放前面避免被后面消费级通配吞掉）
    (re.compile(r"^RTX\s+\d+\s*[A-Z]*\s*Ada", re.IGNORECASE), 2022),  # RTX 4000 Ada / RTX 2000 Ada Generation
    (re.compile(r"^RTX\s+A\d", re.IGNORECASE), 2020),                  # RTX A4000/A5000 等 Ampere 工作站
    (re.compile(r"^TITAN\s+RTX", re.IGNORECASE), 2018),
    (re.compile(r"^Titan\s+V\b", re.IGNORECASE), 2017),
    (re.compile(r"^Titan\s+Xp\b", re.IGNORECASE), 2017),
    (re.compile(r"^Titan\s+X\s*\(Pascal\)", re.IGNORECASE), 2016),
    (re.compile(r"^GeForce\s+GTX\s+Titan\s+X\b", re.IGNORECASE), 2015),
    (re.compile(r"^GeForce\s+GTX\s+Titan\s+Z\b", re.IGNORECASE), 2014),
    (re.compile(r"^GeForce\s+GTX\s+Titan\b", re.IGNORECASE), 2013),   # 含 Titan Black
    # NVIDIA Quadro 世代（粗分到 P/GP/GV）
    (re.compile(r"^Quadro\s+GV\d", re.IGNORECASE), 2017),
    (re.compile(r"^Quadro\s+GP\d", re.IGNORECASE), 2016),
    (re.compile(r"^Quadro\s+P\d", re.IGNORECASE), 2017),
    (re.compile(r"^Quadro\s+M\d", re.IGNORECASE), 2015),
    (re.compile(r"^Quadro\s+K\d", re.IGNORECASE), 2012),
    (re.compile(r"^Quadro\s+RTX", re.IGNORECASE), 2018),
    (re.compile(r"^Quadro\s+\d", re.IGNORECASE), 2010),                # 早期 Quadro 4000/5000/6000 (Fermi)
    (re.compile(r"^NVS\s+", re.IGNORECASE), 2015),
    (re.compile(r"^T\d{3,4}(\s|$)", re.IGNORECASE), 2019),             # T400/T600/T1000 Turing 工作站
    # NVIDIA GeForce 消费级（按世代号前缀）
    (re.compile(r"^GeForce\s+RTX\s+5\d{3}", re.IGNORECASE), 2025),     # Blackwell
    (re.compile(r"^GeForce\s+RTX\s+4\d{3}", re.IGNORECASE), 2022),     # Ada Lovelace
    (re.compile(r"^GeForce\s+RTX\s+3\d{3}", re.IGNORECASE), 2020),     # Ampere
    (re.compile(r"^GeForce\s+RTX\s+2\d{3}", re.IGNORECASE), 2018),     # Turing
    (re.compile(r"^GeForce\s+GTX\s+16\d{2}", re.IGNORECASE), 2019),    # Turing 低端
    (re.compile(r"^GeForce\s+GT\s+10\d{2}", re.IGNORECASE), 2017),     # Pascal GT 1030
    (re.compile(r"^GeForce\s+GTX\s+10\d{2}", re.IGNORECASE), 2016),    # Pascal
    (re.compile(r"^GeForce\s+GTX\s+9\d{2}", re.IGNORECASE), 2014),     # Maxwell
    (re.compile(r"^GeForce\s+GT[XS]?\s+[78]\d{2}", re.IGNORECASE), 2013),  # Kepler refresh
    (re.compile(r"^GeForce\s+GTX\s+6\d{2}", re.IGNORECASE), 2012),     # Kepler
    (re.compile(r"^GeForce\s+GT\s+6\d{2}", re.IGNORECASE), 2012),
    (re.compile(r"^GeForce\s+GT[XS]\s+[45]\d{2}", re.IGNORECASE), 2010),  # Fermi
    (re.compile(r"^GeForce\s+GT\s+[45]\d{2}", re.IGNORECASE), 2011),
    (re.compile(r"^GeForce\s+GT\s+[23]\d{2}", re.IGNORECASE), 2010),
    (re.compile(r"^GeForce\s+GTX\s+2[5-9]\d", re.IGNORECASE), 2009),   # GTX 260/275/285/295 Tesla
    (re.compile(r"^GeForce\s+GTS\s+2\d{2}", re.IGNORECASE), 2009),      # GTS 250
    (re.compile(r"^GeForce\s+210\b", re.IGNORECASE), 2009),
    (re.compile(r"^Quadro\s+NVS", re.IGNORECASE), 2008),
    (re.compile(r"^Quadro\s+FX", re.IGNORECASE), 2008),
    (re.compile(r"^GeForce\s+9", re.IGNORECASE), 2008),
    (re.compile(r"^GeForce\s+8", re.IGNORECASE), 2006),
    (re.compile(r"^GeForce\s+7", re.IGNORECASE), 2005),
    (re.compile(r"^GeForce\s+FX", re.IGNORECASE), 2003),
    # AMD Radeon RX 消费级（按世代号前缀，RDNA4→RDNA1→Polaris）
    (re.compile(r"^Radeon\s+RX\s+9\d{3}", re.IGNORECASE), 2025),        # RDNA4
    (re.compile(r"^Radeon\s+RX\s+7\d{3}", re.IGNORECASE), 2022),        # RDNA3
    (re.compile(r"^Radeon\s+RX\s+6\d{3}", re.IGNORECASE), 2020),        # RDNA2
    (re.compile(r"^Radeon\s+RX\s+5\d{3}", re.IGNORECASE), 2019),        # RDNA1
    (re.compile(r"^Radeon\s+RX\s+VEGA", re.IGNORECASE), 2017),
    (re.compile(r"^Radeon\s+VII\b", re.IGNORECASE), 2019),
    (re.compile(r"^Radeon\s+RX\s+5[5-9]0", re.IGNORECASE), 2017),       # RX 500 系列 Polaris refresh
    (re.compile(r"^Radeon\s+RX\s+5[56]0", re.IGNORECASE), 2017),
    (re.compile(r"^Radeon\s+RX\s+4\d{2}", re.IGNORECASE), 2016),        # RX 400 系列 Polaris
    # AMD Radeon R 系列与 HD 系列
    (re.compile(r"^Radeon\s+R9\s+Fury", re.IGNORECASE), 2015),
    (re.compile(r"^Radeon\s+R9\s+Nano", re.IGNORECASE), 2015),
    (re.compile(r"^Radeon\s+R9\s+3\d{2}", re.IGNORECASE), 2015),        # R9 300 series
    (re.compile(r"^Radeon\s+R9\s+2\d{2}", re.IGNORECASE), 2013),
    (re.compile(r"^Radeon\s+R7\s+3\d{2}", re.IGNORECASE), 2015),
    (re.compile(r"^Radeon\s+R7\s+2\d{2}", re.IGNORECASE), 2013),
    (re.compile(r"^Radeon\s+R5\s+2\d{2}", re.IGNORECASE), 2013),
    (re.compile(r"^Radeon\s+HD\s+7\d{3}", re.IGNORECASE), 2012),
    (re.compile(r"^Radeon\s+HD\s+6\d{3}", re.IGNORECASE), 2010),
    (re.compile(r"^Radeon\s+HD\s+5\d{3}", re.IGNORECASE), 2009),
    (re.compile(r"^Radeon\s+HD\s+4\d{3}", re.IGNORECASE), 2008),
    (re.compile(r"^Radeon\s+HD\s+3\d{3}", re.IGNORECASE), 2007),
    (re.compile(r"^Radeon\s+9\d{3}", re.IGNORECASE), 2003),             # pre-HD 9550 等
    # AMD 工作站
    (re.compile(r"^Radeon\s+PRO\s+W7", re.IGNORECASE), 2023),
    (re.compile(r"^Radeon\s+PRO\s+W6", re.IGNORECASE), 2021),
    (re.compile(r"^Radeon\s+PRO\s+W5500", re.IGNORECASE), 2019),
    (re.compile(r"^Radeon\s+Pro\s+W57", re.IGNORECASE), 2019),
    (re.compile(r"^Radeon\s+Pro\s+Duo", re.IGNORECASE), 2016),
    (re.compile(r"^Radeon\s+Pro\s+VII", re.IGNORECASE), 2020),
    (re.compile(r"^Radeon\s+Pro\s+WX", re.IGNORECASE), 2016),
    (re.compile(r"^FirePro\s+", re.IGNORECASE), 2012),                  # 全部归到 2012（老专业卡代次混杂，保守值）
    (re.compile(r"^Vega\s+Frontier", re.IGNORECASE), 2017),
    (re.compile(r"^FireGL\s+", re.IGNORECASE), 2006),
    # Intel Arc
    (re.compile(r"^Arc\s+B\d{3}", re.IGNORECASE), 2024),                # Battlemage
    (re.compile(r"^Arc\s+A\d{3}", re.IGNORECASE), 2022),                # Alchemist
    (re.compile(r"^Arc\s+Pro\s+A\d", re.IGNORECASE), 2023),
)

# DDR 代次 → 桌面平台首发年。run_build 里 RAM 的 chip 形如 "DDR5-6000-32GB"。
RAM_DDR_RELEASE_YEAR: dict[str, int] = {
    "DDR5": 2021,
    "DDR4": 2014,
    "DDR3": 2007,
    "DDR2": 2003,
    "DDR":  2000,
}
