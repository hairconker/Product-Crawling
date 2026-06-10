#!/usr/bin/env python3
"""京东商品爬虫 · DrissionPage 独立版本

用 DrissionPage 接管真实 Chrome（或自启用户数据目录），绕过 Playwright 指纹检测。
与主脚本 run_cpu_crawl_pw.py 并列，**不相互依赖**。

用法：
    # 前置：一次性准备
    pip install DrissionPage

    # 方案 A（推荐）接管你已打开并登录过的 Chrome
    #   1) 先关所有 Chrome 窗口
    #   2) 用下面命令启动一个带调试端口的 Chrome：
    #      chrome.exe --remote-debugging-port=9222 --user-data-dir="E:\\chrome_jd_crawler"
    #   3) 手动扫码登录京东
    #   4) 保持 Chrome 窗口开着，跑：
    python run_jd_drission.py "i5-12400F"
    python run_jd_drission.py "i5-12400F" --port 9222

    # 方案 B  让脚本自己启动 Chrome（首次需扫码登录）
    python run_jd_drission.py "i5-12400F" --no-takeover --user-data-dir "E:\\chrome_jd_crawler"

    # 模式切换
    python run_jd_drission.py "i5-12400F" --mode list       # 默认：只抓列表页
    python run_jd_drission.py "i5-12400F" --mode detail     # 进每个商品详情页（慢 10x）
    python run_jd_drission.py "i5-12400F" --pages 3         # 翻 3 页
    python run_jd_drission.py --keywords "a,b,c" --pages 2  # 批量多关键词

参考：
    * g1879/DrissionPage v4.1+ (BSD-3)，官方文档 https://drissionpage.cn
    * 本脚本复用主仓的日志目录、CSV 输出格式，与 viewer.html 兼容
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

try:
    from DrissionPage import ChromiumPage, ChromiumOptions
    from DrissionPage.errors import ElementNotFoundError
except ImportError:
    print("\n[ERROR] 未安装 DrissionPage。请执行：\n")
    print("    pip install DrissionPage\n")
    sys.exit(1)


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
DEBUG_DIR = LOG_DIR / "debug"
for d in (LOG_DIR, DEBUG_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def _setup_logger() -> logging.Logger:
    lg = logging.getLogger("jd_drission")
    if lg.handlers:
        return lg
    lg.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    lg.addHandler(console)

    today = datetime.now().strftime("%Y-%m-%d")
    fh = logging.FileHandler(LOG_DIR / f"jd_drission_{today}.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    lg.addHandler(fh)

    eh = logging.FileHandler(LOG_DIR / "error.log", encoding="utf-8")
    eh.setLevel(logging.ERROR)
    eh.setFormatter(fmt)
    lg.addHandler(eh)
    lg.propagate = False
    return lg


log = _setup_logger()


def dump_debug(tag: str, payload: str | bytes, suffix: str = "html") -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = DEBUG_DIR / f"jd_drission_{tag}_{ts}.{suffix}"
    mode = "wb" if isinstance(payload, bytes) else "w"
    enc = None if isinstance(payload, bytes) else "utf-8"
    with open(path, mode, encoding=enc) as f:
        f.write(payload)
    return path


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class SpiderError(Exception):
    def __init__(self, msg: str, *, url: str = "-") -> None:
        super().__init__(msg)
        self.url = url


class NetworkError(SpiderError): ...
class LoginRequiredError(SpiderError): ...
class AntiSpiderError(SpiderError): ...
class ParseError(SpiderError): ...


# ---------------------------------------------------------------------------
# 数据模型（与主脚本 run_cpu_crawl_pw.py 字段兼容，便于 viewer.html 加载）
# ---------------------------------------------------------------------------

@dataclass
class Product:
    platform: str = "jd"
    item_id: str = ""
    title: str = ""
    url: str = ""
    current_price: float | None = None
    origin_price: float | None = None
    shop_name: str | None = None
    location: str | None = None
    image_url: str | None = None
    is_second_hand: bool = False
    # 详情页模式才填
    comment_count: int | None = None
    sku_spec: str | None = None


@dataclass
class SearchResult:
    platform: str
    keyword: str
    mode: str
    pages_requested: int
    success: bool
    products: list[Product] = field(default_factory=list)
    error: str | None = None
    error_type: str | None = None

    @property
    def count(self) -> int:
        return len(self.products)


# ---------------------------------------------------------------------------
# 节流常量（保护账号，不要改得比这更激进）
# ---------------------------------------------------------------------------

LIST_PAGE_DELAY_SEC = 8.0         # 列表页翻页间隔
LIST_PAGE_JITTER_SEC = 3.0        # 抖动 ± 3s，实际 5~11s
DETAIL_PAGE_DELAY_SEC = 4.0       # 详情页之间
DETAIL_PAGE_JITTER_SEC = 2.0
KEYWORD_DELAY_SEC = 45.0          # 批量模式下关键词间
KEYWORD_JITTER_SEC = 15.0
HOME_WARMUP_SEC = 5.0             # 首页暖场
SCROLL_TIMES = 4                  # 列表页触发懒加载滚动次数
CAPTCHA_WAIT_SEC = 300


def _sleep_jitter(base: float, jitter: float, reason: str = "") -> None:
    """带抖动的 sleep，避免规律性请求被识别。"""
    wait = base + random.uniform(-jitter, jitter)
    wait = max(1.0, wait)
    if reason:
        log.info(f"节流 {wait:.1f}s（{reason}）")
    time.sleep(wait)


# ---------------------------------------------------------------------------
# 价格/解析工具
# ---------------------------------------------------------------------------

def _parse_price(text: str | None) -> float | None:
    if not text:
        return None
    s = str(text).replace(",", "").replace("¥", "").replace("￥", "").strip()
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _clean_text(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def _extract_sku_from_url(url: str) -> str | None:
    """从 https://item.jd.com/100031035610.html 抽 100031035610。"""
    m = re.search(r"item\.jd\.com/(\d+)\.html", url or "")
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# 风控检测
# ---------------------------------------------------------------------------

