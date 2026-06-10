"""京东联盟 API 连通性测试脚本。

使用前设置环境变量：
  set JD_APP_KEY=你的unionId        (union.jd.com → 我的推广 → 我的API → 联盟ID)
  set JD_APP_SECRET=你的授权Key      (union.jd.com → 我的推广 → 我的API → 授权Key)

然后运行：
  python scripts/test_jd_api.py
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime
from typing import Any

import requests

GATEWAY = "https://api.jd.com/routerjson"
KEYWORD = "i5-12400F"


def _env_or_die(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        print(f"[FAIL] 缺少环境变量 {name}")
        print(f"  set {name}=<你的值>")
        sys.exit(1)
    return val


def _sign(app_secret: str, params: dict) -> str:
    ordered = sorted(params.items())
    raw = "".join(f"{k}{v}" for k, v in ordered)
    raw = app_secret + raw + app_secret
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _call(api_key: str, app_secret: str, method: str, biz_body: str = "") -> dict:
    params: dict[str, str] = {
        "method": method,
        "app_key": api_key,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "format": "json",
        "v": "2.0",
        "sign_method": "md5",
    }
    if biz_body:
        params["360buy_param_json"] = biz_body
    params["sign"] = _sign(app_secret, params)

    try:
        resp = requests.post(GATEWAY, data=params, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        return {"_error": str(e)}


def _unwrap_union(result: dict) -> dict:
    """京东联盟响应嵌了一层 key → result JSON 字符串，自动拆开。"""
    for k, v in result.items():
        if k.startswith("jd_union_") and isinstance(v, dict):
            inner = v.get("queryResult") or v.get("result") or v.get("code") or v
            if isinstance(inner, str):
                try:
                    return json.loads(inner)
                except (json.JSONDecodeError, TypeError):
                    return {"_raw_str": inner}
            if isinstance(inner, dict):
                return inner
    return result


def main() -> None:
    print("=" * 60)
    print("京东联盟 API 连通性测试")
    print("=" * 60)

    api_key = _env_or_die("JD_APP_KEY")
    app_secret = _env_or_die("JD_APP_SECRET")
    print(f"  APP_KEY   = {api_key[:12]}...")
    print(f"  APP_SECRET= {app_secret[:12]}...")
    print(f"  测试关键词 = {KEYWORD!r}")
    print()

    # ----------------------------------------------------------------
    # 测试 1: 关键词搜索
    # ----------------------------------------------------------------
    print("━" * 60)
    print("[TEST 1] jd.union.open.goods.query — 关键词搜索")
    print("━" * 60)

    r1 = _call(
        api_key, app_secret,
        "jd.union.open.goods.query",
        json.dumps({
            "goodsReqDTO": {
                "keyword": KEYWORD,
                "pageIndex": 1,
                "pageSize": 5,
                "isCoupon": 0,
                "isPG": 0,
            }
        }),
    )

    if r1.get("_error"):
        print(f"  [FAIL] 网络错误: {r1['_error']}")
    else:
        data = _unwrap_union(r1)
        items = data.get("data") or data.get("result") or []
        if isinstance(items, list):
            print(f"  [OK] 返回 {len(items)} 条商品\n")
            for i, it in enumerate(items[:5], 1):
                sku = it.get("skuId", "?")
                name = it.get("skuName", "?")[:50]
                price = it.get("price") or it.get("unitPrice") or "?"
                shop = it.get("shopName") or it.get("shopInfo", {}).get("shopName", "?")
                img = str(it.get("imageUrl", "") or it.get("imageurl", "") or "")[:60]
                print(f"  [{i}] sku={sku}")
                print(f"      名称={name}")
                print(f"      价格=¥{price}  店铺={shop}")
                print(f"      主图={img}")
                print()
        else:
            print(f"  [WARN] 返回格式不符合预期，原始响应:")
            print(json.dumps(r1, ensure_ascii=False, indent=2)[:1000])

    # ----------------------------------------------------------------
    # 测试 2: 京粉精选频道
    # ----------------------------------------------------------------
    print("━" * 60)
    print("[TEST 2] jd.union.open.goods.jingfen.query — 京粉精选")
    print("━" * 60)

    r2 = _call(
        api_key, app_secret,
        "jd.union.open.goods.jingfen.query",
        json.dumps({
            "goodsReq": {
                "eliteId": 1,
                "pageIndex": 1,
                "pageSize": 5,
                "sortName": "price",
                "sort": "asc",
            }
        }),
    )

    if r2.get("_error"):
        print(f"  [FAIL] 网络错误: {r2['_error']}")
    else:
        data2 = _unwrap_union(r2)
        items2 = data2.get("data") or data2.get("result") or []
        if isinstance(items2, list):
            print(f"  [OK] 返回 {len(items2)} 条商品")
        else:
            print(f"  [WARN] 返回格式不符合预期:")
            print(json.dumps(r2, ensure_ascii=False, indent=2)[:600])

    # ----------------------------------------------------------------
    # 汇总
    # ----------------------------------------------------------------
    print()
    print("=" * 60)
    print("测试完成。如果上面看到商品列表 → API 接入成功。")
    print("如果有错误 → 检查 APP_KEY / APP_SECRET 是否正确。")
    print("=" * 60)


if __name__ == "__main__":
    main()
