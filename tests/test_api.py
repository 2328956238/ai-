"""HTTP 层测试：身份校验、会话、工具调用记录、转人工后的静默。"""

from app.models import ChatSession, SessionStatus
from app.services import ai_client


async def test_missing_customer_header(client):
    r = await client.post("/chat/messages", json={"message": "你好"})
    assert r.status_code == 401


async def test_unknown_customer(client):
    r = await client.post("/chat/messages", json={"message": "你好"},
                          headers={"X-Customer-No": "CUST404"})
    assert r.status_code == 401


async def test_chat_inventory_tool_round_trip(client, seeded, transport_factory, tool_call_factory):
    ai_client.set_transport(transport_factory([
        tool_call_factory("query_inventory", {"query": "iPhone 15"}),
        {"role": "assistant", "content": "还有 37 台，4299 元。"},
    ]))

    r = await client.post("/chat/messages", json={"message": "iPhone 15 有货吗"},
                          headers={"X-Customer-No": "CUST001"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_id"]
    assert body["session_status"] == "active"
    assert body["tool_calls"][0]["name"] == "query_inventory"
    assert body["tool_calls"][0]["ok"] is True
    assert body["tool_calls"][0]["result"]["total_available"] == 37
    assert "37" in body["reply"]


async def test_chat_session_continuity(client, seeded, transport_factory):
    ai_client.set_transport(transport_factory([
        {"role": "assistant", "content": "您好"},
        {"role": "assistant", "content": "好的"},
    ]))
    headers = {"X-Customer-No": "CUST001"}

    first = await client.post("/chat/messages", json={"message": "你好"}, headers=headers)
    sid = first.json()["session_id"]

    second = await client.post("/chat/messages", json={"message": "在吗", "session_id": sid},
                               headers=headers)
    assert second.json()["session_id"] == sid


async def test_chat_rejects_other_customers_session(client, seeded, transport_factory):
    ai_client.set_transport(transport_factory([{"role": "assistant", "content": "hi"}]))
    r = await client.post("/chat/messages", json={"message": "你好"},
                          headers={"X-Customer-No": "CUST001"})
    sid = r.json()["session_id"]

    # CUST002 拿 CUST001 的 session_id 必须被拒绝
    r2 = await client.post("/chat/messages", json={"message": "我是别人", "session_id": sid},
                           headers={"X-Customer-No": "CUST002"})
    assert r2.status_code == 403


async def test_session_silent_after_handoff(client, seeded, transport_factory, tool_call_factory):
    ai_client.set_transport(transport_factory([
        tool_call_factory("transfer_to_human",
                          {"reason": "客户要求人工", "reason_code": "customer_request"}),
        {"role": "assistant", "content": "已为您转接人工。"},
    ]))
    headers = {"X-Customer-No": "CUST001"}
    first = await client.post("/chat/messages", json={"message": "我要人工"}, headers=headers)
    body = first.json()
    assert body["handoff"]["reason_code"] == "customer_request"
    assert body["session_status"] == "awaiting_human"

    # 转人工后 AI 必须静默，且不再调用模型
    calls_before = len(ai_client._TRANSPORT.calls)  # type: ignore[union-attr]
    second = await client.post("/chat/messages",
                               json={"message": "还在吗", "session_id": body["session_id"]},
                               headers=headers)
    assert second.json()["session_status"] == "awaiting_human"
    assert "人工" in second.json()["reply"]
    assert len(ai_client._TRANSPORT.calls) == calls_before  # type: ignore[union-attr]


async def test_tools_endpoint_lists_whitelist(client):
    r = await client.get("/chat/tools")
    assert r.status_code == 200
    assert set(r.json()["tools"]) == {"query_inventory", "query_order", "transfer_to_human"}


async def test_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
