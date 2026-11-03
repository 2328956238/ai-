"""Pydantic 模型。原有商品相关 schema 保持不变，文件末尾追加本次新增。"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class ProductStatusEnum(str, Enum):
    active = "active"
    inactive = "inactive"


class ProductBase(BaseModel):
    product_id: str = Field(..., max_length=50)
    title: str = Field(..., max_length=200)
    description: Optional[str] = None
    price: float = Field(..., gt=0)
    category: Optional[str] = None
    source_country: Optional[str] = None


class ProductCreate(ProductBase):
    status: ProductStatusEnum = ProductStatusEnum.active


class ProductUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    price: Optional[float] = Field(None, gt=0)
    category: Optional[str] = None
    source_country: Optional[str] = None
    status: Optional[ProductStatusEnum] = None


class ProductResponse(ProductBase):
    id: int
    status: ProductStatusEnum
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True  # 新版 Pydantic v2 使用 from_attributes


# ============================================================================
# Function Calling 新增
# ============================================================================


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000, description="客户消息")
    session_id: Optional[str] = Field(
        default=None, max_length=64, description="会话 ID，不传则新建会话"
    )


class ToolCallRecord(BaseModel):
    name: str
    arguments: dict[str, Any]
    ok: bool
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    latency_ms: int = 0


class HandoffInfo(BaseModel):
    reason: str
    reason_code: str
    status: str
    created_at: Optional[datetime] = None
    duplicated: bool = False   # True 表示本次是重复触发，复用了既有记录


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    session_status: str
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    handoff: Optional[HandoffInfo] = None
    signal: Optional[dict[str, Any]] = Field(
        default=None, description="本轮命中的行为信号（code / reasons / action）"
    )


class InventoryQueryRequest(BaseModel):
    """query_inventory 工具的参数模型。

    extra="forbid" 是安全要求：模型多传任何字段都会被拒绝，避免参数走私。
    """

    model_config = {"extra": "forbid"}

    query: str = Field(..., min_length=1, max_length=120, description="商品名称 / 商品ID / SKU")
    query_type: str = Field(
        default="auto",
        pattern="^(auto|name|product_id|sku)$",
        description="指定查询维度，默认自动识别",
    )


class OrderQueryRequest(BaseModel):
    model_config = {"extra": "forbid"}

    order_no: str = Field(..., min_length=3, max_length=64, description="订单号")


class TransferToHumanRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: str = Field(..., min_length=2, max_length=500, description="转人工原因")
    reason_code: str = Field(
        default="ai_limit",
        pattern="^(customer_request|ai_limit|emotion|out_of_scope|other)$",
        description="原因分类",
    )
