"""下载 products.image_url 指向的商品图,保存到 data/images/{platform}/{item_id}.{ext}。

设计:
- 幂等:已下载的(image_local_path 已填且文件存在)直接跳过
- 增量:db 加 `image_local_path TEXT` 列,记录本地相对路径(项目根的相对路径)
- 线程池:阿里云 CDN 抗压,默认 8 并发
- 失败不阻塞:单张挂掉只 log warning,继续其他

用法:
    python scripts/download_product_images.py                   # 全量
    python scripts/download_product_images.py --limit 50        # 只下 50 张(冒烟测试)
    python scripts/download_product_images.py --platform xianyu # 指定平台
    python scripts/download_product_images.py --workers 16      # 自定义并发
    python scripts/download_product_images.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    print("[ERROR] requests 未安装,请 pip install requests", file=sys.stderr)
    sys.exit(1)

# ─── 常量 ────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "prices.db"
IMG_ROOT = ROOT / "data" / "images"

# 阿里云 CDN 拒裸 python-requests 的 UA,伪装成普通浏览器
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_REQ_TIMEOUT = 15.0
_PROGRESS_EVERY = 50

# 文件名安全检查:item_id 是闲鱼商品 ID,通常纯数字,但兜底
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.\-]+")

log = logging.getLogger("img_dl")
_db_lock = threading.Lock()  # sqlite3 默认连接非线程安全,UPDATE 时加锁


# ─── 工具 ────────────────────────────────────────────────────────


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    )
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def _ensure_schema(conn: sqlite3.Connection) -> bool:
    cur = conn.execute("PRAGMA table_info(products)")
    existing = {row[1] for row in cur.fetchall()}
    if "image_local_path" in existing:
        return False
    conn.execute("ALTER TABLE products ADD COLUMN image_local_path TEXT")
    conn.commit()
    return True


def _safe_filename(item_id: str, url: str) -> str:
    """基于 item_id + URL 扩展名生成安全文件名。"""
    safe_id = _SAFE_NAME_RE.sub("_", item_id) or "unknown"
    # 从 URL 推扩展名,默认 jpg
    path = urlparse(url).path
    ext = Path(path).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".jpg"
    return f"{safe_id}{ext}"


def _local_path_for(platform: str, item_id: str, url: str) -> Path:
    """返回绝对路径。写 db 时转相对项目根。"""
    fname = _safe_filename(item_id, url)
    return IMG_ROOT / platform / fname


def _rel_for_db(abs_path: Path) -> str:
    """绝对路径 → 项目根相对路径(POSIX 风格,跨平台稳定)。"""
    return abs_path.relative_to(ROOT).as_posix()


# ─── 下载 ────────────────────────────────────────────────────────


def _download_one(
    row_id: int,
    platform: str,
    item_id: str,
    url: str,
    session: requests.Session,
) -> tuple[int, str | None, str | None]:
    """下载一张图,返回 (row_id, local_rel_path_or_None, error_or_None)。"""
    if not url:
        return row_id, None, "empty url"
    abs_path = _local_path_for(platform, item_id, url)
    # 已存在 → 当作成功
    if abs_path.exists() and abs_path.stat().st_size > 0:
        return row_id, _rel_for_db(abs_path), None
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        resp = session.get(url, timeout=_REQ_TIMEOUT, stream=True)
    except requests.Timeout as e:
        return row_id, None, f"timeout: {e}"
    except requests.RequestException as e:
        return row_id, None, f"request: {e}"
    if resp.status_code != 200:
        return row_id, None, f"http {resp.status_code}"
    # 写盘:tmp → rename 原子
    tmp = abs_path.with_suffix(abs_path.suffix + ".tmp")
    try:
        with tmp.open("wb") as fp:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fp.write(chunk)
        if tmp.stat().st_size == 0:
            tmp.unlink(missing_ok=True)
            return row_id, None, "empty body"
        tmp.replace(abs_path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return row_id, None, f"write: {e}"
    return row_id, _rel_for_db(abs_path), None


def _flush_updates(
    conn: sqlite3.Connection, pending: list[tuple[str, int]]
) -> None:
    """批量 UPDATE image_local_path。pending = [(rel_path, row_id), ...]"""
    if not pending:
        return
    with _db_lock:
        cur = conn.cursor()
        cur.executemany(
            "UPDATE products SET image_local_path = ? WHERE id = ?", pending
        )
        conn.commit()


# ─── 主流程 ──────────────────────────────────────────────────────


def run(
    *,
    db_path: Path,
    platform: str | None,
    limit: int | None,
    workers: int,
    dry_run: bool,
) -> int:
    if not db_path.exists():
        log.error("db not found: %s", db_path)
        return 2

    try:
        conn = sqlite3.connect(str(db_path), timeout=30.0)
    except sqlite3.Error as e:
        log.error("connect db failed: %s", e)
        return 2

    try:
        conn.execute("PRAGMA journal_mode=WAL")
        # schema 迁移幂等无害,dry-run 也要建好,否则后面 SELECT 用不存在的列会炸
        added = _ensure_schema(conn)
        if added:
            log.info("schema: added column image_local_path")
        else:
            log.info("schema: already up to date")

        sql = (
            "SELECT id, platform, item_id, image_url FROM products "
            "WHERE image_url != '' "
            "AND (image_local_path IS NULL OR image_local_path = '')"
        )
        params: list[str | int] = []
        if platform:
            sql += " AND platform = ?"
            params.append(platform)
        sql += " ORDER BY id"
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        total = len(rows)
        log.info(
            "to download: %d rows (workers=%d platform=%s limit=%s dry_run=%s)",
            total,
            workers,
            platform or "ALL",
            limit,
            dry_run,
        )
        if total == 0:
            log.info("nothing to do")
            return 0

        if dry_run:
            for r in rows[:10]:
                log.info("would download id=%s %s %s", r[0], r[1], r[3][:80])
            if total > 10:
                log.info("... and %d more", total - 10)
            return 0

        # 多线程下载
        session = requests.Session()
        session.headers.update({"User-Agent": _UA, "Referer": "https://2.taobao.com/"})

        ok = 0
        fail = 0
        pending: list[tuple[str, int]] = []
        last_log = 0

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(_download_one, rid, plat, iid, url, session)
                for (rid, plat, iid, url) in rows
            ]
            for fut in as_completed(futures):
                try:
                    rid, rel, err = fut.result()
                except Exception as e:  # 线程池里的兜底,具体异常已被 _download_one 捕获
                    log.warning("worker crashed: %s", e)
                    fail += 1
                    continue
                if err is not None:
                    fail += 1
                    if fail <= 20 or fail % 50 == 0:
                        log.warning("fail id=%s: %s", rid, err)
                    continue
                ok += 1
                if rel is not None:
                    pending.append((rel, rid))
                # 每 200 条批量入库一次,避免 conn 长事务
                if len(pending) >= 200:
                    _flush_updates(conn, pending)
                    pending.clear()
                # 进度
                done = ok + fail
                if done - last_log >= _PROGRESS_EVERY:
                    last_log = done
                    log.info(
                        "progress %d/%d ok=%d fail=%d",
                        done,
                        total,
                        ok,
                        fail,
                    )

        # 收尾入库
        _flush_updates(conn, pending)

        log.info(
            "DONE total=%d ok=%d fail=%d images_root=%s",
            total,
            ok,
            fail,
            IMG_ROOT,
        )
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--platform", type=str, default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    _configure_logging()
    return run(
        db_path=args.db,
        platform=args.platform,
        limit=args.limit,
        workers=args.workers,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    raise SystemExit(main())
