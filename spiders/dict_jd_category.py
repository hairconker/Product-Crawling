"""阶段 10：京东分类页国行品牌 SKU 补充爬虫。

功能：对给定品类（GPU/CPU/主板/内存/...），枚举分类页筛选区的品牌，按品牌翻页
抓取商品标题（= SKU 全名），输出 `data/skus/{week}/{category}/{brand}_jd.csv`。

红线：
  - **不** from core import（保持自包含），仅从 run_cpu_crawl_pw.py 复用基础设施
  - 沿用项目"睁眼"机制：每次导航后 _snapshot_page + _detect_risk
  - 节流常量用 run_cpu_crawl_pw.py 定义的，不硬写
  - 品牌 slug 与阶段 9（core/dict/categories.py）保持一致，便于阶段 11 合并

前置：
  1. `state/jd_state.json` 存在（先跑 `python scripts/login_helper.py jd`）
  2. `config/dict_jd_categories.yaml` 填实目标品类 cat 值
  3. 建议 Windows 家宽（WSL IP 会被 JD 机房软拦截，搜索页只渲染页脚）
"""

from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 项目根加入 sys.path，才能 import 同级 run_cpu_crawl_pw
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from playwright.sync_api import (  # noqa: E402
    sync_playwright,
    Page,
    BrowserContext,
    TimeoutError as PWTimeout,
)

from run_cpu_crawl_pw import (  # noqa: E402
    log,
    dump_debug,
    _snapshot_page,
    _detect_risk,
    _scroll_page,
    SpiderError,
    NetworkError,
    LoginRequiredError,
    AntiSpiderError,
    ParseError,
    UA_DESKTOP,
    STATE_DIR,
    SCROLL_DELAY_MS,
    PAGE_DELAY_MS,
    JD_PAGE_DELAY_MS,
    PLATFORM_DELAY_SEC,
)


# ---------------------------------------------------------------------------
# JD 页面状态识别（Codex 审 C1/C2 修复）
# ---------------------------------------------------------------------------

def _jd_detect_state(page: Page) -> str | None:
    """辨识 JD 列表页的非正常状态。

    返回值：
      None            - 正常（有商品容器或待验证）
      'ip_block'      - 机房 IP 被软拦截（整页仅 footer，无面包屑 + 无商品容器）
      'passport_ip'   - 跳 passport 且 ReturnUrl 指向 list.jd.com（机房 IP 强制登录挡页）
      'passport_expired' - 跳 passport 但非 list 返回，视为 cookie 过期
      'risk'          - 命中 _detect_risk 风控特征
    """
    url = (page.url or "").lower()
    if "passport.jd.com" in url:
        if "returnurl" in url and "list.jd.com" in url:
            return "passport_ip"
        return "passport_expired"
    risk = _detect_risk(page)
    if risk:
        return "risk"
    try:
        content = page.content()
    except Exception:
        return None
    low = content.lower()
    has_crumbs = "crumbs-nav" in low or "j_crumbsbar" in low or "面包屑" in content
    has_products = "gl-warp" in low or "gl-item" in low
    if not has_crumbs and not has_products:
        return "ip_block"
    return None


# ---------------------------------------------------------------------------
# 品牌中文名 → 项目品牌 slug（与 core/dict/categories.py::BRAND_ALIASES 对齐）
# ---------------------------------------------------------------------------

