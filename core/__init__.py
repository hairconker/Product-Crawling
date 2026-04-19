"""核心基础设施：日志、异常、配置、数据模型、爬虫基类。"""

from core.exceptions import (
    SpiderError,
    ConfigError,
    NetworkError,
    LoginRequiredError,
    AntiSpiderError,
    RateLimitError,
    ParseError,
    PlatformError,
)
from core.logger import get_logger, setup_logging
from core.models import Product, SearchResult

__all__ = [
    "SpiderError",
    "ConfigError",
    "NetworkError",
    "LoginRequiredError",
    "AntiSpiderError",
    "RateLimitError",
    "ParseError",
    "PlatformError",
    "get_logger",
    "setup_logging",
    "Product",
    "SearchResult",
]
