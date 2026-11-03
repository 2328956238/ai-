"""离线评测：场景回放 + 行为信号标注集。

设计原则（和 customer-profile-agent 的 evals 一致）：
- 全程不联网：LLM 用脚本化的假 transport，工具查的是临时 SQLite；
- 每个场景都能复现，失败能定位到"哪个断言、哪条消息"；
- 输出机器可读的 JSON，方便对比不同版本。

用法：
    python scripts/eval_agent.py                 # 跑全部
    python scripts/eval_agent.py --suite replay  # 只跑场景回放
    python scripts/eval_agent.py --suite signals # 只跑信号标注
    python scripts/eval_agent.py --json reports/eval.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DB_PATH = Path(tempfile.gettempdir()) / "eval_agent.db"
os.environ.setdefault("DATABASE_URL", f"sqlite:///{DB_PATH}")
os.environ.setdefault("ASYNC_DATABASE_URL", f"sqlite+aiosqlite:///{DB_PATH}")
os.environ.setdefault("REDIS_ENABLED", "false")
os.environ.setdefault("AI_API_KEY", "eval-key")
os.environ.setdefault("AI_TOOL_MODE", "native")
os.environ.setdefault("AI_MAX_RETRIES", "1")
os.environ.setdefault("AI_MAX_TOOL_ROUNDS", "3")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from app.database import Base  # noqa: E402
from app.models import (  # noqa: E402
    Customer, Inventory, Order, OrderItem, OrderStatus, Product, ProductStatus,
)
from app.services import ai_client, ai_service, tool_service  # noqa: E402
from app.services.signals import detect  # noqa: E402


# ---------------------------------------------------------------------------
# 数据准备
# ---------------------------------------------------------------------------
async def seed(engine):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        c1 = Customer(customer_no="CUST001", name="张三")
        c2 = Customer(customer_no="CUST002", name="李四")
        db.add_all([c1, c2])
        await db.flush()

        p1 = Product(product_id="P-IP15-128", title="iPhone 15 128GB 黑色", price=4299.0,
                     category="电子产品", status=ProductStatus.ACTIVE)
        p2 = Product(product_id="P-AP2", title="AirPods Pro 2", price=1499.0,
                     category="电子产品", status=ProductStatus.ACTIVE)
        db.add_all([p1, p2])
        await db.flush()

        db.add_all([
            Inventory(product_ref_id=p1.id, sku="SKU-IP15-128-BK", warehouse="深圳仓",
                      quantity=40, reserved=3, safety_stock=0),
            Inventory(product_ref_id=p2.id, sku="SKU-AP2-USBC", warehouse="香港仓",
                      quantity=0, reserved=0, safety_stock=0),
        ])

        o1 = Order(order_no="ORD-2026-0001", customer_id=c1.id, status=OrderStatus.SHIPPED,
                   total_amount=8598.0, currency="CNY", logistics_company="顺丰国际",
                   tracking_no="SF123", logistics_status="运输中")
        o2 = Order(order_no="ORD-2026-0002", customer_id=c2.id, status=OrderStatus.PAID,
                   total_amount=5899.0, currency="CNY")
        db.add_all([o1, o2])
        await db.flush()
        db.add_all([
            OrderItem(order_id=o1.id, sku="SKU-IP15-128-BK", title="iPhone 15 128GB 黑色",
                      quantity=2, unit_price=4299.0),
            OrderItem(order_id=o2.id, sku="SKU-IP16-256-WH", title="iPhone 16 256GB 白色",
                      quantity=1, unit_price=5899.0),
        ])
        await db.commit()
    return maker


# ---------------------------------------------------------------------------
# 假模型：按脚本返回 assistant message
# ---------------------------------------------------------------------------
def tool_call(name, args, call_id="c1"):
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}],
    }


def scripted(plan):
    plan = list(plan)

    async def transport(payload):
        return plan.pop(0) if plan else {"role": "assistant", "content": "（脚本耗尽）"}

    transport.calls = []
    return transport


def raising(exc):
    async def transport(payload):
        raise exc
    transport.calls = []
    return transport


# ---------------------------------------------------------------------------
# 场景定义
# ---------------------------------------------------------------------------
SCENARIOS = [
    dict(id="inv_in_stock", message="iPhone 15 还有货吗", customer="CUST001",
         plan=[tool_call("query_inventory", {"query": "iPhone 15"}),
               {"role": "assistant", "content": "这款还有 37 台现货，单价 4299 元。"}],
         expect_tools=["query_inventory"], expect_status="active", reply_has=["37"]),

    dict(id="inv_out_of_stock", message="AirPods Pro 2 有货吗", customer="CUST001",
         plan=[tool_call("query_inventory", {"query": "AirPods Pro 2"}),
               {"role": "assistant", "content": "这款目前缺货，暂时没有现货。"}],
         expect_tools=["query_inventory"], expect_status="active", reply_has=["缺货"]),

    dict(id="inv_not_found", message="有 iPhone 99 吗", customer="CUST001",
         plan=[tool_call("query_inventory", {"query": "iPhone 99"}),
               {"role": "assistant", "content": "抱歉，没有找到这个商品。"}],
         expect_tools=["query_inventory"], expect_status="active", reply_has=["没有找到"]),

    dict(id="order_ok", message="查一下订单 ORD-2026-0001", customer="CUST001",
         plan=[tool_call("query_order", {"order_no": "ORD-2026-0001"}),
               {"role": "assistant", "content": "您的订单已发出，顺丰单号 SF123。"}],
         expect_tools=["query_order"], expect_status="active", reply_has=["SF123"]),

    dict(id="order_cross_customer", message="查订单 ORD-2026-0002", customer="CUST001",
         plan=[tool_call("query_order", {"order_no": "ORD-2026-0002"}),
               {"role": "assistant", "content": "抱歉，没有查到这笔订单。"}],
         expect_tools=["query_order"], expect_status="active", reply_has=["没有查到"],
         expect_tool_result={"found": False}),

    dict(id="handoff_by_model", message="我要投诉", customer="CUST001",
         plan=[tool_call("transfer_to_human",
                         {"reason": "客户投诉", "reason_code": "customer_request"}),
               {"role": "assistant", "content": "已为您转接人工客服。"}],
         expect_tools=["transfer_to_human"], expect_status="awaiting_human",
         expect_handoff=True),

    dict(id="handoff_forced_by_signal", message="转人工", customer="CUST001",
         plan=[{"role": "assistant", "content": "好的，马上为您转接。"}],
         expect_tools=["transfer_to_human"], expect_status="awaiting_human",
         expect_handoff=True, expect_signal="wants_human"),

    dict(id="repeat_question", message="iPhone 15 有没有货", customer="CUST001",
         prior=[{"role": "user", "content": "iPhone 15 有没有货"}],
         plan=[{"role": "assistant", "content": "有的，这款有现货。"}],
         expect_tools=[], expect_status="active", expect_signal="not_understood"),

    dict(id="price_focus", message="到底多少钱", customer="CUST001",
         prior=[{"role": "user", "content": "多少钱"},
                {"role": "user", "content": "16 多少钱"}],
         plan=[{"role": "assistant", "content": "官网价 4299 起，具体看型号。"}],
         expect_tools=[], expect_status="active", expect_signal="price_focus"),

    dict(id="terse_reply", message="嗯", customer="CUST001",
         plan=[{"role": "assistant", "content": "好的"}],
         expect_tools=[], expect_status="active", expect_signal="terse"),

    dict(id="tool_exception", message="查 iPhone 15 库存", customer="CUST001",
         plan=[tool_call("query_inventory", {"query": "iPhone 15"}),
               {"role": "assistant", "content": "系统繁忙，稍后再试。"}],
         expect_tools=["query_inventory"], expect_status="active",
         break_inventory=True),

    dict(id="ai_failure", message="你好", customer="CUST001",
         ai_error=ai_client.AICallError("upstream 502"),
         expect_tools=[], expect_status="active", reply_has=["稍后"]),
]


# ---------------------------------------------------------------------------
# 运行一个场景
# ---------------------------------------------------------------------------
async def run_scenario(maker, sc, monkeypatch_inventory=False):
    ai_service.reset_native_support_cache()

    if sc.get("ai_error"):
        ai_client.set_transport(raising(sc["ai_error"]))
    else:
        ai_client.set_transport(scripted(sc.get("plan", [])))

    async with maker() as db:
        from sqlalchemy import select
        customer = (await db.execute(
            select(Customer).where(Customer.customer_no == sc["customer"])
        )).scalars().first()

        # 需要历史上下文时，先写几条 user 消息
        if sc.get("prior"):
            from app.services import session_service
            sess = await session_service.get_or_create_session(db, customer.id, None)
            for m in sc["prior"]:
                await session_service.append_message(db, sess.session_id, m["role"], m["content"])
            sid = sess.session_id
        else:
            sid = None

        resp = await tool_service.run_chat_turn(
            db=db, customer_id=customer.id, message=sc["message"], session_id=sid
        )
    return resp


def check(sc, resp):
    """返回 (passed, failures[])。"""
    fails = []
    names = [t.name for t in resp.tool_calls]
    if sc["expect_tools"] and names[:len(sc["expect_tools"])] != sc["expect_tools"]:
        fails.append(f"工具链 {names} != {sc['expect_tools']}")
    if not sc["expect_tools"] and names:
        fails.append(f"不该调工具却调了 {names}")

    if resp.session_status != sc["expect_status"]:
        fails.append(f"状态 {resp.session_status} != {sc['expect_status']}")

    if sc.get("reply_has") and not any(k in resp.reply for k in sc["reply_has"]):
        fails.append(f"回复未包含 {sc['reply_has']}：{resp.reply[:40]}")

    if sc.get("expect_signal") and (resp.signal or {}).get("code") != sc["expect_signal"]:
        fails.append(f"信号 {(resp.signal or {}).get('code')} != {sc['expect_signal']}")

    if sc.get("expect_handoff") and not resp.handoff:
        fails.append("缺少 handoff")

    if sc.get("expect_tool_result"):
        for k, v in sc["expect_tool_result"].items():
            got = next((t.result for t in resp.tool_calls), None) or {}
            if got.get(k) != v:
                fails.append(f"工具结果 {k}={got.get(k)} != {v}")
    return (not fails), fails


# ---------------------------------------------------------------------------
# 信号标注集（每条一个期望标签）
# ---------------------------------------------------------------------------
SIGNAL_CASES = [
    # (当前消息, 期望信号, 历史用户消息)
    # —— 中性询价：单次问价属于正常咨询，不是"连续追价"
    ("多少钱", "neutral", []),
    ("iPhone 15 多少钱", "neutral", []),
    ("这款多少钱呢", "neutral", []),
    ("128G 的有吗", "neutral", []),
    ("发货到巴西要多久", "neutral", []),
    ("这个型号支持双卡吗", "neutral", []),
    ("什么时候能发货", "neutral", []),
    ("价格太贵了", "neutral", []),
    # —— 敷衍 / 极短
    ("ok", "terse", []),
    ("好的", "terse", []),
    ("嗯", "terse", []),
    ("?", "terse", []),
    ("...", "terse", []),
    ("emm", "terse", []),
    # —— 明确要人工
    ("我要人工", "wants_human", []),
    ("转人工", "wants_human", []),
    ("找客服", "wants_human", []),
    ("给我真人", "wants_human", []),
    ("我要投诉你们", "wants_human", []),
    ("can I speak to a human", "wants_human", []),
    # —— 连续追价（需要历史）
    ("到底多少钱", "price_focus", ["多少钱", "16 多少钱"]),
    ("能便宜点吗", "price_focus", ["iPhone 15 多少钱", "还是太贵了多少钱"]),
    ("最低多少", "price_focus", ["报价多少", "另一款多少钱"]),
    # —— 重复提问（说明上一轮没被理解）
    ("iPhone 15 有没有货", "not_understood", ["iPhone 15 有没有货"]),
    ("发货到巴西吗", "not_understood", ["发货到巴西吗"]),
    ("支持双卡吗", "not_understood", ["支持双卡吗"]),
]


async def eval_signals():
    total = len(SIGNAL_CASES)
    ok = 0
    detail = []
    for text, expect, history in SIGNAL_CASES:
        hist = [{"role": "user", "content": h} for h in history]
        got = detect(text, hist).code
        passed = got == expect
        ok += passed
        detail.append({"text": text, "expect": expect, "got": got, "pass": passed})
    return {"suite": "signals", "total": total, "passed": ok,
            "accuracy": round(ok / total, 4), "detail": detail}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", choices=["all", "replay", "signals"], default="all")
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    engine = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}")
    maker = await seed(engine)
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "suites": []}

    if args.suite in ("all", "replay"):
        rows = []
        for sc in SCENARIOS:
            resp = await run_scenario(maker, sc)
            passed, fails = check(sc, resp)
            rows.append({"id": sc["id"], "pass": passed, "failures": fails,
                         "tools": [t.name for t in resp.tool_calls],
                         "status": resp.session_status,
                         "signal": (resp.signal or {}).get("code")})
            mark = "PASS" if passed else "FAIL"
            print(f"[{mark}] {sc['id']:<24} tools={[t.name for t in resp.tool_calls]}")
            for f in fails:
                print(f"        - {f}")
        passed = sum(1 for r in rows if r["pass"])
        report["suites"].append({"suite": "replay", "total": len(rows), "passed": passed,
                                 "accuracy": round(passed / len(rows), 4), "detail": rows})

    if args.suite in ("all", "signals"):
        s = await eval_signals()
        print(f"\n[signals] {s['passed']}/{s['total']} = {s['accuracy']*100:.1f}%")
        report["suites"].append(s)

    await engine.dispose()
    if DB_PATH.exists():
        DB_PATH.unlink(missing_ok=True)

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {out}")

    print("\n" + "=" * 50)
    for s in report["suites"]:
        print(f"{s['suite']:<8} {s['passed']}/{s['total']}  ({s['accuracy']*100:.1f}%)")
    return report


if __name__ == "__main__":
    asyncio.run(main())