JD_BRAND_CN_TO_SLUG: dict[str, str] = {
    # GPU 厂商（国际 + 国行）
    "华硕": "asus", "ASUS": "asus",
    "微星": "msi", "MSI": "msi",
    "技嘉": "gigabyte", "GIGABYTE": "gigabyte",
    "影驰": "galax", "GALAX": "galax",
    "七彩虹": "colorful", "Colorful": "colorful",
    "铭瑄": "maxsun", "MAXSUN": "maxsun",
    "万丽": "manli", "Manli": "manli",
    "索泰": "zotac", "ZOTAC": "zotac",
    "耕升": "gainward", "Gainward": "gainward",
    "映众": "inno3d", "INNO3D": "inno3d",
    "盈通": "yeston", "Yeston": "yeston",
    "讯景": "xfx", "XFX": "xfx",
    "撼讯": "powercolor", "PowerColor": "powercolor",
    "蓝宝石": "sapphire", "Sapphire": "sapphire",
    "迪兰恒进": "dataland", "迪兰": "dataland",
    "昂达": "onda", "ONDA": "onda",
    "NVIDIA": "nvidia",
    # CPU
    "英特尔": "intel", "Intel": "intel", "INTEL": "intel",
    "AMD": "amd", "amd": "amd",
    # 主板
    "华擎": "asrock", "ASRock": "asrock",
    # 内存
    "金士顿": "kingston", "Kingston": "kingston",
    "海盗船": "corsair", "Corsair": "corsair",
    "威刚": "adata", "ADATA": "adata",
    "光威": "gloway",
    "芝奇": "gskill", "G.SKILL": "gskill", "G.Skill": "gskill",
    "宇瞻": "apacer", "Apacer": "apacer",
    "十铨": "teamgroup", "TEAMGROUP": "teamgroup", "T-FORCE": "teamgroup",
    "三星": "samsung", "SAMSUNG": "samsung",
    "SK海力士": "skhynix", "海力士": "skhynix",
    "科赋": "klevv", "KLEVV": "klevv",
    "英睿达": "crucial", "Crucial": "crucial", "镁光": "crucial",
    "金百达": "kimtigo",
    "阿斯加特": "asgard",
    # SSD
    "致态": "zhitai", "致钛": "zhitai", "长江存储": "zhitai",
    "铠侠": "kioxia", "KIOXIA": "kioxia",
    "西部数据": "wd", "西数": "wd", "WD": "wd", "WD_BLACK": "wd",
    "希捷": "seagate", "Seagate": "seagate",
    "东芝": "toshiba", "TOSHIBA": "toshiba",
    "闪迪": "sandisk", "SanDisk": "sandisk",
    "爱国者": "aigo", "aigo": "aigo",
    # 电源
    "全汉": "fsp", "FSP": "fsp",
    "安钛克": "antec", "Antec": "antec",
    "海韵": "seasonic", "Seasonic": "seasonic",
    "长城": "greatwall",
    "航嘉": "huntkey", "Huntkey": "huntkey",
    "先马": "segotep",
    "台达": "delta",
    "恩杰": "nzxt", "NZXT": "nzxt",
    "追风者": "phanteks", "Phanteks": "phanteks",
    "超频三": "pccooler",
    # 机箱 / 散热
    "乔思伯": "jonsbo", "JONSBO": "jonsbo",
    "九州风神": "deepcool", "DeepCool": "deepcool",
    "利民": "thermalright", "Thermalright": "thermalright",
    "猫头鹰": "noctua", "Noctua": "noctua",
    "鑫谷": "segotep",
    "金河田": "jinhetian",
    # 网卡
    "TP-LINK": "tplink", "普联": "tplink",
    "水星": "mercury",
    "迅捷": "fast",
    "华三": "h3c", "H3C": "h3c",
    "D-LINK": "dlink", "D-Link": "dlink",
    "网件": "netgear", "NETGEAR": "netgear",
}


# ---------------------------------------------------------------------------
# 轻量 stealth（足够过 JD 一般检测；淘宝级深度 stealth 不需要）
# ---------------------------------------------------------------------------

_STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
window.chrome = window.chrome || { runtime: {} };
"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class SkuEntry:
    sku_id: str
    sku_title: str
    jd_url: str
    source: str = "jd_category"


@dataclass
class BrandCrawlStat:
    category: str
    brand_slug: str
    brand_cn: str
    ev_value: str
    pages_crawled: int
    sku_count: int
    note: str = ""


# ---------------------------------------------------------------------------
# 爬虫主体
# ---------------------------------------------------------------------------


