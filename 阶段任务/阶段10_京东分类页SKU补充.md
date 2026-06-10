# 阶段 10 · 京东自营分类页国行品牌 SKU 补充

> 周期：第 19-20 天
> 目标：对 docyx 完全缺失的国行/国产品牌（影驰 / 七彩虹 / 铭瑄 / 万丽 / 玄派 / 索泰 / 耕升 / 映众 / 盈通 / 先马 / 航嘉 / 长城 等），从**京东自营分类页**抓商品标题作为 SKU 全名，补齐到 `data/skus/YYYY-WW/{category}/{brand}.csv`。

---

## 参考开源项目

| 项目 | 推荐度 | 说明 |
|---|---|---|
| 无成熟开源可直接复用 | — | 国内 DIY 字典生态空白（见阶段 8 淘汰表） |
| 仓库内 `run_cpu_crawl_pw.py` | 内部主参考 | 复用 browser launcher / state loader / stealth JS / `_snapshot_page` / `_detect_risk` |
| 仓库内 `vendor/ai-goofish-monitor` | 辅参考 | Playwright 登录态加载模式 |

## 任务清单

### 调研与配置
- [ ] 10.1 枚举 JD 各品类 `cat` 三级 ID（`https://list.jd.com/list.html?cat=a,b,c`）：
  - 显卡 / CPU / 主板 / 内存 / SSD / HDD / 电源 / 机箱 / 散热 / 网卡
  - 手工在浏览器走一遍分类页提取，登记到 `config/dict_jd_categories.yaml`
- [ ] 10.2 每个分类页 DOM 抽取品牌列表（`.sl-value` 或接口 `/list?cat=...&ev=exbrand_*`）
  - 登记到 `config/dict_jd_brands.yaml`：`{category: {brand_slug: ev_value}}`
  - 品牌 slug 与阶段 9 保持一致（`galax / colorful / maxsun / manli / ...`）

### 实现
- [ ] 10.3 新增 `spiders/dict_jd_category.py`：
  - 不从 `core/` 导入（保持自包含，沿用 `run_cpu_crawl_pw.py` 红线）
  - 复用既有 Playwright 基础设施：stealth JS、state/jd_state.json、snapshot、risk detect
  - 主函数 `crawl_jd_category(category, brand_slug, max_pages=100)`
  - 入参校验：category/brand_slug 必须在 yaml 配置中
- [ ] 10.4 翻页策略：`&page=N`，`N` 上限 100；命中"无更多商品"或连续 2 页标题去重率 >95% 终止
- [ ] 10.5 解析商品 `li.gl-item`：
  - `sku_id`（`data-sku`）、`title`（`.p-name em` 文本）、`jd_url`、`price`（可空，字典阶段不强求）
  - 标题 = SKU 全名（京东自营商品标题天然就是完整型号）
- [ ] 10.6 异常按项目约定分类（`LoginRequiredError / AntiSpiderError / RateLimitError / ParseError`）
  - 登录跳转 / 机房 IP 被拦 → `LoginRequiredError`（提示切 Windows 家宽或用 `run_jd_drission.py` 模式）
  - `rgv587_flag` 等风控特征 → `AntiSpiderError`
- [ ] 10.7 输出 `data/skus/YYYY-WW/{category}/{brand}.csv`：
  - 列：`sku_id / sku_title / jd_url / source=jd_category`
  - 与阶段 9 同目录共存（阶段 11 去重）
- [ ] 10.8 CLI 入口 `scripts/dict_jd_build.py`：
  - `--week YYYY-WW`、`--category gpu`、`--brands galax,colorful`、`--all`、`--headed`
- [ ] 10.9 失败兜底：配置 `--drission-fallback` 切换到 `run_jd_drission.py` 路径（JD 指纹被识破时）

## 节流与安全

- 沿用 `run_cpu_crawl_pw.py` 中已校准的常量：`PAGE_DELAY_MS=8000`、`JD_PAGE_DELAY_MS=15000`、`PLATFORM_DELAY_SEC=15.0`
- **不得**另起一套延迟常量，引用原文件定义或在 CLI 参数里覆盖
- 每分类每品牌跑完后强制 sleep `PLATFORM_DELAY_SEC`

## 已知坑

- **WSL IP 被 JD 软拦截**：搜索页/列表页可能只渲染页脚 → 本脚本优先在 Windows 家宽跑
- `ev=exbrand_xxxx` 的 ID 会随 JD 改版变化 → `dict_jd_brands.yaml` 每季度 sanity check（跑一次若某品牌结果为 0 → WARN）
- 自营 vs POP：分类页含第三方，需要在 URL 加 `&psort=0&click=0`（待调研）或按店铺名过滤
- 国行 vs 国际同品牌：华硕在京东分类页既有"华硕 ROG"（国行）也有"ASUS TUF"（国际），都保留，阶段 11 去重时会合并
- 品牌列表必须人工维护（yaml），不能靠 JD 接口自动全量（会抓到大量杂牌）

## 约束

- 不从 `core/` 导入（保持自包含）
- 复用项目"睁眼"机制（`_snapshot_page` + `_detect_risk`）：每次访问分类页必截图+风控检测
- 遵 `/spider-coding` + `/python-coding` 规范
- 禁止随机 class 选择器（京东 DOM 稳定，用 `.gl-item` `.p-name` 等业务类名）

## 验收标准

- `python scripts/dict_jd_build.py --week 2026-W17 --category gpu --brands galax,colorful,maxsun --headed` 跑通
- `data/skus/2026-W17/gpu/galax.csv` ≥ 30 条影驰 SKU 全名（RTX 4090/4080/4070... + 历史型号）
- `data/skus/2026-W17/gpu/colorful.csv` ≥ 50 条（七彩虹 SKU 覆盖更广）
- `logs/debug/jd_list_*.png` 有分类页截图 + meta
- 机房 IP 环境下跑：优雅抛 `LoginRequiredError`，提示用户切网（不是静默空结果）
- `/codex:rescue` 审查通过（异常分类正确 / 选择器稳定 / 无硬编码数字与原文件冲突 / dump_debug 覆盖所有失败分支）
