"""AI 服务层。

保留原有的 `call_ai` / `enrich_product_batch`（批量生成描述与分类），
在其之上新增真正的 Function Calling 闭环：

    用户消息 → 模型决定是否调用工具 → 后端白名单派发 → 结果回填 → 模型生成最终回复

关于兼容性：
当前项目原本指向超算中心 (api.scnet.cn) 的 DeepSeek-R1-Distill-Qwen-7B，
这类蒸馏小模型 / 第三方兼容网关**不保证**支持 tools 参数。因此这里提供三种模式：
    native —— 只走标准 tools/tool_calls
    prompt —— 提示词约定 JSON 的兜底（任何 chat 模型可用）
    auto   —— 先试 native，遇到不支持自动降级并记住
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from ..config import settings
from ..tools import ToolContext, ToolExecution, execute, openai_tool_schemas
from . import ai_client

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """你是一名跨境电商平台的智能客服助理。

可用工具：
- query_inventory：查询商品的真实库存与价格
- query_order：查询当前客户自己的订单（状态、商品、金额、物流）
- transfer_to_human：转接人工客服

必须遵守的规则：
1. 涉及库存、价格、订单状态、物流的问题，必须先调用对应工具，绝对不允许自己编造数字。
2. 工具返回"未找到"时，如实告知客户，不要猜测，也不要提及其他客户的信息。
3. 客户明确要求人工、情绪明显不满、或你连续多轮无法解决时，调用 transfer_to_human。
4. 回答简洁、口语化，像真人客服，不要罗列工具名和内部字段。
5. 如果只需转人工，调用工具后简短告知客户即可。
"""

PROMPT_MODE_INSTRUCTION = """
【工具调用协议】
你无法直接访问数据库。需要查数据时，只输出一行 JSON，不要输出其他内容：
{"tool": "工具名", "args": {"参数": "值"}}
可用工具与参数：
- query_inventory: {"query": "商品名称/ID/SKU", "query_type": "auto"}
- query_order: {"order_no": "订单号"}
- transfer_to_human: {"reason": "原因", "reason_code": "customer_request|ai_limit|emotion|out_of_scope|other"}
如果不需要工具，正常用中文回答用户即可。
"""


@dataclass
class AgentTurnResult:
    reply: str
    tool_executions: list[ToolExecution] = field(default_factory=list)
    mode: str = "native"
    error: str | None = None

    @property
    def handoff(self) -> dict | None:
        for ex in self.tool_executions:
            if ex.name == "transfer_to_human" and ex.ok and ex.result:
                return ex.result
        return None


# 记住网关是否支持 tools，避免每次请求都先撞一次 400
_NATIVE_SUPPORTED: bool | None = None


def reset_native_support_cache() -> None:
    global _NATIVE_SUPPORTED
    _NATIVE_SUPPORTED = None


# ---------------------------------------------------------------------------
# 原有能力（修复配置读取，逻辑不变）
# ---------------------------------------------------------------------------
async def call_ai(prompt: str) -> str:
    """单轮文本补全，保留给原有的商品描述/分类功能使用。"""
    message = await ai_client.chat([{"role": "user", "content": prompt}])
    content = (message.get("content") or "").strip()
    if not content:
        raise ai_client.AICallError("模型返回空内容")
    return content


async def enrich_product_batch(titles: list[str], concurrency: int = 5) -> list[dict]:
    """并发调用 AI 优化多个商品标题（行为与原实现一致）。"""
    semaphore = asyncio.Semaphore(concurrency)

    async def process_one(title: str):
        async with semaphore:
            prompt = f"""请为以下商品标题生成一段吸引人的中文描述（不超过80字）并给出商品类别（从以下选择：电子产品、家居用品、服装配饰、食品饮料、美妆个护、玩具文具、其他）。格式严格为两行：
