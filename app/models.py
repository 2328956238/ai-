"""数据模型。

原有 Product / BatchTask 完全保留（字段、表名、枚举都没动），只在文件末尾追加
本次 Function Calling 需要的新表：客户、库存、订单、订单明细、会话、消息、转人工记录。
"""

import enum

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .database import Base


class ProductStatus(str, enum.Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(String(50), unique=True, index=True, nullable=False)  # 外部商品ID
    title = Column(String(200), nullable=False)
    description = Column(String(1000), nullable=True)
    price = Column(Float, nullable=False)
    category = Column(String(50), nullable=True)
    source_country = Column(String(50), nullable=True)  # 来源国家
    status = Column(Enum(ProductStatus), default=ProductStatus.ACTIVE)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class BatchTask(Base):
    __tablename__ = "batch_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(String(36), unique=True, index=True, nullable=False)  # UUID
    status = Column(String(20), default="pending")  # pending, processing, completed, failed
    total = Column(Integer, default=0)
    success = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    error_file_path = Column(String(500), nullable=True)  # 错误记录文件路径
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


# ============================================================================
# 以下为 Function Calling 改造新增
# ============================================================================


class Customer(Base):
    """客户。工具调用时的身份来源，用于订单数据隔离。"""

    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    customer_no = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(100), nullable=True)
    phone = Column(String(30), nullable=True)
    email = Column(String(120), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    orders = relationship("Order", back_populates="customer")


class Inventory(Base):
    """库存独立成表。

    原 Product 没有库存字段，按需求"不随意修改现有商品数据结构"，
    这里用独立表通过 product_id 关联，同一个商品可分布在多个仓库。
    """

    __tablename__ = "inventory"
    __table_args__ = (
        UniqueConstraint("sku", "warehouse", name="uq_inventory_sku_warehouse"),
        Index("ix_inventory_product", "product_ref_id"),
    )

    id = Column(Integer, primary_key=True)
    # 关联 products.id（内部主键），不覆盖原有字段
    product_ref_id = Column(Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    sku = Column(String(64), nullable=False, index=True)
    warehouse = Column(String(64), nullable=False, default="default")
    quantity = Column(Integer, nullable=False, default=0)      # 可售数量
    reserved = Column(Integer, nullable=False, default=0)      # 已锁定（下单未发货）
    safety_stock = Column(Integer, nullable=False, default=0)  # 安全库存，不计入可售
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())

    @property
    def available(self) -> int:
        return max(self.quantity - self.reserved - self.safety_stock, 0)


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    order_no = Column(String(64), unique=True, index=True, nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING, nullable=False)
    total_amount = Column(Float, nullable=False, default=0.0)
    currency = Column(String(8), nullable=False, default="CNY")

    receiver_name = Column(String(100), nullable=True)
    receiver_phone = Column(String(30), nullable=True)
    address = Column(String(300), nullable=True)

    # 物流信息：需求里明确要求返回，放在订单主表避免额外 join
    logistics_company = Column(String(80), nullable=True)
    tracking_no = Column(String(80), nullable=True)
    logistics_status = Column(String(40), nullable=True)
    shipped_at = Column(DateTime(timezone=True), nullable=True)
    estimated_delivery = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    customer = relationship("Customer", back_populates="orders")
    items = relationship("OrderItem", back_populates="order", cascade="all, delete-orphan")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_ref_id = Column(Integer, ForeignKey("products.id", ondelete="SET NULL"), nullable=True)
    sku = Column(String(64), nullable=True)
    title = Column(String(200), nullable=False)   # 下单时的标题快照
    quantity = Column(Integer, nullable=False, default=1)
    unit_price = Column(Float, nullable=False, default=0.0)

    order = relationship("Order", back_populates="items")


class SessionStatus(str, enum.Enum):
    ACTIVE = "active"            # AI 正常接待
    AWAITING_HUMAN = "awaiting_human"  # 已请求人工，等待接管
    HUMAN = "human"              # 人工已接管，AI 静默
    CLOSED = "closed"


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), unique=True, index=True, nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    status = Column(Enum(SessionStatus), default=SessionStatus.ACTIVE, nullable=False)
    handoff_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), ForeignKey("chat_sessions.session_id", ondelete="CASCADE"),
                        nullable=False, index=True)
    role = Column(String(16), nullable=False)          # user / assistant / tool / system
    content = Column(Text, nullable=True)
    tool_name = Column(String(64), nullable=True)
    tool_args = Column(JSON, nullable=True)
    tool_result = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    session = relationship("ChatSession", back_populates="messages")


class HandoffStatus(str, enum.Enum):
    PENDING = "pending"    # 等待人工接入
    ACCEPTED = "accepted"  # 人工已接管
    CLOSED = "closed"


class HumanHandoff(Base):
    """转人工记录。

    session_id 上建唯一约束 —— 这是"防止重复执行转人工"的最终保证：
    即使 AI 在一轮里两次调用工具，数据库也只会留下一条记录。
    """

    __tablename__ = "human_handoffs"
    __table_args__ = (UniqueConstraint("session_id", name="uq_handoff_session"),)

    id = Column(Integer, primary_key=True)
    session_id = Column(String(64), nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id", ondelete="CASCADE"), nullable=False)
    reason = Column(Text, nullable=False)
    reason_code = Column(String(40), nullable=False, default="ai_limit")
    status = Column(Enum(HandoffStatus), default=HandoffStatus.PENDING, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    accepted_at = Column(DateTime(timezone=True), nullable=True)
    accepted_by = Column(String(64), nullable=True)
    note = Column(Text, nullable=True)
