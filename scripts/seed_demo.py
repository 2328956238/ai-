"""初始化数据库并写入演示数据。

用法：
    python scripts/seed_demo.py            # 建表 + 写入演示数据（幂等）
    python scripts/seed_demo.py --reset    # 先删表再建（仅用于本地/测试）

演示数据包含两个客户，用来验证订单隔离：
    CUST001 拥有 ORD-2026-0001 / ORD-2026-0002
    CUST002 拥有 ORD-2026-0003
用 CUST001 的身份查询 ORD-2026-0003 必须查不到。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.database import Base  # noqa: E402
from app.db_async import AsyncSessionLocal, async_engine  # noqa: E402
from app.models import (  # noqa: E402
    Customer,
    Inventory,
    Order,
    OrderItem,
    OrderStatus,
    Product,
    ProductStatus,
)


PRODUCTS = [
    dict(product_id="P-IP15-128", title="iPhone 15 128GB 黑色", price=4299.0,
         category="电子产品", source_country="中国", sku="SKU-IP15-128-BK", qty=37, wh="深圳仓"),
    dict(product_id="P-IP16-256", title="iPhone 16 256GB 白色", price=5899.0,
         category="电子产品", source_country="中国", sku="SKU-IP16-256-WH", qty=8, wh="深圳仓"),
    dict(product_id="P-AIRPODS", title="AirPods Pro 2 USB-C", price=1499.0,
         category="电子产品", source_country="越南", sku="SKU-AP2-USBC", qty=0, wh="香港仓"),
    dict(product_id="P-CASE-01", title="透明防摔手机壳", price=39.0,
         category="配件", source_country="中国", sku="SKU-CASE-01", qty=500, wh="深圳仓"),
]

ORDERS = [
    dict(order_no="ORD-2026-0001", customer_no="CUST001", status=OrderStatus.SHIPPED,
         items=[("SKU-IP15-128-BK", "iPhone 15 128GB 黑色", 2, 4299.0)],
         logistics=("顺丰国际", "SF1234567890", "运输中", 2)),
    dict(order_no="ORD-2026-0002", customer_no="CUST001", status=OrderStatus.PAID,
         items=[("SKU-AP2-USBC", "AirPods Pro 2 USB-C", 1, 1499.0)],
         logistics=None),
    dict(order_no="ORD-2026-0003", customer_no="CUST002", status=OrderStatus.PENDING,
         items=[("SKU-IP16-256-WH", "iPhone 16 256GB 白色", 3, 5899.0)],
         logistics=None),
]


async def seed(reset: bool = False) -> None:
    if reset:
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        customers = {}
        for no, name in [("CUST001", "张三"), ("CUST002", "李四")]:
            found = (await db.execute(select(Customer).where(Customer.customer_no == no))).scalars().first()
            if found is None:
                found = Customer(customer_no=no, name=name, phone="1380000000" + no[-1])
                db.add(found)
                await db.flush()
            customers[no] = found

        for p in PRODUCTS:
            found = (await db.execute(select(Product).where(Product.product_id == p["product_id"]))).scalars().first()
            if found is None:
                found = Product(
                    product_id=p["product_id"], title=p["title"], price=p["price"],
                    category=p["category"], source_country=p["source_country"],
                    status=ProductStatus.ACTIVE, description=f"{p['title']} 官方正品",
                )
                db.add(found)
                await db.flush()

            inv = (await db.execute(
                select(Inventory).where(Inventory.sku == p["sku"])
            )).scalars().first()
            if inv is None:
                db.add(Inventory(
                    product_ref_id=found.id, sku=p["sku"], warehouse=p["wh"],
                    quantity=p["qty"], reserved=0, safety_stock=0,
                ))

        for o in ORDERS:
            found = (await db.execute(select(Order).where(Order.order_no == o["order_no"]))).scalars().first()
            if found is not None:
                continue
            total = sum(q * price for _, _, q, price in o["items"])
            order = Order(
                order_no=o["order_no"],
                customer_id=customers[o["customer_no"]].id,
                status=o["status"],
                total_amount=total,
                currency="CNY",
                receiver_name="张三",
                receiver_phone="13800000001",
                address="广东省深圳市南山区",
            )
            if o["logistics"]:
                company, tracking, status, days = o["logistics"]
                order.logistics_company = company
                order.tracking_no = tracking
                order.logistics_status = status
                order.shipped_at = datetime.now(timezone.utc) - timedelta(days=days)
                order.estimated_delivery = datetime.now(timezone.utc) + timedelta(days=3)
            db.add(order)
            await db.flush()
            for sku, title, qty, price in o["items"]:
                db.add(OrderItem(order_id=order.id, sku=sku, title=title, quantity=qty, unit_price=price))

        await db.commit()
    print("✅ 演示数据已就绪：客户 CUST001 / CUST002，商品 4 个，订单 3 笔")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="先删除所有表再重建")
    args = parser.parse_args()
    asyncio.run(seed(reset=args.reset))
