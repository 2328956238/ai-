"""工具层测试：库存、订单、转人工、白名单、参数校验、异常处理。"""

import pytest

from app.models import ChatSession, Customer, SessionStatus
from app.schemas import InventoryQueryRequest, OrderQueryRequest, TransferToHumanRequest
from app.services.human_service import request_handoff
from app.services.inventory_service import query_inventory
from app.services.order_service import get_order_for_customer
from app.tools import ToolContext, execute


async def _ctx(session, seeded):
    cs = ChatSession(session_id="sess-test-1", customer_id=seeded["c1"].id,
                     status=SessionStatus.ACTIVE)
    session.add(cs)
    await session.commit()
    return ToolContext(db=session, customer_id=seeded["c1"].id,
                       session_id=cs.session_id, session=cs)


# ---------------------------------------------------------------- 库存
async def test_query_inventory_ok(session, seeded):
    res = await query_inventory(session, "iPhone 15")
    assert res["found"] is True
    assert res["product"]["product_id"] == "P-IP15-128"
    # quantity 40 - reserved 3 = 37
    assert res["total_available"] == 37
    assert res["in_stock"] is True
    assert res["warehouses"][0]["sku"] == "SKU-IP15-128-BK"


async def test_query_inventory_by_sku(session, seeded):
    res = await query_inventory(session, "SKU-AP2-USBC")
    assert res["found"] is True
    assert res["total_available"] == 0
    assert res["in_stock"] is False


async def test_query_inventory_not_found(session, seeded):
    res = await query_inventory(session, "不存在的商品 XYZ")
    assert res["found"] is False
    assert res["reason"] == "product_not_found"


async def test_inventory_tool_via_registry(session, seeded):
    ctx = await _ctx(session, seeded)
    ex = await execute("query_inventory", {"query": "iPhone 15"}, ctx)
    assert ex.ok is True
    assert ex.result["total_available"] == 37


# ---------------------------------------------------------------- 订单
async def test_query_order_ok(session, seeded):
    res = await get_order_for_customer(session, "ORD-2026-0001", seeded["c1"].id)
    assert res["found"] is True
    order = res["order"]
    assert order["status"] == "shipped"
    assert order["total_amount"] == 8598.0
    assert order["items"][0]["sku"] == "SKU-IP15-128-BK"
    assert order["logistics"]["tracking_no"] == "SF123"
    assert order["logistics"]["status"] == "运输中"


async def test_query_order_unauthorized(session, seeded):
    """CUST001 查 CUST002 的订单，必须查不到，且不泄露存在性。"""
    res = await get_order_for_customer(session, "ORD-2026-0002", seeded["c1"].id)
    assert res["found"] is False
    assert res["reason"] == "not_found"          # 不是 forbidden，避免枚举
    assert "order" not in res


async def test_order_tool_uses_injected_identity(session, seeded):
    """即使模型传了别的订单号，身份也来自上下文，改不了。"""
    ctx = await _ctx(session, seeded)
    ex = await execute("query_order", {"order_no": "ORD-2026-0002"}, ctx)
    assert ex.ok is True
    assert ex.result["found"] is False


# ---------------------------------------------------------------- 转人工
async def test_transfer_to_human(session, seeded):
    ctx = await _ctx(session, seeded)
    ex = await execute("transfer_to_human",
                       {"reason": "客户要求人工", "reason_code": "customer_request"}, ctx)
    assert ex.ok is True
    assert ex.result["handoff_created"] is True
    assert ex.result["duplicated"] is False
    await session.refresh(ctx.session)
    assert ctx.session.status == SessionStatus.AWAITING_HUMAN


async def test_transfer_to_human_is_idempotent(session, seeded):
    ctx = await _ctx(session, seeded)
    first = await request_handoff(session, ctx.session, ctx.customer_id, "第一次", "ai_limit")
    second = await request_handoff(session, ctx.session, ctx.customer_id, "第二次", "ai_limit")
    assert first["handoff_id"] == second["handoff_id"]
    assert second["duplicated"] is True

    from sqlalchemy import func, select
    from app.models import HumanHandoff
    count = (await session.execute(select(func.count()).select_from(HumanHandoff))).scalar()
    assert count == 1


# ---------------------------------------------------------------- 安全
async def test_unknown_tool_rejected(session, seeded):
    ctx = await _ctx(session, seeded)
    ex = await execute("drop_all_tables", {"confirm": True}, ctx)
    assert ex.ok is False
    assert "unknown_tool" in ex.error


async def test_invalid_arguments_rejected(session, seeded):
    ctx = await _ctx(session, seeded)
    # 多传字段（extra=forbid）
    ex = await execute("query_inventory", {"query": "iPhone", "evil": "1=1"}, ctx)
    assert ex.ok is False
    assert "invalid_arguments" in ex.error


async def test_malformed_json_arguments(session, seeded):
    ctx = await _ctx(session, seeded)
    ex = await execute("query_order", "{not-json", ctx)
    assert ex.ok is False
    assert "invalid_arguments" in ex.error


async def test_tool_exception_is_contained(session, seeded, monkeypatch):
    """工具内部抛异常时，不冒泡，返回 ok=False。"""
    import app.tools.inventory_tools as inv_tools

    async def boom(*args, **kwargs):
        raise RuntimeError("数据库连接断了")

    # 打补丁要打在 handler 真正调用的那个符号上（SPEC 已持有原始 handler）
    monkeypatch.setattr(inv_tools, "query_inventory", boom)
    ex = await execute("query_inventory", {"query": "iPhone"}, await _ctx(session, seeded))
    assert ex.ok is False
    assert "tool_error" in ex.error
