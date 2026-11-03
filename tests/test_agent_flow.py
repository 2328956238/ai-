"""Agent / Function Calling 闭环测试（不联网，全部走假 transport）。"""

import json

import pytest

from app.config import settings
from app.models import ChatSession, SessionStatus
from app.services import ai_client, ai_service
from app.tools import ToolContext


async def _ctx(session, seeded):
    cs = ChatSession(session_id="sess-agent-1", customer_id=seeded["c1"].id,
                     status=SessionStatus.ACTIVE)
    session.add(cs)
    await session.commit()
    return ToolContext(db=session, customer_id=seeded["c1"].id,
                       session_id=cs.session_id, session=cs)


async def test_native_tool_call_inventory(session, seeded, transport_factory, tool_call_factory):
    """模型先要库存，后端执行，再让模型基于真实数据回答。"""
    transport = transport_factory([
        tool_call_factory("query_inventory", {"query": "iPhone 15"}),
        {"role": "assistant", "content": "这款还有 37 台现货，单价 4299 元。"},
    ])
    ai_client.set_transport(transport)

    result = await ai_service.agent_turn(await _ctx(session, seeded),
                                         [{"role": "user", "content": "iPhone 15 还有货吗"}])

    assert result.mode == "native"
    assert result.reply.startswith("这款还有 37 台")
    assert len(result.tool_executions) == 1
    ex = result.tool_executions[0]
    assert ex.name == "query_inventory" and ex.ok
    assert ex.result["total_available"] == 37

    # 第二轮请求里必须带上 tool 结果，模型才能引用真实数字
    second = transport.calls[1]["messages"]
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert tool_msgs and "37" in tool_msgs[0]["content"]


async def test_native_tool_call_order(session, seeded, transport_factory, tool_call_factory):
    transport = transport_factory([
        tool_call_factory("query_order", {"order_no": "ORD-2026-0001"}),
        {"role": "assistant", "content": "您的订单已从深圳发出，顺丰单号 SF123。"},
    ])
    ai_client.set_transport(transport)

    result = await ai_service.agent_turn(await _ctx(session, seeded),
                                         [{"role": "user", "content": "我的订单到哪了"}])
    ex = result.tool_executions[0]
    assert ex.ok and ex.result["found"] is True
    assert ex.result["order"]["logistics"]["tracking_no"] == "SF123"


async def test_native_transfer_to_human(session, seeded, transport_factory, tool_call_factory):
    transport = transport_factory([
        tool_call_factory("transfer_to_human",
                          {"reason": "客户明确要求人工", "reason_code": "customer_request"}),
        {"role": "assistant", "content": "已经帮您转接人工客服，请稍等。"},
    ])
    ai_client.set_transport(transport)

    ctx = await _ctx(session, seeded)
    result = await ai_service.agent_turn(ctx, [{"role": "user", "content": "我要人工"}])

    assert result.handoff is not None
    assert result.handoff["handoff_created"] is True
    await session.refresh(ctx.session)
    assert ctx.session.status == SessionStatus.AWAITING_HUMAN


async def test_tool_failure_still_answers(session, seeded, transport_factory, tool_call_factory):
    """工具失败时把错误回填给模型，对话不能中断。"""
    transport = transport_factory([
        tool_call_factory("query_order", {"order_no": "NOPE-404"}),
        {"role": "assistant", "content": "抱歉，没有查到这个订单号，请核对一下。"},
    ])
    ai_client.set_transport(transport)

    result = await ai_service.agent_turn(await _ctx(session, seeded),
                                         [{"role": "user", "content": "查订单 NOPE-404"}])
    ex = result.tool_executions[0]
    assert ex.ok is True and ex.result["found"] is False      # 工具正常返回"未找到"
    assert "没有查到" in result.reply

    tool_msgs = [m for m in transport.calls[1]["messages"] if m.get("role") == "tool"]
    assert "not_found" in tool_msgs[0]["content"]


