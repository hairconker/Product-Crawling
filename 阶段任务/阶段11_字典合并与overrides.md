# 阶段 11 · 字典合并器 + overrides 手填层

> 周期：第 21 天
> 目标：把阶段 9（docyx 国际品牌）、阶段 10（京东自营国行品牌）、用户手填 overrides 三源合并为阶段 12 唯一消费的 `data/skus/YYYY-WW/_merged/` 产物；冲突写报告。

---

## 任务清单

### overrides 文件格式
- [ ] 11.1 定义 `data/dict/overrides/` 目录结构（跨周复用，不分 week）：
  ```
  overrides/
    chips/
      gpu_chips.csv       # 列：chip, generation, vendor, release_year_est, note
      cpu_models.csv
      mb_chipsets.csv
    skus/
      gpu/
        galax.csv         # 列：sku_title, chip, spec_json, note
        colorful.csv
      cpu/
      mb/
  ```
- [ ] 11.2 文件头带固定注释行说明用法（`# manual overrides, priority highest`）
- [ ] 11.3 空文件跳过；缺失目录按无 overrides 处理

### 合并器
- [ ] 11.4 新增 `core/dict/merger.py`：
  - 入参：`week: str`（如 `2026-W17`）
  - 步骤 1：读 `data/dict/{week}/*_chips.csv` + `overrides/chips/*.csv`，**overrides 行覆盖同名 chip**
  - 步骤 2：读 `data/skus/{week}/{category}/{brand}.csv`（docyx + jd_category 两源）+ `overrides/skus/{category}/{brand}.csv`
  - 步骤 3：按 `(category, brand, sku_key)` 去重，`sku_key = lower(strip(replace(sku_title, " ", "")))`
  - 优先级：**overrides > jd_category > docyx**
  - 输出 `data/skus/{week}/_merged/{category}/{brand}.csv`
  - 列：`sku_title / chip / spec_json / source / origin_count`（origin_count = 几个源都出现过）
- [ ] 11.5 冲突报告 `logs/dict_merge_conflicts_{week}.csv`：
  - 同 sku_key 但 sku_title 不完全一致（大小写/空格差异以外的差异）
  - 同 sku_key 但 chip 字段不同（docyx 标 `RTX 4090` vs jd 标 `GeForce RTX 4090`）
- [ ] 11.6 统计摘要 `logs/dict_merge_summary_{week}.json`：每品类每品牌 SKU 数 + 各源贡献比

### CLI
- [ ] 11.7 `scripts/dict_merge.py --week YYYY-WW [--dry-run]`
- [ ] 11.8 `--dry-run` 只打印统计与冲突，不写 _merged

## 归一化策略（重要）

- **sku_key** 仅用于去重匹配：lower + strip + 去空格 + 去破折号
- **sku_title** 原样保留（阶段 12 拼搜索词要用），不归一
- **京东全名 vs 国际全名格式差异**（如 `华硕ROG 玩家国度 STRIX RTX4090-O24G-GAMING` vs `ASUS ROG Strix GeForce RTX 4090 OC 24G`）：
  - **视为同一 SKU**（归并到一条，source 标 `docyx+jd_category`）
  - **但保留两个 sku_title 变体**放到 `alt_titles` 列（阶段 12 可同时搜两个词，提高召回）
- 品牌 slug 统一：所有源入库前已经在阶段 9/10 归一化

## 已知坑

- 同品牌在 docyx 与京东的拼写差异很大：`ASUS` vs `华硕`、`Galax` vs `影驰` → 品牌 slug 表是关键，不能靠字符串匹配
- 用户手填 overrides 可能漏字段 → 容错：缺失字段取同 chip 的默认值或留空，不崩溃
- 阶段 9 / 10 任一源缺失（比如 jd_category 跑失败）：合并器**不中止**，仅用可用源 + 日志 WARN
- overrides 可能填了错别字产生新 SKU：用 `--dry-run` 先审

## 约束

- 纯数据处理，不跑浏览器 / 不发网络请求
- 读写 CSV 用标准库 `csv`，显式 `encoding='utf-8-sig'`（让 Excel 能直接打开中文 CSV）
- 类型标注齐全，用 `pydantic` 或 `dataclass` 表示 SkuRow / ChipRow
- 去重键逻辑单独抽 `sku_key(title: str) -> str` 函数并写单测

## 验收标准

- `python scripts/dict_merge.py --week 2026-W17` 产出：
  - `data/skus/2026-W17/_merged/gpu/asus.csv`（≥ docyx 原始行数，含 alt_titles 合并）
  - `data/skus/2026-W17/_merged/gpu/galax.csv`（≈ jd_category 原始行数）
- `logs/dict_merge_summary_2026-W17.json` 含各品类 SKU 总数
- 冲突报告存在时 stdout 明确提示行数
- 在 overrides 中手填一行覆盖 docyx 的某条 SKU，重跑后 _merged 的该行 source 字段为 `override`
- `/codex:rescue` 审查通过（sku_key 归一化正确 / 优先级覆盖无倒置 / 缺源容错）