class JDCategoryCrawler:
    """持久化 Playwright context，跨品类/品牌复用。调用完记得 close()。"""

    def __init__(
        self,
        *,
        headed: bool = False,
        out_base: Path | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._out_base = out_base or (_ROOT / "data")
        state = state_path or (STATE_DIR / "jd_state.json")
        if not state.exists():
            raise LoginRequiredError(
                f"未找到京东登录态 {state}；请先跑 `python scripts/login_helper.py jd`",
                platform="jd",
                url="-",
            )
        self._p = sync_playwright().start()
        self._browser = self._p.chromium.launch(headless=not headed)
        self._context: BrowserContext = self._browser.new_context(
            user_agent=UA_DESKTOP,
            storage_state=str(state),
            viewport={"width": 1440, "height": 900},
            locale="zh-CN",
        )
        self._context.add_init_script(_STEALTH_JS)

    def close(self) -> None:
        try:
            self._context.close()
        finally:
            try:
                self._browser.close()
            finally:
                self._p.stop()

    # ---- JD 状态分发 ---------------------------------------------------

    def _dispatch_jd_state(self, page: Page, *, ctx: str) -> None:
        """统一把 _jd_detect_state 的非 None 结果映射到项目异常。"""
        state = _jd_detect_state(page)
        if state is None:
            return
        _snapshot_page(page, "jd_dict", f"abort_{state}_{ctx.replace('/', '_')}")
        if state == "ip_block":
            raise LoginRequiredError(
                f"{ctx} 疑似机房 IP 软拦截（页面仅 footer，无商品容器也无面包屑）；"
                "建议切 Windows 家宽或走 run_jd_drission.py 兜底模式",
                platform="jd", url=page.url,
            )
        if state == "passport_ip":
            raise LoginRequiredError(
                f"{ctx} 跳 passport 且 ReturnUrl 指向 list（机房 IP 强制登录挡页，非 cookie 问题）",
                platform="jd", url=page.url,
            )
        if state == "passport_expired":
            raise LoginRequiredError(
                f"{ctx} cookie 失效（跳 passport 且非 list 返回）；请重跑 "
                "`python scripts/login_helper.py jd`",
                platform="jd", url=page.url,
            )
        if state == "risk":
            raise AntiSpiderError(
                f"{ctx} 风控特征命中：{_detect_risk(page)}",
                platform="jd", url=page.url,
            )
        # 未来新增状态
        raise SpiderError(f"{ctx} 未处理的 JD 状态：{state}", platform="jd", url=page.url)

    # ---- 品牌发现 ------------------------------------------------------

    def discover_brands(self, category: str, cat: str) -> dict[str, tuple[str, str]]:
        """访问分类页筛选区，抽品牌列表。

        返回 {brand_slug: (cn_name, ev_value)}，仅包含在 JD_BRAND_CN_TO_SLUG 表里的已知品牌。
        未在表中的品牌写入 logs/debug/jd_dict_unknown_brands_{category}.txt 供人工 review。
        """
        url = f"https://list.jd.com/list.html?cat={cat}"
        page = self._context.new_page()
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except PWTimeout as e:
                raise NetworkError(f"分类页超时: {e}", platform="jd", url=url) from e
            _scroll_page(page, 2)
            _snapshot_page(page, "jd_dict", f"discover_{category}")

            self._dispatch_jd_state(page, ctx=f"discover/{category}")

            raw_brands: dict[str, str] = page.evaluate(
                """
                () => {
                    const out = {};
                    // 严格限定：先找品牌容器，失败才退到通用选择器
                    const container =
                        document.querySelector('#J_selector #brand-16') ||
                        document.querySelector('#J_selector div[id^="brand"]') ||
                        document.querySelector('#brand-16');
                    const scope = container || document;
                    // href= 走 ev= 参数；兼容 data-ev 属性
                    scope.querySelectorAll('a[href*="exbrand_"], a[data-ev*="exbrand_"]').forEach(a => {
                        const name = (a.textContent || '').trim();
                        if (!name) return;
                        const href = a.getAttribute('href') || '';
                        const dataEv = a.getAttribute('data-ev') || '';
                        const m = href.match(/ev=(exbrand_[^&]+)/) || dataEv.match(/(exbrand_[^&]+)/);
                        if (m && !out[name]) out[name] = m[1];
                    });
                    return out;
                }
                """
            )
            if not raw_brands:
                dump_debug("jd_dict", page.content(), "html")
                raise ParseError(
                    "未解析到任何品牌（选择器失效？或品类无筛选区）",
                    platform="jd",
                    url=page.url,
                )

            known: dict[str, tuple[str, str]] = {}
            unknown: list[str] = []
            for cn_name, ev in raw_brands.items():
                slug = JD_BRAND_CN_TO_SLUG.get(cn_name)
                if slug:
                    if slug in known:
                        log.warning(
                            f"[jd_dict] 品牌 slug 冲突：{slug} 同时来自 {known[slug][0]!r} 与 {cn_name!r}，保留前者"
                        )
                        continue
                    known[slug] = (cn_name, ev)
                else:
                    unknown.append(cn_name)

            if unknown:
                unknown_path = (_ROOT / "logs" / "debug" /
                                f"jd_dict_unknown_brands_{category}.txt")
                with open(unknown_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(sorted(unknown)))
                log.info(
                    f"[jd_dict] {category} 识别品牌 {len(known)} / 未识别 {len(unknown)} "
                    f"（未识别列表落盘 {unknown_path.name}）"
                )
            else:
                log.info(f"[jd_dict] {category} 全部 {len(known)} 品牌已识别")
            return known
        finally:
            page.close()

    # ---- 单品牌翻页 ----------------------------------------------------

    def crawl_brand(
        self,
        category: str,
        brand_slug: str,
        cat: str,
        ev_value: str,
        *,
        max_pages: int = 100,
        snapshot_every: int = 10,
    ) -> tuple[list[SkuEntry], int]:
        """按品牌 ev 值翻页抓取。

        返回 (entries, pages_visited)，entries 按 sku_id 去重（S3 修复）。
        每 `snapshot_every` 页 + 首页强制 snapshot（S2 修复，保留证据链）。
        失败状态分派到 `_dispatch_jd_state`（C1/C2 修复）。
        """
        seen_sku_ids: set[str] = set()
        entries: list[SkuEntry] = []
        prev_dup_ratio = 0.0
        pages_visited = 0
        page = self._context.new_page()
        try:
            for page_no in range(1, max_pages + 1):
                url = (
                    f"https://list.jd.com/list.html?"
                    f"cat={cat}&page={page_no}&ev={ev_value}&psort=0&click=0"
                )
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60000)
                except PWTimeout as e:
                    raise NetworkError(
                        f"列表页超时 {category}/{brand_slug} p={page_no}: {e}",
                        platform="jd",
                        url=url,
                    ) from e
                pages_visited += 1

                # 周期性 snapshot + 风控检测
                if page_no == 1 or page_no % snapshot_every == 0:
                    _snapshot_page(
                        page, "jd_dict",
                        f"{category}_{brand_slug}_p{page_no}",
                    )

                # 先统一分派非正常状态（ip_block / passport / risk）
                self._dispatch_jd_state(page, ctx=f"{category}/{brand_slug}/p{page_no}")

                # 等商品容器
                try:
                    page.wait_for_selector(
                        "ul.gl-warp li.gl-item, .gl-i-wrap",
                        timeout=20000,
                    )
                except PWTimeout:
                    # 再次判状态（页面可能在等待期间退化）
                    self._dispatch_jd_state(
                        page, ctx=f"{category}/{brand_slug}/p{page_no}/timeout",
                    )
                    # 仍无状态命中 → 合法空品牌（极少见：面包屑在但商品 0 条）
                    log.info(
                        f"[jd_dict] {category}/{brand_slug} p={page_no} "
                        f"面包屑在但无 gl-warp，视为该 ev 无商品，终止"
                    )
                    break

                _scroll_page(page, 6)
                self._dispatch_jd_state(
                    page, ctx=f"{category}/{brand_slug}/p{page_no}/post_scroll",
                )

                items = page.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('li.gl-item')).map(li => {
                        const a = li.querySelector('.p-name a, .p-name em');
                        const em = li.querySelector('.p-name em');
                        const img = li.querySelector('.p-img a');
                        const title = (em ? em.textContent : (a ? a.textContent : '')).trim();
                        const href = img ? img.getAttribute('href') || '' : '';
                        return {
                            sku_id: li.getAttribute('data-sku') || '',
                            title: title,
                            jd_url: href ? (href.startsWith('http') ? href : 'https:' + href) : '',
                        };
                    }).filter(x => x.sku_id && x.title)
                    """
                )

                if not items:
                    log.info(
                        f"[jd_dict] {category}/{brand_slug} p={page_no} 解析 0 条，终止"
                    )
                    break

                new_cnt = 0
                for it in items:
                    sku_id = it["sku_id"]
                    if not sku_id or sku_id in seen_sku_ids:
                        continue
                    seen_sku_ids.add(sku_id)
                    entries.append(SkuEntry(
                        sku_id=sku_id,
                        sku_title=it["title"],
                        jd_url=it["jd_url"],
                    ))
                    new_cnt += 1

                dup_ratio = 1.0 - (new_cnt / len(items))
                log.info(
                    f"[jd_dict] {category}/{brand_slug} p={page_no} "
                    f"items={len(items)} new={new_cnt} dup={dup_ratio:.0%}"
                )

                if dup_ratio > 0.95 and prev_dup_ratio > 0.95:
                    log.info(
                        f"[jd_dict] {category}/{brand_slug} 连续两页重复率 >95%，终止翻页"
                    )
                    break
                prev_dup_ratio = dup_ratio

                page.wait_for_timeout(JD_PAGE_DELAY_MS)
        finally:
            page.close()
        return entries, pages_visited

    # ---- 落盘 ----------------------------------------------------------

    def write_csv(
        self,
        category: str,
        brand_slug: str,
        entries: list[SkuEntry],
        week: str,
    ) -> Path | None:
        if not entries:
            log.info(f"[jd_dict] {category}/{brand_slug} 0 条，跳过写入")
            return None
        out_path = self._out_base / "skus" / week / category / f"{brand_slug}_jd.csv"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(
                f, fieldnames=["sku_id", "sku_title", "jd_url", "source"]
            )
            w.writeheader()
            for e in entries:
                w.writerow({
                    "sku_id": e.sku_id,
                    "sku_title": e.sku_title,
                    "jd_url": e.jd_url,
                    "source": e.source,
                })
        tmp.replace(out_path)
        log.info(f"[jd_dict] {category}/{brand_slug} wrote {len(entries)} → {out_path}")
        return out_path


# ---------------------------------------------------------------------------
# 顶层编排：跑完一个品类的所有品牌
# ---------------------------------------------------------------------------


def run_category(
    crawler: JDCategoryCrawler,
    *,
    category: str,
    cat: str,
    week: str,
    brand_filter: set[str] | None = None,
    max_pages: int = 100,
    min_brand_items: int = 5,
) -> list[BrandCrawlStat]:
    """对单个品类，先 discover 品牌，再按品牌抓取。返回每个品牌的统计。"""
    stats: list[BrandCrawlStat] = []
    known = crawler.discover_brands(category, cat)
    target_slugs = (
        set(known.keys()) if brand_filter is None
        else set(known.keys()) & brand_filter
    )
    if not target_slugs:
        log.warning(
            f"[jd_dict] {category}: 目标品牌集合为空 "
            f"（filter={brand_filter}, discovered={list(known.keys())}）"
        )
        return stats

    for idx, slug in enumerate(sorted(target_slugs), 1):
        cn_name, ev = known[slug]
        log.info(
            f"[jd_dict] {category} brand {idx}/{len(target_slugs)}: "
            f"{slug} ({cn_name}) ev={ev}"
        )
        try:
            entries, pages_visited = crawler.crawl_brand(
                category, slug, cat, ev, max_pages=max_pages,
            )
        except (LoginRequiredError, AntiSpiderError) as e:
            # 登录 / 风控 → 整个品类停（继续只会雪崩）
            log.error(f"[jd_dict] {category}/{slug} 触发 {type(e).__name__}: {e}")
            stats.append(BrandCrawlStat(
                category=category, brand_slug=slug, brand_cn=cn_name,
                ev_value=ev, pages_crawled=0, sku_count=0,
                note=f"aborted:{type(e).__name__}",
            ))
            raise
        except SpiderError as e:
            log.error(f"[jd_dict] {category}/{slug} {type(e).__name__}: {e}")
            stats.append(BrandCrawlStat(
                category=category, brand_slug=slug, brand_cn=cn_name,
                ev_value=ev, pages_crawled=0, sku_count=0,
                note=f"error:{type(e).__name__}",
            ))
            continue

        crawler.write_csv(category, slug, entries, week)
        note = "ok"
        if len(entries) < min_brand_items:
            note = f"probably_missing:{len(entries)}<{min_brand_items}"
        stats.append(BrandCrawlStat(
            category=category, brand_slug=slug, brand_cn=cn_name,
            ev_value=ev, pages_crawled=pages_visited,
            sku_count=len(entries), note=note,
        ))
        # 品牌间隔，保护账号
        if idx < len(target_slugs):
            import time
            time.sleep(PLATFORM_DELAY_SEC)

    return stats
