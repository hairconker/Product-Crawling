"""配置加载。优先级：环境变量 > config/config.yaml > 内置默认值。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from core.exceptions import ConfigError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "config.yaml"
EXAMPLE_PATH = ROOT / "config" / "config.yaml.example"
STATE_DIR = ROOT / "state"


class PlatformConfig(BaseModel):
    enabled: bool = True
    cookie: str | None = None
    storage_state_path: str | None = None
    min_interval: float = 1.0
    extra: dict[str, Any] = Field(default_factory=dict)


class DebugConfig(BaseModel):
    save_html_on_error: bool = True
    screenshot_on_error: bool = True
    verbose: bool = False


class ProxyConfig(BaseModel):
    enabled: bool = False
    pool: list[str] = Field(default_factory=list)


class AppSettings(BaseModel):
    xianyu: PlatformConfig = Field(default_factory=PlatformConfig)
    jd: PlatformConfig = Field(default_factory=PlatformConfig)
    taobao: PlatformConfig = Field(default_factory=PlatformConfig)
    pdd: PlatformConfig = Field(default_factory=PlatformConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)


_cached: AppSettings | None = None


def load_settings(path: Path | None = None, *, reload: bool = False) -> AppSettings:
    """加载配置。默认读取 config/config.yaml；不存在则使用 example 兜底并 WARN。"""
    global _cached
    if _cached is not None and not reload:
        return _cached

    target = path or CONFIG_PATH
    if not target.exists():
        if EXAMPLE_PATH.exists():
            target = EXAMPLE_PATH
        else:
            raise ConfigError(
                f"找不到配置文件 {target}，也找不到 example。请先复制 config.yaml.example 为 config.yaml"
            )

    try:
        with open(target, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"配置文件 YAML 解析失败：{e}") from e

    try:
        _cached = AppSettings(**raw)
    except Exception as e:
        raise ConfigError(f"配置字段校验失败：{e}") from e

    return _cached
