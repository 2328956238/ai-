import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 必须在导入 app 之前设置环境变量（settings 是模块级单例）
os.environ.update(
    {
        "DATABASE_URL": "sqlite:///./test_product.db",
        "ASYNC_DATABASE_URL": "sqlite+aiosqlite:///./test_product.db",
        "REDIS_ENABLED": "false",
        "AI_API_KEY": "test-key-not-real",
        "AI_BASE_URL": "https://example.invalid/v1",
        "AI_MODEL": "test-model",
        "AI_TOOL_MODE": "native",
        "AI_MAX_TOOL_ROUNDS": "3",
        "AI_MAX_RETRIES": "1",
        "AI_TIMEOUT": "5",
    }
)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app import db_async, models  # noqa: E402,F401
from app.database import Base  # noqa: E402
from app.db_async import get_async_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import ai_client, ai_service  # noqa: E402


@pytest_asyncio.fixture
async def engine(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    eng = create_async_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


@pytest_asyncio.fixture
async def seeded(session):
    """两个客户 + 商品 + 库存 + 订单，用于隔离性测试。"""
    from datetime import datetime, timedelta, timezone

    from app.models import (
        Customer, Inventory, Order, OrderItem, OrderStatus, Product, ProductStatus,
    )

    c1 = Customer(customer_no="CUST001", name="张三")
    c2 = Customer(customer_no="CUST002", name="李四")
    session.add_all([c1, c2])
    await session.flush()

    p1 = Product(product_id="P-IP15-128", title="iPhone 15 128GB 黑色", price=4299.0,
                 category="电子产品", status=ProductStatus.ACTIVE)
    p2 = Product(product_id="P-AP2", title="AirPods Pro 2", price=1499.0,
                 category="电子产品", status=ProductStatus.ACTIVE)
    session.add_all([p1, p2])
    await session.flush()

    session.add_all([
        Inventory(product_ref_id=p1.id, sku="SKU-IP15-128-BK", warehouse="深圳仓",
                  quantity=40, reserved=3, safety_stock=0),
        Inventory(product_ref_id=p2.id, sku="SKU-AP2-USBC", warehouse="香港仓",
                  quantity=0, reserved=0, safety_stock=0),
    ])

    o1 = Order(order_no="ORD-2026-0001", customer_id=c1.id, status=OrderStatus.SHIPPED,
               total_amount=8598.0, currency="CNY",
               logistics_company="顺丰国际", tracking_no="SF123", logistics_status="运输中",
               shipped_at=datetime.now(timezone.utc) - timedelta(days=2),
               estimated_delivery=datetime.now(timezone.utc) + timedelta(days=3))
    o2 = Order(order_no="ORD-2026-0002", customer_id=c2.id, status=OrderStatus.PAID,
               total_amount=5899.0, currency="CNY")
    session.add_all([o1, o2])
    await session.flush()

    session.add_all([
        OrderItem(order_id=o1.id, sku="SKU-IP15-128-BK", title="iPhone 15 128GB 黑色",
                  quantity=2, unit_price=4299.0),
        OrderItem(order_id=o2.id, sku="SKU-IP16-256-WH", title="iPhone 16 256GB 白色",
                  quantity=1, unit_price=5899.0),
    ])
    await session.commit()

    return {"c1": c1, "c2": c2, "p1": p1, "p2": p2, "o1": o1, "o2": o2}


@pytest_asyncio.fixture
async def client(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _override():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_async_db] = _override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_ai_state():
    ai_client.set_transport(None)
    ai_service.reset_native_support_cache()
    yield
    ai_client.set_transport(None)
    ai_service.reset_native_support_cache()


def make_transport(script: list[dict]):
    """按脚本顺序返回 assistant message，并记录收到的 payload。"""
    calls: list[dict] = []

    async def transport(payload: dict) -> dict:
        calls.append(payload)
        if not script:
            return {"role": "assistant", "content": "（脚本已耗尽）"}
        return script.pop(0)

    transport.calls = calls  # type: ignore[attr-defined]
    return transport


def tool_call(name: str, args: dict, call_id: str = "call_1") -> dict:
    import json

    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        ],
    }


@pytest.fixture
def transport_factory():
    return make_transport


@pytest.fixture
def tool_call_factory():
    return tool_call
