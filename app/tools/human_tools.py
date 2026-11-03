"""transfer_to_human：转人工。

真实业务逻辑在 human_service：落工单、改会话状态、可幂等复用。
这里只做参数到服务的映射。
"""

from __future__ import annotations

from ..schemas import TransferToHumanRequest
from ..services.human_service import request_handoff
from .tool_registry import ToolContext, ToolSpec, register


async def _handle(ctx: ToolContext, args: TransferToHumanRequest) -> dict:
    return await request_handoff(
        db=ctx.db,
        session=ctx.session,
        customer_id=ctx.customer_id,
        reason=args.reason,
        reason_code=args.reason_code,
    )


SPEC = ToolSpec(
    name="transfer_to_human",
    description=(
        "把当前会话转接给人工客服。当客户明确要求人工、情绪明显不满、问题超出你的能力范围，"
        "或你连续多轮都无法解决时调用。调用后会话状态会变更，人工客服会看到转接原因。"
        "如果当前会话已经转过人工，本工具会返回既有记录，不会重复建单。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "转人工的具体原因，写清楚客户遇到什么问题，供人工客服参考",
            },
            "reason_code": {
                "type": "string",
                "enum": ["customer_request", "ai_limit", "emotion", "out_of_scope", "other"],
                "description": "原因分类：客户主动要求 / AI 能力不足 / 情绪问题 / 超出范围 / 其他",
                "default": "ai_limit",
            },
        },
        "required": ["reason"],
        "additionalProperties": False,
    },
    args_model=TransferToHumanRequest,
    handler=_handle,
)

register(SPEC)
