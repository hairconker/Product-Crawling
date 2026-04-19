#!/usr/bin/env python3
"""CPU 价格爬虫 · 最小依赖版（只用 requests + 标准库）

用法：
    python3 run_cpu_crawl.py                     # 默认搜 "i5-12400F"
    python3 run_cpu_crawl.py "i9-14900K"         # 指定 CPU 关键词
    python3 run_cpu_crawl.py "R7 7800X3D" --only jd

说明：
    * 京东：无需登录即可搜索，最稳定
    * 闲鱼：API 带 sign 签名，需 cookie，默认 best-effort
    * 淘宝：必须登录 cookie，默认 best-effort
  任何平台失败都不会中断其他平台，错误写入 logs/error.log。
  拼多多已移除（移动端登录复杂、pc 端商品极少，不再支持）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
DEBUG_DIR = LOG_DIR / "debug"
STATE_DIR = ROOT / "state"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_cookie(platform: str) -> str | None:
    """从 state/{platform}_cookie.txt 读 cookie 字符串。不存在返回 None。"""
    path = STATE_DIR / f"{platform}_cookie.txt"
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8").strip()
    if not content or content.startswith("#"):
        return None
    return content.replace("\r", "").replace("\n", "")


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("cpu_crawler")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    today = datetime.now().strftime("%Y-%m-%d")
    file_handler = logging.FileHandler(LOG_DIR / f"crawl_{today}.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    error_handler = logging.FileHandler(LOG_DIR / "error.log", encoding="utf-8")
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(fmt)
    logger.addHandler(error_handler)

    logger.propagate = False
    return logger


log = _setup_logger()


def dump_debug(platform: str, payload: str | bytes, suffix: str = "html") -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = DEBUG_DIR / f"{platform}_{ts}.{suffix}"
    mode = "wb" if isinstance(payload, bytes) else "w"
    encoding = None if isinstance(payload, bytes) else "utf-8"
    with open(path, mode, encoding=encoding) as f:
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
    success: bool
    products: list[Product] = field(default_factory=list)
    error: str | None = None
    error_type: str | None = None

    @property
    def count(self) -> int:
        return len(self.products)


# ---------------------------------------------------------------------------
# 基础 HTTP
# ---------------------------------------------------------------------------

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)


def http_get(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    cookies: str | None = None,
    timeout: int = 15,
    platform: str = "-",
) -> requests.Response:
    req_id = uuid.uuid4().hex[:8]
    h = {"User-Agent": UA_DESKTOP, "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        h.update(headers)
    if cookies:
        h["Cookie"] = cookies

    log.debug(f"[{platform}][{req_id}] GET {url} params={params}")
    try:
        r = requests.get(url, params=params, headers=h, timeout=timeout)
    except requests.RequestException as e:
        log.warning(f"[{platform}][{req_id}] 网络错误: {e}")
        raise NetworkError(str(e), platform=platform, url=url) from e
    log.debug(
        f"[{platform}][{req_id}] ← {r.status_code} {len(r.content)} bytes "
        f"elapsed={r.elapsed.total_seconds():.2f}s"
    )
    return r


# ---------------------------------------------------------------------------
# 京东
# ---------------------------------------------------------------------------

def crawl_jd(keyword: str, max_items: int = 20) -> SearchResult:
    platform = "jd"
    result = SearchResult(platform=platform, keyword=keyword, success=False)
    cookie = load_cookie(platform)
    if not cookie:
        log.warning(f"[jd] 未配置 state/jd_cookie.txt，将以匿名身份尝试（大概率失败）")
    try:
        # 第一步：搜索主页（拿 SSR 主页面 + 带动后续 cookie）
        url = "https://search.jd.com/Search"
        params = {"keyword": keyword, "enc": "utf-8", "wq": keyword, "page": 1}
        headers = {"Referer": "https://www.jd.com/"}
        r = http_get(url, params=params, headers=headers, cookies=cookie, platform=platform)
        if r.status_code != 200:
            raise NetworkError(
                f"HTTP {r.status_code}", platform=platform, url=r.url
            )
        html = r.text

        # JD 现在可能 SSR 带商品也可能不带，优先匹配 SSR
        items = re.findall(r'data-sku="(\d+)"', html)

        # 如无 SSR 商品，走 s_new.php 异步接口（必须带 cookie）
        if not items:
            log.info("[jd] SSR 无商品，改调 s_new.php 异步接口")
            snew_url = "https://search.jd.com/s_new.php"
            snew_params = {
                "keyword": keyword,
                "enc": "utf-8",
                "qrst": 1,
                "stock": 1,
                "page": 1,
                "s": 1,
                "scrolling": "y",
            }
            snew_headers = {
                "Referer": f"https://search.jd.com/Search?keyword={keyword}",
                "X-Requested-With": "XMLHttpRequest",
            }
            rs = http_get(
                snew_url, params=snew_params, headers=snew_headers,
                cookies=cookie, platform=platform,
            )
            snew_text = rs.text
            # s_new.php 可能返回 JSON 错误码（风控）或 HTML 片段
            if snew_text.lstrip().startswith("{"):
                try:
                    jd_err = json.loads(snew_text)
                    reason = jd_err.get("body", {}).get("errorReason") or jd_err.get("message")
                    path = dump_debug(platform, snew_text, suffix="json")
                    raise AntiSpiderError(
                        f"JD s_new 风控: {reason}。调试文件 {path}",
                        platform=platform, url=rs.url,
                    )
                except json.JSONDecodeError:
                    pass
            items = re.findall(r'data-sku="(\d+)"', snew_text)
            if items:
                html = snew_text  # 复用下方的 block 提取逻辑

        if not items:
            path = dump_debug(platform, html)
            if cookie:
                raise ParseError(
                    f"带 cookie 仍未匹配到商品，HTML 结构可能变化。调试文件 {path}",
                    platform=platform, url=r.url,
                )
            raise LoginRequiredError(
                f"未匹配到商品节点，需登录后提供 cookie。调试文件 {path}。"
                f"参考 登录流程.md 第一节",
                platform=platform, url=r.url,
            )

        sku_ids = list(dict.fromkeys(items))[:max_items]
        log.info(f"[jd] 解析到 {len(sku_ids)} 个候选 sku")

        item_blocks: dict[str, str] = {}
        for sku in sku_ids:
            # 同时兼容 <li> 和 <div> 容器
            m = re.search(
                r'<(li|div)[^>]+data-sku="' + sku + r'"[^>]*>(.*?)</\1>',
                html,
                re.DOTALL,
            )
            if m:
                item_blocks[sku] = m.group(2)

        def _strip(s: str) -> str:
            return re.sub(r"<[^>]+>", "", s).strip()

        products: list[Product] = []
        for sku, block in item_blocks.items():
            title_match = re.search(
                r'<div class="p-name[^"]*">.*?<em>(.*?)</em>', block, re.DOTALL
            )
            title = _strip(title_match.group(1)) if title_match else ""

            shop_match = re.search(
                r'<div class="p-shop[^"]*">.*?<a[^>]*>(.*?)</a>', block, re.DOTALL
            )
            shop = _strip(shop_match.group(1)) if shop_match else None

            img_match = re.search(
                r'<img[^>]+data-lazy-img="([^"]+)"', block
            ) or re.search(r'<img[^>]+src="([^"]+)"', block)
            img = img_match.group(1) if img_match else None
            if img and img.startswith("//"):
                img = "https:" + img

            products.append(
                Product(
                    platform=platform,
                    item_id=sku,
                    title=title,
                    url=f"https://item.jd.com/{sku}.html",
                    image_url=img,
                    shop_name=shop,
                )
            )

        if products:
            price_url = "https://p.3.cn/prices/mgets"
            price_params = {
                "skuIds": ",".join(f"J_{p.item_id}" for p in products),
                "type": 1,
            }
            try:
                pr = http_get(
                    price_url,
                    params=price_params,
                    platform=platform,
                    timeout=10,
                    headers={"Referer": "https://search.jd.com/"},
                )
                if pr.status_code == 200:
                    price_data = pr.json()
                    price_map = {item["id"].lstrip("J_"): item for item in price_data}
                    for p in products:
                        pd = price_map.get(p.item_id)
                        if pd:
                            try:
                                p.current_price = float(pd.get("p")) if pd.get("p") and pd.get("p") != "-1" else None
                                p.origin_price = float(pd.get("m")) if pd.get("m") and pd.get("m") != "0" else None
                            except (TypeError, ValueError):
                                pass
            except Exception as e:
                log.warning(f"[jd] 价格接口失败，商品价格保留空: {e}")

        result.products = products
        result.success = True
        log.info(f"[jd] 完成，{len(products)} 条商品")
    except SpiderError as e:
        result.error = str(e)
        result.error_type = type(e).__name__
        log.error(f"[jd] {e}")
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        result.error_type = "UnknownError"
        log.error(f"[jd] 未捕获异常:\n{traceback.format_exc()}")
    return result


# ---------------------------------------------------------------------------
# 闲鱼（无 cookie best-effort）
# ---------------------------------------------------------------------------

def crawl_xianyu(keyword: str, max_items: int = 20) -> SearchResult:
    platform = "xianyu"
    result = SearchResult(platform=platform, keyword=keyword, success=False)
    cookie = load_cookie(platform)
    if not cookie:
        log.warning(f"[xianyu] 未配置 state/xianyu_cookie.txt，将 best-effort 尝试")
    try:
        url = "https://www.goofish.com/search"
        params = {"q": keyword}
        r = http_get(url, params=params, cookies=cookie, platform=platform)
        if r.status_code != 200:
            raise NetworkError(
                f"HTTP {r.status_code}", platform=platform, url=r.url
            )
        html = r.text

        # 闲鱼前端是 SPA，HTML 里不直接含商品。通常要命中 hofApiMtopMwpApi 或调 goofish.pc.search
        # 无 cookie 尝试：解析 html 中的 window.__PRELOADED_STATE__ / __NEXT_DATA__
        m = re.search(r'window\.__PRELOADED_STATE__\s*=\s*({.*?})\s*;?\s*</script>', html, re.DOTALL)
        if not m:
            m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)

        if not m:
            path = dump_debug(platform, html)
            raise LoginRequiredError(
                f"闲鱼 HTML 无预载数据（SPA），需 cookie + Playwright。调试页已保存 {path}",
                platform=platform,
                url=r.url,
            )

        # 理论走不到这里（无 cookie 几乎不会有 state）；为完整性保留解析
        try:
            state = json.loads(m.group(1))
        except Exception as e:
            raise ParseError(
                f"预载 JSON 解析失败: {e}", platform=platform, url=r.url
            ) from e
        log.info(f"[xianyu] 拿到预载数据 keys={list(state.keys())[:5]}")
        # 实际字段需要登录后才能可靠解析，此处仅表明路径。
        raise LoginRequiredError(
            "闲鱼搜索结果需登录态解析，请配置 cookie",
            platform=platform,
            url=r.url,
        )
    except SpiderError as e:
        result.error = str(e)
        result.error_type = type(e).__name__
        log.error(f"[xianyu] {e}")
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        result.error_type = "UnknownError"
        log.error(f"[xianyu] 未捕获异常:\n{traceback.format_exc()}")
    return result


# ---------------------------------------------------------------------------
# 淘宝（无 cookie best-effort，几乎必然跳登录）
# ---------------------------------------------------------------------------

def crawl_taobao(keyword: str, max_items: int = 20) -> SearchResult:
    platform = "taobao"
    result = SearchResult(platform=platform, keyword=keyword, success=False)
    cookie = load_cookie(platform)
    if not cookie:
        log.warning(f"[taobao] 未配置 state/taobao_cookie.txt，将 best-effort 尝试")
    try:
        url = "https://s.taobao.com/search"
        params = {"q": keyword}
        r = http_get(url, params=params, cookies=cookie, platform=platform)
        if r.status_code != 200:
            raise NetworkError(
                f"HTTP {r.status_code}", platform=platform, url=r.url
            )
        html = r.text

        if "login.taobao.com" in r.url or "登录" in html[:2000] and "g_page_config" not in html:
            path = dump_debug(platform, html)
            raise LoginRequiredError(
                f"被重定向到登录页，无 cookie 无法搜索。调试页已保存 {path}",
                platform=platform,
                url=r.url,
            )

        m = re.search(r'g_page_config\s*=\s*({.*?});\s*g_srp_loadCss', html, re.DOTALL)
        if not m:
            path = dump_debug(platform, html)
            raise LoginRequiredError(
                f"未找到 g_page_config，可能需要 cookie。调试页已保存 {path}",
                platform=platform,
                url=r.url,
            )

        state = json.loads(m.group(1))
        auctions = (
            state.get("mods", {})
            .get("itemlist", {})
            .get("data", {})
            .get("auctions", [])
        )
        if not auctions:
            raise ParseError(
                "auctions 列表为空", platform=platform, url=r.url
            )

        products: list[Product] = []
        for a in auctions[:max_items]:
            products.append(
                Product(
                    platform=platform,
                    item_id=str(a.get("nid", "")),
                    title=a.get("raw_title") or a.get("title") or "",
                    url=("https:" + a.get("detail_url")) if a.get("detail_url", "").startswith("//") else a.get("detail_url", ""),
                    current_price=float(a["view_price"]) if a.get("view_price") else None,
                    shop_name=a.get("nick"),
                    location=a.get("item_loc"),
                    image_url=("https:" + a["pic_url"]) if a.get("pic_url", "").startswith("//") else a.get("pic_url"),
                )
            )
        result.products = products
        result.success = True
        log.info(f"[taobao] 完成，{len(products)} 条商品")
    except SpiderError as e:
        result.error = str(e)
        result.error_type = type(e).__name__
        log.error(f"[taobao] {e}")
    except Exception as e:
        result.error = f"{type(e).__name__}: {e}"
        result.error_type = "UnknownError"
        log.error(f"[taobao] 未捕获异常:\n{traceback.format_exc()}")
    return result


# ---------------------------------------------------------------------------
# 调度
# ---------------------------------------------------------------------------

# 拼多多已移除（移动端登录复杂、pc 端商品极少）
CRAWLERS = {
    "jd": crawl_jd,
    "xianyu": crawl_xianyu,
    "taobao": crawl_taobao,
}


PLATFORM_DELAY_SEC = 6.0  # 跨平台节流，保护账号


def run_all(keyword: str, platforms: list[str], max_items: int = 20) -> dict[str, SearchResult]:
    results: dict[str, SearchResult] = {}
    for idx, name in enumerate(platforms):
        if idx > 0:
            log.info(f"跨平台休眠 {PLATFORM_DELAY_SEC}s（保护账号）")
            time.sleep(PLATFORM_DELAY_SEC)
        log.info(f"========== 开始爬取 {name} : {keyword} ==========")
        t0 = time.monotonic()
        results[name] = CRAWLERS[name](keyword, max_items=max_items)
        log.info(f"========== {name} 耗时 {time.monotonic() - t0:.2f}s ==========")
    return results


def print_report(keyword: str, results: dict[str, SearchResult]) -> None:
    print(f"\n{'=' * 72}")
    print(f"CPU 关键词：{keyword}")
    print(f"爬取时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'=' * 72}\n")
    for name, r in results.items():
        status = "OK" if r.success else "FAIL"
        print(f"[{status}] {name.upper():8s} 商品数={r.count}  " + (f"错误={r.error_type}" if not r.success else ""))
        if not r.success and r.error:
            print(f"   原因: {r.error}")
        for i, p in enumerate(r.products[:5], 1):
            price = f"¥{p.current_price:.2f}" if p.current_price else "N/A"
            print(f"   {i}. {price:10s}  {p.title[:60]}")
            print(f"      {p.url}")
        if r.count > 5:
            print(f"   ... 另有 {r.count - 5} 条")
        print()

    out_path = LOG_DIR / f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "keyword": keyword,
                "crawled_at": datetime.now().isoformat(),
                "results": {
                    name: {
                        "success": r.success,
                        "count": r.count,
                        "error": r.error,
                        "error_type": r.error_type,
                        "products": [asdict(p) for p in r.products],
                    }
                    for name, r in results.items()
                },
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"详细结果已保存：{out_path}")
    print(f"日志：{LOG_DIR}/crawl_{datetime.now().strftime('%Y-%m-%d')}.log")


def main() -> int:
    parser = argparse.ArgumentParser(description="CPU 价格爬虫 · 最小依赖版")
    parser.add_argument("keyword", nargs="?", default="i5-12400F", help="CPU 关键词")
    parser.add_argument(
        "--only",
        choices=list(CRAWLERS.keys()),
        action="append",
        help="只跑指定平台（可多次）",
    )
    parser.add_argument("--max", type=int, default=20, help="每平台最多爬多少条")
    args = parser.parse_args()

    platforms = args.only or list(CRAWLERS.keys())
    log.info(f"开始任务：keyword='{args.keyword}' platforms={platforms}")

    try:
        results = run_all(args.keyword, platforms, max_items=args.max)
    except Exception:
        log.error(f"调度层异常:\n{traceback.format_exc()}")
        return 2
    print_report(args.keyword, results)
    any_success = any(r.success for r in results.values())
    return 0 if any_success else 1


if __name__ == "__main__":
    sys.exit(main())