_RISK_URL_PATTERNS = [
    ("risk_handler", "京东 risk_handler 风控"),
    ("privatedomain/risk", "京东 privatedomain 风控"),
    ("from=pc_search_sd", "JD 假响应重定向（账号/IP 已被标记）"),
    ("passport.jd.com/new/login", "跳转到登录页，cookie 失效"),
]


def _detect_risk(current_url: str) -> str | None:
    url_lower = (current_url or "").lower()
    for pat, label in _RISK_URL_PATTERNS:
        if pat.lower() in url_lower:
            return f"{label}（{pat}）"
    return None


def _raise_for_risk(current_url: str) -> None:
    """按 URL 风险类型抛出对应 SpiderError（LoginRequired vs AntiSpider）。"""
    risk = _detect_risk(current_url)
    if not risk:
        return
    url_lower = (current_url or "").lower()
    if "passport.jd.com/new/login" in url_lower:
        raise LoginRequiredError(f"命中登录页：{risk}", url=current_url)
    raise AntiSpiderError(f"命中风控：{risk}", url=current_url)


# ---------------------------------------------------------------------------
# 浏览器接管 / 启动
# ---------------------------------------------------------------------------

def open_chromium(
    *,
    takeover_port: int | None,
    user_data_dir: str | None,
    headless: bool,
) -> ChromiumPage:
    """打开 Chromium。优先接管 takeover_port 上已运行的 Chrome，否则自启。"""
    co = ChromiumOptions()
    if takeover_port:
        co.set_local_port(takeover_port)
        log.info(f"尝试接管 127.0.0.1:{takeover_port} 上的 Chrome")
    if user_data_dir:
        co.set_user_data_path(user_data_dir)
        log.info(f"使用用户数据目录 {user_data_dir}")
    if headless:
        co.headless(True)
    # 反检测：DrissionPage 默认已处理 webdriver 等属性
    co.set_argument("--disable-blink-features=AutomationControlled")
    try:
        page = ChromiumPage(co)
    except Exception as e:
        raise NetworkError(
            f"打开 Chromium 失败：{e}。若用接管模式，请确认 Chrome 已在 "
            f"--remote-debugging-port={takeover_port} 启动并保持开启"
        ) from e
    log.info(f"✓ Chromium 就绪，当前 UA={page.user_agent[:100] if page.user_agent else '?'}")
    return page


