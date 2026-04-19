"""统一日志模块。

设计要点：
- 控制台彩色输出，INFO 起；
- 每个平台独立按天滚动文件 logs/{platform}_{date}.log，DEBUG 起，保留 14 天；
- ERROR 单独归档 logs/error.log，保留 30 天，便于事后排查；
- 通过 get_logger(spider="jd", keyword="...") 绑定上下文字段，方便检索。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from loguru import logger

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
DEBUG_DIR = LOG_DIR / "debug"

_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{extra[platform]}</cyan>:<cyan>{extra[spider]}</cyan> "
    "<level>{message}</level>"
)

_FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{extra[platform]}:{extra[spider]} | "
    "req={extra[request_id]} kw={extra[keyword]} | "
    "{name}:{function}:{line} - {message}"
)

_initialized = False


def setup_logging(
    *,
    console_level: str = "INFO",
    file_level: str = "DEBUG",
    log_dir: Path | None = None,
) -> None:
    """初始化全局日志。重复调用安全（只会初始化一次）。"""
    global _initialized
    if _initialized:
        return

    target_dir = log_dir or LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    logger.remove()

    logger.configure(
        extra={
            "platform": "-",
            "spider": "-",
            "keyword": "-",
            "request_id": "-",
        }
    )

    logger.add(
        sys.stderr,
        level=console_level,
        format=_CONSOLE_FORMAT,
        colorize=True,
        backtrace=True,
        diagnose=False,
    )

    logger.add(
        str(target_dir / "{time:YYYY-MM-DD}.log"),
        level=file_level,
        format=_FILE_FORMAT,
        rotation="00:00",
        retention="14 days",
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=True,
    )

    logger.add(
        str(target_dir / "error.log"),
        level="ERROR",
        format=_FILE_FORMAT,
        rotation="10 MB",
        retention="30 days",
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=True,
    )

    _initialized = True


def get_logger(
    *,
    platform: str = "-",
    spider: str = "-",
    keyword: str = "-",
    request_id: str = "-",
    **extra: Any,
):
    """返回绑定上下文的 logger。在每个 spider 入口调用。"""
    if not _initialized:
        setup_logging()
    return logger.bind(
        platform=platform,
        spider=spider,
        keyword=keyword,
        request_id=request_id,
        **extra,
    )


def dump_debug_artifact(name: str, content: str | bytes, suffix: str = "html") -> Path:
    """把响应/页面落盘，方便复盘。返回保存路径。"""
    from datetime import datetime

    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = DEBUG_DIR / f"{name}_{ts}.{suffix}"
    mode = "wb" if isinstance(content, bytes) else "w"
    encoding = None if isinstance(content, bytes) else "utf-8"
    with open(path, mode, encoding=encoding) as f:
        f.write(content)
    return path