优化描述：...
类别：...
商品标题：{title}"""
            try:
                result = await call_ai(prompt)
                description, category = "", "其他"
                for line in result.split("\n"):
                    if line.startswith("优化描述："):
                        description = line.replace("优化描述：", "").strip()
                    elif line.startswith("类别："):
                        category = line.replace("类别：", "").strip()
                return {
                    "original_title": title,
                    "optimized_description": description or f"优质{title}，值得购买",
                    "category": category if category in [
                        "电子产品", "家居用品", "服装配饰", "食品饮料", "美妆个护", "玩具文具", "其他"
                    ] else "其他",
                }
            except Exception as exc:
                return {
                    "original_title": title,
                    "optimized_description": f"精选{title}，品质保证",
                    "category": "其他",
                    "error": str(exc),
                }

    return await asyncio.gather(*[process_one(t) for t in titles])


# ---------------------------------------------------------------------------
# Function Calling 主循环
# ---------------------------------------------------------------------------
def _tool_result_payload(ex: ToolExecution) -> dict[str, Any]:
    if ex.ok:
        return ex.result or {}
    return {"error": ex.error or "tool_failed"}


async def _native_loop(ctx: ToolContext, messages: list[dict], executions: list[ToolExecution],
                       guidance: str = "") -> str:
    system = SYSTEM_PROMPT + (f"\n\n【本轮行为要求】{guidance}" if guidance else "")
    msgs = [{"role": "system", "content": system}] + messages
    tools = openai_tool_schemas()

    for _ in range(settings.ai_max_tool_rounds):
        message = await ai_client.chat(msgs, tools=tools, tool_choice="auto")
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            return (message.get("content") or "").strip()

        # 回填 assistant 的 tool_calls，保持协议完整
        msgs.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            }
        )

        for call in tool_calls:
            fn = (call.get("function") or {})
            name = fn.get("name") or ""
            raw_args = fn.get("arguments")
            ex = await execute(name, raw_args, ctx)
            executions.append(ex)
            msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id") or name,
                    "content": json.dumps(_tool_result_payload(ex), ensure_ascii=False, default=str),
                }
            )

    # 工具轮次用尽仍未收敛，强制转人工更安全
    ex = await execute(
        "transfer_to_human",
        {"reason": "多轮工具调用未能解决客户问题", "reason_code": "ai_limit"},
        ctx,
    )
    executions.append(ex)
    return "这个问题我需要请人工同事帮您处理，已经为您转接，请稍等。"


async def _prompt_loop(ctx: ToolContext, messages: list[dict], executions: list[ToolExecution],
                       guidance: str = "") -> str:
    system = SYSTEM_PROMPT + PROMPT_MODE_INSTRUCTION + (f"\n\n【本轮行为要求】{guidance}" if guidance else "")
    msgs = [{"role": "system", "content": system}] + messages

    for _ in range(settings.ai_max_tool_rounds):
        message = await ai_client.chat(msgs)  # 不传 tools
        content = (message.get("content") or "").strip()

        parsed = _try_parse_tool_json(content)
        if parsed is None:
            return content

        name, args = parsed
        ex = await execute(name, args, ctx)
        executions.append(ex)

        msgs.append({"role": "assistant", "content": content})
        msgs.append(
            {
                "role": "user",
                "content": "工具执行结果（这是真实数据，请据此回答客户）："
                + json.dumps(_tool_result_payload(ex), ensure_ascii=False, default=str),
            }
        )

    return "这个问题我需要请人工同事帮您处理，已经为您转接，请稍等。"


def _try_parse_tool_json(content: str) -> tuple[str, dict] | None:
    """从模型输出里提取工具调用 JSON。解析失败一律当作普通回复。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "tool" not in data:
        return None
    name = data.get("tool")
    args = data.get("args") or data.get("arguments") or {}
    if not isinstance(name, str) or not isinstance(args, dict):
        return None
    return name, args


async def agent_turn(ctx: ToolContext, messages: list[dict], guidance: str = "") -> AgentTurnResult:
    """执行一轮 Agent（可能包含多次工具调用）。"""
    global _NATIVE_SUPPORTED
    executions: list[ToolExecution] = []
    mode = settings.ai_tool_mode

    try:
        if mode in ("native", "auto") and _NATIVE_SUPPORTED is not False:
            try:
                reply = await _native_loop(ctx, messages, executions, guidance)
                _NATIVE_SUPPORTED = True
                return AgentTurnResult(reply=reply, tool_executions=executions, mode="native")
            except ai_client.ToolsUnsupported as exc:
                _NATIVE_SUPPORTED = False
                logger.warning("当前模型/网关不支持 tools，降级到 prompt 模式: %s", exc)
                if mode == "native":
                    return AgentTurnResult(
                        reply="抱歉，当前模型服务不支持工具调用，请联系管理员配置支持 Function Calling 的模型。",
                        tool_executions=executions,
                        mode="native",
                        error="tools_unsupported",
                    )

        reply = await _prompt_loop(ctx, messages, executions, guidance)
        return AgentTurnResult(reply=reply, tool_executions=executions, mode="prompt")

    except ai_client.AIConfigError as exc:
        logger.error("AI 配置错误: %s", exc)
        return AgentTurnResult(
            reply="抱歉，客服系统还在配置中，暂时无法回答。请稍后再试或联系人工客服。",
            tool_executions=executions,
            mode=mode,
            error="config_error",
        )
    except ai_client.AICallError as exc:
        logger.error("AI 调用失败: %s", exc)
        return AgentTurnResult(
            reply="抱歉，我这边暂时无法连接服务，请稍后再试，我也可以帮您转人工。",
            tool_executions=executions,
            mode=mode,
            error="ai_call_failed",
        )