# ---------------------------------------------------------------------------
# 弹窗关闭
# ---------------------------------------------------------------------------

_POPUP_SELECTORS = [
    "css:[aria-label*='关闭']",
    "css:[aria-label*='close' i]",
    "css:.J-close",
    "css:.J-closeBtn",
    "css:.dialog-close",
    "css:.modal-close",
    "css:.popup-close",
    "css:.close-btn",
    "css:button.close",
    "css:i.close",
    "css:.icon-close",
    "css:div[class*='closeIcon']",
    "css:div[class*='closeBtn']",
]


def dismiss_popups(page: ChromiumPage, *, max_attempts: int = 3) -> int:
    """关闭常见弹窗。DrissionPage 的 ele() 找不到会返回 None（不像 Playwright 抛）。"""
    closed = 0
    try:
        page.actions.key_down("escape").key_up("escape")
        time.sleep(0.2)
    except Exception:
        pass
    for _ in range(max_attempts):
        hit = False
        for sel in _POPUP_SELECTORS:
            try:
                el = page.ele(sel, timeout=0.5)
                if not el:
                    continue
                # 可见性粗检：有尺寸
                rect = el.rect.size if hasattr(el.rect, "size") else None
                if rect and rect[0] > 5 and rect[1] > 5:
                    el.click(by_js=True)
                    log.info(f"关闭弹窗 {sel}")
                    closed += 1
                    hit = True
                    time.sleep(0.4)
                    break
            except Exception:
                continue
        if not hit:
            break
    return closed


# ---------------------------------------------------------------------------
# 登录检测
# ---------------------------------------------------------------------------

def verify_account(page: ChromiumPage) -> tuple[bool, str]:
    """从当前浏览器 cookies 看是否登录。

    returns (is_logged_in, 人读信息)。不强制：JD 可匿名搜索。
    """
    try:
        cookies = {c["name"]: c.get("value", "") for c in page.cookies()}
    except Exception as e:
        return False, f"读取 cookies 失败：{e}"
    pin = cookies.get("pin") or cookies.get("pinId")
    thor = cookies.get("thor")
    if pin or thor:
        from urllib.parse import unquote
        try:
            pin_dec = unquote(pin) if pin else None
        except Exception:
            pin_dec = pin
        return True, f"已登录 pin={pin_dec!r}"
    return False, "未登录（JD 匿名也能搜，继续）"


# ---------------------------------------------------------------------------
# JD 首页 form 搜索
# ---------------------------------------------------------------------------

# 按优先级试搜索框选择器（你给过的真实 HTML：input.jd_pc_search_bar_react_search_input aria-label='搜索'）
_JD_SEARCH_INPUT_SELS = [
    "css:input#key",
    "css:input[name='keyword']",
    "css:input[aria-label*='搜索']",
    "css:input[class*='search_input']",
    "css:input[placeholder*='搜索']",
]
_JD_SEARCH_BTN_SELS = [
    "css:button.jd_pc_search_bar_react_search_btn",
    "css:form#search button.button",
    "css:button[type='submit'][class*='search']",
    "css:.button[type='submit']",
]


def _first_visible(page: ChromiumPage, selectors: list[str]):
    """返回第一个能 ele 命中的元素，没找到返回 None。"""
    for sel in selectors:
        try:
            el = page.ele(sel, timeout=0.8)
            if el:
                return el, sel
        except Exception:
            continue
    return None, None


