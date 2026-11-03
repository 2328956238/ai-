"""对话 / Function Calling 路由。

身份：用 X-Customer-No 头模拟已登录客户（真实项目应换成 JWT / Session）。
客户身份只从服务端查库得到，绝不从模型参数或请求体里取 customer_id。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db_async import get_async_db
from ..models import Customer
from ..schemas import ChatRequest, ChatResponse
from ..services import human_service, tool_service
from ..tools import tool_names

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/chat", tags=["chat"])


async def get_current_customer(
    x_customer_no: str | None = Header(default=None, alias="X-Customer-No"),
    db: AsyncSession = Depends(get_async_db),
) -> Customer:
    if not x_customer_no:
        raise HTTPException(status_code=401, detail="缺少 X-Customer-No 请求头")
    res = await db.execute(select(Customer).where(Customer.customer_no == x_customer_no))
    customer = res.scalars().first()
    if customer is None:
        raise HTTPException(status_code=401, detail="客户不存在或未登录")
    return customer


@router.post("/messages", response_model=ChatResponse)
async def send_message(
    payload: ChatRequest,
    customer: Customer = Depends(get_current_customer),
    db: AsyncSession = Depends(get_async_db),
):
    """发送一条消息，返回 AI 回复（可能包含工具调用记录）。"""
    try:
        return await tool_service.run_chat_turn(
            db=db,
            customer_id=customer.id,
            message=payload.message,
            session_id=payload.session_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/tools")
async def list_tools():
    """查看当前白名单内的工具（便于排查与验收）。"""
    return {"tools": tool_names()}


@router.post("/handoffs/{session_id}/accept")
async def accept_handoff(
    session_id: str,
    agent: str,
    note: str | None = None,
    db: AsyncSession = Depends(get_async_db),
):
    """人工客服接管（为后续工作台预留）。"""
    result = await human_service.accept_handoff(db, session_id, agent=agent, note=note)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="转人工记录不存在")
    return result


@router.post("/handoffs/{session_id}/close")
async def close_session(session_id: str, db: AsyncSession = Depends(get_async_db)):
    result = await human_service.close_session(db, session_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail="会话不存在")
    return result
