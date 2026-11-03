"""订单查询服务。

安全边界都在这里：
- 只允许按「订单号 + 当前客户」联合查询。
- 查不到和越权返回同一个结果，避免通过错误信息探测别人订单是否存在。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models import Order, OrderItem

logger = logging.getLogger(__name__)


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


async def get_order_for_customer(
    db: AsyncSession, order_no: str, customer_id: int
) -> dict[str, Any]:
    """按订单号查询，并强制校验归属。

    越权时返回 found=False / reason=not_found（不回显"无权限"，防止枚举）。
    """
    order_no = (order_no or "").strip()
    if not order_no:
        return {"found": False, "reason": "empty_order_no"}

    try:
        res = await db.execute(
            select(Order).where(Order.order_no == order_no).options(selectinload(Order.items))
        )
        order = res.scalars().first()

        if order is None:
            return {"found": False, "reason": "not_found"}

        if order.customer_id != customer_id:
            # 记一条安全日志，但不告诉调用方差异
            logger.warning(
                "越权订单查询被拦截 order_no=%s caller=%s owner=%s",
                order_no, customer_id, order.customer_id,
            )
            return {"found": False, "reason": "not_found"}

        return {
            "found": True,
            "order": {
                "order_no": order.order_no,
                "status": order.status.value if order.status else None,
                "total_amount": order.total_amount,
                "currency": order.currency,
                "created_at": _iso(order.created_at),
                "items": [
                    {
                        "sku": it.sku,
                        "title": it.title,
                        "quantity": it.quantity,
                        "unit_price": it.unit_price,
                    }
                    for it in (order.items or [])
                ],
                "logistics": {
                    "company": order.logistics_company,
                    "tracking_no": order.tracking_no,
                    "status": order.logistics_status,
                    "shipped_at": _iso(order.shipped_at),
                    "estimated_delivery": _iso(order.estimated_delivery),
                },
            },
        }
    except SQLAlchemyError as exc:
        logger.exception("查询订单数据库异常: %s", exc)
        return {"found": False, "reason": "database_error"}


async def list_customer_orders(db: AsyncSession, customer_id: int, limit: int = 5) -> list[dict]:
    """最近订单列表（可选工具能力，当前未暴露给模型）。"""
    try:
        res = await db.execute(
            select(Order).where(Order.customer_id == customer_id).order_by(Order.id.desc()).limit(limit)
        )
        return [
            {
                "order_no": o.order_no,
                "status": o.status.value if o.status else None,
                "total_amount": o.total_amount,
                "created_at": _iso(o.created_at),
            }
            for o in res.scalars().all()
        ]
    except SQLAlchemyError:
        logger.exception("订单列表查询失败")
        return []