def jd_search_from_home(page: ChromiumPage, keyword: str) -> None:
    """从 JD 首页搜索框走 form 提交。失败抛 ParseError，调用方做 fallback。"""
    el, sel = _first_visible(page, _JD_SEARCH_INPUT_SELS)
    if not el:
        raise ParseError(
            f"JD 首页未找到搜索框（试过 {_JD_SEARCH_INPUT_SELS}）",
            url=page.url,
        )
    log.info(f"搜索框命中 {sel}")
    try:
        el.clear()
        # DrissionPage 的 input() 支持模拟打字（更像人）
        el.input(keyword, clear=True)
        time.sleep(0.3)
    except Exception as e:
        raise ParseError(f"搜索框输入失败：{e}", url=page.url) from e

    btn, btn_sel = _first_visible(page, _JD_SEARCH_BTN_SELS)
    if btn:
        log.info(f"搜索按钮命中 {btn_sel}")
        try:
            btn.click()
        except Exception as e:
            log.warning(f"点击搜索按钮失败（{e}），尝试回车")
            el.input("\n")
    else:
        log.info("未找到搜索按钮，用回车提交")
        el.input("\n")

    # 等跳转
    try:
        page.wait.load_start(timeout=10)
        page.wait.doc_loaded(timeout=20)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 列表页抽取（模式 2 · 默认）
# ---------------------------------------------------------------------------

# 列表页商品容器选择器（按优先级），URL 模式匹配最稳
_LIST_ITEM_SELS = [
    "css:li.gl-item[data-sku]",
    "css:li[data-sku]",
    "css:div[data-sku]",
    "css:a[href*='item.jd.com/']",
]


def parse_list_page(page: ChromiumPage, keyword: str, keyword_tokens: list[str]) -> list[Product]:
    """从 JD 搜索结果列表页抽取商品。按 URL 模式去重。"""
    # 滚动触发懒加载
    for i in range(SCROLL_TIMES):
        try:
            page.scroll.to_bottom()
        except Exception:
            pass
        time.sleep(random.uniform(1.2, 2.2))

    # DrissionPage run_js 期望可执行 JS 语句（含 return），不是箭头函数字面量
    raw = page.run_js(r"""
        const anchors = document.querySelectorAll("a[href*='item.jd.com/']");
        const seen = new Set();
        const out = [];
        anchors.forEach(a => {
            const m = (a.href || '').match(/item\.jd\.com\/(\d+)\.html/);
            if (!m) return;
            const sku = m[1];
            if (seen.has(sku)) return;
            seen.add(sku);

            // 往上找含价格（¥）的卡片容器
            let card = a;
            for (let i = 0; i < 10 && card.parentElement; i++) {
                card = card.parentElement;
                if ((card.innerText || '').includes('¥')) break;
            }
            const text = (card.innerText || '').trim();
            const img = card.querySelector('img');
            let imgUrl = null;
            if (img) {
                imgUrl = img.getAttribute('src')
                      || img.getAttribute('data-lazy-img')
                      || img.getAttribute('data-img')
                      || img.getAttribute('data-src');
                if (imgUrl && imgUrl.startsWith('//')) imgUrl = 'https:' + imgUrl;
            }
            out.push({
                sku, href: a.href,
                text: text.slice(0, 600),
                img: imgUrl,
            });
        });
        return out;
    """)

    if not raw:
        return []

    products: list[Product] = []
    seen: set[str] = set()
    for it in raw:
        sku = it.get("sku")
        if not sku or sku in seen:
            continue
        text = it.get("text") or ""
        # 关键词过滤（兼容 HTML 里的标签）
        lower = text.lower()
        if keyword_tokens and not any(t in lower for t in keyword_tokens):
            continue

        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        price_line = next((ln for ln in lines if "¥" in ln or "￥" in ln), "")
        title = next(
            (ln for ln in lines if len(ln) > 6 and "¥" not in ln and "￥" not in ln),
            "",
        )
        shop = None
        for ln in reversed(lines):
            if len(ln) < 30 and "¥" not in ln and "￥" not in ln and ln != title:
                shop = ln
                break

        seen.add(sku)
        products.append(
            Product(
                item_id=str(sku),
                title=_clean_text(title)[:200],
                url=f"https://item.jd.com/{sku}.html",
                current_price=_parse_price(price_line),
                shop_name=shop,
                image_url=it.get("img"),
            )
        )
    log.info(f"列表页抽取 {len(products)} 条（关键词 token={keyword_tokens}）")
    return products


