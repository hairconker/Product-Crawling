#!/usr/bin/env python3
"""CPU 价格爬虫 · Playwright 版（SPA + 翻页 + 去重 + mtop 响应拦截）

用法：
    python run_cpu_crawl_pw.py                          # 默认 i5-12400F, 3 页/平台
    python run_cpu_crawl_pw.py "i9-14900K" --pages 5
    python run_cpu_crawl_pw.py "R7 7800X3D" --only jd --pages 3
    python run_cpu_crawl_pw.py "i5-12400F" --headed    # 显示浏览器窗口（调试用）

前置：
    1. pip install playwright
    2. python -m playwright install chromium
    3. python scripts\\login_helper.py all  （扫码登录所有平台）

行为：
    * 自动加载 state/{platform}_state.json 登录态
    * 京东：SSR/s_new.php + DOM 抽取（自写）
    * 闲鱼：page.on('response') 拦截 mtop.idlemtopsearch.pc.search JSON
    * 淘宝：首页搜索框输入 + 拦截 mtop 搜索响应（绕过 rgv587 硬封的 s.taobao.com 搜索 URL）
    * 任意平台失败不中断其他平台；所有异常记入 logs/error.log

外部代码参考（按 github-reuse skill 规范）：
    * vendor/superboyyy-xianyu-spider/spider.py (MIT, 4cf59de2a744)
        —— 闲鱼 mtop 响应拦截模式（crawl_xianyu_pw）
    * vendor/cclient-tmallSign/routes/tmall.js (Apache-2.0, 505bbfa432cc)
        —— 淘宝 mtop sign 公式（mtop_sign）
    * vendor/xinlingqudongX-TSDK/TSDK/api/taobao/h5.py (归档, e201ad2fc578)
        —— 淘宝 mtop URL 模板
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
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
    from playwright.sync_api import (
        sync_playwright,
        Page,
        BrowserContext,
        TimeoutError as PWTimeout,
    )
except ImportError:
    print("\n[ERROR] 未安装 playwright。请先执行：\n")
    print("    pip install playwright")
    print("    python -m playwright install chromium\n")
    sys.exit(1)


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
DEBUG_DIR = LOG_DIR / "debug"
STATE_DIR = ROOT / "state"
for d in (LOG_DIR, DEBUG_DIR, STATE_DIR):
    d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def _setup_logger() -> logging.Logger:
    lg = logging.getLogger("cpu_crawler_pw")
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
    fh = logging.FileHandler(LOG_DIR / f"crawl_pw_{today}.log", encoding="utf-8")
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


def dump_debug(platform: str, payload: str | bytes, suffix: str = "html") -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = DEBUG_DIR / f"{platform}_{ts}.{suffix}"
    mode = "wb" if isinstance(payload, bytes) else "w"
    enc = None if isinstance(payload, bytes) else "utf-8"
    with open(path, mode, encoding=enc) as f:
        f.write(payload)
    return path


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class SpiderError(Exception):
    def __init__(self, msg: str, *, platform: str = "-", url: str = "-") -> None:
        super().__init__(msg)
        self.platform = platform
        self.url = url

    def __str__(self) -> str:
        return f"{self.args[0]} | platform={self.platform} | url={self.url}"


class NetworkError(SpiderError): ...
class LoginRequiredError(SpiderError): ...
class AntiSpiderError(SpiderError): ...
class ParseError(SpiderError): ...


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------

@dataclass
class Product:
    platform: str
    item_id: str
    title: str
    url: str
    current_price: float | None = None
    origin_price: float | None = None
    shop_name: str | None = None
    location: str | None = None
    image_url: str | None = None
    is_second_hand: bool = False


@dataclass
class SearchResult:
    platform: str
    keyword: str
    pages_requested: int
    success: bool
    products: list[Product] = field(default_factory=list)
    error: str | None = None
    error_type: str | None = None

    @property
    def count(self) -> int:
        return len(self.products)


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)

# 节流常量（保护账号 / 避免封号）
# 所有滚动、翻页、平台切换的间隔都基于这些数值，不要在业务代码里硬编码
SCROLL_DELAY_MS = 2200           # 每次滚动后等待
PAGE_DELAY_MS = 4500              # 同平台内翻页间隔
PLATFORM_DELAY_SEC = 6.0          # 跨平台间隔
CAPTCHA_WAIT_SEC = 300            # 滑块/验证码出现时最多等用户 5 分钟


def _parse_price(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\d+(?:\.\d+)?", text.replace(",", ""))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _snapshot_page(page: Page, platform: str, tag: str) -> Path | None:
    """截图 + URL + title 落盘。让爬虫'睁眼'：每次访问后能看到真正看到了什么。

    产物：
        logs/debug/{platform}_{tag}_{ts}.png    - 全页截图
        logs/debug/{platform}_{tag}_{ts}.meta.json - 当前 URL / title / 视口
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    base = DEBUG_DIR / f"{platform}_{tag}_{ts}"
    png = base.with_suffix(".png")
    meta = base.with_suffix(".meta.json")
    try:
        page.screenshot(path=str(png), full_page=True)
    except Exception as e:
        log.warning(f"[{platform}] screenshot 失败: {e}")
        return None

    info: dict[str, Any] = {
        "url": page.url,
        "ts": ts,
    }
    try:
        info["title"] = page.title()
    except Exception:
        info["title"] = None
    try:
        info["viewport"] = page.viewport_size
    except Exception:
        info["viewport"] = None

    with open(meta, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    log.info(
        f"[{platform}] 📸 {tag} → {png.name}  title={info['title']!r}  url={info['url']}"
    )
    return png


# 风控响应特征：命中任一即视为被拦截
_RISK_PATTERNS: list[tuple[str, str]] = [
    ("rgv587_flag", "淘宝/阿里 rgv587 风控"),
    ("deny_h5.html", "阿里 deny_h5 punish 拒绝页"),
    ("/punish/", "阿里 punish 惩罚重定向"),
    ("risk_handler", "京东 risk_handler 风控"),
    ("privatedomain/risk", "京东 privatedomain 风控"),
    ("punish.html", "通用惩罚重定向"),
    ("X5Referer", "滑块前置页"),
]


def _detect_risk(page: Page) -> str | None:
    """检测风控 JSON/HTML 特征。没命中返回 None。"""
    try:
        content = page.content()[:3000]
    except Exception:
        return None
    cur_url = (page.url or "").lower()
    for pattern, label in _RISK_PATTERNS:
        if pattern.lower() in content.lower() or pattern.lower() in cur_url:
            return f"{label}（标记 {pattern!r}）"
    # 纯 JSON 响应而非 HTML（淘宝 deny 的典型）
    stripped = content.lstrip()
    if stripped.startswith("{") and "rgv587" not in stripped:
        # 纯 JSON 且前 500 字符里有典型风控字段
        if any(k in stripped for k in ['"punish"', '"deny"', '"code":"4', '"errorCode"']):
            return "平台返回纯 JSON（疑似风控拦截，非搜索结果 HTML）"
    return None


def _scroll_page(page: Page, times: int = 4, delay_ms: int | None = None) -> None:
    """多次滚到底部，触发 lazy load。delay_ms 默认取全局 SCROLL_DELAY_MS。"""
    wait = delay_ms if delay_ms is not None else SCROLL_DELAY_MS
    for _ in range(times):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        page.wait_for_timeout(wait)


# 阿里体系滑块/验证码常见 DOM 特征（淘宝、闲鱼共用）
_ALI_CAPTCHA_SELECTORS = [
    "#nc_1_wrapper",
    "#nc_1_n1z",
    "#nocaptcha",
    ".nc_wrapper",
    "[id^='baxia']",
    "div.J_MIDDLEWARE_FRAME_WIDGET",
]


def _has_captcha(page: Page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            if page.query_selector(sel):
                return True
        except Exception:
            continue
    return False


def _wait_for_captcha_pass(
    page: Page,
    platform: str,
    selectors: list[str],
    *,
    headed: bool,
    timeout_sec: int = CAPTCHA_WAIT_SEC,
) -> bool:
    """检测并等待用户手动通过滑块/验证码。

    返回：
        True  ——  原本没滑块，或滑块已被用户通过
        False ——  检测到滑块但在无头模式（用户无法操作）或超时
    """
    if not _has_captcha(page, selectors):
        return True

    if not headed:
        log.warning(
            f"[{platform}] 检测到滑块/验证码，但当前是无头模式，无法人工通过。"
            f"请加 --headed 重试"
        )
        return False

    log.warning(
        f"[{platform}] 检测到滑块/验证码，请在浏览器中手动完成（最多等 {timeout_sec}s）..."
    )
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_sec:
        try:
            page.wait_for_timeout(2500)
        except Exception:
            return False
        if not _has_captcha(page, selectors):
            log.info(f"[{platform}] ✓ 验证已通过，继续爬取")
            return True
    log.error(f"[{platform}] 滑块等待超时（{timeout_sec}s），放弃本平台")
    return False


def _wait_for_products(
    page: Page,
    platform: str,
    selectors: list[str],
    *,
    timeout_ms: int = 30000,
    min_count: int = 1,
) -> int:
    """基础设施：智能等待商品 DOM 就绪（替代硬编码 wait_for_timeout）。

    任一 selector 出现且命中 ≥ min_count 个元素即认为加载完成。
    返回命中元素数；超时返回 0（由调用方决定是否抛异常）。
    """
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_ms / 1000:
        for sel in selectors:
            try:
                count = len(page.query_selector_all(sel))
            except Exception:
                count = 0
            if count >= min_count:
                elapsed = time.monotonic() - t0
                log.info(
                    f"[{platform}] 商品 DOM 就绪：{count} 个节点（selector={sel!r}，等待 {elapsed:.1f}s）"
                )
                return count
        try:
            page.wait_for_timeout(800)
        except Exception:
            break
    log.warning(f"[{platform}] 商品 DOM 等待超时 {timeout_ms / 1000:.0f}s，selectors={selectors}")
    return 0


# 登录态检测：每平台的关键 cookie（有值即判为已登录）
_LOGIN_MARKERS: dict[str, list[str]] = {
    "jd": ["thor", "pin", "pinId"],
    "xianyu": ["unb", "_nk_", "tracknick"],
    "taobao": ["unb", "tracknick", "_nk_"],
}


def _login_is_valid(
    context: BrowserContext, platform: str
) -> tuple[bool, str, list[str]]:
    """检查 context 里是否包含该平台的登录标志 cookie。

    返回 (is_valid, 人类可读说明, 命中的 cookie 名列表)。
    仅做"存在且非空"检查，无法验证 cookie 是否被服务端接受。
    """
    markers = _LOGIN_MARKERS.get(platform, [])
    if not markers:
        return True, f"{platform} 未定义登录标志，跳过检查", []
    try:
        cookies = context.cookies()
    except Exception as e:
        return False, f"读取 cookies 失败: {e}", []
    hit = [
        c["name"]
        for c in cookies
        if c.get("name") in markers
        and (c.get("value") or "").strip()
        and (c.get("value") or "") not in ("0", "null", "undefined")
    ]
    if hit:
        return True, f"登录 cookie 就绪: {hit}", hit
    return (
        False,
        f"缺少登录 cookie（期望任一: {markers}，state 文件过期或未登录）",
        [],
    )


def _read_account_info(context: BrowserContext, platform: str) -> dict[str, str | None]:
    """从 cookies 抽取用户可读的身份字段（username / nickname / user_id）。

    - JD: `pin` cookie 含 URL-encoded 用户名；`pinId` 也可作 fallback
    - 闲鱼/淘宝: `tracknick` 为昵称（可能 unicode 转义），`unb` 为数字用户 ID
    """
    from urllib.parse import unquote

    def _safe_unquote(v: str | None) -> str | None:
        if not v:
            return None
        try:
            return unquote(v)
        except Exception:
            return v

    try:
        cookies = {c["name"]: c.get("value") or "" for c in context.cookies()}
    except Exception:
        cookies = {}
    info: dict[str, str | None] = {
        "username": None,
        "nickname": None,
        "user_id": None,
    }
    if platform == "jd":
        info["username"] = _safe_unquote(cookies.get("pin")) or None
        info["user_id"] = cookies.get("pinId") or None
    elif platform in ("xianyu", "taobao"):
        raw_nick = cookies.get("tracknick") or cookies.get("_nk_") or ""
        # tracknick 有时是 `\u6d4b\u8bd5` 形式的 unicode escape，尝试解出
        if raw_nick and "\\u" in raw_nick:
            try:
                decoded = raw_nick.encode("utf-8").decode("unicode_escape")
                info["nickname"] = decoded
            except Exception:
                info["nickname"] = _safe_unquote(raw_nick)
        else:
            info["nickname"] = _safe_unquote(raw_nick) or None
        info["user_id"] = cookies.get("unb") or None
    return info


def verify_account(
    context: BrowserContext, platform: str
) -> tuple[bool, str, dict[str, str | None]]:
    """综合账号检测：cookie 校验 + 身份字段读取。

    返回 (is_usable, 人类可读说明, account_info)。
    脚本会在每个 crawler 入口调用一次，测试脚本 main() 不用关心细节。
    """
    ok, detail, hit = _login_is_valid(context, platform)
    info = _read_account_info(context, platform)
    if not ok:
        return False, detail, info
    human = info.get("nickname") or info.get("username")
    if not human and info.get("user_id"):
        human = f"UID:{info['user_id']}"
    if not human:
        return False, f"登录 cookie 存在 {hit} 但无身份字段，cookie 可能已损坏", info
    return True, f"账号可用 [{platform}] {human}（markers={hit}）", info


def refresh_storage_state(
    context: BrowserContext, platform: str
) -> None:
    """把当前 context 的最新 cookies 回写到 state/{platform}_state.json。

    保持登录的核心机制：每次成功爬取后调用，cookies 在浏览器里被服务端刷新过
    （新的 sign/_m_h5_tk 等）即可被下次运行复用，延长 state 有效期。
    """
    state_path = STATE_DIR / f"{platform}_state.json"
    try:
        context.storage_state(path=str(state_path))
        log.info(f"[{platform}] 登录态已刷新回写 {state_path.name}")
    except Exception as e:
        log.warning(f"[{platform}] 刷新 storage_state 失败: {e}")


def _make_context(
    p, *, state_file: Path | None, mobile: bool = False
) -> BrowserContext:
    ua = UA_MOBILE if mobile else UA_DESKTOP
    viewport = {"width": 390, "height": 844} if mobile else {"width": 1366, "height": 900}
    browser = p.chromium.launch(headless=True)
    kwargs: dict[str, Any] = {"user_agent": ua, "viewport": viewport, "locale": "zh-CN"}
    if state_file and state_file.exists():
        kwargs["storage_state"] = str(state_file)
    else:
        log.warning(f"未找到登录态 {state_file}，将以匿名模式打开")
    return browser.new_context(**kwargs)


# ---------------------------------------------------------------------------
# 京东
# ---------------------------------------------------------------------------

# 京东搜索 XHR 特征（来自 HTML 内嵌的 api.m.jd.com 引用）
# JD 新版搜索页完全 CSR，商品列表由 api.m.jd.com 的 functionId=xxxSearch 返回
_JD_API_HOST = "api.m.jd.com"
_JD_SEARCH_HINTS = ["search", "Search", "wareSearch", "uniformSearch", "keyword"]


def _jd_extract_products(result_json: dict) -> list[Product]:
    """从 api.m.jd.com 搜索 JSON 递归抽商品。端点具体 schema 随 functionId 不同。"""
    products: list[Product] = []
    seen_ids: set[str] = set()
    _MAX_DEPTH = 200  # 防 RecursionError

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > _MAX_DEPTH:
            return
        if isinstance(node, dict):
            sku = (
                node.get("wareId")
                or node.get("skuId")
                or node.get("sku_id")
                or node.get("sku")
                or node.get("pid")
            )
            name = node.get("wname") or node.get("name") or node.get("title") or node.get("wareName")
            if sku and name and str(sku).isdigit() and str(sku) not in seen_ids:
                seen_ids.add(str(sku))
                price = (
                    _parse_price(str(node.get("jdPrice") or ""))
                    or _parse_price(str(node.get("price") or ""))
                    or _parse_price(str(node.get("priceShow") or ""))
                    or _parse_price(str(node.get("salePrice") or ""))
                )
                shop = (
                    node.get("shopName")
                    or node.get("venderName")
                    or node.get("shop", {}).get("name") if isinstance(node.get("shop"), dict) else None
                )
                img = (
                    node.get("imageUrl")
                    or node.get("imgUrl")
                    or node.get("image")
                    or node.get("pic")
                    or ""
                )
                if img and not str(img).startswith("http"):
                    img = "https:" + img if str(img).startswith("//") else f"https://img14.360buyimg.com/n1/{img}"
                products.append(
                    Product(
                        platform="jd",
                        item_id=str(sku),
                        title=str(name)[:200],
                        url=f"https://item.jd.com/{sku}.html",
                        current_price=price,
                        shop_name=shop if isinstance(shop, str) else None,
                        image_url=img or None,
                    )
                )
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                _walk(v, depth + 1)

    try:
        _walk(result_json)
    except RecursionError:
        log.warning("[jd] _walk 递归过深，已截断")
    return products


def crawl_jd_pw(
    context: BrowserContext,
    keyword: str,
    max_pages: int,
    *,
    headed: bool = False,
) -> list[Product]:
    """京东搜索：HTML 是骨架，商品走 api.m.jd.com XHR。

    之前基于 li[data-sku] DOM 抽取在新版 JD 已失效（HTML 确认无此节点）。
    改为 response 拦截模式：等任何命中 api.m.jd.com + search 关键词的 XHR 响应。
    """
    ok, detail, acct = verify_account(context, "jd")
    if ok:
        log.info(f"[jd] ✓ {detail}")
    else:
        log.warning(f"[jd] ⚠ {detail}（JD 可不登录搜索，继续尝试）账号信息={acct}")

    page = context.new_page()
    mtop_batches: list[dict] = []

    def _on_response(response: Any) -> None:
        try:
            url = response.url or ""
            if _JD_API_HOST in url and any(h in url for h in _JD_SEARCH_HINTS):
                try:
                    j = response.json()
                    mtop_batches.append(j)
                    log.info(f"[jd] 拦截到搜索响应 url={url[:150]}")
                except Exception as e:
                    log.warning(f"[jd] 响应 JSON 解析失败: {e} url={url[:120]}")
        except Exception:
            pass

    page.on("response", _on_response)
    seen: dict[str, Product] = {}
    try:
        for i in range(max_pages):
            pnum = i * 2 + 1  # JD 翻页 1,3,5...
            url = "https://search.jd.com/Search?" + urlencode(
                {"keyword": keyword, "enc": "utf-8", "page": pnum}
            )
            log.info(f"[jd] page {i + 1}/{max_pages} → {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except PWTimeout:
                log.warning(f"[jd] page {pnum} goto 超时，尝试继续")

            _snapshot_page(page, "jd", f"p{pnum}_after_goto")
            risk = _detect_risk(page)
            if risk:
                dump_debug("jd", page.content())
                raise AntiSpiderError(
                    f"JD 命中风控：{risk}。见 logs/debug/ 截图",
                    platform="jd", url=page.url,
                )
            if "login" in (page.url or "").lower() or "passport" in (page.url or ""):
                raise LoginRequiredError(
                    "JD 跳到登录页，请重新 login_helper.py jd",
                    platform="jd", url=page.url,
                )

            # 等搜索 XHR 响应 + 多次滚动（JD 的商品是分批异步加载）
            before = len(mtop_batches)
            t0 = time.monotonic()
            while time.monotonic() - t0 < 25:
                if len(mtop_batches) > before:
                    break
                page.wait_for_timeout(800)

            # 滚动触发更多批次（懒加载下半页 30 条）
            for _ in range(4):
                _scroll_page(page, times=1)

            page.wait_for_timeout(PAGE_DELAY_MS)

        if not mtop_batches:
            _snapshot_page(page, "jd", "no_api_response")
            dump_debug("jd", page.content())
            raise ParseError(
                f"未拦截到 {_JD_API_HOST} 搜索响应。可能 functionId 已变更。"
                f"hint patterns={_JD_SEARCH_HINTS}，见 logs/debug/",
                platform="jd", url=page.url,
            )

        # 统一解析所有批次
        for batch in mtop_batches:
            for prod in _jd_extract_products(batch):
                if prod.item_id not in seen:
                    seen[prod.item_id] = prod

        log.info(f"[jd] 拦截 {len(mtop_batches)} 批 XHR，去重后 {len(seen)} 条")
        if seen:
            refresh_storage_state(context, "jd")
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass
        page.close()
    return list(seen.values())


# ---------------------------------------------------------------------------
# 闲鱼
# ---------------------------------------------------------------------------

_XIANYU_MTOP_SEARCH_URL = "mtop.taobao.idlemtopsearch.pc.search"


def _safe_get(data: Any, *keys: Any, default: Any = None) -> Any:
    """逐层安全取嵌套 dict 值。"""
    for k in keys:
        try:
            data = data[k]
        except (KeyError, TypeError, IndexError):
            return default
    return data


def _parse_xianyu_price(price_parts: Any) -> float | None:
    """闲鱼 mtop 返回的 price 是 list of dict(text)；拼接后处理"万"单位。"""
    if not isinstance(price_parts, list):
        return None
    text = "".join(
        str(p.get("text", "")) for p in price_parts if isinstance(p, dict)
    )
    text = text.replace("当前价", "").replace("¥", "").replace(",", "").strip()
    if not text:
        return None
    if "万" in text:
        try:
            return float(text.replace("万", "")) * 10000
        except ValueError:
            return None
    try:
        return float(re.search(r"\d+(?:\.\d+)?", text).group(0))
    except (AttributeError, ValueError):
        return None


def _xianyu_extract_products(result_json: dict) -> list[Product]:
    """把闲鱼 mtop.idlemtopsearch.pc.search 的 JSON 响应转成 Product 列表。

    参考：vendor/superboyyy-xianyu-spider/spider.py::on_response (MIT, 4cf59de2)
    """
    products: list[Product] = []
    items = _safe_get(result_json, "data", "resultList", default=[]) or []
    for it in items:
        main = _safe_get(it, "data", "item", "main", "exContent", default={}) or {}
        click_args = _safe_get(
            it, "data", "item", "main", "clickParam", "args", default={}
        ) or {}
        item_id = (
            click_args.get("id")
            or _safe_get(it, "data", "item", "main", "itemId")
            or _safe_get(main, "itemId")
            or ""
        )
        if not item_id:
            continue
        raw_link = _safe_get(it, "data", "item", "main", "targetUrl", default="") or ""
        url = raw_link.replace("fleamarket://", "https://www.goofish.com/")
        pic = main.get("picUrl") or ""
        if pic and not pic.startswith("http"):
            pic = "https:" + pic
        products.append(
            Product(
                platform="xianyu",
                item_id=str(item_id),
                title=str(main.get("title") or "")[:200],
                url=url,
                current_price=_parse_xianyu_price(main.get("price")),
                shop_name=main.get("userNickName") or None,
                location=main.get("area") or None,
                image_url=pic or None,
                is_second_hand=True,
            )
        )
    return products


def crawl_xianyu_pw(
    context: BrowserContext,
    keyword: str,
    max_pages: int,
    *,
    headed: bool = False,
) -> list[Product]:
    """闲鱼搜索：走 mtop.idlemtopsearch.pc.search 响应拦截，而非 DOM 抽取。

    参考：vendor/superboyyy-xianyu-spider/spider.py (MIT, commit 4cf59de2a744)
      - 核心思路：page.on("response") 监听 h5api.m.goofish.com 的 mtop 搜索接口
      - 拿结构化 JSON 比 DOM 文本硬抽稳定得多
    本项目改造：async→sync、去掉 Tortoise ORM 入库、保留 _snapshot_page/_detect_risk。
    """
    ok, detail, acct = verify_account(context, "xianyu")
    if ok:
        log.info(f"[xianyu] ✓ {detail}")
    else:
        log.warning(f"[xianyu] ⚠ {detail}（建议 login_helper.py xianyu）账号信息={acct}")

    page = context.new_page()
    seen: dict[str, Product] = {}
    mtop_batches: list[dict] = []

    def _has_valid_mtop_batch() -> bool:
        return any(
            _safe_get(batch, "data", "resultList", default=[]) for batch in mtop_batches
        )

    def _on_response(response: Any) -> None:
        try:
            if _XIANYU_MTOP_SEARCH_URL in (response.url or ""):
                try:
                    j = response.json()
                    mtop_batches.append(j)
                    got = len(_safe_get(j, "data", "resultList", default=[]) or [])
                    log.info(f"[xianyu] 拦截到 mtop 响应 +{got} 条")
                except Exception as e:
                    log.warning(f"[xianyu] mtop 响应 JSON 解析失败: {e}")
        except Exception:
            pass

    page.on("response", _on_response)

    try:
        log.info("[xianyu] → https://www.goofish.com (搜索框输入模式)")
        try:
            page.goto("https://www.goofish.com", wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            log.warning("[xianyu] 首页 goto 超时")

        _snapshot_page(page, "xianyu", "after_home")
        risk = _detect_risk(page)
        if risk:
            dump_debug("xianyu", page.content())
            raise AntiSpiderError(
                f"闲鱼命中风控：{risk}", platform="xianyu", url=page.url,
            )

        # 关闭广告弹窗（语义选择器优先，class 子串兜底）
        for close_sel in (
            "[aria-label*='关闭']",
            "[role='button'][aria-label*='close' i]",
            "div[class*='closeIconBg']",
        ):
            try:
                page.wait_for_selector(close_sel, timeout=2000)
                page.click(close_sel)
                log.info(f"[xianyu] 关闭广告弹窗（{close_sel}）")
                break
            except PWTimeout:
                continue

        # 输入关键词并提交（触发 mtop 请求）。先 query_selector 探测，命中再 fill，避免 30s 空等
        search_input_selectors = (
            "input[class*='search-input']",  # 闲鱼当前确认命中
            "input[type='search']",
            "input[placeholder*='搜索']",
        )
        filled_sel = None
        for sel in search_input_selectors:
            try:
                if page.query_selector(sel):
                    page.fill(sel, keyword, timeout=3000)
                    filled_sel = sel
                    break
            except Exception as e:
                log.debug(f"[xianyu] 搜索框 {sel!r} 填入失败: {e}")
        try:
            if filled_sel:
                log.info(f"[xianyu] 搜索框命中 {filled_sel!r}")
                page.click("button[type='submit']", timeout=3000)
            else:
                raise RuntimeError("所有搜索框选择器均未命中")
        except Exception as e:
            # fallback：直接 goto 搜索 URL
            log.warning(f"[xianyu] 搜索框交互失败 ({e})，改为直接 URL 访问")
            page.goto(
                "https://www.goofish.com/search?" + urlencode({"q": keyword}),
                wait_until="domcontentloaded",
                timeout=30000,
            )

        # 等首次 mtop 响应或滑块
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20:
            if _has_valid_mtop_batch():
                break
            if _has_captcha(page, _ALI_CAPTCHA_SELECTORS):
                if not _wait_for_captcha_pass(
                    page, "xianyu", _ALI_CAPTCHA_SELECTORS, headed=headed
                ):
                    dump_debug("xianyu", page.content())
                    raise AntiSpiderError(
                        "闲鱼滑块未通过/无头模式", platform="xianyu", url=page.url,
                    )
            if "login" in (page.url or "").lower():
                raise LoginRequiredError(
                    "闲鱼跳登录", platform="xianyu", url=page.url,
                )
            page.wait_for_timeout(800)

        if not _has_valid_mtop_batch():
            _snapshot_page(page, "xianyu", "no_mtop_response")
            dump_debug("xianyu", page.content())
            raise ParseError(
                f"20s 内未拦截到 {_XIANYU_MTOP_SEARCH_URL} 响应；"
                f"可能 mtop 路径已变更，见 logs/debug/",
                platform="xianyu", url=page.url,
            )

        # 翻页：语义优先，class 子串兜底
        next_btn_selectors = (
            "[aria-label*='下一页']:not([disabled])",
            "button:has-text('下一页'):not([disabled])",
            "[class*='search-pagination-arrow-right']:not([disabled])",
        )
        for p_idx in range(1, max_pages):
            page.wait_for_timeout(PAGE_DELAY_MS)
            next_btn = None
            for sel in next_btn_selectors:
                try:
                    next_btn = page.query_selector(sel)
                    if next_btn:
                        break
                except Exception:
                    continue
            if not next_btn:
                log.info(f"[xianyu] 第 {p_idx} 页后无下一页按钮，停止翻页")
                break
            try:
                next_btn.click()
                log.info(f"[xianyu] 翻到第 {p_idx + 1} 页")
            except Exception as e:
                log.warning(f"[xianyu] 翻页点击失败: {e}")
                break
            # 等下一批 mtop 响应
            before = len(mtop_batches)
            t0 = time.monotonic()
            while time.monotonic() - t0 < 15 and len(mtop_batches) == before:
                page.wait_for_timeout(500)

        # 汇总所有批次解析
        for batch in mtop_batches:
            for prod in _xianyu_extract_products(batch):
                if prod.item_id not in seen:
                    seen[prod.item_id] = prod

        log.info(f"[xianyu] 拦截 {len(mtop_batches)} 批 mtop 响应，去重后 {len(seen)} 条")
        if seen:
            refresh_storage_state(context, "xianyu")
    finally:
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass
        page.close()
    return list(seen.values())


# ---------------------------------------------------------------------------
# 淘宝
# ---------------------------------------------------------------------------

# 淘宝 mtop 搜索接口常见路径（按命中优先级）。浏览器自然触发时路径会变，
# 这里用 substring 匹配，命中任一即采纳。
_TAOBAO_MTOP_PATTERNS = [
    "mtop.taobao.wsearch.appsearch",
    "mtop.relationrecommend.wirelessrecommend",
    "mtop.taobao.sale.wsearch",
    "mtop.taobao.search",
    "/h5/mtop.taobao.",   # 任意以 mtop.taobao. 开头
    "/h5/mtop.alimama.",  # 阿里妈妈系列（推荐/搜索混合）
    "h5api.m.taobao.com/h5/mtop.",  # 全站兜底
]
# 不强制 search 关键字（放宽）：很多搜索 api 名里不含 search，如 appsearch 只在函数名里


def mtop_sign(token: str, t_ms: str, app_key: str, data: str) -> str:
    """淘宝 mtop 请求签名 = md5(token & t & appKey & data)。

    参考：vendor/cclient-tmallSign/routes/tmall.js#L19-L189 (Apache-2.0, 505bbfa4)
        209 行 JS 其实是 MD5 实现，Python 一行等价。
    token: 取自 cookie `_m_h5_tk` 字段的 `_` 前部分（如 "abcdef_1234" 取 "abcdef"）。
    """
    return hashlib.md5(f"{token}&{t_ms}&{app_key}&{data}".encode()).hexdigest()


def _parse_taobao_price(text: Any) -> float | None:
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    s = str(text).replace(",", "").replace("¥", "").strip()
    m = re.search(r"\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _keyword_tokens(keyword: str) -> list[str]:
    """把 'i5-12400F' / 'R7 7800X3D' 拆成可匹配的小写 token（≥2 字符）。

    剥除引号 / 括号等字面噪声，避免命令行传入带引号时残留破坏匹配。
    """
    _STRIP_CHARS = "\"'“”‘’（）()[]【】{}"
    out: list[str] = []
    for t in re.split(r"[\s\-_/]+", keyword):
        cleaned = t.strip(_STRIP_CHARS)
        if len(cleaned) >= 2:
            out.append(cleaned.lower())
    return out


def _title_matches_keyword(title: str, tokens: list[str]) -> bool:
    """标题（去 HTML 标签后，小写）至少命中一个 token。"""
    if not tokens:
        return True
    clean = re.sub(r"<[^>]+>", "", title).lower()
    return any(t in clean for t in tokens)


def _taobao_extract_products(result_json: dict, keyword: str = "") -> list[Product]:
    """从淘宝 mtop 响应 JSON 抽商品，按关键词过滤首页推荐噪声。

    参考：vendor/xinlingqudongX-TSDK/TSDK/api/taobao/h5.py（mtop URL 模板约定）
    """
    products: list[Product] = []
    seen_ids: set[str] = set()
    tokens = _keyword_tokens(keyword)
    _MAX_DEPTH = 200  # 防 RecursionError

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > _MAX_DEPTH:
            return
        if isinstance(node, dict):
            # 识别典型商品节点：同时含 item_id 和 title 字段
            iid = (
                node.get("itemId")
                or node.get("item_id")
                or node.get("nid")
                or node.get("auctionId")
            )
            title = node.get("title") or node.get("raw_title")
            if iid and title and str(iid) not in seen_ids:
                # 关键词过滤：标题不含任一 token 就跳过（首页推荐噪声会在这里被挡掉）
                if not _title_matches_keyword(str(title), tokens):
                    for v in node.values():
                        _walk(v, depth + 1)
                    return
                seen_ids.add(str(iid))
                price = (
                    _parse_taobao_price(node.get("price"))
                    or _parse_taobao_price(node.get("view_price"))
                    or _parse_taobao_price(node.get("priceWap"))
                    or _parse_taobao_price(node.get("reservePrice"))
                )
                pic = (
                    node.get("pic_url")
                    or node.get("picUrl")
                    or node.get("pic")
                    or node.get("imgUrl")
                    or ""
                )
                if pic and not str(pic).startswith("http"):
                    pic = "https:" + pic
                shop = (
                    node.get("nick")
                    or node.get("shopTitle")
                    or node.get("sellerNick")
                )
                loc = node.get("item_loc") or node.get("location")
                products.append(
                    Product(
                        platform="taobao",
                        item_id=str(iid),
                        title=str(title)[:200],
                        url=f"https://item.taobao.com/item.htm?id={iid}",
                        current_price=price,
                        shop_name=shop,
                        location=loc,
                        image_url=pic or None,
                    )
                )
            for v in node.values():
                _walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                _walk(v, depth + 1)

    try:
        _walk(result_json)
    except RecursionError:
        log.warning("[taobao] _walk 递归过深，已截断")
    return products


def crawl_taobao_pw(
    context: BrowserContext,
    keyword: str,
    max_pages: int,
    *,
    headed: bool = False,
) -> list[Product]:
    """淘宝搜索：首页暖场建信任 + 直接 goto s.taobao.com/search + 拦截 mtop。

    不走 form 交互（Enter/click btn-search）——会触发自动补全选中其他搜索建议词
    （已观察到"搜了 cpu 又搜了内存条"的双搜索现象）。
    参考：vendor/xinlingqudongX-TSDK/TSDK/api/taobao/h5.py（mtop URL 模板）
         vendor/cclient-tmallSign/routes/tmall.js（sign 算法）
    """
    ok, detail, acct = verify_account(context, "taobao")
    if not ok:
        raise LoginRequiredError(
            f"淘宝账号检测失败：{detail}。请重跑 login_helper.py taobao。账号信息={acct}",
            platform="taobao", url="-",
        )
    log.info(f"[taobao] ✓ {detail}")

    page = context.new_page()
    seen: dict[str, Product] = {}
    mtop_batches: list[dict] = []
    # 跟踪所有挂过 response listener 的 page，finally 里统一 remove，避免泄漏
    _listened_pages: list[Page] = [page]

    def _on_response(response: Any) -> None:
        try:
            url = response.url or ""
            if any(p in url for p in _TAOBAO_MTOP_PATTERNS):
                try:
                    j = response.json()
                    mtop_batches.append(j)
                    log.info(f"[taobao] 拦截到 mtop 响应 url={url[:150]}")
                except Exception as e:
                    log.warning(f"[taobao] mtop JSON 解析失败: {e} url={url[:120]}")
        except Exception:
            pass

    def _on_new_page(new_page: Page) -> None:
        try:
            new_page.on("response", _on_response)
            _listened_pages.append(new_page)
        except Exception as e:
            log.warning(f"[taobao] 绑定新 page 响应 listener 失败: {e}")

    # 监听整个 context（form target=_blank 会开新 tab，响应事件在新 page 上）
    context.on("page", _on_new_page)
    page.on("response", _on_response)

    try:
        # Step 1 首页"暖场"：仅为刷新 cookie / 建立行为画像，不做任何 form 交互
        log.info("[taobao] → https://www.taobao.com （首页暖场，不在此搜索）")
        try:
            page.goto("https://www.taobao.com", wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            log.warning("[taobao] 首页 goto 超时")

        _snapshot_page(page, "taobao", "after_home")
        risk = _detect_risk(page)
        if risk:
            dump_debug("taobao", page.content())
            raise AntiSpiderError(
                f"淘宝首页命中风控：{risk}", platform="taobao", url=page.url,
            )
        if "login" in (page.url or "").lower() or "qrlogin" in (page.url or "").lower():
            raise LoginRequiredError(
                "淘宝首页即跳登录（state 已过期），请重跑 login_helper.py taobao",
                platform="taobao", url=page.url,
            )

        # 让首页 JS 跑完 3 秒（加 cookie），但不动 form（避免自动补全选中建议）
        page.wait_for_timeout(3000)

        # Step 2 清掉暖场期间收到的"首页推荐 mtop 批次"，避免污染搜索结果
        pre_search_noise = len(mtop_batches)
        mtop_batches.clear()
        log.info(f"[taobao] 清空首页暖场期间 {pre_search_noise} 批推荐响应（噪声）")

        # Step 3 直接 goto 搜索 URL（暖场后的 cookie 已足够通过风控）
        search_url = "https://s.taobao.com/search?" + urlencode({"q": keyword})
        log.info(f"[taobao] → {search_url}")
        try:
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        except PWTimeout:
            log.warning("[taobao] 搜索页 goto 超时")

        _snapshot_page(page, "taobao", "after_search_goto")
        log.info(f"[taobao] 进入搜索页后 URL={page.url}")

        if "s.taobao.com/search" not in (page.url or ""):
            dump_debug("taobao", page.content())
            final_url = (page.url or "").lower()
            if "login" in final_url or "qrlogin" in final_url or "passport" in final_url:
                raise LoginRequiredError(
                    f"搜索页被跳到登录，请重跑 login_helper.py taobao。final={page.url}",
                    platform="taobao", url=page.url,
                )
            raise AntiSpiderError(
                f"goto 后 URL 被劫持到 {page.url}（疑似风控）",
                platform="taobao", url=page.url,
            )

        # Step 4 智能等商品 DOM 就绪（替代硬编码 sleep）
        product_selectors = [
            "a[href*='item.taobao.com/item.htm?id=']",
            "a[href*='detail.tmall.com/item.htm?id=']",
        ]
        hit_count = _wait_for_products(
            page, "taobao", product_selectors, timeout_ms=30000, min_count=5
        )

        if hit_count == 0:
            # 商品没渲染——检查是否滑块/风控/登录
            if _has_captcha(page, _ALI_CAPTCHA_SELECTORS):
                if not _wait_for_captcha_pass(
                    page, "taobao", _ALI_CAPTCHA_SELECTORS, headed=headed
                ):
                    dump_debug("taobao", page.content())
                    raise AntiSpiderError(
                        "淘宝滑块未通过/无头模式，请加 --headed",
                        platform="taobao", url=page.url,
                    )
                # 通过滑块后再等
                hit_count = _wait_for_products(
                    page, "taobao", product_selectors, timeout_ms=20000, min_count=5
                )
            risk = _detect_risk(page)
            if risk:
                dump_debug("taobao", page.content())
                raise AntiSpiderError(
                    f"搜索后命中风控：{risk}", platform="taobao", url=page.url,
                )
            if hit_count == 0:
                dump_debug("taobao", page.content())
                raise ParseError(
                    "商品 DOM 始终未出现（HTML 已落盘供分析）",
                    platform="taobao", url=page.url,
                )

        # Step 5 滚动加载更多（淘宝是无限下拉）
        for r in range(max_pages * 4):
            _scroll_page(page, times=1)
            if r % 3 == 2:
                cur = len(page.query_selector_all(
                    "a[href*='item.htm?id=']"
                ))
                log.info(f"[taobao] 滚动 {r + 1} 轮，当前 DOM 商品锚点 {cur} 个")

        # Step 6 从渲染后的 DOM 抽商品
        tokens = _keyword_tokens(keyword)
        raw = page.evaluate(
            r"""(tokens) => {
                const anchors = document.querySelectorAll(
                  "a[href*='item.taobao.com/item.htm?id='], a[href*='detail.tmall.com/item.htm?id=']"
                );
                const seen = new Set();
                const out = [];
                anchors.forEach(a => {
                    const m = (a.href || '').match(/[?&]id=(\d+)/);
                    if (!m) return;
                    const id = m[1];
                    if (seen.has(id)) return;
                    // 往上找含价格（¥）的卡片节点
                    let card = a;
                    for (let i = 0; i < 8 && card.parentElement; i++) {
                        card = card.parentElement;
                        if ((card.innerText || '').includes('¥')) break;
                    }
                    const text = (card.innerText || '').trim();
                    // 关键词过滤：文本需命中任一 token
                    const low = text.toLowerCase();
                    if (tokens.length && !tokens.some(t => low.includes(t))) return;
                    seen.add(id);
                    const img = card.querySelector('img');
                    out.push({
                        id,
                        href: a.href.startsWith('http') ? a.href : 'https:' + a.href,
                        text: text.slice(0, 500),
                        img: img ? (img.src || img.getAttribute('data-src')) : null,
                    });
                });
                return out;
            }""",
            tokens,
        )

        for it in raw:
            if it["id"] in seen:
                continue
            lines = [ln.strip() for ln in it["text"].split("\n") if ln.strip()]
            price_line = next((ln for ln in lines if "¥" in ln), "")
            title = next((ln for ln in lines if len(ln) > 6 and "¥" not in ln), "")
            shop = next(
                (ln for ln in reversed(lines) if len(ln) < 30 and "¥" not in ln and ln != title),
                None,
            )
            seen[it["id"]] = Product(
                platform="taobao",
                item_id=it["id"],
                title=title[:200],
                url=it["href"],
                current_price=_parse_price(price_line),
                shop_name=shop,
                image_url=it["img"],
            )

        log.info(
            f"[taobao] DOM 抽取完成，关键词 {tokens} 过滤后 {len(seen)} 条 "
            f"（mtop 辅助 {len(mtop_batches)} 批，未启用）"
        )

        # 兜底：DOM 0 条才启用 mtop 数据
        if not seen and mtop_batches:
            log.warning("[taobao] DOM 抽取 0 条，启用 mtop 兜底")
            for batch in mtop_batches:
                for prod in _taobao_extract_products(batch, keyword=keyword):
                    if prod.item_id not in seen:
                        seen[prod.item_id] = prod

        # 调试信息：落盘最终搜索页 HTML
        try:
            dump_debug("taobao", page.content())
        except Exception:
            pass

        if seen:
            refresh_storage_state(context, "taobao")
    finally:
        # 清理 context 上的 page listener + 所有已挂 response listener 的 page
        try:
            context.remove_listener("page", _on_new_page)
        except Exception:
            pass
        for p in _listened_pages:
            try:
                p.remove_listener("response", _on_response)
            except Exception:
                pass
        page.close()
    return list(seen.values())


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------

# 拼多多已移除（移动端登录复杂、pc 端商品极少，性价比低）
CRAWLERS = {
    "jd": (crawl_jd_pw, "jd_state.json", False),
    "xianyu": (crawl_xianyu_pw, "xianyu_state.json", False),
    "taobao": (crawl_taobao_pw, "taobao_state.json", False),
}


_STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {}, app: {}, csi: () => {} };
const origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (origQuery) {
    window.navigator.permissions.query = (p) =>
        p && p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : origQuery(p);
}
"""


def run_one(
    p, platform: str, keyword: str, max_pages: int, headed: bool
) -> SearchResult:
    func, state_name, mobile = CRAWLERS[platform]
    result = SearchResult(
        platform=platform, keyword=keyword, pages_requested=max_pages, success=False
    )
    state_path = STATE_DIR / state_name
    browser = None
    try:
        browser = p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        kwargs: dict[str, Any] = {
            "user_agent": UA_MOBILE if mobile else UA_DESKTOP,
            "viewport": {"width": 390, "height": 844} if mobile else {"width": 1366, "height": 900},
            "locale": "zh-CN",
        }
        if state_path.exists():
            kwargs["storage_state"] = str(state_path)
        else:
            log.warning(f"[{platform}] 未找到 {state_path}，匿名访问大概率失败")
        context = browser.new_context(**kwargs)
        context.add_init_script(_STEALTH_JS)
        products = func(context, keyword, max_pages, headed=headed)
        result.products = products
        result.success = True
        log.info(f"[{platform}] 完成，共 {len(products)} 条（已去重）")
        context.close()
    except SpiderError as e:
        result.error = str(e)
        result.error_type = type(e).__name__
        log.error(f"[{platform}] {e}")
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        result.error_type = "UnknownError"
        log.error(f"[{platform}] 未捕获异常:\n{traceback.format_exc()}")
    finally:
        if browser:
            try:
                browser.close()
            except Exception:
                pass
    return result


def print_report(keyword: str, results: dict[str, SearchResult]) -> None:
    print(f"\n{'=' * 72}")
    print(f"CPU 关键词：{keyword}")
    print(f"爬取时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 72}\n")
    for name, r in results.items():
        status = "OK" if r.success else "FAIL"
        print(
            f"[{status}] {name.upper():8s} 请求页数={r.pages_requested}  "
            f"去重后商品={r.count}  "
            + (f"错误={r.error_type}" if not r.success else "")
        )
        if not r.success and r.error:
            print(f"   原因: {r.error}")
        # 按价格排序显示前 10 条
        shown = sorted(
            r.products, key=lambda p: (p.current_price is None, p.current_price or 1e9)
        )[:10]
        for i, pr in enumerate(shown, 1):
            price = f"¥{pr.current_price:.2f}" if pr.current_price else "N/A"
            print(f"   {i:2d}. {price:10s}  {pr.title[:55]}")
            print(f"       {pr.url}")
        if r.count > 10:
            print(f"   ... 另有 {r.count - 10} 条（已写入 JSON 报告）")
        print()

    out = LOG_DIR / f"report_pw_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(
            {
                "keyword": keyword,
                "crawled_at": datetime.now().isoformat(),
                "results": {
                    name: {
                        "success": r.success,
                        "pages_requested": r.pages_requested,
                        "count": r.count,
                        "error": r.error,
                        "error_type": r.error_type,
                        "products": [asdict(pr) for pr in r.products],
                    }
                    for name, r in results.items()
                },
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"详细 JSON 报告：{out}")
    print(f"日志：logs/crawl_pw_{datetime.now().strftime('%Y-%m-%d')}.log")


def main() -> int:
    parser = argparse.ArgumentParser(description="CPU 价格爬虫 · Playwright 版")
    parser.add_argument("keyword", nargs="?", default="i5-12400F")
    parser.add_argument(
        "--only",
        choices=list(CRAWLERS.keys()),
        action="append",
        help="只跑指定平台（可多次）",
    )
    parser.add_argument(
        "--pages", type=int, default=3, help="每平台爬取页数，默认 3"
    )
    parser.add_argument(
        "--headed", action="store_true", help="显示浏览器窗口（默认无头）"
    )
    args = parser.parse_args()

    platforms = args.only or list(CRAWLERS.keys())
    log.info(
        f"任务：keyword='{args.keyword}' platforms={platforms} "
        f"pages={args.pages} headed={args.headed}"
    )

    results: dict[str, SearchResult] = {}
    with sync_playwright() as p:
        for idx, name in enumerate(platforms):
            if idx > 0:
                log.info(
                    f"跨平台节流：休眠 {PLATFORM_DELAY_SEC}s 再继续下一平台（保护账号）"
                )
                time.sleep(PLATFORM_DELAY_SEC)
            log.info(f"========== 开始 {name} ==========")
            t0 = time.monotonic()
            results[name] = run_one(p, name, args.keyword, args.pages, args.headed)
            log.info(f"========== {name} 耗时 {time.monotonic() - t0:.2f}s ==========")
    print_report(args.keyword, results)
    return 0 if any(r.success for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
