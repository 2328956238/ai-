"""query_order：查询当前客户自己的订单。

客户身份来自后端注入的 ToolContext，不来自模型参数 ——
模型无法通过任何参数指定"查别人"。
"""

from __future__ import annotations

from ..schemas import OrderQueryRequest
from ..services.order_service import get_order_for_customer
from .tool_registry import ToolContext, ToolSpec, register


async def _handle(ctx: ToolContext, args: OrderQueryRequest) -> dict:
    return await get_order_for_customer(ctx.db, args.order_no, ctx.customer_id)


SPEC = ToolSpec(
    name="query_order",
    description=(
        "按订单号查询当前客户自己的订单详情，包括订单状态、商品明细、金额和物流信息。"
        "只能查询当前会话所属客户的订单；查询不到时如实告知，不要猜测或编造订单信息。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "order_no": {
                "type": "string",
                "description": "订单号，例如 ORD20260101001",
            }
        },
        "required": ["order_no"],
        "additionalProperties": False,
    },
    args_model=OrderQueryRequest,
    handler=_handle,
)

register(SPEC)
