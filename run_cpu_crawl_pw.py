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
import csv
import hashlib
import json
import logging
import re
import sqlite3
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


# Windows 默认 stdout 是 GBK,遇到 ¥/Emoji 等非 GBK 字符会 UnicodeEncodeError
# 导致 print_report 收尾阶段挂掉(数据其实早就落盘)。强制 stdout/stderr 走 UTF-8,
# 不影响日志文件(logging 模块自己设了 UTF-8)。Python 3.7+ 支持 reconfigure。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    # 旧 Python 或非 standard stream(如 IDE 包装):静默兜底,不影响主流程
    pass


ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
DEBUG_DIR = LOG_DIR / "debug"
# 商品落盘双写入口:csv 是 ground truth(viewer.html 消费),db 是分析查询入口。
# db 失败只 log warning,不阻塞爬取。
_PRICES_DB = ROOT / "data" / "prices.db"
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "data"          # 阶段 12：字典/SKU/价格三层产出根
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
    console.setLevel(logging.WARNING)
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


def _setup_brief_logger() -> logging.Logger:
    """简要日志：只记录任务级进度、当前动作、ETA、账号/风控停机原因。"""
    lg = logging.getLogger("cpu_crawler_pw.brief")
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(message)s", "%Y-%m-%d %H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    lg.addHandler(console)

    today = datetime.now().strftime("%Y-%m-%d")
    fh = logging.FileHandler(LOG_DIR / f"crawl_pw_brief_{today}.log", encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    lg.addHandler(fh)
    lg.propagate = False
    return lg


log = _setup_logger()
brief_log = _setup_brief_logger()


def brief(message: str) -> None:
    """同时写详细日志和简要日志；简要日志也是默认控制台输出。"""
    log.info("[brief] %s", message)
    brief_log.info(message)


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "未知"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rem = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{rem:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _session_elapsed_sec(session_state: dict) -> float | None:
    started_at = session_state.get("started_at")
    if not started_at:
        return None
    try:
        started = datetime.fromisoformat(str(started_at))
    except ValueError:
        return None
    now = datetime.now(started.tzinfo) if started.tzinfo else datetime.now()
    return max(0.0, (now - started).total_seconds())


def _progress_counts(session_state: dict) -> tuple[int, int, int, int, int]:
    keywords = session_state.get("keywords", []) or []
    platforms = session_state.get("platforms", []) or []
    total = len(keywords) * len(platforms)
    done = success = failed = skipped = 0
    for per_kw in (session_state.get("progress", {}) or {}).values():
        for entry in (per_kw or {}).values():
            status = entry.get("status")
            if status in {"completed", "failed", "skipped"}:
                done += 1
            if status == "completed":
                success += 1
            elif status == "failed":
                failed += 1
            elif status == "skipped":
                skipped += 1
    return done, total, success, failed, skipped


def emit_progress_brief(session_state: dict | None, current: str) -> None:
    """输出用户需要看的短进度：百分比、当前动作、已耗时、预计剩余。"""
    if session_state is None:
        brief(f"当前：{current}")
        return
    done, total, success, failed, skipped = _progress_counts(session_state)
    pct = (done / total * 100.0) if total else 100.0
    elapsed = _session_elapsed_sec(session_state)
    remaining: float | None = None
    if done > 0 and total > done and elapsed is not None:
        remaining = elapsed * (total - done) / done
    elif done == 0:
        raw_est = session_state.get("estimated_total_sec")
        if isinstance(raw_est, (int, float)):
            remaining = float(raw_est)
    brief(
        f"进度 {done}/{total} ({pct:.1f}%) | 当前：{current} | "
        f"成功 {success} 失败 {failed} 跳过 {skipped} | "
        f"已耗时 {_format_duration(elapsed)} | 预计剩余 {_format_duration(remaining)}"
    )


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
class RateLimitError(AntiSpiderError): ...     # CLAUDE.md 约定：429/大促火爆 属 AntiSpider 子类
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


@dataclass
class SkuRef:
    """阶段 12：字典模式下的单个 SKU 搜索单元。"""
    sku_title: str
    category: str
    brand: str
    chip: str
    alt_titles: list[str] = field(default_factory=list)


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

# 节流常量（保护账号 / 避免封号）——2026-04 改为"成果计数"节流：
#   逐页/跨平台只留必要等待；真正的"长休"按累计抓到 BATCH_SIZE 条触发一次。
#   动机：原先 page/platform 级硬等待让批量任务非常慢，但只要请求量受控，
#         平台风控更看的是"单位时间产出"而不是"每页等多少秒"。
# 所有滚动、翻页、平台切换的间隔都基于这些数值，不要在业务代码里硬编码
# JD 账号已被风控封禁，默认不再跑 JD（见 DEFAULT_PLATFORMS）
SCROLL_DELAY_MS = 1500            # 每次滚动后等待（懒加载触发够用）
PAGE_DELAY_MS = 2000              # 同平台内翻页间隔
PLATFORM_DELAY_SEC = 5.0          # 跨平台间隔（启动新 context 缓冲）
KEYWORD_BATCH_SIZE = 4            # 每爬 N 个关键词休息一次
KEYWORD_DELAY_SEC = 20.0          # 达到 batch 阈值时休眠 20s
KEYWORD_JITTER_SEC = 5.0          # ±5s 抖动，实际 15~25s
CAPTCHA_WAIT_SEC = 300            # 滑块/验证码出现时最多等用户 5 分钟

# 成果批次节流：跨平台跨关键词全局累计 N 条，触发一次长休（用 _maybe_batch_rest）
BATCH_SIZE = 30                  # 累计每 30 条触发一次长休
BATCH_REST_SEC = 45              # 每批之间休眠 45s

# --fast 预设：argparse 命中 --fast 时由 _apply_fast_preset() 覆盖以下常量。
# JD_* 常量刻意不动：JD 风控最敏感且账号已被警告过，激进档不波及 JD。
_FAST_PRESET: dict[str, float | int] = {
    "BATCH_SIZE": 100,
    "BATCH_REST_SEC": 15,
    "KEYWORD_DELAY_SEC": 5.0,
    "KEYWORD_BATCH_SIZE": 8,
    "PLATFORM_DELAY_SEC": 2.0,
}

# JD 专用（若显式 --only jd 启用，比其他平台更慢）
JD_KEYWORD_BATCH_SIZE = 4        # JD 也按 4 个一组
JD_KEYWORD_DELAY_SEC = 30.0      # JD batch 休眠 30s ±10s（实际 20~40s）
JD_KEYWORD_JITTER_SEC = 10.0
JD_PAGE_DELAY_MS = 3000          # JD 翻页间隔
JD_HOME_WARMUP_MS = 5000         # JD 每关键词搜索前首页暖场 5s

# 默认启用的平台：排除 JD（账号已封，2026-04 暂停；显式 --only jd 可覆盖）
DEFAULT_PLATFORMS = ["xianyu", "taobao"]

# 会话状态文件（中断续跑用；main() 里会按 signature 决定是否续）
ACTIVE_STATE_FILE = ROOT / "state" / "crawl_active.json"


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


def _goto_with_retry(
    page: Page,
    url: str,
    platform: str,
    *,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 30000,
    retries: int = 2,
    backoff_sec: float = 8.0,
) -> None:
    """带重试的 goto。

    针对：
    - `net::ERR_CONNECTION_CLOSED` / `ERR_CONNECTION_RESET`：IP 被服务端短时间限流，等一会儿通常恢复
    - `ERR_NAME_NOT_RESOLVED`：DNS 抖动
    - `ERR_TIMED_OUT`：网络层超时（不同于 Playwright 的 load-state 超时）

    最终仍失败抛 `NetworkError`（可被上层 tenacity/backoff 重试）。
    """
    # Playwright 的原生 Error 类（不是 PWTimeout）
    from playwright.sync_api import Error as PWError

    last_err: str = ""
    for attempt in range(retries + 1):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            return
        except PWTimeout as e:
            last_err = f"PWTimeout: {e}"
            log.warning(f"[{platform}] goto {url} 超时（{attempt + 1}/{retries + 1}）: {last_err[:120]}")
        except PWError as e:
            msg = str(e)
            last_err = f"PWError: {msg}"
            if any(hint in msg for hint in ("net::ERR_", "ERR_CONNECTION", "ERR_NAME_", "ERR_TIMED_OUT", "ERR_SOCKET_")):
                log.warning(
                    f"[{platform}] goto {url} 网络层错误（{attempt + 1}/{retries + 1}）: {msg[:160]} "
                    f"—— 可能被服务端短时限流"
                )
            else:
                # 非网络错误（如协议错误）直接向上抛
                raise
        if attempt < retries:
            wait = backoff_sec * (attempt + 1)
            log.info(f"[{platform}] 等 {wait:.1f}s 后重试 goto ...")
            time.sleep(wait)

    raise NetworkError(
        f"{platform} goto {url} 重试 {retries + 1} 次全部失败（最后错误：{last_err[:200]}）"
        f"。若多次出现可能 IP 被淘宝/京东临时限流，建议：1) 等 15-30 分钟 2) 换网络/代理 3) 降低爬取频率",
        platform=platform, url=url,
    )


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
                img = ""
                # JD 不同 functionId 用不同字段名，按样本 JSON 实测结果排列（image_url 最主流）
                for field in (
                    "image_url", "imageurl", "imageUrl", "imgUrl",     # 最常命中
                    "hoverImgUrl", "longImageUrl", "long_image_url",
                    "rawAiImageurl", "pzqjImgUrl",
                    "image", "pic", "picUrl", "goodsImageUrl",
                    "pictureUrl", "mainPic", "skuImageUrl",
                    "goodsPic", "mainImage", "img", "thumbUrl",
                ):
                    v = node.get(field)
                    if v and isinstance(v, str):
                        img = v
                        break
                # list 形式字段（imageUrlList / image_url_list / images 等）
                if not img:
                    for field in ("imageUrlList", "image_url_list", "images", "imageList", "pics"):
                        v = node.get(field)
                        if isinstance(v, list) and v:
                            first = v[0]
                            if isinstance(first, str):
                                img = first
                                break
                            if isinstance(first, dict):
                                img = (
                                    first.get("url")
                                    or first.get("src")
                                    or first.get("image_url")
                                    or first.get("imageUrl")
                                    or ""
                                )
                                if img:
                                    break
                if img:
                    if img.startswith("//"):
                        img = "https:" + img
                    elif not img.startswith("http"):
                        # JD 常见相对 path：jfs/t1/xxx.jpg → 拼完整 CDN
                        img = f"https://img14.360buyimg.com/n1/{img}"
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


def _dismiss_popups(page: Page, platform: str, *, max_attempts: int = 3) -> int:
    """关闭常见弹窗（隐私 / 促销 / 登录提醒 / PLUS / APP 推广）。

    策略：
    1. 先 Escape 键（多数弹窗监听）
    2. 再扫通用关闭按钮 selector，见到可见的就点击
    3. 循环最多 max_attempts 次（弹窗可能分层先后出现）

    返回关闭的弹窗数量，供调用方 log。
    """
    closed = 0
    # 1) Escape
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
    except Exception:
        pass

    # 2) 通用关闭 selectors（覆盖 JD / 阿里系 / 通用）
    selectors = [
        # 语义优先
        "[aria-label*='关闭']",
        "[aria-label='Close' i]",
        "[aria-label*='close' i]",
        # JD 命名约定 J- 前缀
        ".J-close",
        ".J-closeBtn",
        "#__popup .close",
        # 通用 class
        ".dialog-close",
        ".modal-close",
        ".popup-close",
        ".close-btn",
        "a.close[href='javascript:void(0)']",
        "button.close",
        "i.close",
        ".icon-close",
        "div[class*='closeIcon']",
        "div[class*='closeBtn']",
    ]

    for attempt in range(max_attempts):
        hit = False
        for sel in selectors:
            try:
                el = page.query_selector(sel)
                if not el:
                    continue
                # 可见性检查（尺寸 > 5px 且无 display:none）
                try:
                    bb = el.bounding_box()
                except Exception:
                    bb = None
                if not bb or (bb.get("width", 0) < 5 or bb.get("height", 0) < 5):
                    continue
                el.click(timeout=2000)
                log.info(f"[{platform}] 关闭弹窗 selector={sel!r}")
                closed += 1
                hit = True
                page.wait_for_timeout(400)
                break
            except Exception:
                continue
        if not hit:
            break
    if closed:
        log.info(f"[{platform}] 共关闭 {closed} 个弹窗")
    return closed


# JD 首页搜索框选择器——按优先级尝试（JD 改版频繁，多 fallback 覆盖）
_JD_SEARCH_INPUT_SELECTORS = [
    "input#key",                        # 经典 id
    "input[name='keyword']",            # 最稳的 form field name
    "input#search-key",                 # 新版可能用 search-key
    "input[placeholder*='搜索']",
    "input[aria-label*='搜索']",
    "input[type='search']",
    ".search-combobox input",
    "#search input[type='text']",
    "form[action*='search.jd.com'] input[type='text']",
]

# JD 首页搜索按钮选择器
_JD_SEARCH_BUTTON_SELECTORS = [
    "form#search button.button",
    "button.button[type='submit']",
    ".button[type='submit']",
    "button.search-btn",
    "button[aria-label*='搜索']",
    "form[action*='search.jd.com'] button[type='submit']",
]


def _jd_find_first(page: Page, selectors: list[str]) -> str | None:
    """按优先级试 selector 列表，返回第一个命中可见元素的 selector。"""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if not el:
                continue
            try:
                bb = el.bounding_box()
            except Exception:
                bb = None
            if bb and bb.get("width", 0) >= 5 and bb.get("height", 0) >= 5:
                return sel
        except Exception:
            continue
    return None


def _jd_submit_search_from_home(page: Page, keyword: str) -> None:
    """从 JD 首页搜索框提交搜索。避免直接 goto search.jd.com 触发 risk_handler。

    真实用户行为：
    1. 点击搜索框聚焦
    2. 输入关键词（快速打字，不让自动补全 dropdown 劫持 Enter）
    3. 按 Enter 提交
    跳转后 URL 会自然带 pvid/spmTag 等追踪参数。
    """
    input_sel = _jd_find_first(page, _JD_SEARCH_INPUT_SELECTORS)
    if not input_sel:
        # 打印页面上所有 input 的属性供排查
        try:
            inputs = page.evaluate(
                "() => [...document.querySelectorAll('input')].slice(0,10).map("
                "e => ({id: e.id, name: e.name, type: e.type, placeholder: e.placeholder, cls: e.className}))"
            )
            log.warning(f"[jd] 首页 input 列表（前 10）: {inputs}")
        except Exception:
            pass
        raise ParseError(
            f"JD 首页未找到搜索框（试过 {_JD_SEARCH_INPUT_SELECTORS}）",
            platform="jd", url=page.url,
        )
    log.info(f"[jd] 搜索框命中 {input_sel!r}")
    try:
        page.click(input_sel, timeout=5000)
        page.fill(input_sel, "")
        # 30ms/char 快速输入：suggest dropdown 还没来得及出现，不会被劫持
        page.type(input_sel, keyword, delay=30)
        page.wait_for_timeout(250)
    except Exception as e:
        raise ParseError(f"JD 搜索框输入失败: {e}", platform="jd", url=page.url)

    # 先试 Enter，失败再试 submit 按钮
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            page.press(input_sel, "Enter")
        return
    except PWTimeout:
        log.warning("[jd] Enter 未触发 navigation，尝试点击搜索按钮")

    button_sel = _jd_find_first(page, _JD_SEARCH_BUTTON_SELECTORS)
    if not button_sel:
        raise ParseError(
            f"JD 首页未找到搜索按钮（试过 {_JD_SEARCH_BUTTON_SELECTORS}）",
            platform="jd", url=page.url,
        )
    log.info(f"[jd] 搜索按钮命中 {button_sel!r}")
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            page.click(button_sel, timeout=5000)
    except PWTimeout:
        raise ParseError(
            "JD 首页 form 提交失败（Enter 和按钮都无跳转）",
            platform="jd", url=page.url,
        )


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
                    j = _response_json_loose(response)
                    mtop_batches.append(j)
                    log.info(f"[jd] 拦截到搜索响应 url={url[:150]}")
                except Exception as e:
                    log.warning(f"[jd] 响应 JSON 解析失败: {e} url={url[:120]}")
        except Exception:
            pass

    page.on("response", _on_response)
    seen: dict[str, Product] = {}
    try:
        # Step 0 从 JD 首页搜索框 form 提交（模拟真实用户，避免直 goto 触发 risk_handler）
        def _submit_verify() -> bool:
            """返回 True 表示 form submit 后真的到了搜索页，False 表示未到（需 fallback）。"""
            _jd_submit_search_from_home(page, keyword)
            cur = (page.url or "").lower()
            # 典型"假成功"URL：https://www.jd.com/?from=pc_search_sd（带空格关键词常命中）
            return "search.jd.com/search" in cur

        log.info("[jd] → https://www.jd.com/ 首页暖场 + form 提交搜索")
        _goto_with_retry(page, "https://www.jd.com/", "jd")
        page.wait_for_timeout(JD_HOME_WARMUP_MS)  # 停留几秒模拟真实浏览
        _dismiss_popups(page, "jd")

        submit_ok = False
        try:
            if _submit_verify():
                submit_ok = True
                log.info(f"[jd] ✓ form submit 成功，URL={page.url}")
            else:
                log.warning(
                    f"[jd] form submit 后 URL={page.url[:200]} 未到搜索页（JD 把带空格关键词"
                    f"重定向回首页），fallback 直接 goto"
                )
        except ParseError as e:
            log.warning(f"[jd] form 提交失败：{e}，关闭弹窗后重试")
            _dismiss_popups(page, "jd", max_attempts=5)
            try:
                if _submit_verify():
                    submit_ok = True
                    log.info(f"[jd] ✓ 第二次尝试 form submit 成功，URL={page.url}")
            except ParseError as e2:
                log.warning(f"[jd] form 二次失败：{e2}")

        if not submit_ok:
            # fallback 直接 goto 搜索 URL（空格 urlencode 为 + 号由 urllib 自动处理）
            fallback_url = "https://search.jd.com/Search?" + urlencode(
                {"keyword": keyword, "enc": "utf-8", "page": 1}
            )
            log.info(f"[jd] fallback → {fallback_url}")
            _goto_with_retry(page, fallback_url, "jd")
            # fallback 后再验一次是否跳到风控/首页
            if "search.jd.com/search" not in (page.url or "").lower():
                log.error(f"[jd] fallback 后 URL 仍非搜索页：{page.url}")

        for i in range(max_pages):
            pnum = i * 2 + 1  # JD 翻页 1,3,5...
            if i == 0:
                # 首页已经跳到搜索页（或 fallback 已 goto），不需要再 goto
                log.info(f"[jd] page 1/{max_pages} 已在搜索页 URL={page.url[:120]}")
            else:
                url = "https://search.jd.com/Search?" + urlencode(
                    {"keyword": keyword, "enc": "utf-8", "page": pnum}
                )
                log.info(f"[jd] page {i + 1}/{max_pages} → {url}")
                try:
                    _goto_with_retry(page, url, "jd")
                except NetworkError as e:
                    log.warning(f"[jd] page {pnum} goto 最终失败: {e}，跳过此页")
                    continue
                except PWTimeout:
                    log.warning(f"[jd] page {pnum} goto 超时，尝试继续")

            _snapshot_page(page, "jd", f"p{pnum}_after_goto")
            risk = _detect_risk(page)
            if risk:
                cur_url = (page.url or "").lower()
                # JD risk_handler 在 headed 模式下让用户手动通过验证
                if "risk_handler" in cur_url and headed:
                    log.warning(
                        f"[jd] 命中 risk_handler 风控验证页。请在浏览器里完成验证（拖滑块/扫脸），"
                        f"最多等 {CAPTCHA_WAIT_SEC}s..."
                    )
                    t0 = time.monotonic()
                    passed = False
                    while time.monotonic() - t0 < CAPTCHA_WAIT_SEC:
                        page.wait_for_timeout(2500)
                        new_url = (page.url or "").lower()
                        if "risk_handler" not in new_url and "search.jd.com" in new_url:
                            passed = True
                            log.info(f"[jd] ✓ 用户已通过验证，当前 URL={page.url}")
                            break
                    if not passed:
                        dump_debug("jd", page.content())
                        raise AntiSpiderError(
                            f"JD risk_handler 验证 {CAPTCHA_WAIT_SEC}s 内未通过。"
                            f"建议：重登 cookie / 等 1-3 小时 / 换 IP",
                            platform="jd", url=page.url,
                        )
                else:
                    dump_debug("jd", page.content())
                    msg = f"JD 命中风控：{risk}。见 logs/debug/ 截图"
                    if "risk_handler" in cur_url:
                        msg += "。无头模式无法人工通过验证，请加 --headed 重试"
                    raise AntiSpiderError(msg, platform="jd", url=page.url)
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

            # JD 翻页间隔比其他平台更长（账号容易被风控）
            page.wait_for_timeout(JD_PAGE_DELAY_MS)

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

        # 样本 JSON 落盘：第一批含商品的响应保存，方便后续精确加字段
        for batch in mtop_batches:
            if _jd_extract_products(batch):
                sample_path = dump_debug(
                    "jd", json.dumps(batch, ensure_ascii=False, indent=2), suffix="json"
                )
                log.info(f"[jd] 样本 XHR JSON 已落盘 {sample_path.name}（可人工检查图片字段名）")
                break

        # DOM fallback：缺图的 sku 尝试从搜索页 DOM 查 img
        missing_skus = [p.item_id for p in seen.values() if not p.image_url]
        if missing_skus:
            log.info(f"[jd] {len(missing_skus)} 个商品缺 image_url，从 DOM fallback 查找")
            try:
                dom_imgs = page.evaluate(
                    r"""(skus) => {
                        const out = {};
                        skus.forEach(sku => {
                            // JD 搜索页商品卡片多种可能的属性
                            const sels = [
                                `[data-sku="${sku}"] img`,
                                `[data-wareid="${sku}"] img`,
                                `a[href*="item.jd.com/${sku}.html"] img`,
                                `a[href*="/${sku}.html"] img`,
                            ];
                            for (const sel of sels) {
                                const el = document.querySelector(sel);
                                if (el) {
                                    let src = el.getAttribute('src')
                                           || el.getAttribute('data-lazy-img')
                                           || el.getAttribute('data-img')
                                           || el.getAttribute('data-src');
                                    if (src) {
                                        if (src.startsWith('//')) src = 'https:' + src;
                                        out[sku] = src;
                                        return;
                                    }
                                }
                            }
                        });
                        return out;
                    }""",
                    missing_skus,
                )
                filled = 0
                for sku, url in (dom_imgs or {}).items():
                    if sku in seen and url:
                        seen[sku].image_url = url
                        filled += 1
                log.info(f"[jd] DOM fallback 补回 {filled}/{len(missing_skus)} 张图片")
            except Exception as e:
                log.warning(f"[jd] DOM fallback 失败: {e}")

        # 登录态刷新统一在 platform 批次末尾做一次（减少盘 I/O）
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
_XIANYU_SCROLLS_PER_PAGE = 4
_XIANYU_IDLE_ROUNDS_TO_STOP = 2
_XIANYU_SCROLL_WAIT_SEC = 8.0
_XIANYU_EMPTY_RESULT_TEXTS = (
    "小闲鱼没有找到你想要的宝贝",
    "没有找到你想要的宝贝",
)


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


def _xianyu_is_empty_state(page: Page) -> bool:
    """页面显示官方空结果文案时，按正常 0 条结果处理。"""
    try:
        content = page.content()
    except Exception:
        return False
    return any(text in content for text in _XIANYU_EMPTY_RESULT_TEXTS)


def _xianyu_visible_card_count(page: Page) -> int:
    """统计当前结果卡片数量，供滚动停止条件使用。"""
    try:
        return int(
            page.evaluate(
                r"""() => {
                    const anchors = Array.from(document.querySelectorAll(
                        "a[href*='item?id='], a[class*='feeds-item-wrap']"
                    ));
                    const seen = new Set();
                    for (const anchor of anchors) {
                        const href = anchor.href || "";
                        const match = href.match(/[?&]id=(\d+)/);
                        if (match) {
                            seen.add(match[1]);
                        }
                    }
                    return seen.size;
                }"""
            )
        )
    except Exception:
        return 0


def _xianyu_extract_products_from_dom(page: Page, keyword: str) -> list[Product]:
    """DOM 兜底：仅在页面已有结果卡片但 mtop 未命中时使用。"""
    tokens = _keyword_tokens(keyword)
    raw = page.evaluate(
        r"""() => {
            const anchors = Array.from(document.querySelectorAll(
                "a[href*='item?id='], a[class*='feeds-item-wrap']"
            ));
            const out = [];
            const seen = new Set();
            for (const anchor of anchors) {
                const href = anchor.href || "";
                const match = href.match(/[?&]id=(\d+)/);
                if (!match) continue;
                const itemId = match[1];
                if (seen.has(itemId)) continue;
                seen.add(itemId);
                const titleEl = anchor.querySelector("[class*='row1-wrap-title']");
                const priceEl = anchor.querySelector("[class*='price-wrap']");
                const sellerEl = anchor.querySelector("[class*='seller-text-wrap']");
                const imgEl = anchor.querySelector("img");
                const title = (
                    titleEl?.getAttribute("title")
                    || titleEl?.textContent
                    || anchor.getAttribute("title")
                    || ""
                ).trim();
                const seller = (
                    sellerEl?.getAttribute("title")
                    || sellerEl?.textContent
                    || ""
                ).trim();
                let img = (
                    imgEl?.getAttribute("src")
                    || imgEl?.getAttribute("data-src")
                    || ""
                ).trim();
                if (img.startsWith("//")) img = "https:" + img;
                out.push({
                    item_id: itemId,
                    href,
                    title,
                    price: (priceEl?.textContent || "").trim(),
                    seller,
                    img,
                });
            }
            return out;
        }"""
    )
    products: list[Product] = []
    for item in raw:
        title = str(item.get("title") or "").strip()
        if not title or not _title_matches_keyword(title, tokens):
            continue
        products.append(
            Product(
                platform="xianyu",
                item_id=str(item["item_id"]),
                title=title[:200],
                url=str(item.get("href") or ""),
                current_price=_parse_price(item.get("price")),
                shop_name=str(item.get("seller") or "").strip() or None,
                image_url=str(item.get("img") or "").strip() or None,
                is_second_hand=True,
            )
        )
    return products


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
    """闲鱼搜索：优先走 mtop 响应拦截，未命中时退回 DOM 兜底。

    参考：
      - vendor/ai-goofish-monitor/src/services/search_pagination.py (9923efac)
      - vendor/superboyyy-xianyu-spider/spider.py (4cf59de2a744)
    本项目改造：async→sync、保留 _snapshot_page/_detect_risk，并把 --pages 映射为滚动深度预算。
    """
    ok, detail, acct = verify_account(context, "xianyu")
    if ok:
        log.info(f"[xianyu] ✓ {detail}")
    else:
        raise LoginRequiredError(
            f"闲鱼账号检测失败：{detail}。请重跑 login_helper.py xianyu。账号信息={acct}",
            platform="xianyu", url="-",
        )

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
                    j = _response_json_loose(response)
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
            _goto_with_retry(page, "https://www.goofish.com", "xianyu")
        except NetworkError:
            raise  # 上层 run_platform_batch 会归类为 NetworkError 继续下一关键词
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
            search_url = "https://www.goofish.com/search?" + urlencode({"q": keyword})
            _goto_with_retry(page, search_url, "xianyu")

        # 等首次 mtop 响应或滑块
        t0 = time.monotonic()
        while time.monotonic() - t0 < 20:
            if _has_valid_mtop_batch() or _xianyu_is_empty_state(page):
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

        dom_fallback_mode = False
        visible_cards = _xianyu_visible_card_count(page)
        if _xianyu_is_empty_state(page):
            _snapshot_page(page, "xianyu", "empty_result")
            log.info(f"[xianyu] 搜索结果为空，关键词 {keyword!r} 返回 0 条")
            return []
        if not _has_valid_mtop_batch():
            if visible_cards > 0:
                dom_fallback_mode = True
                log.warning(
                    f"[xianyu] 20s 内未拦截到 {_XIANYU_MTOP_SEARCH_URL}，"
                    f"但页面已有 {visible_cards} 个结果卡片，改走 DOM 兜底"
                )
            else:
                _snapshot_page(page, "xianyu", "no_mtop_response")
                dump_debug("xianyu", page.content())
                raise ParseError(
                    f"20s 内未拦截到 {_XIANYU_MTOP_SEARCH_URL} 响应；"
                    f"可能 mtop 路径已变更，见 logs/debug/",
                    platform="xianyu", url=page.url,
                )

        log.info(
            f"[xianyu] pages={max_pages} -> 滚动预算 "
            f"{max(1, max_pages) * _XIANYU_SCROLLS_PER_PAGE} 轮 "
            f"(每页 {_XIANYU_SCROLLS_PER_PAGE} 轮)"
        )
        total_scroll_rounds = max(1, max_pages) * _XIANYU_SCROLLS_PER_PAGE
        idle_rounds = 0
        for round_idx in range(total_scroll_rounds):
            before_batches = len(mtop_batches)
            before_cards = visible_cards
            _scroll_page(page, times=1)

            t0 = time.monotonic()
            while time.monotonic() - t0 < _XIANYU_SCROLL_WAIT_SEC:
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
                current_cards = _xianyu_visible_card_count(page)
                if len(mtop_batches) > before_batches or current_cards > before_cards:
                    visible_cards = current_cards
                    break
                page.wait_for_timeout(500)
            else:
                visible_cards = _xianyu_visible_card_count(page)

            batch_growth = len(mtop_batches) - before_batches
            card_growth = max(0, visible_cards - before_cards)
            if batch_growth == 0 and card_growth == 0:
                idle_rounds += 1
            else:
                idle_rounds = 0
            log.info(
                f"[xianyu] 滚动轮次 {round_idx + 1}/{total_scroll_rounds} "
                f"mtop+{batch_growth} cards+{card_growth} "
                f"idle={idle_rounds}/{_XIANYU_IDLE_ROUNDS_TO_STOP}"
            )
            if _xianyu_is_empty_state(page):
                _snapshot_page(page, "xianyu", "empty_result")
                log.info(f"[xianyu] 滚动后确认空结果，关键词 {keyword!r} 返回 0 条")
                return []
            if idle_rounds >= _XIANYU_IDLE_ROUNDS_TO_STOP:
                log.info(f"[xianyu] 连续 {idle_rounds} 轮无新增批次/卡片，提前停止滚动")
                break

        if dom_fallback_mode:
            dom_products = _xianyu_extract_products_from_dom(page, keyword)
            if not dom_products and visible_cards > 0:
                _snapshot_page(page, "xianyu", "dom_fallback_empty")
                dump_debug("xianyu", page.content())
                raise ParseError(
                    "闲鱼页面已有结果卡片，但 DOM 兜底提取 0 条；可能结果 DOM 已变化，见 logs/debug/",
                    platform="xianyu", url=page.url,
                )
            for prod in dom_products:
                if prod.item_id not in seen:
                    seen[prod.item_id] = prod
            log.info(
                f"[xianyu] DOM 兜底提取完成，去重后 {len(seen)} 条 "
                f"(可见卡片 {visible_cards})"
            )
        else:
            for batch in mtop_batches:
                for prod in _xianyu_extract_products(batch):
                    if prod.item_id not in seen:
                        seen[prod.item_id] = prod
            log.info(
                f"[xianyu] 拦截 {len(mtop_batches)} 批 mtop 响应，去重后 {len(seen)} 条"
            )
        # 登录态刷新统一在 platform 批次末尾做一次
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


def _response_json_loose(response: Any) -> dict:
    """Parse JSON or mtop JSONP response bodies."""
    try:
        data = response.json()
        if isinstance(data, dict):
            return data
    except Exception:
        pass

    text = response.text()
    body = text.strip()
    match = re.match(r"^[\w.$]+\((.*)\)\s*;?\s*$", body, re.S)
    if match:
        body = match.group(1)
    else:
        start = body.find("{")
        end = body.rfind("}")
        if start >= 0 and end > start:
            body = body[start:end + 1]
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("mtop response body is not a JSON object")
    return data


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
                    j = _response_json_loose(response)
                    mtop_batches.append(j)
                    log.info(f"[taobao] 拦截到 mtop 响应 url={url[:150]}")
                except Exception as e:
                    log.debug(f"[taobao] skipped non-json mtop response: {e} url={url[:120]}")
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
            _goto_with_retry(page, "https://www.taobao.com", "taobao")
        except NetworkError:
            raise  # 明确归类为 NetworkError 而非 UnknownError
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
        search_url = "https://h5.m.taobao.com/search.htm?" + urlencode({"q": keyword})
        log.info(f"[taobao] → {search_url}")
        try:
            _goto_with_retry(page, search_url, "taobao", timeout_ms=20000, retries=1)
        except NetworkError:
            raise
        except PWTimeout:
            log.warning("[taobao] 搜索页 goto 超时")

        _snapshot_page(page, "taobao", "after_search_goto")
        log.info(f"[taobao] 进入搜索页后 URL={page.url}")

        if not any(p in (page.url or "") for p in ("s.taobao.com/search", "h5.m.taobao.com/search")):
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
            if hit_count == 0 and mtop_batches:
                for batch in mtop_batches:
                    for prod in _taobao_extract_products(batch, keyword=keyword):
                        if prod.item_id not in seen:
                            seen[prod.item_id] = prod
                if seen:
                    log.info(f"[taobao] DOM 0 条，使用 mtop 兜底得到 {len(seen)} 条")
            if hit_count == 0 and not seen:
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
                    // 淘宝价格用 CSS Modules：priceInt--xxx + priceFloat--xxx 两个 span 分开
                    // 前缀稳定，hash 后缀变；优先结构化拼接，失败 fallback 到 innerText 扫描
                    let priceStr = null;
                    const intEl = card.querySelector('[class^="priceInt--"], [class*=" priceInt--"]');
                    if (intEl) {
                        let v = (intEl.innerText || '').trim();
                        const floatEl = card.querySelector('[class^="priceFloat--"], [class*=" priceFloat--"]');
                        if (floatEl) {
                            const frac = (floatEl.innerText || '').trim();
                            if (frac) v += frac.startsWith('.') ? frac : ('.' + frac);
                        }
                        if (v) priceStr = v;
                    }
                    if (!priceStr) {
                        // fallback：找含 ¥ 的行
                        const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
                        const ln = lines.find(l => l.includes('¥'));
                        if (ln) priceStr = ln;
                    }
                    const img = card.querySelector('img');
                    out.push({
                        id,
                        href: a.href.startsWith('http') ? a.href : 'https:' + a.href,
                        text: text.slice(0, 500),
                        price: priceStr,
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
                current_price=_parse_price(it.get("price")),
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

        # 登录态刷新统一在 platform 批次末尾做一次
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


# ---------------------------------------------------------------------------
# 会话状态（中断续跑）——落盘格式：
# state/crawl_active.json
# {
#   "signature": "<md5>",       // 根据 keywords+platforms+pages 计算，用于识别会话一致性
#   "keywords": [...],
#   "platforms": [...],
#   "pages": 3,
#   "session_id": "sess_<ts>",
#   "started_at": "...", "last_update": "...",
#   "products_csv": "logs/crawl_running_<sess>.csv",
#   "progress": {
#     "RTX 4060": {
#       "taobao":  {"status": "completed", "count": 45, "error": null, "error_type": null},
#       "xianyu":  {"status": "pending"}
#     }, ...
#   }
# }
# 每完成一个 (关键词, 平台) 立刻把 progress 更新并原子写盘。商品明细同时 append 到 products_csv。
# ---------------------------------------------------------------------------

_PRODUCT_CSV_COLS = [
    "keyword", "platform", "item_id", "title",
    "current_price", "origin_price", "shop_name", "location",
    "is_second_hand", "url", "image_url", "crawled_at",
]


def _compute_session_signature(
    keywords: list[str], platforms: list[str], pages: int,
) -> str:
    """指纹：keywords（排序去重）+ platforms（排序）+ pages。同一套任务无论执行顺序都得到相同签名。"""
    import hashlib
    payload = json.dumps(
        {
            "keywords": sorted(set(keywords)),
            "platforms": sorted(set(platforms)),
            "pages": int(pages),
        },
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def _load_active_state() -> dict | None:
    if not ACTIVE_STATE_FILE.exists():
        return None
    try:
        return json.loads(ACTIVE_STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"读取 active state 失败（{e}），按新会话处理")
        return None


def _save_active_state(state: dict) -> None:
    state["last_update"] = datetime.now().isoformat(timespec="seconds")
    ACTIVE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = ACTIVE_STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    tmp.replace(ACTIVE_STATE_FILE)  # 原子替换


def _archive_active_state(state: dict, final_csv: Path | None = None) -> Path:
    """会话结束（正常或手动归档）：把 active.json 改名为 crawl_done_<ts>.json。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = ACTIVE_STATE_FILE.parent / f"crawl_done_{ts}.json"
    if final_csv is not None:
        state["products_csv_final"] = str(final_csv)
    state["finished_at"] = datetime.now().isoformat(timespec="seconds")
    archive.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    try:
        ACTIVE_STATE_FILE.unlink()
    except FileNotFoundError:
        pass
    return archive


def _new_session_state(
    keywords: list[str], platforms: list[str], pages: int,
) -> tuple[dict, Path]:
    session_id = datetime.now().strftime("sess_%Y%m%d_%H%M%S")
    products_csv = LOG_DIR / f"crawl_running_{session_id}.csv"
    # 新 CSV 写表头
    products_csv.parent.mkdir(parents=True, exist_ok=True)
    with products_csv.open("w", encoding="utf-8-sig", newline="") as f:
        csv.DictWriter(f, fieldnames=_PRODUCT_CSV_COLS).writeheader()
    state = {
        "signature": _compute_session_signature(keywords, platforms, pages),
        "keywords": keywords,
        "platforms": platforms,
        "pages": int(pages),
        "session_id": session_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "last_update": datetime.now().isoformat(timespec="seconds"),
        "products_csv": str(products_csv),
        "progress": {},
    }
    _save_active_state(state)
    return state, products_csv


def _append_products_csv(csv_path: Path, keyword: str, platform: str, products: list) -> None:
    """增量 append 到 running CSV（每条商品一行）+ 同步落盘到 per-keyword CSV。

    两层写入：
    1. **会话级 CSV**（csv_path）—— 单次 run 产出，兼容 viewer.html 传统
    2. **按关键词 CSV**（logs/by_keyword/{slug}.csv）—— 跨会话累积，是断点续跑
       和 item_id 级去重的真源。已存在 item_id 跳过，新条目每条 writerow+flush。

    失败只记 warning，不中断爬取。
    """
    now_iso = datetime.now().isoformat(timespec="seconds")
    # 按 (keyword, platform) 预扫 per-keyword CSV 的 item_id 集合，做增量去重
    kw_seen = _load_keyword_seen_ids(keyword, platform)
    kw_path = _keyword_csv_path(keyword)
    _ensure_keyword_csv(kw_path)

    new_written = 0
    try:
        # session CSV 追加；per-keyword CSV 同步逐行 flush
        with csv_path.open("a", encoding="utf-8-sig", newline="") as f_sess, \
             kw_path.open("a", encoding="utf-8-sig", newline="") as f_kw:
            w_sess = csv.DictWriter(f_sess, fieldnames=_PRODUCT_CSV_COLS)
            w_kw = csv.DictWriter(f_kw, fieldnames=_PRODUCT_CSV_COLS)
            for pr in products:
                d = asdict(pr)
                iid = str(d.get("item_id", "") or "").strip()
                row = {
                    "keyword": keyword,
                    "platform": platform,
                    "item_id": iid,
                    "title": d.get("title", ""),
                    "current_price": d.get("current_price", "") or "",
                    "origin_price": d.get("origin_price", "") or "",
                    "shop_name": d.get("shop_name", "") or "",
                    "location": d.get("location", "") or "",
                    "is_second_hand": d.get("is_second_hand", False),
                    "url": d.get("url", ""),
                    "image_url": d.get("image_url", "") or "",
                    "crawled_at": now_iso,
                }
                w_sess.writerow(row)
                # per-keyword 是 item_id 去重源；已写过的不再写
                if iid and iid not in kw_seen:
                    w_kw.writerow(row)
                    f_kw.flush()  # 逐行刷盘：崩溃不丢数据
                    kw_seen.add(iid)
                    new_written += 1
    except OSError as e:
        log.warning(f"增量写 CSV 失败（{csv_path} / {kw_path}）：{e}")
        return

    if products:
        dup = len(products) - new_written
        if dup:
            log.info(
                f"[{platform}] {keyword!r} 按 item_id 去重 {dup}/{len(products)} "
                f"已在 {kw_path.name}，新增 {new_written} 条"
            )

    # csv 已落盘后同步写 db(失败不阻塞,csv 仍是 ground truth)
    _append_products_db(keyword, platform, products, now_iso)


# ---------------------------------------------------------------------------
# DB 写入:csv 旁路同步 prices.db.products(INSERT OR IGNORE 幂等)
# ---------------------------------------------------------------------------


def _append_products_db(
    keyword: str, platform: str, products: list, now_iso: str
) -> None:
    """把抓到的商品同步写入 prices.db.products。

    设计:
    - keyword 用文件名 slug 形式(`Arc_A310`),与 scripts/build_price_db.py
      历史导入完全一致,避免 db 里出现 `Arc A310`(空格)和 `Arc_A310`(下划线)
      并存的两套命名。
    - 短连接(每次 keyword+platform 完成开关一次),WAL 模式,与并行进程不冲突。
    - INSERT OR IGNORE 配合 UNIQUE(keyword,platform,item_id) 约束保证幂等;
      同一 item_id 再爬不重复入库。
    - 任何 sqlite3.Error 只 log warning,不抛 SpiderError、不阻塞爬虫——
      csv 是 ground truth,db 写失败可以 build_price_db.py 事后补救。
    """
    if not products:
        return
    kw_slug = _keyword_slug(keyword)
    try:
        conn = sqlite3.connect(str(_PRICES_DB), timeout=10.0)
    except sqlite3.Error as e:
        log.warning(f"[db] connect {_PRICES_DB} failed platform={platform}: {e}")
        return
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        cur = conn.cursor()
        # 幂等建表,与 scripts/build_price_db.py 保持 schema 一致
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'xianyu',
                item_id TEXT NOT NULL,
                title TEXT,
                current_price REAL,
                origin_price REAL,
                shop_name TEXT,
                location TEXT,
                is_second_hand INTEGER DEFAULT 1,
                url TEXT,
                image_url TEXT,
                crawled_at TEXT,
                is_outlier INTEGER DEFAULT 0,
                UNIQUE(keyword, platform, item_id)
            )
            """
        )
        inserted = 0
        skipped = 0
        for pr in products:
            d = asdict(pr)
            iid = str(d.get("item_id", "") or "").strip()
            if not iid:
                continue
            price = _coerce_price(d.get("current_price"))
            origin = _coerce_price(d.get("origin_price"))
            try:
                cur.execute(
                    """INSERT OR IGNORE INTO products
                       (keyword, platform, item_id, title, current_price, origin_price,
                        shop_name, location, is_second_hand, url, image_url, crawled_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        kw_slug,
                        (platform or "").lower(),
                        iid,
                        (d.get("title") or "")[:500],
                        price,
                        origin,
                        d.get("shop_name") or "",
                        d.get("location") or "",
                        1 if d.get("is_second_hand") else 0,
                        d.get("url") or "",
                        d.get("image_url") or "",
                        now_iso,
                    ),
                )
            except sqlite3.Error as e:
                log.warning(
                    f"[db] insert failed platform={platform} kw={keyword!r} iid={iid}: {e}"
                )
                continue
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
        conn.commit()
        if inserted or skipped:
            log.info(
                f"[db] {platform} {keyword!r} 入库 inserted={inserted} skipped={skipped}"
            )
    except sqlite3.Error as e:
        log.warning(f"[db] tx failed platform={platform} kw={keyword!r}: {e}")
    finally:
        try:
            conn.close()
        except sqlite3.Error:
            pass


def _coerce_price(value: Any) -> float | None:
    """price 字段宽松转 float;失败返回 None(让 db 字段留空,不让一行因价格炸)。"""
    if value is None or value == "" or value == "None":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 按关键词 CSV：跨会话累积 + item_id 级断点续跑源
# ---------------------------------------------------------------------------
_BY_KEYWORD_DIR = LOG_DIR / "by_keyword"


def _keyword_slug(keyword: str) -> str:
    """关键词 → 文件名安全 slug。

    规则：
      - 纯 ASCII + 常见符号：保留可读名（下划线替非安全字符，截 80 字）
      - 含中文/非 ASCII：md5[:10] 前缀 + 可读头 8 字符（若有）便于人工区分
    """
    import hashlib
    import re as _re
    kw = keyword.strip()
    if not kw:
        return "empty"
    if all(ord(c) < 128 for c in kw):
        slug = _re.sub(r"[^A-Za-z0-9_.\-]+", "_", kw).strip("_")
        return (slug or "unknown")[:80]
    prefix = _re.sub(r"[^\w]+", "", kw, flags=_re.UNICODE)[:8]
    h = hashlib.md5(kw.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{h}" if prefix else h


def _keyword_csv_path(keyword: str) -> Path:
    """按关键词定位 per-keyword CSV。"""
    return _BY_KEYWORD_DIR / f"{_keyword_slug(keyword)}.csv"


def _ensure_keyword_csv(path: Path) -> None:
    """不存在就创建并写表头；已存在不改。"""
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        csv.DictWriter(f, fieldnames=_PRODUCT_CSV_COLS).writeheader()


def _load_keyword_seen_ids(keyword: str, platform: str) -> set[str]:
    """读 per-keyword CSV，返回 (keyword, platform) 下已知的 item_id 集合。

    文件不存在返回空集；读失败记 warning 返回空集（宁可重写也不中断）。
    """
    path = _keyword_csv_path(keyword)
    if not path.exists():
        return set()
    seen: set[str] = set()
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("platform") == platform:
                    iid = (row.get("item_id") or "").strip()
                    if iid:
                        seen.add(iid)
    except (OSError, csv.Error) as e:
        log.warning(f"读 {path} 失败（{e}），按空集处理")
    return seen


def _summarize_keyword_progress(keywords: list[str], platforms: list[str]) -> None:
    """启动时打印"每个 (kw, platform) 已有多少条"。空仓库返回全 0 一行。"""
    if not keywords:
        return
    rows: list[tuple[str, str, int]] = []
    for kw in keywords:
        for pf in platforms:
            n = len(_load_keyword_seen_ids(kw, pf))
            rows.append((kw, pf, n))
    total = sum(n for _, _, n in rows)
    if total == 0:
        log.info(f"📊 by_keyword 进度：空（{len(keywords)} 关键词 × {len(platforms)} 平台）")
        return
    log.info(f"📊 by_keyword 进度（共 {total} 条已落盘）：")
    for kw, pf, n in rows:
        if n:
            log.info(f"   {kw!r:20s} / {pf:7s}: {n} 条 → {_keyword_csv_path(kw).name}")


_BATCH_COUNTER: list[int] = [0]


def _reset_batch_counter() -> None:
    _BATCH_COUNTER[0] = 0


def _apply_fast_preset() -> None:
    """--fast 命中时把节流常量覆盖成激进档，并打印改前/改后对比。

    用 globals() 改模块级常量，不动调用点（调用点直接引用常量名）。
    JD_* 常量不在预设里，刻意保留——JD 风控最敏感。
    """
    g = globals()
    for k, new in _FAST_PRESET.items():
        old = g.get(k)
        g[k] = new
        log.warning(f"[fast] 节流覆盖 {k}: {old} → {new}")
    log.warning(
        "[fast] 已启用激进档；淘宝最敏感，请盯 logs/debug/taobao_*.png；"
        "命中 rgv587_flag 立即去掉 --fast"
    )


def _maybe_batch_rest(new_items: int, *, label: str = "") -> None:
    """全局累计 BATCH_SIZE 条触发一次 BATCH_REST_SEC 长休。

    每跨过一个 BATCH_SIZE 倍数就 sleep 一次（同一次产出跨多倍只休一次，避免一关键词
    返回上百条时 sleep 数分钟）。在 main() / _run_dict_mode() 入口需调 _reset_batch_counter
    防止跨次运行串扰。
    """
    if new_items <= 0:
        return
    before = _BATCH_COUNTER[0]
    after = before + new_items
    _BATCH_COUNTER[0] = after
    if after // BATCH_SIZE > before // BATCH_SIZE:
        log.info(
            f"批次节流：累计 {after} 条（+{new_items} from {label or '未知'}），"
            f"≥{BATCH_SIZE} 倍数 → 休眠 {BATCH_REST_SEC}s"
        )
        brief(
            f"账号保护节流：累计 {after} 条，休眠 {_format_duration(BATCH_REST_SEC)} "
            f"后继续（来源 {label or '未知'}）"
        )
        time.sleep(BATCH_REST_SEC)


def _keyword_sleep(idx: int, total: int, platform: str) -> None:
    """同平台关键词间节流：每爬 BATCH_SIZE 个才休息一次（批量模式，减少空等）。

    - 非 batch 边界直接放行，不休眠
    - JD 用 JD_KEYWORD_* 常量，节流更重
    - 其他平台 20s ±5s（15~25s），每 4 个关键词一次
    """
    if idx == 0:
        return
    import random
    if platform == "jd":
        batch = JD_KEYWORD_BATCH_SIZE
        base = JD_KEYWORD_DELAY_SEC
        jitter = JD_KEYWORD_JITTER_SEC
    else:
        batch = KEYWORD_BATCH_SIZE
        base = KEYWORD_DELAY_SEC
        jitter = KEYWORD_JITTER_SEC
    if idx % batch != 0:
        return
    wait = base + random.uniform(-jitter, jitter)
    wait = max(3.0, wait)
    log.info(
        f"[{platform}] 批量节流：已连续爬 {batch} 个，休眠 {wait:.1f}s "
        f"（{idx + 1}/{total}，保护账号）"
    )
    brief(
        f"{platform}: 关键词批次节流，休眠 {_format_duration(wait)} "
        f"后继续（{idx + 1}/{total}）"
    )
    time.sleep(wait)


def run_platform_batch(
    p: Any,
    platform: str,
    keywords: list[str],
    max_pages: int,
    headed: bool,
    *,
    session_state: dict | None = None,
    products_csv: Path | None = None,
) -> dict[str, SearchResult]:
    """同平台批量：一次 browser + 一次登录态 + 循环多关键词。

    优化点：
    - 只开一次 chromium + 只创建一次 context + 只注入一次 stealth
    - 每关键词间 _keyword_sleep 节流
    - 所有关键词爬完后统一 refresh_storage_state 一次（减少盘 I/O）
    - 单个关键词失败不影响其他关键词

    中断续跑：
    - 若 session_state 传入，每个关键词完成后把 status/count/error 写回
      state["progress"][kw][platform] 并调用 _save_active_state
    - 若 products_csv 传入，products 同步 append 到该 CSV
    - 启动前扫 session_state 已完成的 (kw, platform)，直接跳过
    """
    func, state_name, mobile = CRAWLERS[platform]
    state_path = STATE_DIR / state_name
    results: dict[str, SearchResult] = {
        kw: SearchResult(
            platform=platform, keyword=kw, pages_requested=max_pages, success=False
        )
        for kw in keywords
    }

    # 续跑：预扫已完成的关键词（跳过集合；不恢复 products，products 已在 running CSV 里）
    skip_set: set[str] = set()
    if session_state is not None:
        prog_map = session_state.get("progress", {})
        for kw in keywords:
            entry = prog_map.get(kw, {}).get(platform)
            if entry and entry.get("status") == "completed":
                skip_set.add(kw)
                results[kw].success = True  # 标识成功；products 保持空，本次内存中不再持有
        if skip_set:
            log.info(
                f"[{platform}] 续跑：跳过已完成 {len(skip_set)}/{len(keywords)} 关键词"
            )
    browser = None
    context = None
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

        any_success = False

        def _persist(
            kw_: str,
            status: str,
            count: int,
            err: str | None,
            err_type: str | None,
            *,
            emit: bool = True,
        ) -> None:
            """把一条 (kw, platform) 的结果写回 session_state 并落盘。"""
            if session_state is None:
                return
            session_state.setdefault("progress", {}).setdefault(kw_, {})[platform] = {
                "status": status,
                "count": count,
                "error": (err or "")[:500] if err else None,
                "error_type": err_type,
            }
            try:
                _save_active_state(session_state)
            except OSError as save_err:
                log.warning(f"[{platform}] state 写盘失败：{save_err}")
            if emit:
                status_label = {
                    "completed": f"完成 {count} 条",
                    "failed": f"失败 {err_type or 'Unknown'}",
                    "skipped": "已跳过",
                }.get(status, status)
                emit_progress_brief(session_state, f"{platform} / {kw_}: {status_label}")

        def _mark_remaining_skipped(start_idx: int, reason: str, err_type: str) -> None:
            if session_state is None:
                return
            skipped = 0
            for rest_kw in keywords[start_idx:]:
                if rest_kw in skip_set:
                    continue
                results[rest_kw].error = reason
                results[rest_kw].error_type = err_type
                session_state.setdefault("progress", {}).setdefault(rest_kw, {})[platform] = {
                    "status": "skipped",
                    "count": 0,
                    "error": reason[:500],
                    "error_type": err_type,
                }
                skipped += 1
            if skipped:
                try:
                    _save_active_state(session_state)
                except OSError as save_err:
                    log.warning(f"[{platform}] state 写盘失败：{save_err}")
                emit_progress_brief(
                    session_state,
                    f"{platform}: {reason}，跳过剩余 {skipped} 个关键词",
                )

        for idx, kw in enumerate(keywords):
            if kw in skip_set:
                continue  # 续跑：已完成的不重跑，不休眠不搜索
            _keyword_sleep(idx, len(keywords), platform)
            log.info(
                f"[{platform}] ---- 关键词 {idx + 1}/{len(keywords)}: {kw!r} ----"
            )
            emit_progress_brief(session_state, f"{platform} / {kw}: 开始爬取")
            try:
                products = func(context, kw, max_pages, headed=headed)
                results[kw].products = products
                results[kw].success = True
                any_success = any_success or bool(products)
                log.info(f"[{platform}] '{kw}' 完成，共 {len(products)} 条")
                if products_csv is not None:
                    _append_products_csv(products_csv, kw, platform, products)
                _persist(kw, "completed", len(products), None, None)
                _maybe_batch_rest(len(products), label=f"[{platform}] {kw!r}")
            except SpiderError as e:
                results[kw].error = str(e)
                results[kw].error_type = type(e).__name__
                log.error(f"[{platform}] '{kw}' 失败: {e}")
                _persist(kw, "failed", 0, str(e), type(e).__name__)
                if isinstance(e, (LoginRequiredError, AntiSpiderError)):
                    reason = (
                        f"账号/风控保护触发：{type(e).__name__}，停止 {platform} "
                        "剩余关键词，避免连续请求影响账号"
                    )
                    log.warning(f"[{platform}] {reason}")
                    brief(reason)
                    _mark_remaining_skipped(idx + 1, reason, "SkippedAfterRisk")
                    break
            except Exception as e:
                # 浏览器被用户手动关闭 / 崩溃 / 目标关闭类错误，友好归类不打整段 traceback
                err_name = type(e).__name__
                err_msg = str(e)[:200]
                if "TargetClosed" in err_name or "has been closed" in err_msg:
                    results[kw].error = f"浏览器/页面已关闭：{err_msg}"
                    results[kw].error_type = "BrowserClosed"
                    log.error(
                        f"[{platform}] '{kw}' 浏览器/页面被关闭（可能手动关窗或进程崩溃），"
                        f"剩余关键词将跳过本平台"
                    )
                    _persist(kw, "failed", 0, results[kw].error, "BrowserClosed")
                    _mark_remaining_skipped(
                        idx + 1,
                        "浏览器/页面已关闭，停止本平台剩余关键词；下次可同命令续跑",
                        "BrowserClosed",
                    )
                    # 整个平台 context 已失效，没必要再尝试下一关键词
                    break
                results[kw].error = f"{err_name}: {err_msg}"
                results[kw].error_type = "UnknownError"
                log.error(
                    f"[{platform}] '{kw}' 未捕获异常:\n{traceback.format_exc()}"
                )
                _persist(kw, "failed", 0, f"{err_name}: {err_msg}", "UnknownError")

        # 全部关键词跑完后统一刷新登录态
        if any_success and context is not None:
            try:
                refresh_storage_state(context, platform)
            except Exception as e:
                log.warning(f"[{platform}] 批次末尾刷新 state 失败: {e}")
    except Exception as e:
        log.error(f"[{platform}] context 级异常:\n{traceback.format_exc()}")
        for kw in keywords:
            if not results[kw].error:
                results[kw].error = f"{type(e).__name__}: {e}"
                results[kw].error_type = "UnknownError"
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
    return results


def print_report(
    keywords: list[str],
    results: dict[str, dict[str, SearchResult]],
    platforms: list[str],
) -> None:
    """按关键词分组打印；JSON 以 {keyword: {platform: ...}} 嵌套结构输出。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'=' * 72}")
    print(f"批量搜索报告 · {len(keywords)} 关键词 × {len(platforms)} 平台")
    print(f"爬取时间：{ts}")
    print(f"{'=' * 72}")

    for kw in keywords:
        print(f"\n── 关键词 {kw!r} ──")
        per_kw = results.get(kw, {})
        for name in platforms:
            r = per_kw.get(name)
            if r is None:
                print(f"[SKIP] {name.upper():8s} 未执行")
                continue
            status = "OK" if r.success else "FAIL"
            print(
                f"[{status}] {name.upper():8s} 去重后商品={r.count}  "
                + (f"错误={r.error_type}" if not r.success else "")
            )
            if not r.success and r.error:
                print(f"   原因: {r.error[:160]}")
            shown = sorted(
                r.products,
                key=lambda p: (p.current_price is None, p.current_price or 1e9),
            )[:5]
            for i, pr in enumerate(shown, 1):
                price = f"¥{pr.current_price:.2f}" if pr.current_price else "N/A"
                print(f"   {i}. {price:10s}  {pr.title[:50]}")
            if r.count > 5:
                print(f"   ... 另有 {r.count - 5} 条（见 JSON）")

    ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_json = LOG_DIR / f"report_pw_{ts_tag}.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(
            {
                "keywords": keywords,
                "platforms": platforms,
                "crawled_at": datetime.now().isoformat(),
                "results": {
                    kw: {
                        name: {
                            "success": r.success,
                            "pages_requested": r.pages_requested,
                            "count": r.count,
                            "error": r.error,
                            "error_type": r.error_type,
                            "products": [asdict(pr) for pr in r.products],
                        }
                        for name, r in per_kw.items()
                    }
                    for kw, per_kw in results.items()
                },
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # CSV：两个文件，products 明细 + summary 摘要。UTF-8-SIG 加 BOM，Excel 不乱码
    out_products = LOG_DIR / f"report_pw_{ts_tag}_products.csv"
    out_summary = LOG_DIR / f"report_pw_{ts_tag}_summary.csv"
    product_cols = [
        "keyword", "platform", "item_id", "title",
        "current_price", "origin_price", "shop_name", "location",
        "is_second_hand", "url", "image_url", "crawled_at",
    ]
    with open(out_products, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=product_cols)
        w.writeheader()
        for kw, per_kw in results.items():
            for name, r in per_kw.items():
                for pr in r.products:
                    d = asdict(pr)
                    w.writerow({
                        "keyword": kw,
                        "platform": name,
                        "item_id": d.get("item_id", ""),
                        "title": d.get("title", ""),
                        "current_price": d.get("current_price", "") or "",
                        "origin_price": d.get("origin_price", "") or "",
                        "shop_name": d.get("shop_name", "") or "",
                        "location": d.get("location", "") or "",
                        "is_second_hand": d.get("is_second_hand", False),
                        "url": d.get("url", ""),
                        "image_url": d.get("image_url", "") or "",
                        "crawled_at": datetime.now().isoformat(timespec="seconds"),
                    })

    summary_cols = [
        "keyword", "platform", "success", "count",
        "pages_requested", "error_type", "error",
    ]
    with open(out_summary, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=summary_cols)
        w.writeheader()
        for kw, per_kw in results.items():
            for name, r in per_kw.items():
                w.writerow({
                    "keyword": kw,
                    "platform": name,
                    "success": "Y" if r.success else "N",
                    "count": r.count,
                    "pages_requested": r.pages_requested,
                    "error_type": r.error_type or "",
                    "error": (r.error or "")[:500],
                })

    print(f"\n详细 JSON 报告：{out_json}")
    print(f"CSV · 商品明细：{out_products}")
    print(f"CSV · 任务摘要：{out_summary}")
    print(f"日志：logs/crawl_pw_{datetime.now().strftime('%Y-%m-%d')}.log")


def _parse_keywords(args: argparse.Namespace) -> list[str]:
    """从命令行解析关键词列表。优先级：--keywords-file > --keywords > positional keyword。"""
    if args.keywords_file:
        path = Path(args.keywords_file)
        if not path.exists():
            raise FileNotFoundError(f"--keywords-file 指定的文件不存在: {path}")
        kws: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                kws.append(s)
        return kws
    if args.keywords:
        return [k.strip() for k in args.keywords.split(",") if k.strip()]
    return [args.keyword]


# ---------------------------------------------------------------------------
# 阶段 12：字典模式（--from-dict 读 data/skus/{week}/_merged/，按 SKU 出 JSON）
# ---------------------------------------------------------------------------


def _load_search_terms_from_dict(
    week: str,
    *,
    categories: list[str] | None = None,
    brands: list[str] | None = None,
    chips: list[str] | None = None,
    out_base: Path = DATA_DIR,
) -> list[SkuRef]:
    """从 data/skus/{week}/_merged/{category}/{brand}.csv 读 SKU 全名作为搜索词。"""
    merged_root = out_base / "skus" / week / "_merged"
    if not merged_root.exists():
        raise FileNotFoundError(
            f"字典合并产物不存在：{merged_root}\n"
            f"请先按顺序跑：\n"
            f"  python scripts/dict_build.py --week {week}\n"
            f"  python scripts/dict_merge.py --week {week}"
        )
    # Codex 审 S5：输入统一 lower+strip 再比较（目录名/文件 stem 都是小写）
    want_cats = (
        {c.strip().lower() for c in categories if c.strip()}
        if categories else None
    )
    want_brands = (
        {b.strip().lower() for b in brands if b.strip()}
        if brands else None
    )
    want_chips = [c.strip().lower() for c in (chips or []) if c.strip()]

    out: list[SkuRef] = []
    for cat_dir in sorted(p for p in merged_root.iterdir() if p.is_dir()):
        if want_cats and cat_dir.name not in want_cats:
            continue
        for brand_file in sorted(cat_dir.glob("*.csv")):
            brand = brand_file.stem
            if want_brands and brand not in want_brands:
                continue
            with open(brand_file, "r", encoding="utf-8-sig", newline="") as fp:
                reader = csv.DictReader(fp)
                for row in reader:
                    title = (row.get("sku_title") or "").strip()
                    if not title:
                        continue
                    chip = (row.get("chip") or "").strip()
                    if want_chips and not any(c in chip.lower() for c in want_chips):
                        continue
                    alts_raw = row.get("alt_titles") or "[]"
                    try:
                        alts = json.loads(alts_raw)
                        alts = [a for a in alts if isinstance(a, str) and a]
                    except (ValueError, TypeError):
                        alts = []
                    out.append(SkuRef(
                        sku_title=title, category=cat_dir.name,
                        brand=brand, chip=chip, alt_titles=alts,
                    ))
    return out


_SKU_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _sku_slug(title: str, max_len: int = 80) -> str:
    """SKU 全名 → 文件名安全 slug。

    总是附加 6 位 md5 尾：非截断情况下的 punctuation/大小写变体（"Foo-Bar" vs
    "Foo_Bar"）也会产生同一 normalized 前缀，必须用 hash 区分（Codex 审 S1 修）。
    """
    raw = _SKU_SLUG_RE.sub("-", title.lower()).strip("-") or "unknown"
    tail = hashlib.md5(title.encode("utf-8")).hexdigest()[:6]
    if len(raw) + 7 <= max_len:
        return f"{raw}-{tail}"
    return f"{raw[:max_len - 7]}-{tail}"


def _dict_output_path(out_base: Path, week: str, ref: SkuRef) -> Path:
    return (
        out_base / "prices" / week / ref.category / ref.brand
        / f"{_sku_slug(ref.sku_title)}.json"
    )


_ERR_TYPE_TO_STATUS: dict[str, str] = {
    "NetworkError": "network_error",
    "LoginRequiredError": "login_required",
    "AntiSpiderError": "anti_spider",
    "RateLimitError": "rate_limit",
    "ParseError": "parse_error",
    "SkippedAfterRisk": "anti_spider",
    "BrowserClosed": "network_error",
}


def _platform_status_payload(result: SearchResult) -> dict[str, Any]:
    """SearchResult → 阶段 12 输出 payload。status 严格遵守阶段 12 md 枚举：
    ok | no_result | network_error | login_required | anti_spider | rate_limit | parse_error
    """
    if not result.success:
        status = _ERR_TYPE_TO_STATUS.get(result.error_type or "", "parse_error")
        return {"status": status, "items": [], "error": result.error}
    if result.count == 0:
        return {"status": "no_result", "items": [], "error": None}
    return {
        "status": "ok",
        "items": [asdict(p) for p in result.products],
        "error": None,
    }


def _write_dict_mode_result(
    out_base: Path,
    week: str,
    ref: SkuRef,
    per_platform: dict[str, SearchResult],
    search_terms_used: list[str],
) -> Path:
    """把单个 SKU 的多平台结果落盘为 data/prices/.../{sku_slug}.json。原子写。"""
    path = _dict_output_path(out_base, week, ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "week": week,
        "category": ref.category,
        "brand": ref.brand,
        "chip": ref.chip,
        "sku_title": ref.sku_title,
        "search_terms_used": search_terms_used,
        "crawled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "platforms": {
            pf: _platform_status_payload(per_platform.get(pf))
            for pf in sorted(per_platform)
        },
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return path


def _estimate_duration(
    n_keywords: int, n_platforms: int, max_pages: int
) -> float:
    """估算批量任务耗时（秒），让用户知道会跑多久。"""
    per_kw_per_pf = 25 + max_pages * 8  # 粗略估计：启动+搜索 25s，每页 8s
    crawl_time = n_keywords * n_platforms * per_kw_per_pf
    # 关键词级 batch 模式（同平台连续 N 个关键词休一次）
    batch_rests_per_pf = max(0, (n_keywords - 1) // KEYWORD_BATCH_SIZE)
    keyword_throttle = batch_rests_per_pf * KEYWORD_DELAY_SEC * n_platforms
    # 成果级 batch 节流：粗估 30 条/页（实际平台差异大）
    items_est = n_keywords * n_platforms * max_pages * 30
    item_rests = max(0, items_est // BATCH_SIZE - 1)
    item_throttle = item_rests * BATCH_REST_SEC
    platform_throttle = (n_platforms - 1) * PLATFORM_DELAY_SEC
    return crawl_time + keyword_throttle + item_throttle + platform_throttle


def _run_dict_mode(args: argparse.Namespace) -> int:
    """阶段 12：字典模式主入口。不污染原有 CLI 流程。"""
    _reset_batch_counter()
    try:
        sku_refs = _load_search_terms_from_dict(
            week=args.from_dict,
            categories=(args.category.split(",") if args.category else None),
            brands=(args.brand.split(",") if args.brand else None),
            chips=(args.chip.split(",") if args.chip else None),
        )
    except FileNotFoundError as e:
        log.error(str(e))
        return 2

    if args.resume:
        before = len(sku_refs)
        sku_refs = [
            s for s in sku_refs
            if not _dict_output_path(DATA_DIR, args.from_dict, s).exists()
        ]
        log.info(f"--resume: 跳过已存在 {before - len(sku_refs)} 条，剩余 {len(sku_refs)}")

    if not sku_refs:
        log.info(f"字典模式：无可跑 SKU（week={args.from_dict}, 过滤后为空）")
        return 0

    if args.dry_run:
        print(f"[dry-run] {len(sku_refs)} SKU 将被搜索（week={args.from_dict}）")
        for s in sku_refs[:50]:
            print(
                f"  {s.category:12s} {s.brand:14s} "
                f"chip={s.chip[:26]:26s} | {s.sku_title}"
            )
        if len(sku_refs) > 50:
            print(f"  ... 共 {len(sku_refs)} 条")
        return 0

    platforms = args.only or list(DEFAULT_PLATFORMS)
    if "jd" not in platforms and args.only is None:
        log.info("JD 默认禁用（账号已封，2026-04 暂停）。要跑加 --only jd 显式启用")

    # 展开 search terms：canonical + alt_titles（Codex 审 C1 修复）
    include_alts = not args.no_alt_titles
    ref_terms: dict[int, list[str]] = {}  # id(ref) → 该 SKU 要搜的 term 列表
    all_terms: list[str] = []
    for ref in sku_refs:
        terms = [ref.sku_title]
        if include_alts:
            terms.extend(t for t in ref.alt_titles if t and t != ref.sku_title)
        ref_terms[id(ref)] = terms
        all_terms.extend(terms)
    unique_terms = list(dict.fromkeys(all_terms))

    # 字典模式复用同一套 active state（签名基于 unique_terms + platforms + pages）
    signature = _compute_session_signature(unique_terms, platforms, args.pages)
    existing = _load_active_state()
    session_state: dict
    products_csv: Path
    if existing is not None and existing.get("signature") == signature:
        session_state = existing
        products_csv = Path(existing.get("products_csv", LOG_DIR / f"crawl_running_{existing.get('session_id', 'sess_unknown')}.csv"))
        if not products_csv.exists():
            products_csv.parent.mkdir(parents=True, exist_ok=True)
            with products_csv.open("w", encoding="utf-8-sig", newline="") as f:
                csv.DictWriter(f, fieldnames=_PRODUCT_CSV_COLS).writeheader()
            session_state["products_csv"] = str(products_csv)
        done_cnt = sum(
            1 for per_kw in session_state.get("progress", {}).values()
            for entry in per_kw.values() if entry.get("status") == "completed"
        )
        log.info(
            f"🔄 字典模式续跑 {session_state.get('session_id')}，"
            f"已完成 {done_cnt}/{len(unique_terms) * len(platforms)} (kw × 平台)"
        )
    elif existing is not None and existing.get("signature") != signature:
        log.warning(
            "检测到 state/crawl_active.json 签名不匹配；若要开新任务加 --fresh"
        )
        return 2
    else:
        session_state, products_csv = _new_session_state(unique_terms, platforms, args.pages)
        log.info(f"🆕 字典模式新会话 {session_state['session_id']}")

    est_sec = _estimate_duration(len(unique_terms), len(platforms), args.pages)
    session_state["estimated_total_sec"] = est_sec
    try:
        _save_active_state(session_state)
    except OSError as e:
        log.warning(f"写入预估耗时失败：{e}")
    log.info(
        f"字典模式：SKU={len(sku_refs)} 搜索词={len(unique_terms)}"
        f"{'（含 alts）' if include_alts else '（仅 canonical）'} × "
        f"平台={len(platforms)} × 页={args.pages}  headed={args.headed}"
    )
    log.info(f"预估耗时 {est_sec / 60:.1f} 分钟（含节流）")
    log.info(f"进度：{ACTIVE_STATE_FILE}  CSV：{products_csv}  Ctrl+C 可安全中断续跑")
    brief(
        f"字典模式任务开始：{len(unique_terms)} 搜索词 × {len(platforms)} 平台 × "
        f"{args.pages} 页，预估 {_format_duration(est_sec)}"
    )
    emit_progress_brief(session_state, "准备启动浏览器")
    _summarize_keyword_progress(unique_terms, platforms)

    results: dict[str, dict[str, SearchResult]] = {kw: {} for kw in unique_terms}
    try:
        with sync_playwright() as p:
            for pf_idx, pf_name in enumerate(platforms):
                if pf_idx > 0:
                    log.info(f"跨平台节流 {PLATFORM_DELAY_SEC}s → {pf_name}")
                    brief(
                        f"跨平台节流：休眠 {_format_duration(PLATFORM_DELAY_SEC)} "
                        f"后进入 {pf_name}"
                    )
                    time.sleep(PLATFORM_DELAY_SEC)
                log.info(f"========== 开始 {pf_name}（字典模式 {len(unique_terms)} KW）==========")
                emit_progress_brief(session_state, f"开始平台 {pf_name}")
                t0 = time.monotonic()
                batch_result = run_platform_batch(
                    p, pf_name, unique_terms, args.pages, args.headed,
                    session_state=session_state, products_csv=products_csv,
                )
                platform_elapsed = time.monotonic() - t0
                log.info(f"========== {pf_name} 完成，耗时 {platform_elapsed:.1f}s ==========")
                emit_progress_brief(
                    session_state,
                    f"平台 {pf_name} 完成，耗时 {_format_duration(platform_elapsed)}",
                )
                for kw, r in batch_result.items():
                    results[kw][pf_name] = r
    except KeyboardInterrupt:
        log.warning("⏸ Ctrl+C 中断；进度已落盘，下次启动续跑")
        brief("任务被 Ctrl+C 中断；进度已落盘，下次同参数会续跑")
        return 130

    # 按 SKU 落盘：聚合该 SKU 所有 term 的结果（跨 alt_titles）
    written = 0
    for ref in sku_refs:
        used_terms = ref_terms[id(ref)]
        per_pf_merged: dict[str, SearchResult] = {}
        for pf in platforms:
            # 合并该 SKU 所有 term 在该平台的结果
            combined: SearchResult | None = None
            for term in used_terms:
                r = results.get(term, {}).get(pf)
                if r is None:
                    continue
                if combined is None:
                    combined = SearchResult(
                        platform=pf, keyword=ref.sku_title,
                        pages_requested=args.pages,
                        success=r.success, error=r.error, error_type=r.error_type,
                        products=list(r.products),
                    )
                else:
                    # 有任一成功即视为成功；products 合并（可能含重复 item_id）
                    if r.success:
                        combined.success = True
                        combined.error = None
                        combined.error_type = None
                        combined.products.extend(r.products)
                    elif not combined.success and combined.error is None:
                        combined.error = r.error
                        combined.error_type = r.error_type
            if combined is not None:
                per_pf_merged[pf] = combined
        _write_dict_mode_result(
            DATA_DIR, args.from_dict, ref, per_pf_merged,
            search_terms_used=used_terms,
        )
        written += 1
    log.info(
        f"字典模式 done：写入 {written} sku.json → "
        f"{DATA_DIR / 'prices' / args.from_dict}"
    )
    # 归档 active state + 重命名 running CSV 为最终报告
    ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_csv = LOG_DIR / f"report_pw_dict_{args.from_dict}_{ts_tag}_products.csv"
    try:
        products_csv.rename(final_csv)
    except OSError as e:
        log.warning(f"rename running CSV 失败：{e}")
        final_csv = products_csv
    _archive_active_state(session_state, final_csv)
    log.info(f"✅ 字典模式完成，商品明细 → {final_csv}")
    emit_progress_brief(session_state, f"字典模式完成，商品明细 {final_csv}")
    any_ok = any(
        r.success for per_kw in results.values() for r in per_kw.values()
    )
    return 0 if any_ok else 1


def main() -> int:
    _reset_batch_counter()
    parser = argparse.ArgumentParser(
        description="多平台商品搜索爬虫 · Playwright 版（支持批量关键词 + 字典模式）"
    )
    parser.add_argument(
        "keyword", nargs="?", default="i5-12400F",
        help="单关键词（未指定 --keywords/--keywords-file 时使用）",
    )
    parser.add_argument(
        "--keywords",
        help='批量：逗号分隔的关键词列表，如 "i5-12400F,R7 7800X3D,i9-14900K"',
    )
    parser.add_argument(
        "--keywords-file",
        help="批量：每行一个关键词的文本文件（# 开头为注释）",
    )
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
    parser.add_argument(
        "--fast", action="store_true",
        help="激进节流档：BATCH_SIZE 100/REST 15s/KEYWORD_DELAY 5s/PLATFORM 2s（JD 不变）。"
             "理论提速 3-4 倍；淘宝最易触发 rgv587_flag，第一次跑务必盯 logs/debug/。",
    )
    # 阶段 12：字典模式参数 --------------------------------------------------
    parser.add_argument(
        "--from-dict", metavar="WEEK",
        help="从 data/skus/{WEEK}/_merged/ 读 SKU 全名作为关键词（ISO 周号，如 2026-W17）",
    )
    parser.add_argument(
        "--category",
        help="字典模式：逗号分隔品类 slug（如 gpu,cpu），默认全量",
    )
    parser.add_argument(
        "--brand",
        help="字典模式：逗号分隔品牌 slug（如 asus,galax），默认全量",
    )
    parser.add_argument(
        "--chip",
        help="字典模式：逗号分隔 chip 子串（如 'RTX 4090,RTX 3090'），默认全量",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="字典模式：只打印将搜的 SKU 列表（前 50 + 总数），不真跑",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="字典模式：跳过已存在的 sku.json（断点续跑；仅检查文件存在，不验证完整性）",
    )
    parser.add_argument(
        "--fresh", action="store_true",
        help="非字典模式：丢弃已有 state/crawl_active.json 重开会话",
    )
    parser.add_argument(
        "--no-alt-titles", action="store_true",
        help="字典模式：仅搜 canonical sku_title，不额外搜 alt_titles（默认会搜以提高召回）",
    )
    parser.add_argument(
        "--failed-retry", metavar="FILE",
        help="字典模式：从 failed.csv 读失败 SKU 列表重跑（暂未实现，留作后续扩展）",
    )
    parser.add_argument(
        "--state-suffix", metavar="SUFFIX", default="",
        help="多账号并发场景:给 state 文件名加后缀,如 --state-suffix B 会用"
             " state/xianyu_state_B.json 和 state/crawl_active_B.json,跟主账号(空 suffix)"
             "完全隔离。先用 scripts/login_helper.py 配合 --state-suffix 登录到对应文件。",
    )
    args = parser.parse_args()

    # --state-suffix:同名后缀影响 platform state 文件 + crawl_active 文件
    # 让账号 A(空 suffix) 和账号 B(--state-suffix B) 在 state 层完全隔离
    if args.state_suffix:
        suffix = args.state_suffix.strip().lstrip("_")
        if not suffix.replace("_", "").replace("-", "").isalnum():
            log.error("--state-suffix 只允许字母/数字/下划线/连字符,得到: %r", suffix)
            return 2
        # 1) CRAWLERS dict 内的 state 文件名加 suffix
        for _plat, (_func, _name, _mobile) in list(CRAWLERS.items()):
            _base = _name.rsplit(".json", 1)[0]
            CRAWLERS[_plat] = (_func, f"{_base}_{suffix}.json", _mobile)
        # 2) crawl_active.json 也加 suffix
        global ACTIVE_STATE_FILE
        ACTIVE_STATE_FILE = ROOT / "state" / f"crawl_active_{suffix}.json"
        log.info("[multi-account] state suffix='%s' applied", suffix)

    if args.fast:
        _apply_fast_preset()

    # 互斥检查：--from-dict 不能与 --keywords/--keywords-file 混用
    if args.from_dict and (args.keywords or args.keywords_file):
        log.error("--from-dict 与 --keywords/--keywords-file 互斥；请二选一")
        return 2
    if args.failed_retry and not args.from_dict:
        log.error("--failed-retry 必须配合 --from-dict 使用")
        return 2
    if args.failed_retry:
        log.warning("--failed-retry 尚未实现，当前仅打印警告并按 --from-dict 正常流程跑")

    # 字典模式：从 _merged 读搜索词 + 按 SKU 出 JSON
    if args.from_dict:
        return _run_dict_mode(args)

    try:
        keywords = _parse_keywords(args)
    except FileNotFoundError as e:
        log.error(str(e))
        return 2
    if not keywords:
        log.error("关键词列表为空")
        return 2

    platforms = args.only or list(DEFAULT_PLATFORMS)
    if "jd" not in platforms and args.only is None:
        log.info("JD 默认禁用（账号被风控，2026-04 暂停）。要跑加 --only jd 显式启用")

    # ---------- 会话状态（中断续跑） ----------
    signature = _compute_session_signature(keywords, platforms, args.pages)
    existing = _load_active_state()
    session_state: dict
    products_csv: Path
    if args.fresh and existing is not None:
        log.info("--fresh：归档旧 active state，开新会话")
        _archive_active_state(existing)
        existing = None
    if existing is not None and existing.get("signature") == signature:
        # 续跑：同一任务签名
        session_state = existing
        products_csv = Path(existing.get("products_csv", LOG_DIR / f"crawl_running_{existing.get('session_id', 'sess_unknown')}.csv"))
        if not products_csv.exists():
            # running CSV 丢了，重新建个带表头的
            products_csv.parent.mkdir(parents=True, exist_ok=True)
            with products_csv.open("w", encoding="utf-8-sig", newline="") as f:
                csv.DictWriter(f, fieldnames=_PRODUCT_CSV_COLS).writeheader()
            session_state["products_csv"] = str(products_csv)
        done_cnt = sum(
            1 for per_kw in session_state.get("progress", {}).values()
            for entry in per_kw.values() if entry.get("status") == "completed"
        )
        total_cells = len(keywords) * len(platforms)
        log.info(
            f"🔄 续跑会话 {session_state.get('session_id')}（signature {signature}），"
            f"已完成 {done_cnt}/{total_cells} (kw × 平台)"
        )
    elif existing is not None and existing.get("signature") != signature:
        log.warning(
            f"检测到 state/crawl_active.json 存在但签名不同（当前任务 {signature}，旧 "
            f"{existing.get('signature')}）。请确认要：\n"
            f"  - 继续旧任务？请用旧参数重跑（见 state 文件里的 keywords/platforms/pages）\n"
            f"  - 开新任务？请加 --fresh\n"
        )
        return 2
    else:
        session_state, products_csv = _new_session_state(keywords, platforms, args.pages)
        log.info(f"🆕 新会话 {session_state['session_id']}（signature {signature}）")

    est_sec = _estimate_duration(len(keywords), len(platforms), args.pages)
    session_state["estimated_total_sec"] = est_sec
    try:
        _save_active_state(session_state)
    except OSError as e:
        log.warning(f"写入预估耗时失败：{e}")
    log.info(
        f"批量任务：{len(keywords)} 关键词 × {len(platforms)} 平台 × {args.pages} 页 "
        f"headed={args.headed}"
    )
    log.info(f"关键词: {keywords}")
    log.info(f"预估耗时约 {est_sec / 60:.1f} 分钟（含节流保护，请耐心）")
    log.info(f"进度文件：{ACTIVE_STATE_FILE}  商品 CSV：{products_csv}")
    log.info("中断可安全 Ctrl+C；下次同命令启动会自动续跑。")
    brief(
        f"任务开始：{len(keywords)} 关键词 × {len(platforms)} 平台 × "
        f"{args.pages} 页，预估 {_format_duration(est_sec)}"
    )
    emit_progress_brief(session_state, "准备启动浏览器")
    _summarize_keyword_progress(keywords, platforms)

    results: dict[str, dict[str, SearchResult]] = {kw: {} for kw in keywords}
    try:
        with sync_playwright() as p:
            for pf_idx, pf_name in enumerate(platforms):
                if pf_idx > 0:
                    log.info(
                        f"跨平台节流：休眠 {PLATFORM_DELAY_SEC}s 后进入 {pf_name}"
                    )
                    brief(
                        f"跨平台节流：休眠 {_format_duration(PLATFORM_DELAY_SEC)} "
                        f"后进入 {pf_name}"
                    )
                    time.sleep(PLATFORM_DELAY_SEC)
                log.info(
                    f"========== 开始 {pf_name}（批量 {len(keywords)} 关键词） =========="
                )
                emit_progress_brief(session_state, f"开始平台 {pf_name}")
                t0 = time.monotonic()
                batch_result = run_platform_batch(
                    p, pf_name, keywords, args.pages, args.headed,
                    session_state=session_state, products_csv=products_csv,
                )
                elapsed = time.monotonic() - t0
                log.info(
                    f"========== {pf_name} 批次完成 耗时 {elapsed:.1f}s "
                    f"（{elapsed / max(1, len(keywords)):.1f}s/关键词均摊） =========="
                )
                emit_progress_brief(
                    session_state,
                    f"平台 {pf_name} 完成，耗时 {_format_duration(elapsed)}",
                )
                for kw, r in batch_result.items():
                    results[kw][pf_name] = r
    except KeyboardInterrupt:
        log.warning("⏸ Ctrl+C 中断；进度已落盘到 state/crawl_active.json，下次启动续跑")
        brief("任务被 Ctrl+C 中断；进度已落盘，下次同参数会续跑")
        return 130

    print_report(keywords, results, platforms)

    # 会话完成 → 归档 state + 重命名 running CSV 为最终报告
    ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
    final_csv = LOG_DIR / f"report_pw_{ts_tag}_products.csv"
    try:
        products_csv.rename(final_csv)
    except OSError as e:
        log.warning(f"rename running CSV 失败：{e}（保留 {products_csv}）")
        final_csv = products_csv
    _archive_active_state(session_state, final_csv)
    log.info(f"✅ 会话完成，商品明细 → {final_csv}")
    emit_progress_brief(session_state, f"任务完成，商品明细 {final_csv}")

    any_success = any(
        r.success for per_kw in results.values() for r in per_kw.values()
    )
    return 0 if any_success else 1


if __name__ == "__main__":
    sys.exit(main())
