"""把 t_hardware_price_history.image_url 回填到 t_hardware.image。

策略:
- 仅在 t_hardware.image 为 NULL / 空串 / 占位图(dummyimage/coreIcon/files/images/) 时填
- 取该 chip 对应 keyword 下出现次数最多的 image_url(优先非 -fleamarket 的;如全是 fleamarket 取第一张)
- 同时支持 SQLite + MySQL,行为一致

用法:
    python scripts/fill_hardware_image.py --target sqlite --dry-run
    python scripts/fill_hardware_image.py --target sqlite --apply
    python scripts/fill_hardware_image.py --target mysql  --apply
    python scripts/fill_hardware_image.py --target both   --apply
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from _db_common import backend, force_utf8_stdout  # noqa: E402

DEFAULT_SQLITE = ROOT / "data" / "prices.db"

log = logging.getLogger("fill_image")


def _setup_log() -> None:
    force_utf8_stdout()
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                     datefmt="%H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


_SKU_SUFFIXES = ("盒装", "散片", "原厂散热套装", "主板套装拆分",
                 "高频优选批次", "下架样例")


def _norm(s: str | None) -> str:
    if not s:
        return ""
    t = s.lower().strip()
    # 先剥 SKU 中文后缀(curated_budget_catalog 命名约定)
    for suf in _SKU_SUFFIXES:
        if t.endswith(suf):
            t = t[: -len(suf)].strip()
            break
    t = re.sub(r"[_/\-]+", " ", t)
    for n in ("intel ", "amd ", "nvidia ", "geforce ", "core ", "ryzen ",
              "锐龙", "酷睿", "radeon "):
        t = t.replace(n, "")
    return re.sub(r"\s+", "", t)


def _is_placeholder(img: str | None) -> bool:
    if not img:
        return True
    s = img.strip().lower()
    return (
        "dummyimage.com" in s
        or "/imgs/coreicon/" in s
        or "/files/images/" in s
    )


def _is_unrenderable(img: str | None) -> bool:
    """微信小程序 <image> 不支持 heic,视作需要替换。"""
    if not img:
        return False
    s = img.strip().lower()
    return s.endswith(".heic") or ".heic?" in s


def _pick_best_image(urls: list[str]) -> str | None:
    """挑选优先级:
    1. 非 fleamarket 的 jpg/png(干净的产品图)
    2. fleamarket 的 jpg/png(2 手平台的实物图,微信支持)
    3. 非 fleamarket 的 heic(只能让 alicdn 自动协商,可能仍失败)
    4. 兜底 fleamarket heic(微信小程序渲染不出来)
    """
    if not urls:
        return None

    def is_renderable(u: str) -> bool:
        lo = u.lower()
        return not (lo.endswith(".heic") or ".heic?" in lo)

    def is_clean(u: str) -> bool:
        return "fleamarket" not in u.lower()

    tier1 = [u for u in urls if is_renderable(u) and is_clean(u)]
    tier2 = [u for u in urls if is_renderable(u) and not is_clean(u)]
    tier3 = [u for u in urls if not is_renderable(u) and is_clean(u)]
    tier4 = [u for u in urls if not is_renderable(u) and not is_clean(u)]
    pool = tier1 or tier2 or tier3 or tier4
    return Counter(pool).most_common(1)[0][0]


def fill_one(target: str, sqlite_path: str | None, dry_run: bool) -> None:
    log.info("─── 后端: %s ───", target)
    kwargs = {"sqlite_path": sqlite_path} if target == "sqlite" else {}
    with backend(target, **kwargs) as db:
        cur = db.cursor()
        ph = db.placeholder()
        try:
            # 1. 拉所有 history image_url,按 keyword 聚合
            cur.execute(
                "SELECT keyword, image_url FROM t_hardware_price_history "
                "WHERE image_url IS NOT NULL AND image_url <> '' "
                "AND keyword IS NOT NULL AND keyword <> ''"
            )
            by_kw: dict[str, list[str]] = {}
            for kw, img in cur.fetchall():
                by_kw.setdefault(_norm(kw), []).append(img)
            log.info("  聚合 %d 个 keyword 的图片池", len(by_kw))

            # 2. 拉所有 t_hardware 行,占位/空的尝试匹配
            cur.execute("SELECT id, category, model, image FROM t_hardware "
                        "WHERE deleted = 0")
            rows = cur.fetchall()
            log.info("  扫描 t_hardware: %d 行", len(rows))

            updated = upgraded_heic = skipped_realimage = nomatch = 0
            update_sql = f"UPDATE t_hardware SET image = {ph} WHERE id = {ph}"
            for hid, cat, model, img in rows:
                is_placeholder = _is_placeholder(img)
                is_heic = _is_unrenderable(img)
                if not is_placeholder and not is_heic:
                    skipped_realimage += 1
                    continue
                pool = by_kw.get(_norm(model))
                if not pool:
                    nomatch += 1
                    continue
                best = _pick_best_image(pool)
                if not best:
                    nomatch += 1
                    continue
                # heic 升级 jpg 时,只在新候选不是 heic 才做替换;否则保留 heic 不动
                if is_heic and (best.lower().endswith(".heic") or ".heic?" in best.lower()):
                    nomatch += 1  # 同型号闲鱼里也没有 jpg 候选,放弃替换
                    continue
                if is_heic:
                    upgraded_heic += 1
                else:
                    updated += 1
                if not dry_run:
                    cur.execute(update_sql, (best, hid))
            log.info("  updated=%d, upgraded_heic→jpg=%d, skip_real=%d, nomatch=%d",
                     updated, upgraded_heic, skipped_realimage, nomatch)
            if dry_run:
                db.rollback()
                log.info("  DRY-RUN 不写库")
            else:
                db.commit()
                log.info("  ✓ 提交")
        finally:
            cur.close()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--target", required=True, choices=("sqlite", "mysql", "both"))
    p.add_argument("--sqlite-path", default=str(DEFAULT_SQLITE))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if not args.dry_run and not args.apply:
        p.error("--dry-run 或 --apply 必选其一")

    _setup_log()
    targets = ("sqlite", "mysql") if args.target == "both" else (args.target,)
    for t in targets:
        fill_one(t, args.sqlite_path, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
