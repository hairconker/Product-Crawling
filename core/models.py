"""统一数据模型。各平台返回必须归一化为 Product / SearchResult。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


class Platform(str, Enum):
    XIANYU = "xianyu"
    JD = "jd"
    TAOBAO = "taobao"
    PDD = "pdd"


class Product(BaseModel):
    """归一化的商品。各平台 spider 必须填齐核心字段，平台特有字段塞 raw。"""

    platform: Platform
    item_id: str = Field(..., description="平台内唯一商品 ID（如京东 sku_id）")
    title: str
    url: str
    current_price: float | None = Field(None, description="当前售价（元），无价时 None")
    origin_price: float | None = Field(None, description="划线/原价（元）")
    currency: str = "CNY"

    image_url: str | None = None
    shop_name: str | None = None
    shop_url: str | None = None
    location: str | None = None
    sales: int | None = Field(None, description="近期销量（条件平台才有）")
    is_second_hand: bool = False

    crawled_at: datetime = Field(default_factory=datetime.now)
    raw: dict[str, Any] = Field(default_factory=dict, description="平台原始字段，不参与去重")

    def dedup_key(self) -> str:
        return f"{self.platform.value}:{self.item_id}"


class SearchResult(BaseModel):
    """一次搜索的返回容器。"""

    platform: Platform
    keyword: str
    page: int
    total_pages: int | None = None
    products: list[Product] = Field(default_factory=list)
    crawled_at: datetime = Field(default_factory=datetime.now)
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.products)