async def test_ai_call_failure_returns_graceful_reply(session, seeded, transport_factory):
    async def boom(payload):
        raise ai_client.AICallError("upstream 502")

    ai_client.set_transport(boom)
    result = await ai_service.agent_turn(await _ctx(session, seeded),
                                         [{"role": "user", "content": "在吗"}])
    assert result.error == "ai_call_failed"
    assert "稍后再试" in result.reply


async def test_missing_api_key_returns_config_error(session, seeded, monkeypatch):
    monkeypatch.setattr(settings, "ai_api_key", "")
    ai_client.set_transport(None)
    result = await ai_service.agent_turn(await _ctx(session, seeded),
                                         [{"role": "user", "content": "在吗"}])
    assert result.error == "config_error"


async def test_prompt_mode_fallback(session, seeded, transport_factory, monkeypatch):
    """网关不支持 tools 时，走提示词 JSON 协议。"""
    monkeypatch.setattr(settings, "ai_tool_mode", "prompt")

    transport = transport_factory([
        {"role": "assistant",
         "content": json.dumps({"tool": "query_inventory", "args": {"query": "iPhone 15"}})},
        {"role": "assistant", "content": "目前还有 37 台。"},
    ])
    ai_client.set_transport(transport)

    result = await ai_service.agent_turn(await _ctx(session, seeded),
                                         [{"role": "user", "content": "还有多少台"}])
    assert result.mode == "prompt"
    assert result.tool_executions[0].result["total_available"] == 37
    assert "37" in result.reply


async def test_auto_mode_downgrades_when_tools_unsupported(
    session, seeded, transport_factory, monkeypatch
):
    """auto 模式下，原生调用报"不支持 tools"应自动降级到 prompt。"""
    monkeypatch.setattr(settings, "ai_tool_mode", "auto")
    attempts = {"native": 0}

    async def transport(payload):
        if "tools" in payload:
            attempts["native"] += 1
            raise ai_client.ToolsUnsupported("tools not supported")
        return {"role": "assistant", "content": "现在还有 37 台。"}

    ai_client.set_transport(transport)
    result = await ai_service.agent_turn(await _ctx(session, seeded),
                                         [{"role": "user", "content": "库存多少"}])
    assert attempts["native"] == 1
    assert result.mode == "prompt"
    assert "37" in result.reply


async def test_max_rounds_forces_handoff(session, seeded, transport_factory, tool_call_factory,
                                          monkeypatch):
    """模型一直要工具、迟迟不给答案时，强制转人工，避免死循环。"""
    monkeypatch.setattr(settings, "ai_max_tool_rounds", 2)
    script = [tool_call_factory("query_inventory", {"query": "iPhone 15"}, f"c{i}") for i in range(3)]
    ai_client.set_transport(transport_factory(script))

    ctx = await _ctx(session, seeded)
    result = await ai_service.agent_turn(ctx, [{"role": "user", "content": "嗯？"}])

    assert result.handoff is not None
    assert any(ex.name == "transfer_to_human" for ex in result.tool_executions)


async def test_multi_turn_keeps_context(session, seeded, transport_factory):
    """多轮：第二轮请求里必须带上第一轮的历史。"""
    transport = transport_factory([
        {"role": "assistant", "content": "您好，请问想看哪款？"},
        {"role": "assistant", "content": "好的，我帮您查。"},
    ])
    ai_client.set_transport(transport)
    ctx = await _ctx(session, seeded)

    await ai_service.agent_turn(ctx, [{"role": "user", "content": "你好"}])
    await ai_service.agent_turn(
        ctx,
        [{"role": "user", "content": "你好"},
         {"role": "assistant", "content": "您好，请问想看哪款？"},
         {"role": "user", "content": "iPhone 15"}],
    )

    history = transport.calls[1]["messages"]
    contents = [m["content"] for m in history if m.get("role") == "user"]
    assert "你好" in contents and "iPhone 15" in contents
