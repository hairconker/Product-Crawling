"""型号信息填充：已知型号直接匹配，未知型号标记待搜索。

先用内置的 CPU 发布时间数据库填充 model_info 表。
剩余未匹配的型号输出到 unknown_models.txt，后续可通过 WebSearch 补齐。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime

DB_PATH = "data/prices.db"

# ═══════════════════════════════════════════════════════════════
# CPU 发布年份数据库（按系列 + 代数规律批量生成 + 手动修正）
# ═══════════════════════════════════════════════════════════════

# --- Intel 系列发布年表 ---
# Core 2nd gen (Sandy Bridge): 2011
# Core 3rd gen (Ivy Bridge): 2012
# Core 4th gen (Haswell): 2013, refresh (Devil's Canyon) 2014
# Core 5th gen (Broadwell): 2015
# Core 6th gen (Skylake): 2015
# Core 7th gen (Kaby Lake): 2017
# Core 8th gen (Coffee Lake): 2017Q4-2018
# Core 9th gen (Coffee Lake Refresh): 2018Q4-2019
# Core 10th gen (Comet Lake): 2020
# Core 11th gen (Rocket Lake): 2021Q1
# Core 12th gen (Alder Lake): 2021Q4-2022
# Core 13th gen (Raptor Lake): 2022Q4-2023
# Core 14th gen (Raptor Lake Refresh): 2023Q4-2024
# Core Ultra 200 (Arrow Lake): 2024Q4

# Pentium/Celeron: follow mainstream gen release year
# Xeon E3 v3=2013, v4=2014, v5=2015, v6=2017
# Xeon E5 v3=2014, v4=2016
# Xeon E-21xx = 2018 (Coffee Lake), E-22xx = 2019 (Coffee Lake Refresh)

# --- AMD 系列发布年表 ---
# AM3+ FX: FX-4xxx/6xxx/8xxx/9xxx = 2011-2013
# FM2 A-series: A4/A6/A8/A10 = 2012-2014
# FM2+ A-series: A4/A6/A8/A10 (78xx/88xx) = 2014-2015
# AM4 Ryzen 1000 = 2017
# AM4 Ryzen 2000 = 2018
# AM4 Ryzen 3000 = 2019
# AM4 Ryzen 4000 (APU) = 2020
# AM4 Ryzen 5000 = 2020Q4-2022
# AM5 Ryzen 7000 = 2022Q4
# AM5 Ryzen 8000 (APU) = 2024
# AM5 Ryzen 9000 = 2024
# Threadripper 3000 = 2019
# Athlon 200GE/220GE/240GE/3000G = 2018-2019
# EPYC 4004 = 2024


def build_intel_map() -> dict[str, dict]:
    """构建 Intel CPU 信息映射。"""
    m: dict[str, dict] = {}

    # ── Pentium / Celeron (Haswell 2013, Skylake 2015, Kaby Lake 2017, Coffee Lake 2018-2019) ──
    haswell_2013 = {
        "Pentium_G3240": 2014, "Pentium_G3250": 2014, "Pentium_G3250T": 2014,
        "Pentium_G3260": 2014, "Pentium_G3420": 2013, "Pentium_G3430": 2013,
        "Pentium_G3440": 2014, "Pentium_G3450": 2014, "Pentium_G3460": 2014,
        "Pentium_G3470": 2015,
        "Celeron_G1820": 2013, "Celeron_G1830": 2013, "Celeron_G1840": 2014,
        "Celeron_G1850": 2014,
    }
    kaby_2017 = {  # Pentium/Celeron 7th gen
    }
    skylake_2015 = {
        "Pentium_G4400T": 2015, "Pentium_G4500": 2015, "Pentium_G4500T": 2015,
        "Pentium_G4520": 2015, "Celeron_G3920": 2015, "Celeron_G3930T": 2015,
    }
    kaby_2017_pentium = {
        "Pentium_G4560T": 2017, "Pentium_G4600T": 2017, "Pentium_G4620": 2017,
        "Celeron_G3950": 2017,
    }
    coffee_2018 = {
        "Pentium_Gold_G5400T": 2018, "Pentium_Gold_G5500": 2018,
        "Pentium_Gold_G5500T": 2018, "Pentium_Gold_G5600": 2018,
        "Celeron_G4900T": 2018, "Celeron_G4920": 2018, "Celeron_G4930": 2018,
    }
    coffee_refresh_2019 = {
        "Pentium_Gold_G6500": 2019, "Pentium_Gold_G6605": 2019,
        "Celeron_G5905T": 2019, "Celeron_G5920": 2019,
    }

    all_pentium_celeron = {}
    for d in [haswell_2013, skylake_2015, kaby_2017_pentium, coffee_2018, coffee_refresh_2019]:
        all_pentium_celeron.update(d)

    for model, year in all_pentium_celeron.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "Intel"}

    # ── Core i3 ──
    i3_map = {
        # 4th gen Haswell
        "i3-4130T": 2013, "i3-4150": 2014, "i3-4160T": 2014, "i3-4330": 2013,
        "i3-4340": 2013, "i3-4350": 2014, "i3-4360": 2014, "i3-4370": 2014,
        # 6th gen Skylake
        "i3-6098P": 2015, "i3-6100": 2015, "i3-6100T": 2015, "i3-6300T": 2015,
        # 7th gen Kaby Lake
        "i3-7101TE": 2017, "i3-7300": 2017, "i3-7300T": 2017,
        # 8th gen Coffee Lake
        "i3-8100": 2017, "i3-8100T": 2018, "i3-8300": 2018, "i3-8300T": 2018,
        # 9th gen Coffee Lake Refresh
        "i3-9100F": 2019, "i3-9300": 2019, "i3-9320": 2019, "i3-9350K": 2019, "i3-9350KF": 2019,
        # 10th gen Comet Lake
        "i3-10100": 2020, "i3-10100F": 2020, "i3-10105F": 2021, "i3-10305": 2021, "i3-10325": 2021,
        # 12th gen Alder Lake
        "i3-12100": 2022, "i3-12100F": 2022,
        # 13th gen Raptor Lake
        "i3-13100F": 2023,
        # 14th gen Raptor Lake Refresh
        "i3-14100": 2024, "i3-14100F": 2024,
    }
    for model, year in i3_map.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "Intel"}

    # ── Core i5 ──
    i5_map = {
        # 4th gen
        "i5-4440S": 2013, "i5-4460S": 2014, "i5-4590": 2014, "i5-4590T": 2014,
        "i5-4670S": 2013, "i5-4670T": 2013, "i5-4690K": 2014, "i5-4690S": 2014,
        # 5th gen Broadwell
        "i5-5675C": 2015,
        # 6th gen
        "i5-6400T": 2015, "i5-6500": 2015, "i5-6600K": 2015, "i5-6600T": 2015,
        # 7th gen
        "i5-7400": 2017, "i5-7500": 2017, "i5-7500T": 2017, "i5-7600": 2017,
        "i5-7600T": 2017, "i5-7640X": 2017,
        # 8th gen
        "i5-8400": 2017, "i5-8500": 2018, "i5-8600K": 2017, "i5-8600T": 2018,
        # 9th gen
        "i5-9400F": 2019, "i5-9400T": 2019, "i5-9600K": 2018, "i5-9600T": 2019,
        # 10th gen
        "i5-10400": 2020, "i5-10400F": 2020, "i5-10600K": 2020,
        # 11th gen
        "i5-11400": 2021, "i5-11400F": 2021, "i5-11600K": 2021,
        # 12th gen
        "i5-12400": 2022, "i5-12400F": 2022, "i5-12500": 2022,
        "i5-12600K": 2021, "i5-12600KF": 2021,
        # 13th gen
        "i5-13400": 2023, "i5-13400F": 2023, "i5-13500": 2023,
        "i5-13600K": 2022, "i5-13600KF": 2022,
        # 14th gen
        "i5-14400": 2024, "i5-14400F": 2024, "i5-14500": 2024,
        "i5-14600K": 2023, "i5-14600KF": 2023,
    }
    for model, year in i5_map.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "Intel"}

    # ── Core i7 ──
    i7_map = {
        # 4th gen
        "i7-4770": 2013, "i7-4770K": 2013, "i7-4770T": 2013,
        "i7-4790": 2014, "i7-4790K": 2014, "i7-4790T": 2014,
        # 6th gen
        "i7-6700": 2015, "i7-6700K": 2015,
        # 7th gen
        "i7-7700": 2017, "i7-7700K": 2017,
        # 8th gen
        "i7-8700": 2017, "i7-8700K": 2017,
        # 9th gen
        "i7-9700": 2019, "i7-9700F": 2019, "i7-9700K": 2018, "i7-9700KF": 2019, "i7-9700T": 2019,
        # 10th gen
        "i7-10700": 2020, "i7-10700F": 2020, "i7-10700K": 2020, "i7-10700KF": 2020,
        # 11th gen
        "i7-11700": 2021, "i7-11700F": 2021, "i7-11700K": 2021, "i7-11700KF": 2021,
        # 12th gen
        "i7-12700": 2022, "i7-12700F": 2022, "i7-12700K": 2021, "i7-12700KF": 2021,
        # 13th gen
        "i7-13700F": 2023, "i7-13700K": 2022, "i7-13700KF": 2022,
        # 14th gen
        "i7-14700": 2024, "i7-14700F": 2024, "i7-14700K": 2023, "i7-14700KF": 2023,
    }
    for model, year in i7_map.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "Intel"}

    # ── Core i9 ──
    i9_map = {
        # 7th gen X-series
        "i9-7940X": 2017,
        # 9th gen
        "i9-9900K": 2018, "i9-9900KF": 2019, "i9-9920X": 2018,
        # 10th gen
        "i9-10850K": 2020, "i9-10900K": 2020,
        # 11th gen
        "i9-11900K": 2021,
        # 12th gen
        "i9-12900K": 2021, "i9-12900KF": 2021, "i9-12900KS": 2022,
        # 13th gen
        "i9-13900K": 2022, "i9-13900KF": 2022, "i9-13900KS": 2023,
        # 14th gen
        "i9-14900K": 2023, "i9-14900KF": 2023, "i9-14900KS": 2024,
    }
    for model, year in i9_map.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "Intel"}

    # ── Core Ultra 200 ──
    ultra_map = {
        "Ultra_5_225": 2024, "Ultra_5_225F": 2024, "Ultra_5_235": 2024,
        "Ultra_5_245K": 2024,
        "Ultra_7_265": 2024, "Ultra_7_265F": 2024, "Ultra_7_265K": 2024,
        "Ultra_7_265KF": 2024,
        "Ultra_9_285": 2024, "Ultra_9_285K": 2024,
    }
    for model, year in ultra_map.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "Intel"}

    # ── Xeon E3 v3/v5/v6 ──
    xeon_e3 = {
        "Xeon_E3-1220_V3": 2013, "Xeon_E3-1225_V3": 2013, "Xeon_E3-1226_V3": 2013,
        "Xeon_E3-1240_V3": 2013, "Xeon_E3-1245_V3": 2013, "Xeon_E3-1246_V3": 2013,
        "Xeon_E3-1265L_V3": 2013, "Xeon_E3-1271_V3": 2013, "Xeon_E3-1275_V3": 2013,
        "Xeon_E3-1276_V3": 2013,
        "Xeon_E3-1220_V5": 2015, "Xeon_E3-1225_V5": 2015, "Xeon_E3-1230_V5": 2015,
        "Xeon_E3-1235L_V5": 2015, "Xeon_E3-1240_V5": 2015, "Xeon_E3-1245_V5": 2015,
        "Xeon_E3-1260L_V5": 2015, "Xeon_E3-1270_V5": 2015, "Xeon_E3-1275_V5": 2015,
        "Xeon_E3-1280_V5": 2015,
        "Xeon_E3-1225_V6": 2017, "Xeon_E3-1230_V6": 2017, "Xeon_E3-1240_V6": 2017,
        "Xeon_E3-1270_V6": 2017, "Xeon_E3-1275_V6": 2017, "Xeon_E3-1280_V6": 2017,
        "Xeon_E3-1285_V6": 2017,
    }
    for model, year in xeon_e3.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "Intel"}

    # ── Xeon E-21xx / E-22xx ──
    xeon_e = {
        "Xeon_E-2104G": 2018, "Xeon_E-2124": 2018, "Xeon_E-2124G": 2018,
        "Xeon_E-2126G": 2018, "Xeon_E-2134": 2018, "Xeon_E-2136": 2018,
        "Xeon_E-2144G": 2018, "Xeon_E-2146G": 2018, "Xeon_E-2174G": 2018,
        "Xeon_E-2176G": 2018,
        "Xeon_E-2236": 2019, "Xeon_E-2244G": 2019, "Xeon_E-2246G": 2019,
        "Xeon_E-2274G": 2019, "Xeon_E-2276G": 2019, "Xeon_E-2278G": 2019,
    }
    for model, year in xeon_e.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "Intel"}

    # ── Xeon E5 v3/v4 ──
    xeon_e5 = {
        "Xeon_E5-1630_V3": 2014, "Xeon_E5-1680_V3": 2014, "Xeon_E5-2603_V3": 2014,
        "Xeon_E5-2609_V3": 2014, "Xeon_E5-2622_V3": 2014, "Xeon_E5-2623_V3": 2014,
        "Xeon_E5-2630L_V3": 2014, "Xeon_E5-2637_V3": 2014, "Xeon_E5-2640_V3": 2014,
        "Xeon_E5-2643_V3": 2014, "Xeon_E5-2650L_V3": 2014, "Xeon_E5-2650_V3": 2014,
        "Xeon_E5-2658_V3": 2014, "Xeon_E5-2683_V3": 2014, "Xeon_E5-2685_V3": 2014,
        "Xeon_E5-2687W_V3": 2014, "Xeon_E5-2690_V3": 2014, "Xeon_E5-2698_V3": 2014,
        "Xeon_E5-2603_V4": 2016, "Xeon_E5-2609_V4": 2016, "Xeon_E5-2620_V4": 2016,
        "Xeon_E5-2643_V4": 2016, "Xeon_E5-2650L_V4": 2016, "Xeon_E5-2680_V4": 2016,
        "Xeon_E5-2699_V4": 2016,
    }
    for model, year in xeon_e5.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "Intel"}

    return m


def build_amd_map() -> dict[str, dict]:
    """构建 AMD CPU 信息映射。"""
    m: dict[str, dict] = {}

    # ── FM1/FM2 A-series ──
    # A4-4000, A4-4020, A4-5300, A4-6300B, A4-7300 = FM2 2012-2013
    # A6-5400K, A6-6420K, A6-7470K, A6-9500, A6-9500E, A6-9550 = FM2/FM2+/AM4
    a_series = {
        "A4-4000": 2013, "A4-4020": 2014, "A4-5300": 2012, "A4-6300B": 2013,
        "A4-7300": 2014,
        "A6-5400K": 2012, "A6-6420K": 2014, "A6-7470K": 2014,
        "A6-9500": 2016, "A6-9500E": 2016, "A6-9550": 2016,
        "A8-5500": 2012, "A8-5600K": 2012, "A8-6500": 2013, "A8-7670K": 2015,
        "A10-5700": 2012, "A10-5800K": 2012, "A10-6790K": 2013, "A10-6800B": 2013,
        "A10-7860k": 2015, "A10-7870K": 2015, "A10-7890K": 2016,
        "A10-9700E": 2017, "A12-9800": 2017, "A12-9800E": 2017,
        # Pro variants
        "Pro_A4-7300B": 2014, "Pro_A6-7400B": 2014,
        "Pro_A8-7600B": 2014, "Pro_A10-7800B": 2014, "Pro_A10-7850B": 2015,
    }
    for model, year in a_series.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "AMD"}

    # ── Athlon (FM2/AM4) ──
    athlon = {
        "Athlon_X2_350": 2012, "Athlon_X2_450": 2012,
        "Athlon_X4_740": 2012, "Athlon_X4_750K": 2013, "Athlon_X4_760K": 2013,
        "Athlon_X4_840": 2014, "Athlon_X4_870K": 2015, "Athlon_X4_880K": 2016,
        "Athlon_X4_940": 2017, "Athlon_X4_950": 2017, "Athlon_X4_970": 2017,
        "Athlon_220GE": 2018, "Athlon_240GE": 2018, "Athlon_3000G_14nm": 2019,
    }
    for model, year in athlon.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "AMD"}

    # ── Sempron ──
    m["Sempron_X2_250"] = {"release_year": 2012, "category": "CPU", "brand": "AMD"}

    # ── FX series ──
    fx = {
        "FX-4320": 2012, "FX-8310": 2014, "FX-8350": 2012,
        "FX-8370E": 2014, "FX-9370": 2013,
    }
    for model, year in fx.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "AMD"}

    # ── Opteron ──
    opteron = {
        "Opteron_6320": 2012, "Opteron_6328": 2012, "Opteron_6344": 2012,
        "Opteron_6378": 2012,
    }
    for model, year in opteron.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "AMD"}

    # ── Ryzen 3 ──
    ryzen3 = {
        "Ryzen_3_2200G": 2018, "Ryzen_3_3100": 2020, "Ryzen_3_3200G": 2019,
        "Ryzen_3_4100": 2022,
    }
    for model, year in ryzen3.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "AMD"}

    # ── Ryzen 5 ──
    ryzen5 = {
        "Ryzen_5_1600X": 2017, "Ryzen_5_1600_12nm": 2019, "Ryzen_5_1600_14nm": 2017,
        "Ryzen_5_2400G": 2018, "Ryzen_5_2600": 2018, "Ryzen_5_2600X": 2018,
        "Ryzen_5_3400G": 2019, "Ryzen_5_3500X": 2019, "Ryzen_5_3600": 2019,
        "Ryzen_5_3600X": 2019, "Ryzen_5_4500": 2022, "Ryzen_5_4600G": 2020,
        "Ryzen_5_5500": 2022, "Ryzen_5_5500GT": 2024, "Ryzen_5_5600": 2022,
        "Ryzen_5_5600G": 2021, "Ryzen_5_5600GT": 2024, "Ryzen_5_5600X": 2020,
        "Ryzen_5_5600X3D": 2023, "Ryzen_5_5600XT": 2024,
        "Ryzen_5_7500F": 2023, "Ryzen_5_7600": 2023, "Ryzen_5_7600X": 2022,
        "Ryzen_5_7600X3D": 2024, "Ryzen_5_8400F": 2024, "Ryzen_5_8500G": 2024,
        "Ryzen_5_8600G": 2024, "Ryzen_5_9600": 2024, "Ryzen_5_9600X": 2024,
    }
    for model, year in ryzen5.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "AMD"}

    # ── Ryzen 7 ──
    ryzen7 = {
        "Ryzen_7_1700": 2017, "Ryzen_7_2700": 2018, "Ryzen_7_2700X": 2018,
        "Ryzen_7_3700X": 2019, "Ryzen_7_3800X": 2019,
        "Ryzen_7_5700": 2022, "Ryzen_7_5700G": 2021, "Ryzen_7_5700X": 2022,
        "Ryzen_7_5700X3D": 2024, "Ryzen_7_5800X": 2020, "Ryzen_7_5800X3D": 2022,
        "Ryzen_7_5800XT": 2024,
        "Ryzen_7_7700": 2023, "Ryzen_7_7700X": 2022, "Ryzen_7_7800X3D": 2023,
        "Ryzen_7_8700F": 2024, "Ryzen_7_8700G": 2024,
        "Ryzen_7_9700X": 2024, "Ryzen_7_9800X3D": 2024,
    }
    for model, year in ryzen7.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "AMD"}

    # ── Ryzen 9 ──
    ryzen9 = {
        "Ryzen_9_3900X": 2019, "Ryzen_9_3900XT": 2020, "Ryzen_9_3950X": 2019,
        "Ryzen_9_5900X": 2020, "Ryzen_9_5950X": 2020,
        "Ryzen_9_7900": 2023, "Ryzen_9_7900X": 2022, "Ryzen_9_7900X3D": 2023,
        "Ryzen_9_7950X": 2022, "Ryzen_9_7950X3D": 2023,
        "Ryzen_9_9900X": 2024, "Ryzen_9_9900X3D": 2024,
        "Ryzen_9_9950X": 2024, "Ryzen_9_9950X3D": 2024,
    }
    for model, year in ryzen9.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "AMD"}

    # ── Threadripper ──
    tr = {
        "Threadripper_3960X": 2019, "Threadripper_3970X": 2019,
        "Threadripper_3990X": 2020,
    }
    for model, year in tr.items():
        m[model] = {"release_year": year, "category": "CPU", "brand": "AMD"}

    # ── EPYC ──
    m["EPYC_4564P"] = {"release_year": 2024, "category": "CPU", "brand": "AMD"}

    return m


def fill_model_info(conn: sqlite3.Connection) -> list[str]:
    """用内置数据填充 model_info，返回未匹配的型号列表。"""
    all_info = {}
    all_info.update(build_intel_map())
    all_info.update(build_amd_map())

    models = conn.execute("SELECT DISTINCT keyword FROM products ORDER BY keyword").fetchall()
    cursor = conn.cursor()
    matched = 0
    unknown: list[str] = []

    for (model,) in models:
        info = all_info.get(model)
        if info:
            cursor.execute(
                """INSERT OR REPLACE INTO model_info
                   (model, category, brand, release_year, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (model, info["category"], info["brand"], info["release_year"], datetime.now().isoformat()),
            )
            matched += 1
        else:
            unknown.append(model)

    conn.commit()
    print(f"  内置匹配: {matched}/{len(models)}")
    print(f"  未匹配:   {len(unknown)}")
    return unknown


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    print("=== 填充 model_info 表 ===")
    unknown = fill_model_info(conn)

    if unknown:
        print("\n--- 未匹配型号（需搜索补充）---")
        for m in unknown:
            print(f"  {m}")
        # 写入文件
        with open("data/unknown_models.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(unknown))
        print(f"\n已写入 data/unknown_models.txt ({len(unknown)} 个)")
    else:
        print("\n全部匹配完成!")

    # 统计
    total = conn.execute("SELECT COUNT(*) FROM model_info").fetchone()[0]
    print(f"\nmodel_info 表共 {total} 条")
    conn.close()


if __name__ == "__main__":
    main()