# ---------------------------------------------------------------------------
# 详情页抽取（模式 1 · 慢速详细）
# ---------------------------------------------------------------------------

def parse_detail_page(page: ChromiumPage, sku: str) -> dict[str, Any]:
    """进详情页 https://item.jd.com/{sku}.html 抽精细字段。

    返回 dict（merge 到 Product 上），失败返回空 dict。
    """
    url = f"https://item.jd.com/{sku}.html"
    try:
        page.get(url, timeout=20)
    except Exception as e:
        log.warning(f"详情页 goto 失败 sku={sku}: {e}")
        return {}

    risk = _detect_risk(page.url)
    if risk:
        log.warning(f"详情页命中风控 sku={sku}: {risk}")
        return {}

    out: dict[str, Any] = {}
    # 价格：现价 + 划线价
    for sel in ("css:.summary-price-wrap .price",
                "css:.price.J-p-" + sku,
                "css:.p-price .price"):
        el = page.ele(sel, timeout=1.5)
        if el and el.text:
            out["current_price"] = _parse_price(el.text)
            break
    for sel in ("css:.p-price-plus .origin",
                "css:del.origin-price",
                "css:.summary-price .origin"):
        el = page.ele(sel, timeout=0.5)
        if el and el.text:
            out["origin_price"] = _parse_price(el.text)
            break

    # 标题
    for sel in ("css:.sku-name", "css:h1.itemName", "css:#name h1"):
        el = page.ele(sel, timeout=1.0)
        if el and el.text:
            out["title"] = _clean_text(el.text)[:300]
            break

    # 店铺名
    for sel in ("css:.name .shopname", "css:.shop-name a", "css:#popbox .mt a"):
        el = page.ele(sel, timeout=0.5)
        if el and el.text:
            out["shop_name"] = _clean_text(el.text)
            break

    # 图片（主图）
    for sel in ("css:#spec-img", "css:.spec-items img", "css:#preview img"):
        el = page.ele(sel, timeout=0.5)
        if el:
            src = el.attr("src") or el.attr("data-origin") or ""
            if src.startswith("//"):
                src = "https:" + src
            if src:
                out["image_url"] = src
                break

    # 评论数
    for sel in ("css:#comment-count .count",
                "css:.comment-count a em",
                "css:.comments-count"):
        el = page.ele(sel, timeout=0.5)
        if el and el.text:
            m = re.search(r"[\d.]+", el.text.replace(",", ""))
            if m:
                try:
                    n = float(m.group(0))
                    if "万" in el.text:
                        n *= 10000
                    out["comment_count"] = int(n)
                except ValueError:
                    pass
            break

    # SKU 规格（默认选中的）
    try:
        specs = page.eles("css:.p-choose .item.selected", timeout=0.5)
        if specs:
            out["sku_spec"] = " / ".join(_clean_text(s.text) for s in specs if s.text)[:200]
    except Exception:
        pass

    return out


# ---------------------------------------------------------------------------
# 翻页
# ---------------------------------------------------------------------------

def jd_goto_page(page: ChromiumPage, keyword: str, page_num: int) -> None:
    """直接 goto 下一页 URL。JD 翻页用奇数 page=1,3,5（每页 60 = 2 × 30 异步）。"""
    url = "https://search.jd.com/Search?" + urlencode(
        {"keyword": keyword, "enc": "utf-8", "page": page_num}
    )
    log.info(f"翻页 → {url}")
    try:
        page.get(url, timeout=20)
    except Exception as e:
        raise NetworkError(f"翻页 goto 失败：{e}", url=url) from e


