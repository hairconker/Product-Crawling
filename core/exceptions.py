"""自定义异常体系。

所有 spider 抛出的异常都应是 SpiderError 子类，便于统一捕获、重试与日志。
"""

from __future__ import annotations

from typing import Any


class SpiderError(Exception):
    """爬虫异常基类。携带平台、URL 与原始响应片段，方便日志关联。"""

    def __init__(
        self,
        message: str,
        *,
        platform: str | None = None,
        url: str | None = None,
        raw_response: Any = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.platform = platform
        self.url = url
        self.request_id = request_id
        self.raw_response = self._truncate(raw_response)

    @staticmethod
    def _truncate(data: Any, limit: int = 2048) -> Any:
        if data is None:
            return None
        text = data if isinstance(data, str) else repr(data)
        if len(text) > limit:
            return text[:limit] + f"... <truncated, total {len(text)} chars>"
        return text

    def __str__(self) -> str:
        parts = [self.message]
        if self.platform:
            parts.append(f"platform={self.platform}")
        if self.url:
            parts.append(f"url={self.url}")
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        return " | ".join(parts)


class ConfigError(SpiderError):
    """配置缺失或格式错误。"""


class NetworkError(SpiderError):
    """网络层错误：超时、DNS、连接拒绝、代理失败等。可重试。"""


class LoginRequiredError(SpiderError):
    """登录态缺失或失效，需要用户重新提供 cookie / 扫码。不可自动重试。"""


class AntiSpiderError(SpiderError):
    """触发风控：CAPTCHA、滑块、IP 封禁等。"""


class RateLimitError(AntiSpiderError):
    """限流（HTTP 429 或业务码）。可退避后重试。"""


class ParseError(SpiderError):
    """页面/接口结构变化导致解析失败。说明参考项目可能已过时，需更新代码。"""


class PlatformError(SpiderError):
    """平台返回业务错误（商品下架、关键词违规等）。一般不可重试。"""
