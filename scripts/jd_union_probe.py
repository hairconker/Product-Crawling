"""
京东联盟开放API 可用性探测

目标:
1. 用本机 secrets/jd_union.json 里的 appkey/secretkey 测试一组核心接口
2. 验证两条业务流:
   流A 关键词搜索 -> 物料/精选 接口直接拿到价格 + 券 = 到手价
   流B 已知 skuId -> 转链(promotion.common.get) + 推广商品信息(promotiongoodsinfo) 拿券价
3. 输出每个接口的可用状态(HTTP/业务码/示例字段),供后续做价格管线选型

约定:
- 业务参数走 360buy_param_json (string of JSON)
- 签名: MD5( secret + sorted(k+v for k,v in sysParams) + secret ).upper()
- 排序时只用"系统级参数"(method/app_key/timestamp/format/v/sign_method/360buy_param_json/access_token?)

用法:
    python scripts\\jd_union_probe.py
    python scripts\\jd_union_probe.py --keyword "i5-12400F"
    python scripts\\jd_union_probe.py --sku 100012043978
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# Windows 控制台默认 GBK，会把中文业务消息变成乱码 -> 强制 utf-8
if isinstance(sys.stdout, io.TextIOWrapper):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets" / "jd_union.json"
GATEWAY = "https://api.jd.com/routerjson"
HTTP_TIMEOUT = 15.0


def load_credentials() -> tuple[str, str]:
    if not SECRETS.exists():
        raise SystemExit(f"找不到凭据文件: {SECRETS}")
    data = json.loads(SECRETS.read_text(encoding="utf-8"))
    return data["app_key"], data["secret_key"]


def md5_upper(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().upper()


def sign_params(params: dict[str, str], secret: str) -> str:
    joined = "".join(f"{k}{params[k]}" for k in sorted(params))
    return md5_upper(secret + joined + secret)


def call(
    method: str,
    biz_params: dict[str, Any],
    *,
    app_key: str,
    secret: str,
    access_token: str | None = None,
) -> dict[str, Any]:
    sys_params: dict[str, str] = {
        "method": method,
        "app_key": app_key,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "format": "json",
        "v": "1.0",
        "sign_method": "md5",
        "360buy_param_json": json.dumps(biz_params, ensure_ascii=False, separators=(",", ":")),
    }
    if access_token:
        sys_params["access_token"] = access_token

    sys_params["sign"] = sign_params(sys_params, secret)
    body = urllib.parse.urlencode(sys_params).encode("utf-8")

    req = urllib.request.Request(
        GATEWAY,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return {"_transport_error": repr(exc)}

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw[:500]}


def unwrap_response(method: str, payload: dict[str, Any]) -> tuple[str, Any]:
    """返回 (业务状态摘要, 内层 result_json or None)"""
    if "_transport_error" in payload:
        return f"传输错误: {payload['_transport_error']}", None
    if "_raw" in payload:
        return f"非 JSON 响应: {payload['_raw']}", None

    if "error_response" in payload:
        err = payload["error_response"]
        return (
            f"网关错误 code={err.get('code')} msg={err.get('zh_desc') or err.get('msg')} sub={err.get('sub_code')}",
            None,
        )

    resp_key = method.replace(".", "_") + "_responce"
    inner = payload.get(resp_key) or next(
        (v for k, v in payload.items() if k.endswith("_responce") or k.endswith("_response")),
        None,
    )
    if not isinstance(inner, dict):
        return f"未识别的响应结构: keys={list(payload.keys())}", None

    result_raw = inner.get("result") or inner.get("queryResult")
    if isinstance(result_raw, str):
        try:
            result = json.loads(result_raw)
        except json.JSONDecodeError:
            return f"result 非 JSON: {result_raw[:200]}", None
    else:
        result = result_raw

    if isinstance(result, dict):
        code = result.get("code")
        msg = result.get("message") or result.get("msg")
        return f"业务 code={code} msg={msg}", result
    return f"业务结果非 dict: {type(result).__name__}", result


def pretty_price(sku: dict[str, Any]) -> str:
    price_info = sku.get("priceInfo") or {}
    coupon_info = sku.get("couponInfo") or {}
    coupons = coupon_info.get("couponList") or []
    base = price_info.get("price")
    lowest = price_info.get("lowestCouponPrice") or price_info.get("lowestPrice")
    discount = next((c.get("discount") for c in coupons), None)
    return (
        f"price={base} lowestCouponPrice={lowest} coupon_discount={discount} "
        f"title={(sku.get('skuName') or '')[:40]}"
    )


def probe_goods_query(keyword: str, *, app_key: str, secret: str) -> None:
    print(f"\n=== [A1] jd.union.open.goods.query  keyword={keyword!r} ===")
    biz = {
        "goodsReqDTO": {
            "keyword": keyword,
            "pageIndex": 1,
            "pageSize": 5,
            "sortName": "inOrderCount30DaysSku",
            "sort": "desc",
        }
    }
    payload = call("jd.union.open.goods.query", biz, app_key=app_key, secret=secret)
    status, result = unwrap_response("jd.union.open.goods.query", payload)
    print(status)
    if isinstance(result, dict) and isinstance(result.get("data"), list):
        for i, sku in enumerate(result["data"][:5], 1):
            print(f"  #{i} skuId={sku.get('skuId')}  {pretty_price(sku)}")


def probe_material_query(keyword: str, *, app_key: str, secret: str) -> None:
    """物料查询: eliteId 是必填枚举(1=精选爆款 / 2=好券商品 ...) 不能 0
    实测 eliteId=1 才合法; keyword 仅在该频道内过滤
    """
    print(f"\n=== [A2] jd.union.open.goods.material.query  keyword={keyword!r} ===")
    biz = {
        "eliteId": 1,
        "pageIndex": 1,
        "pageSize": 5,
        "keyword": keyword,
    }
    payload = call("jd.union.open.goods.material.query", biz, app_key=app_key, secret=secret)
    status, result = unwrap_response("jd.union.open.goods.material.query", payload)
    print(status)
    if isinstance(result, dict) and isinstance(result.get("data"), list):
        for i, sku in enumerate(result["data"][:5], 1):
            print(f"  #{i} skuId={sku.get('skuId')}  {pretty_price(sku)}")


def probe_jingfen_query(*, app_key: str, secret: str) -> None:
    print("\n=== [A3] jd.union.open.goods.jingfen.query  eliteId=1(精选爆款) ===")
    biz = {"eliteId": 1, "pageIndex": 1, "pageSize": 3}
    payload = call("jd.union.open.goods.jingfen.query", biz, app_key=app_key, secret=secret)
    status, _ = unwrap_response("jd.union.open.goods.jingfen.query", payload)
    print(status)


def probe_promotion_link(sku: str, *, app_key: str, secret: str, raw: bool = False) -> None:
    print(f"\n=== [B1] jd.union.open.promotion.common.get  skuId={sku} ===")
    biz = {
        "promotionCodeReq": {
            "materialId": f"https://item.jd.com/{sku}.html",
            "siteId": "4104823411",
        }
    }
    payload = call("jd.union.open.promotion.common.get", biz, app_key=app_key, secret=secret)
    if raw:
        print("  RAW:", json.dumps(payload, ensure_ascii=False)[:600])
    status, result = unwrap_response("jd.union.open.promotion.common.get", payload)
    print(status)
    if isinstance(result, dict):
        data = result.get("data") or {}
        print(f"  clickURL={data.get('clickURL')}")
        print(f"  shortURL={data.get('shortURL')}")


def probe_promotiongoodsinfo(sku: str, *, app_key: str, secret: str) -> None:
    print(f"\n=== [B2] jd.union.open.goods.promotiongoodsinfo.query  skuIds={sku} ===")
    biz = {"skuIds": sku}
    payload = call(
        "jd.union.open.goods.promotiongoodsinfo.query",
        biz,
        app_key=app_key,
        secret=secret,
    )
    status, result = unwrap_response("jd.union.open.goods.promotiongoodsinfo.query", payload)
    print(status)
    if isinstance(result, dict) and isinstance(result.get("result"), list):
        for sku_row in result["result"]:
            print(
                f"  skuId={sku_row.get('skuId')} "
                f"unitPrice={sku_row.get('unitPrice')} "
                f"wlUnitPrice={sku_row.get('wlUnitPrice')} "
                f"commissionShare={sku_row.get('commisionRatioWl')} "
                f"name={(sku_row.get('goodsName') or '')[:40]}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="JD Union API 可用性探测")
    parser.add_argument("--keyword", default="i5-12400F", help="搜索关键词（流A）")
    parser.add_argument("--sku", default="100012043978", help="测试 skuId（流B 用; i5-12400F 散片）")
    parser.add_argument("--raw", action="store_true", help="打印 promotion.common.get 原始响应")
    args = parser.parse_args()

    app_key, secret = load_credentials()
    print(f"app_key={app_key[:8]}...  endpoint={GATEWAY}")

    # 流A: 关键词 -> 商品信息（自带价格/券）
    probe_goods_query(args.keyword, app_key=app_key, secret=secret)
    probe_material_query(args.keyword, app_key=app_key, secret=secret)
    probe_jingfen_query(app_key=app_key, secret=secret)

    # 流B: skuId -> 转链 + 推广商品信息
    probe_promotion_link(args.sku, app_key=app_key, secret=secret, raw=args.raw)
    probe_promotiongoodsinfo(args.sku, app_key=app_key, secret=secret)

    print("\n完成。按业务 code/msg 判断每个接口对当前账号的可见范围。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
