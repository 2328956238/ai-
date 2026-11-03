"""query_inventory：查询商品真实库存。"""

from __future__ import annotations

from ..schemas import InventoryQueryRequest
from ..services.inventory_service import query_inventory
from .tool_registry import ToolContext, ToolSpec, register


async def _handle(ctx: ToolContext, args: InventoryQueryRequest) -> dict:
    return await query_inventory(ctx.db, args.query, args.query_type)


SPEC = ToolSpec(
    name="query_inventory",
    description=(
        "查询商品的真实库存数量。当客户询问某个商品有没有货、还剩多少、价格多少时调用。"
        "支持用商品名称、商品ID 或 SKU 查询。必须调用本工具获取库存，禁止自行编造库存数字。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "商品名称、商品ID 或 SKU 编号",
            },
            "query_type": {
                "type": "string",
                "enum": ["auto", "name", "product_id", "sku"],
                "description": "查询维度，默认 auto 自动识别，通常不需要传",
                "default": "auto",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    args_model=InventoryQueryRequest,
    handler=_handle,
)

register(SPEC)
