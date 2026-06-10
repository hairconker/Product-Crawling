"""装机字典本地展示 HTTP 服务（stdlib 实现，零第三方依赖）。

提供 JSON API 给 ``dict_viewer.html`` 消费，实时读取 ``data/dict/`` +
``data/skus/`` 下的 CSV 快照。相比把全量 SKU 嵌入静态 HTML，这里走
``per-brand`` 懒加载——芯片表是一次性全量返回（量级 2k），SKU 按品牌
按需拉取。

用法::

    python scripts/dict_server.py                 # 默认 127.0.0.1:8765，自动开浏览器
    python scripts/dict_server.py --port 8080
    python scripts/dict_server.py --host 0.0.0.0  # 局域网可访问（谨慎）
    python scripts/dict_server.py --no-browser
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DICT_DIR = DATA_DIR / "dict"
SKUS_DIR = DATA_DIR / "skus"
HTML_FILE = ROOT / "dict_viewer.html"

# 分类键 → 用户可读中文标签（显示顺序按本字典）
CATEGORY_LABELS: dict[str, str] = {
    "cpu": "CPU / 处理器",
    "gpu": "GPU / 显卡",
    "mb": "主板",
    "ram": "内存",
    "storage_ssd": "SSD / 固态",
    "storage_hdd": "HDD / 机械",
    "psu": "电源",
    "cooler": "散热",
    "case": "机箱",
    "nic_wired": "有线网卡",
    "nic_wireless": "无线网卡",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def count_csv_rows(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            next(reader)
        except StopIteration:
            return 0
        return sum(1 for _ in reader)


def list_weeks() -> list[str]:
    if not DICT_DIR.exists():
        return []
    return sorted(
        p.name for p in DICT_DIR.iterdir()
        if p.is_dir() and not p.name.startswith("_")
    )


def resolve_brand_dir(week: str, category: str) -> Path | None:
    """优先用 ``_merged``（合并后品牌全表），否则回落到原始按品牌拆分的目录。"""
    merged = SKUS_DIR / week / "_merged" / category
    if merged.exists():
        return merged
    raw = SKUS_DIR / week / category
    return raw if raw.exists() else None


def build_summary(week: str) -> dict:
    dict_dir = DICT_DIR / week
    raw_summary: dict = {}
    summary_file = dict_dir / "_summary.json"
    if summary_file.exists():
        raw_summary = json.loads(summary_file.read_text(encoding="utf-8"))
    raw_cats = raw_summary.get("categories", {})

    categories: list[dict] = []
    for cat_key, cat_label in CATEGORY_LABELS.items():
        chips_n = count_csv_rows(dict_dir / f"{cat_key}_chips.csv")
        brand_dir = resolve_brand_dir(week, cat_key)
        brand_count = 0
        sku_n = 0
        if brand_dir is not None:
            for brand_csv in brand_dir.glob("*.csv"):
                brand_count += 1
                sku_n += count_csv_rows(brand_csv)
        categories.append({
            "key": cat_key,
            "label": cat_label,
            "chips": chips_n,
            "brands": brand_count,
            "skus": sku_n,
            "dropped": raw_cats.get(cat_key, {}).get("dropped", 0),
        })

    return {
        "week": week,
        "categories": categories,
        "total_chips": sum(c["chips"] for c in categories),
        "total_skus": sum(c["skus"] for c in categories),
        "total_brands": sum(c["brands"] for c in categories),
        "total_categories": sum(1 for c in categories if c["skus"] or c["chips"]),
        "unfiltered_count": raw_summary.get("unfiltered_count", 0),
    }


def load_chips(week: str, category: str) -> list[dict]:
    return read_csv(DICT_DIR / week / f"{category}_chips.csv")


def list_brands(week: str, category: str) -> list[dict]:
    brand_dir = resolve_brand_dir(week, category)
    if brand_dir is None:
        return []
    rows = [
        {"brand": p.stem, "count": count_csv_rows(p)}
        for p in sorted(brand_dir.glob("*.csv"))
    ]
    rows.sort(key=lambda x: -x["count"])
    return rows


def _parse_spec(raw: str) -> dict | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_alt_titles(raw: str) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return [str(x) for x in parsed]
    return []


def load_skus(week: str, category: str, brand: str) -> list[dict]:
    brand_dir = resolve_brand_dir(week, category)
    if brand_dir is None:
        return []
    target = brand_dir / f"{brand}.csv"
    rows = read_csv(target)
    out: list[dict] = []
    for r in rows:
        out.append({
            "brand": brand,
            "sku_title": r.get("sku_title", ""),
            "chip": r.get("chip", ""),
            "spec": _parse_spec(r.get("spec_json", "")),
            "source": r.get("source", ""),
            "origin_count": int(r["origin_count"]) if r.get("origin_count", "").isdigit() else None,
            "alt_titles": _parse_alt_titles(r.get("alt_titles", "")),
        })
    return out


def load_unfiltered(week: str, limit: int = 500) -> list[dict]:
    rows = read_csv(DICT_DIR / week / "_unfiltered.csv")
    return rows[:limit] if limit > 0 else rows


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # 静音默认访问日志；保留错误输出
        return

    # ---- helpers ----
    def _send_json(self, data: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, content_type: str) -> None:
        if not path.exists():
            self.send_error(HTTPStatus.NOT_FOUND, f"缺少文件：{path.name}")
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _require(self, params: dict[str, str], *keys: str) -> tuple[str, ...]:
        missing = [k for k in keys if not params.get(k)]
        if missing:
            raise ValueError(f"缺少参数: {', '.join(missing)}")
        return tuple(params[k] for k in keys)

    # ---- routing ----
    def do_GET(self) -> None:  # noqa: N802  (BaseHTTPRequestHandler 约定)
        try:
            self._route()
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except FileNotFoundError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:  # noqa: BLE001
            self._send_json(
                {"error": f"{type(exc).__name__}: {exc}"},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            raise

    def _route(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        if path in ("/", "/index.html"):
            self._send_file(HTML_FILE, "text/html; charset=utf-8")
            return

        if path == "/api/weeks":
            weeks = list_weeks()
            self._send_json({"weeks": weeks, "latest": weeks[-1] if weeks else None})
            return

        if path == "/api/summary":
            week = params.get("week") or (list_weeks() or [None])[-1]
            if not week:
                raise ValueError("data/dict/ 下无任何周次快照")
            self._send_json(build_summary(week))
            return

        if path == "/api/chips":
            week, category = self._require(params, "week", "category")
            self._send_json({"chips": load_chips(week, category)})
            return

        if path == "/api/brands":
            week, category = self._require(params, "week", "category")
            self._send_json({"brands": list_brands(week, category)})
            return

        if path == "/api/skus":
            week, category, brand = self._require(params, "week", "category", "brand")
            skus = load_skus(week, category, brand)
            self._send_json({"total": len(skus), "skus": skus})
            return

        if path == "/api/unfiltered":
            (week,) = self._require(params, "week")
            limit = int(params.get("limit", "500"))
            self._send_json({"rows": load_unfiltered(week, limit=limit)})
            return

        self.send_error(HTTPStatus.NOT_FOUND)


def main() -> int:
    ap = argparse.ArgumentParser(description="装机字典本地展示服务")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = ap.parse_args()

    if not DICT_DIR.exists():
        print(f"[ERR] 数据目录不存在：{DICT_DIR}", file=sys.stderr)
        print("      请先运行 scripts/dict_build.py 生成字典快照", file=sys.stderr)
        return 1
    if not HTML_FILE.exists():
        print(f"[ERR] 前端页面缺失：{HTML_FILE}", file=sys.stderr)
        return 1

    weeks = list_weeks()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"[dict-server] 启动于 {url}")
    print(f"  数据根       : {DATA_DIR}")
    print(f"  可用周次     : {', '.join(weeks) if weeks else '(无)'}")
    print("  Ctrl+C 停止")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except OSError:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[dict-server] 停止")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
