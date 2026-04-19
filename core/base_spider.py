"""爬虫抽象基类。

所有平台 spider 都继承 BaseSpider，统一：
- 接口（search / detail / login_state_valid）
- 日志注入
- 限流（min_interval + 抖动）
- 重试策略（仅 NetworkError / RateLimitError 自动重试）
- 异常包装（任何未捕获异常都包成 SpiderError 子类抛出）
"""

from __future__ import annotations

import random
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)
import logging

from core.exceptions import (
    NetworkError,
    RateLimitError,
    SpiderError,
)
from core.logger import get_logger
from core.models import Platform, SearchResult


_std_logger = logging.getLogger("spider.retry")


def retryable(max_attempts: int = 3, min_wait: float = 1.0, max_wait: float = 30.0):
    """装饰器：仅对 NetworkError / RateLimitError 重试，指数退避。"""

    return retry(
        reraise=True,
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=min_wait, max=max_wait),
        retry=retry_if_exception_type((NetworkError, RateLimitError)),
        before_sleep=before_sleep_log(_std_logger, logging.WARNING),
    )


class BaseSpider(ABC):
    """爬虫基类。子类必须实现 platform / search / login_state_valid。"""

    platform: Platform
    min_interval: float = 1.0
    jitter: float = 0.5

    def __init__(self, *, request_id: str | None = None) -> None:
        if not hasattr(self, "platform"):
            raise SpiderError(
                f"{self.__class__.__name__} 必须设置 platform 类属性",
                platform="-",
            )
        self.request_id = request_id or uuid.uuid4().hex[:12]
        self._last_request_at: float = 0.0
        self.log = get_logger(
            platform=self.platform.value,
            spider=self.__class__.__name__,
            request_id=self.request_id,
        )

    def _wait(self) -> None:
        """限流：保证两次请求间至少 min_interval 秒（含 ±jitter 抖动）。"""
        now = time.monotonic()
        elapsed = now - self._last_request_at
        base = self.min_interval
        wait = base + random.uniform(-base * self.jitter, base * self.jitter)
        wait = max(0.0, wait - elapsed)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    @abstractmethod
    def login_state_valid(self) -> bool:
        """登录态校验。无需登录的平台返回 True。"""
        ...

    @abstractmethod
    def search(
        self,
        keyword: str,
        page: int = 1,
        max_pages: int = 1,
        **kwargs: Any,
    ) -> SearchResult:
        """按关键词搜索商品。返回归一化的 SearchResult。"""
        ...

    def detail(self, item_id: str) -> Any:
        """按 item_id 获取商品详情。可选实现。"""
        raise NotImplementedError(
            f"{self.__class__.__name__} 暂未实现 detail()"
        )

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} platform={self.platform.value} req={self.request_id}>"