# ---------------------------------------------------------------------------
# 主爬取函数
# ---------------------------------------------------------------------------

def _keyword_tokens(keyword: str) -> list[str]:
    _STRIP = "\"'""''（）()[]【】{}"
    return [
        t.strip(_STRIP).lower()
        for t in re.split(r"[\s\-_/]+", keyword)
        if len(t.strip(_STRIP)) >= 2
    ]


def crawl_jd(
    page: ChromiumPage,
    keyword: str,
    *,
    max_pages: int,
    mode: str,
    headed: bool,
) -> SearchResult:
    result = SearchResult(
        platform="jd",
        keyword=keyword,
        mode=mode,
        pages_requested=max_pages,
        success=False,
    )
    tokens = _keyword_tokens(keyword)

    try:
        # Step 0 首页暖场
        log.info(f"[{keyword}] → 首页暖场")
        try:
            page.get("https://www.jd.com/", timeout=20)
        except Exception as e:
            raise NetworkError(f"首页 goto 失败：{e}", url="https://www.jd.com/") from e
        _raise_for_risk(page.url)

        time.sleep(HOME_WARMUP_SEC)
        dismiss_popups(page)

        # Step 1 form 搜索
        try:
            jd_search_from_home(page, keyword)
        except ParseError as e:
            log.warning(f"form 提交失败（{e}），fallback 直接 goto 搜索 URL")

        # 验证是否真到搜索页
        try:
            _raise_for_risk(page.url)
        except SpiderError:
            dump_debug(f"after_search_{keyword}", page.html)
            raise

        if "search.jd.com/search" not in page.url.lower():
            # fallback goto
            fallback = "https://search.jd.com/Search?" + urlencode(
                {"keyword": keyword, "enc": "utf-8", "page": 1}
            )
            log.warning(f"form 未到搜索页，fallback → {fallback}")
            page.get(fallback, timeout=20)
            _raise_for_risk(page.url)

        # Step 2 循环翻页抽取
        all_products: dict[str, Product] = {}
        for i in range(max_pages):
            pnum = i * 2 + 1  # 1,3,5...
            if i > 0:
                jd_goto_page(page, keyword, pnum)
                try:
                    _raise_for_risk(page.url)
                except SpiderError:
                    dump_debug(f"list_page_{keyword}_{pnum}", page.html)
                    raise

            products = parse_list_page(page, keyword, tokens)
            for p in products:
                if p.item_id not in all_products:
                    all_products[p.item_id] = p

            log.info(
                f"[{keyword}] page {i + 1}/{max_pages}（JD page={pnum}）"
                f" 本页 {len(products)}，累计 {len(all_products)}"
            )

            if i < max_pages - 1:
                _sleep_jitter(LIST_PAGE_DELAY_SEC, LIST_PAGE_JITTER_SEC, "翻页节流")

        # Step 3（可选）详情页补充
        if mode == "detail" and all_products:
            log.info(f"[{keyword}] 进入详情页模式，将对 {len(all_products)} 条商品补充字段")
            enriched = 0
            for idx, (sku, prod) in enumerate(all_products.items(), 1):
                try:
                    detail = parse_detail_page(page, sku)
                    if detail:
                        for k, v in detail.items():
                            if v is not None:
                                setattr(prod, k, v)
                        enriched += 1
                    if idx % 5 == 0:
                        log.info(f"[{keyword}] 详情页进度 {idx}/{len(all_products)}，已补充 {enriched}")
                    _sleep_jitter(
                        DETAIL_PAGE_DELAY_SEC, DETAIL_PAGE_JITTER_SEC, "详情页节流"
                    )
                except Exception as e:
                    log.warning(f"详情页 sku={sku} 处理异常（跳过）：{e}")
            log.info(f"[{keyword}] 详情页补充完成 {enriched}/{len(all_products)}")

        result.products = list(all_products.values())
        result.success = True

    except SpiderError as e:
        result.error = str(e)
        result.error_type = type(e).__name__
        log.error(f"[{keyword}] {type(e).__name__}: {e}")
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        result.error_type = "UnknownError"
        log.error(f"[{keyword}] 未捕获异常:\n{traceback.format_exc()}")

    return result


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def write_outputs(results: dict[str, SearchResult], mode: str) -> None:
    """JSON + 两个 CSV。结构与 run_cpu_crawl_pw.py 一致，兼容 viewer.html。"""
    ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    # JSON
    out_json = LOG_DIR / f"jd_drission_{ts_tag}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(
            {
                "tool": "run_jd_drission.py",
                "mode": mode,
                "keywords": list(results.keys()),
                "platforms": ["jd"],
                "crawled_at": datetime.now().isoformat(),
                "results": {
                    kw: {
                        "jd": {
                            "success": r.success,
                            "pages_requested": r.pages_requested,
                            "mode": r.mode,
                            "count": r.count,
                            "error": r.error,
                            "error_type": r.error_type,
                            "products": [asdict(p) for p in r.products],
                        }
                    }
                    for kw, r in results.items()
                },
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # products.csv
    product_cols = [
        "keyword", "platform", "item_id", "title",
        "current_price", "origin_price", "shop_name", "location",
        "is_second_hand", "url", "image_url",
        "comment_count", "sku_spec", "crawled_at",
    ]
    out_products = LOG_DIR / f"jd_drission_{ts_tag}_products.csv"
    with open(out_products, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=product_cols)
        w.writeheader()
        for kw, r in results.items():
            for p in r.products:
                d = asdict(p)
                w.writerow({
                    "keyword": kw,
                    "platform": "jd",
                    "item_id": d.get("item_id", ""),
                    "title": d.get("title", ""),
                    "current_price": d.get("current_price", "") or "",
                    "origin_price": d.get("origin_price", "") or "",
                    "shop_name": d.get("shop_name", "") or "",
                    "location": d.get("location", "") or "",
                    "is_second_hand": d.get("is_second_hand", False),
                    "url": d.get("url", ""),
                    "image_url": d.get("image_url", "") or "",
                    "comment_count": d.get("comment_count", "") or "",
                    "sku_spec": d.get("sku_spec", "") or "",
                    "crawled_at": datetime.now().isoformat(timespec="seconds"),
                })

    # summary.csv
    summary_cols = ["keyword", "platform", "success", "count", "mode", "pages_requested", "error_type", "error"]
    out_summary = LOG_DIR / f"jd_drission_{ts_tag}_summary.csv"
    with open(out_summary, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=summary_cols)
        w.writeheader()
        for kw, r in results.items():
            w.writerow({
                "keyword": kw,
                "platform": "jd",
                "success": "Y" if r.success else "N",
                "count": r.count,
                "mode": r.mode,
                "pages_requested": r.pages_requested,
                "error_type": r.error_type or "",
                "error": (r.error or "")[:500],
            })

    print(f"\n详细 JSON 报告：{out_json}")
    print(f"CSV · 商品明细：{out_products}")
    print(f"CSV · 任务摘要：{out_summary}")


def print_console_summary(results: dict[str, SearchResult]) -> None:
    print(f"\n{'=' * 72}")
    print(f"京东 DrissionPage 爬取 · {len(results)} 关键词")
    print(f"爬取时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 72}")
    for kw, r in results.items():
        status = "OK" if r.success else "FAIL"
        print(
            f"\n[{status}] '{kw}' (mode={r.mode}) 商品 {r.count} 条"
            + (f"  错误={r.error_type}" if not r.success else "")
        )
        if not r.success and r.error:
            print(f"   原因: {r.error[:160]}")
        shown = sorted(
            r.products,
            key=lambda p: (p.current_price is None, p.current_price or 1e9),
        )[:5]
        for i, p in enumerate(shown, 1):
            price = f"¥{p.current_price:.2f}" if p.current_price else "N/A"
            print(f"   {i}. {price:10s}  {p.title[:55]}")
        if r.count > 5:
            print(f"   ... 另有 {r.count - 5} 条（见 CSV/JSON）")


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _parse_keywords(args: argparse.Namespace) -> list[str]:
    if args.keywords_file:
        p = Path(args.keywords_file)
        if not p.exists():
            raise FileNotFoundError(f"--keywords-file 找不到 {p}")
        return [
            s.strip()
            for s in p.read_text(encoding="utf-8").splitlines()
            if s.strip() and not s.startswith("#")
        ]
    if args.keywords:
        return [k.strip() for k in args.keywords.split(",") if k.strip()]
    return [args.keyword]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="京东商品爬虫 · DrissionPage 独立版（接管真 Chrome，反检测更强）"
    )
    parser.add_argument("keyword", nargs="?", default="i5-12400F",
                        help="单关键词；批量用 --keywords 或 --keywords-file")
    parser.add_argument("--keywords", help='批量：逗号分隔，如 "i5-12400F,R7 7800X3D"')
    parser.add_argument("--keywords-file", help="批量：每行一个关键词的文本文件")
    parser.add_argument("--pages", type=int, default=2, help="每关键词抓多少页（默认 2）")
    parser.add_argument(
        "--mode", choices=["list", "detail"], default="list",
        help="list（默认，仅列表页抓 URL/价格/标题）或 detail（进每个详情页抽精细字段，慢 10x）",
    )
    parser.add_argument(
        "--port", type=int, default=9222,
        help="接管已打开的 Chrome 调试端口（默认 9222；-1 关闭接管走自启）",
    )
    parser.add_argument("--no-takeover", action="store_true",
                        help="不接管，让 DrissionPage 自己启动 Chromium")
    parser.add_argument(
        "--user-data-dir",
        default=None,
        help='自启模式下的用户数据目录（如 "E:\\chrome_jd_crawler"）',
    )
    parser.add_argument("--headless", action="store_true",
                        help="无头模式（JD 反爬强时不建议）")
    args = parser.parse_args()

    try:
        keywords = _parse_keywords(args)
    except FileNotFoundError as e:
        log.error(str(e))
        return 2

    keywords = [kw.strip() for kw in keywords if kw and kw.strip()]
    if not keywords:
        log.error("未提供有效关键词。请使用非空 keyword / --keywords / --keywords-file")
        return 2
    if args.pages < 1:
        log.error("--pages 必须 >= 1")
        return 2

    log.info(
        f"DrissionPage JD 爬虫 · {len(keywords)} 关键词 × {args.pages} 页 · mode={args.mode}"
    )
    log.info(f"关键词: {keywords}")

    takeover = None if args.no_takeover or args.port < 0 else args.port
    try:
        page = open_chromium(
            takeover_port=takeover,
            user_data_dir=args.user_data_dir,
            headless=args.headless,
        )
    except SpiderError as e:
        log.error(f"浏览器启动失败：{e}")
        return 2

    ok, detail = verify_account(page)
    log.info(f"账号状态：{detail}")

    results: dict[str, SearchResult] = {}
    try:
        for idx, kw in enumerate(keywords):
            if idx > 0:
                _sleep_jitter(KEYWORD_DELAY_SEC, KEYWORD_JITTER_SEC, "关键词节流")
            log.info(f"========== 关键词 {idx + 1}/{len(keywords)}: {kw!r} ==========")
            results[kw] = crawl_jd(
                page, kw,
                max_pages=args.pages,
                mode=args.mode,
                headed=not args.headless,
            )
    finally:
        # 接管模式不 quit（用户的 Chrome 不能替他关）；自启模式才 quit
        if takeover is None:
            try:
                page.quit()
            except Exception:
                pass

    print_console_summary(results)
    write_outputs(results, args.mode)
    return 0 if any(r.success for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
