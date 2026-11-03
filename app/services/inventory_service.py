"""库存查询服务（只读，供 query_inventory 工具调用）。

原则：
- 库存数字只能来自数据库，模型中不允许出现任何库存推断。
- Redis 只是加速层，任何 Redis 异常都必须退化到查库，不能让业务失败。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import Inventory, Product, ProductStatus

logger = logging.getLogger(__name__)

_redis = None
_redis_checked = False


def _get_redis():
    """惰性初始化 Redis；不可用时返回 None，调用方自行降级。"""
    global _redis, _redis_checked
    if _redis_checked:
        return _redis
    _redis_checked = True
    if not settings.redis_enabled:
        return None
    try:
        import redis.asyncio as aioredis

        _redis = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=settings.redis_timeout,
            socket_connect_timeout=settings.redis_timeout,
        )
    except Exception as exc:  # pragma: no cover - 环境缺依赖时
        logger.warning("Redis 初始化失败，库存查询将只走数据库: %s", exc)
        _redis = None
    return _redis


async def _cache_get(key: str) -> dict[str, Any] | None:
    client = _get_redis()
    if client is None:
        return None
    try:
        raw = await client.get(key)
        return json.loads(raw) if raw else None
    except Exception as exc:  # Redis 挂了不影响主流程
        logger.warning("Redis 读取失败（已降级）: %s", exc)
        return None


async def _cache_set(key: str, value: dict[str, Any]) -> None:
    client = _get_redis()
    if client is None:
        return
    try:
        await client.setex(key, settings.cache_ttl, json.dumps(value, ensure_ascii=False, default=str))
    except Exception as exc:
        logger.warning("Redis 写入失败（忽略）: %s", exc)


def _empty(reason: str, query: str) -> dict[str, Any]:
    return {"found": False, "reason": reason, "query": query}


async def _load_product(db: AsyncSession, query: str, query_type: str) -> Product | None:
    """按 SKU / 商品ID / 名称 定位商品。全部使用参数绑定，不拼接任何 SQL。"""
    q = query.strip()

    if query_type in ("auto", "sku"):
        rows = await db.execute(select(Inventory).where(Inventory.sku == q).limit(1))
        inv = rows.scalars().first()
        if inv is not None:
            res = await db.execute(select(Product).where(Product.id == inv.product_ref_id))
            product = res.scalars().first()
            if product is not None:
                return product

    if query_type in ("auto", "product_id"):
        res = await db.execute(select(Product).where(Product.product_id == q))
        product = res.scalars().first()
        if product is not None:
            return product

    if query_type in ("auto", "name"):
        # 名称模糊匹配：先精确再包含，避免 "iPhone" 命中一堆无关商品
        res = await db.execute(select(Product).where(Product.title == q).limit(1))
        product = res.scalars().first()
        if product is not None:
            return product
        res = await db.execute(
            select(Product)
            .where(or_(Product.title.like(f"%{q}%"), Product.description.like(f"%{q}%")))
            .order_by(Product.id)
            .limit(1)
        )
        return res.scalars().first()

    return None


async def query_inventory(db: AsyncSession, query: str, query_type: str = "auto") -> dict[str, Any]:
    """查询商品真实库存。返回结构固定，便于模型稳定引用。"""
    query = (query or "").strip()
    if not query:
        return _empty("empty_query", query)

    cache_key = f"inv:{query_type}:{query.lower()}"
    cached = await _cache_get(cache_key)
    if cached is not None:
        cached["cached"] = True
        return cached

    try:
        product = await _load_product(db, query, query_type)
        if product is None:
            return _empty("product_not_found", query)

        rows = await db.execute(select(Inventory).where(Inventory.product_ref_id == product.id))
        items = rows.scalars().all()

        warehouses = [
            {
                "sku": it.sku,
                "warehouse": it.warehouse,
                "quantity": it.quantity,
                "reserved": it.reserved,
                "available": max(it.quantity - it.reserved - it.safety_stock, 0),
            }
            for it in items
        ]
        total_available = sum(w["available"] for w in warehouses)

        result = {
            "found": True,
            "product": {
                "product_id": product.product_id,
                "title": product.title,
                "price": product.price,
                "category": product.category,
                "status": product.status.value if product.status else None,
            },
            "total_available": total_available,
            "warehouses": warehouses,
            "in_stock": total_available > 0,
            "cached": False,
        }
        if product.status == ProductStatus.INACTIVE:
            result["note"] = "该商品已下架，库存仅供参考"

        await _cache_set(cache_key, result)
        return result

    except SQLAlchemyError as exc:
        logger.exception("查询库存数据库异常: %s", exc)
        return {"found": False, "reason": "database_error", "query": query}
