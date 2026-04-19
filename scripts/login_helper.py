#!/usr/bin/env python3
"""扫码登录助手

用法（Windows PowerShell）：
    python scripts\\login_helper.py jd            # 京东
    python scripts\\login_helper.py xianyu        # 闲鱼
    python scripts\\login_helper.py taobao        # 淘宝
    python scripts\\login_helper.py pdd           # 拼多多
    python scripts\\login_helper.py all           # 依次登录全部 4 个

流程：
    1. 打开真实 Chrome 浏览器（headed），跳到指定平台登录页
    2. 等你扫码/输密码完成登录
    3. 检测登录成功后，自动把 storage_state 保存到 state/{platform}_state.json，
       并把 Cookie 字符串同步写到 state/{platform}_cookie.txt（给 run_cpu_crawl.py 用）
    4. 关掉浏览器

前置依赖（一次性）：
    pip install playwright
    playwright install chromium

如果 pip 没装：
    python -m ensurepip --user
    # 或下载 get-pip.py
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Page
except ImportError:
    print("\n[ERROR] 还没安装 playwright。请先在项目根目录运行：\n")
    print("    pip install playwright")
    print("    playwright install chromium\n")
    sys.exit(1)


ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)


PLATFORMS = {
    "jd": {
        "name": "京东",
        "login_url": "https://passport.jd.com/new/login.aspx",
        "post_login_url": "https://search.jd.com/Search?keyword=cpu",
        # 登录成功后出现的关键 cookie（任一出现即认为登录完成）
        "login_cookie_markers": ["thor", "pin", "pinId"],
        "cookie_domain_filter": [".jd.com", "jd.com", ".search.jd.com"],
        "mobile_ua": False,
    },
    "xianyu": {
        "name": "闲鱼",
        "login_url": "https://www.goofish.com/",
        "post_login_url": "https://www.goofish.com/search?q=cpu",
        # unb = taobao 体系用户 ID；_m_h5_tk 也能作为兜底
        "login_cookie_markers": ["unb", "_nk_", "tracknick"],
        "cookie_domain_filter": [".goofish.com", "goofish.com", ".taobao.com", ".alicdn.com"],
        "mobile_ua": False,
    },
    "taobao": {
        "name": "淘宝",
        "login_url": "https://login.taobao.com/member/login.jhtml",
        "post_login_url": "https://s.taobao.com/search?q=cpu",
        "login_cookie_markers": ["unb", "tracknick", "_nk_"],
        "cookie_domain_filter": [".taobao.com", "taobao.com", ".tmall.com"],
        "mobile_ua": False,
    },
    # 拼多多已移除
}

UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)


def wait_for_login(
    page: Page, context, cfg: dict, timeout_sec: int = 600
) -> bool:
    """自动检测登录完成：轮询 context.cookies()，关键 cookie 出现即保存。

    无需用户按回车。各平台的 login_cookie_markers 字段出现任意一个且非空即判定已登录。
    """
    markers = cfg["login_cookie_markers"]
    print("\n" + "=" * 60)
    print(f"  请在弹出的浏览器中完成 {cfg['name']} 登录（扫码 / 账号密码）")
    print(f"  检测到关键 cookie 后脚本会【自动保存并关闭浏览器】")
    print(f"  监听 cookie: {markers}")
    print("=" * 60 + "\n")

    import time as _t
    t0 = _t.monotonic()
    last_log = 0.0
    while _t.monotonic() - t0 < timeout_sec:
        try:
            cookies = context.cookies()
        except Exception as e:
            print(f"[{cfg['name']}] 读取 cookie 异常: {e}")
            break

        hit = [
            m for m in markers
            if any(
                c.get("name") == m and (c.get("value") or "").strip()
                and (c.get("value") or "") not in ("0", "null", "undefined")
                for c in cookies
            )
        ]
        if hit:
            print(f"[{cfg['name']}] ✓ 检测到登录 cookie {hit}，自动保存...")
            return True

        now = _t.monotonic()
        if now - last_log > 15:
            print(
                f"[{cfg['name']}] 等待登录... 已等 {int(now - t0)}s / {timeout_sec}s "
                f"（当前 {len(cookies)} 条 cookie）"
            )
            last_log = now

        try:
            page.wait_for_timeout(1500)
        except Exception:
            # 浏览器被用户手动关闭
            break
    print(f"[{cfg['name']}] ✗ 超时未检测到登录 cookie（{timeout_sec}s）")
    return False


def do_login(key: str) -> bool:
    cfg = PLATFORMS[key]
    print(f"\n===== 登录 {cfg['name']} ({key}) =====")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=UA_MOBILE if cfg["mobile_ua"] else UA_DESKTOP,
            viewport={"width": 390, "height": 844} if cfg["mobile_ua"] else {"width": 1280, "height": 800},
        )
        page = context.new_page()
        page.goto(cfg["login_url"], wait_until="domcontentloaded", timeout=30000)

        ok = wait_for_login(page, context, cfg)
        if not ok:
            print(f"[{key}] 登录超时/未检测到登录。浏览器先不关，你可手动完成后 Ctrl+C 重试")
            browser.close()
            return False

        # 访问业务页，确保拿齐全部业务 cookie（PDD 的 PDDAccessToken 尤其关键）
        try:
            page.goto(cfg["post_login_url"], wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(3500)
        except PWTimeout:
            print(f"[{key}] post_login_url 加载超时，继续保存已有 cookie")
        except Exception as e:
            print(f"[{key}] post_login_url 异常: {e}，继续保存")

        # 保存 storage_state
        state_path = STATE_DIR / f"{key}_state.json"
        context.storage_state(path=str(state_path))
        print(f"[{key}] storage_state 已保存: {state_path}")

        # 同时把 cookie 字符串写入 cookie.txt
        cookies = context.cookies()
        filtered = [
            c for c in cookies
            if any(d in (c.get("domain") or "") for d in cfg["cookie_domain_filter"])
        ]
        cookie_line = "; ".join(f"{c['name']}={c['value']}" for c in filtered)
        cookie_path = STATE_DIR / f"{key}_cookie.txt"
        cookie_path.write_text(cookie_line, encoding="utf-8")
        print(f"[{key}] cookie.txt 已保存（{len(filtered)} 条 cookie）: {cookie_path}")

        browser.close()
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="扫码登录助手")
    parser.add_argument(
        "platform",
        choices=list(PLATFORMS.keys()) + ["all"],
        help="要登录的平台",
    )
    args = parser.parse_args()

    targets = list(PLATFORMS.keys()) if args.platform == "all" else [args.platform]
    results: dict[str, bool] = {}
    for key in targets:
        try:
            results[key] = do_login(key)
        except KeyboardInterrupt:
            print(f"\n[{key}] 用户中断")
            results[key] = False
        except Exception as e:
            print(f"\n[{key}] 异常: {e}")
            results[key] = False

    print("\n===== 汇总 =====")
    for k, ok in results.items():
        print(f"  {k:8s}: {'✓ 已保存' if ok else '✗ 未完成'}")
    print(f"\n接下来可以跑：python run_cpu_crawl.py \"i5-12400F\"")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
