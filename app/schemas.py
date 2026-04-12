from pydantic import BaseModel, Field
from typing import Optional
from datetime import datetime
from enum import Enum

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