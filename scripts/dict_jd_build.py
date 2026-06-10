"""阶段 10 CLI：京东分类页国行品牌 SKU 补充。

用法：
  python scripts/dict_jd_build.py --week 2026-W17                       # 全品类 + 全已识别品牌
  python scripts/dict_jd_build.py --week 2026-W17 --categories gpu      # 单品类
  python scripts/dict_jd_build.py --categories gpu --brands galax,colorful --headed
  python scripts/dict_jd_build.py --categories gpu --dry-run            # 只 discover 不抓

前置：
  1. state/jd_state.json 存在（跑 `python scripts/login_helper.py jd`）
  2. config/dict_jd_categories.yaml 里对应 category 的 cat ID 已填实
  3. 建议 Windows 家宽环境（WSL IP 可能被 JD 软拦截）
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from spiders.dict_jd_category import (  # noqa: E402
    JDCategoryCrawler,
    run_category,
    BrandCrawlStat,
)
from run_cpu_crawl_pw import (  # noqa: E402
    LoginRequiredError,
    AntiSpiderError,
    SpiderError,
    PLATFORM_DELAY_SEC,
)


_CONFIG_PATH = _ROOT / "config" / "dict_jd_categories.yaml"
_CAT_RE = re.compile(r"^\d+(?:,\d+){0,3}$")


def current_iso_week() -> str:
    y, w, _ = date.today().isocalendar()
    return f"{y}-W{w:02d}"


def _die(msg: str, code: int = 2) -> None:
    sys.stderr.write(f"[err] {msg}\n")
    sys.exit(code)


def _load_config() -> dict:
    """读取 + 严格校验 yaml。任何结构性错误直接退出 code=2（非 AttributeError）。"""
    if not _CONFIG_PATH.exists():
        _die(f"找不到 {_CONFIG_PATH}；请先提供 config/dict_jd_categories.yaml")
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        _die(f"yaml 解析失败：{e}")
        return {}  # unreachable, for type checker
    if not isinstance(raw, dict):
        _die(f"yaml 顶层必须是 mapping，实际 {type(raw).__name__}")
    cats = raw.get("categories")
    if not isinstance(cats, dict):
        _die(f"yaml.categories 必须是 mapping，实际 {type(cats).__name__}")
    for name, v in cats.items():
        if not isinstance(v, dict):
            _die(f"yaml.categories[{name!r}] 必须是 mapping")
        cat = str(v.get("cat", "")).strip()
        if cat and not _CAT_RE.match(cat):
            _die(
                f"yaml.categories[{name!r}].cat 格式非法（期望 'a' / 'a,b' / 'a,b,c' / "
                f"'a,b,c,d' 数字，实际 {cat!r}）"
            )
    defaults = raw.get("defaults")
    if defaults is not None and not isinstance(defaults, dict):
        _die(f"yaml.defaults 必须是 mapping，实际 {type(defaults).__name__}")
    return raw


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="阶段 10：京东分类页国行品牌 SKU 补充",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--week", default=None, help="ISO 周号（默认当前周）")
    ap.add_argument("--categories", default=None,
                    help="逗号分隔品类 slug；默认 yaml 中 cat 非空的所有品类")
    ap.add_argument("--brands", default=None,
                    help="逗号分隔品牌 slug；默认所有已识别品牌")
    ap.add_argument("--headed", action="store_true",
                    help="显示浏览器窗口（调试 / 过滑块必须）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只 discover 品牌，不抓 SKU")
    ap.add_argument("--max-pages", type=int, default=None,
                    help="单品牌最多翻页数（默认 yaml defaults.max_pages 或 100）")
    ap.add_argument("--out", default=None, help="输出根目录（默认 <project>/data）")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    cfg = _load_config()
    cat_cfg: dict[str, dict] = cfg.get("categories") or {}
    defaults: dict = cfg.get("defaults") or {}
    max_pages = args.max_pages or int(defaults.get("max_pages", 100))
    min_brand_items = int(defaults.get("min_brand_items", 5))

    # 选择品类
    if args.categories:
        wanted = [c.strip() for c in args.categories.split(",") if c.strip()]
    else:
        wanted = [k for k, v in cat_cfg.items() if (v or {}).get("cat")]
    missing = [c for c in wanted if c not in cat_cfg or not (cat_cfg[c] or {}).get("cat")]
    if missing:
        sys.stderr.write(
            f"[err] 品类 {missing} 在 yaml 中无 cat 值；请先填实 "
            f"{_CONFIG_PATH.name}\n"
        )
        return 2

    brand_filter = (
        set(b.strip() for b in args.brands.split(",") if b.strip())
        if args.brands else None
    )
    week = args.week or current_iso_week()
    out_base = Path(args.out).resolve() if args.out else None

    run_meta = {
        "week": week,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "categories": wanted,
        "brand_filter": sorted(brand_filter) if brand_filter else None,
        "dry_run": args.dry_run,
        "headed": args.headed,
        "max_pages": max_pages,
    }
    print(f"[start] {json.dumps(run_meta, ensure_ascii=False)}")

    crawler = JDCategoryCrawler(headed=args.headed, out_base=out_base)
    all_stats: list[BrandCrawlStat] = []
    try:
        for i, cat_slug in enumerate(wanted, 1):
            cat_id = cat_cfg[cat_slug]["cat"]
            print(f"\n[{i}/{len(wanted)}] === category={cat_slug} cat={cat_id} ===")
            if args.dry_run:
                known = crawler.discover_brands(cat_slug, cat_id)
                print(f"  discovered {len(known)} known brands:")
                for slug, (cn, ev) in sorted(known.items()):
                    print(f"    {slug:14s}  ev={ev:22s}  cn={cn}")
                continue
            try:
                stats = run_category(
                    crawler,
                    category=cat_slug, cat=cat_id, week=week,
                    brand_filter=brand_filter,
                    max_pages=max_pages, min_brand_items=min_brand_items,
                )
            except (LoginRequiredError, AntiSpiderError) as e:
                print(f"  [abort] {type(e).__name__}: {e}")
                print(f"  切 Windows 家宽 / 重登录后重试；已完成品类不会丢")
                return 3
            all_stats.extend(stats)
            if i < len(wanted):
                import time
                time.sleep(PLATFORM_DELAY_SEC)
    finally:
        crawler.close()

    # 汇总报告
    if all_stats:
        summary_path = (out_base or (_ROOT / "data")) / "skus" / week / "_jd_build_summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = summary_path.with_suffix(summary_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                **run_meta,
                "ended_at": datetime.now().isoformat(timespec="seconds"),
                "stats": [asdict(s) for s in all_stats],
            }, f, ensure_ascii=False, indent=2)
        tmp.replace(summary_path)
        print(f"\n[done] {len(all_stats)} brand-runs → {summary_path}")
        probably_missing = [s for s in all_stats if s.note.startswith("probably_missing")]
        errors = [s for s in all_stats if s.note.startswith("error:")]
        if errors:
            print(f"  errors: {len(errors)}")
        if probably_missing:
            print(f"  probably_missing (<{min_brand_items}): {len(probably_missing)}")
    else:
        print("\n[done] 无 stats 输出（dry-run 或所有品类为空）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
