"""行为信号测试：识别对不对、优先级对不对、动作对不对、有没有真的影响对话。"""

from datetime import datetime, timedelta, timezone

import pytest

from app.services import ai_client, signals
from app.services.signals import detect, guidance_note


def _u(text):
    return {"role": "user", "content": text}


# ----------------------------------------------------------------- 基础识别
def test_terse():
    r = detect("ok")
    assert r.code == "terse"
    assert r.action["long_explain"] is False


def test_terse_ignores_real_question():
    """短句但带问号，不算敷衍。"""
    r = detect("有货吗")
    assert r.code != "terse"


def test_short_complaint_is_not_terse():
    """'价格太贵了' 只有 5 个字，但含业务信息，不能判成敷衍。"""
    r = detect("价格太贵了")
    assert r.code != "terse"


def test_wants_human():
    r = detect("我要找人工客服")
    assert r.code == "wants_human"
    assert r.action["force_handoff"] is True


def test_wants_human_english():
    assert detect("can I speak to a human").code == "wants_human"


def test_price_focus():
    history = [_u("iPhone 15多少钱"), _u("那16呢，多少钱")]
    r = detect("到底多少钱能便宜点吗", history)
    assert r.code == "price_focus"
    assert r.action["give_price_range"] is True
    assert r.action["ask_next"] is False


def test_not_understood_on_repeat():
    history = [_u("iPhone 15 128G 有货吗")]
    r = detect("iPhone 15 128g 有货吗？", history)
    assert r.code == "not_understood"
    assert r.action["rephrase"] is True


def test_neutral():
    r = detect("我想问下 iPhone 15 的发货时间", [_u("你好")])
    assert r.code == "neutral"
    assert r.action["ask_next"] is True


def test_dormant():
    old = datetime.now(timezone.utc) - timedelta(days=2)
    r = detect("在吗", [_u("你好")], last_seen=old)
    assert r.code == "dormant"


# ----------------------------------------------------------------- 优先级
def test_priority_human_beats_price():
    history = [_u("多少钱"), _u("还是多少钱")]
    r = detect("多少钱？我要人工", history)
    assert r.code == "wants_human"


def test_priority_repeat_beats_short():
    history = [_u("发货到巴西吗")]
    r = detect("发货到巴西吗", history)
    assert r.code == "not_understood"


# ----------------------------------------------------------------- 提示词
def test_guidance_note_empty_for_neutral():
    assert guidance_note(detect("这个型号支持双卡吗")) == ""


def test_guidance_note_for_terse():
    note = guidance_note(detect("嗯"))
    assert "简短" in note


# ----------------------------------------------------------------- 端到端
async def test_force_handoff_without_model_tool_call(
    client, seeded, transport_factory
):
    """客户说要人工：模型没调工具，后端也必须转人工。"""
    ai_client.set_transport(
        transport_factory([{"role": "assistant", "content": "好的，马上为您转接。"}])
    )
    r = await client.post(
        "/chat/messages",
        json={"message": "我要人工客服"},
        headers={"X-Customer-No": "CUST001"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["session_status"] == "awaiting_human"
    assert body["handoff"] is not None
    assert body["handoff"]["reason_code"] == "customer_request"
    assert body["signal"]["code"] == "wants_human"
    assert any(t["name"] == "transfer_to_human" and t["ok"] for t in body["tool_calls"])


async def test_guidance_injected_into_system_prompt(client, seeded, transport_factory):
    """敷衍信号要把行为约束写进 system prompt。"""
    transport = transport_factory([{"role": "assistant", "content": "好的"}])
    ai_client.set_transport(transport)

    r = await client.post(
        "/chat/messages", json={"message": "ok"}, headers={"X-Customer-No": "CUST001"}
    )
    assert r.json()["signal"]["code"] == "terse"

    system = transport.calls[0]["messages"][0]
    assert system["role"] == "system"
    assert "行为要求" in system["content"]
    assert "简短" in system["content"]


async def test_repeat_question_signal(client, seeded, transport_factory):
    transport = transport_factory([
        {"role": "assistant", "content": "这款有货"},
        {"role": "assistant", "content": "抱歉，我再说一遍：这款有货"},
    ])
    ai_client.set_transport(transport)
    headers = {"X-Customer-No": "CUST001"}

    first = await client.post("/chat/messages", json={"message": "iPhone 15 有货吗"}, headers=headers)
    sid = first.json()["session_id"]
    second = await client.post(
        "/chat/messages",
        json={"message": "iPhone 15 有货吗？", "session_id": sid},
        headers=headers,
    )
    assert second.json()["signal"]["code"] == "not_understood"
