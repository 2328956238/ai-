"""行为信号识别 —— 不猜"情绪"，只认可观察的行为。

为什么要这样设计：

* "客户生气了" 无法验证，面试被追问一句就站不住；
* "客户连续三轮只问价格、一个问题都没回答" 是可数事实，且直接指向动作。

因此本模块输出的是 **可枚举的行为标签 + 对应动作**，而不是 0~1 的情绪分值。
所有判定都能在回放里逐条复现，误判可以定位到具体规则。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

# 价格相关词（多语言常见说法）
PRICE_RE = re.compile(
    r"(多少钱|什么价|价格|报价|便宜的?|优惠|打折|折扣|最低|"
    r"price|cost|how much|discount|quote)",
    re.IGNORECASE,
)

# 明确要人工
HUMAN_RE = re.compile(
    r"(人工|真人|转人工|找客服|客服在吗|投诉|"
    r"human|agent|real person|speak to someone|complain)",
    re.IGNORECASE,
)

# 无问号也能表达提问的中文疑问词
QUESTION_HINT_RE = re.compile(r"(吗|呢|怎么|如何|多少|什么|哪|能不能|有没有|是否)")

PUNCT_RE = re.compile(r"[\s,，.。!！?？;；:：、~～…\-—_()（）\[\]【】\"'“”‘’]+")


@dataclass
class SignalResult:
    """一次判定的结果。action 直接可执行，不需要调用方再解释。"""

    code: str
    reasons: list[str] = field(default_factory=list)
    action: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"code": self.code, "reasons": self.reasons, "action": self.action}


# 单一动作表：信号 -> 行为调整
ACTIONS: dict[str, dict] = {
    "wants_human": {
        "force_handoff": True,     # 不赌模型判断，直接转人工
        "ask_next": False,
        "long_explain": False,
        "give_price_range": False,
    },
    "not_understood": {
        "force_handoff": False,
        "ask_next": False,         # 客户没被理解时，别再追问新字段
        "long_explain": False,
        "rephrase": True,          # 换一种说法重答
    },
    "price_focus": {
        "force_handoff": False,
        "ask_next": False,
        "long_explain": False,
        "give_price_range": True,  # 给价格区间，别绕
    },
    "terse": {
        "force_handoff": False,
        "ask_next": True,
        "long_explain": False,     # 短回复配短回答，别重发长段说明
    },
    "dormant": {
        "force_handoff": False,
        "ask_next": False,
        "long_explain": False,
    },
    "neutral": {
        "force_handoff": False,
        "ask_next": True,
        "long_explain": True,
    },
}

PRIORITY = ["wants_human", "not_understood", "price_focus", "terse", "dormant", "neutral"]


def _norm(text: str) -> str:
    return PUNCT_RE.sub("", (text or "").strip().lower())


def _similar(a: str, b: str) -> float:
    """0~1 相似度。用于识别"重复提问"，不追求语义级准确。"""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _is_question(text: str) -> bool:
    return ("?" in text) or ("？" in text) or bool(QUESTION_HINT_RE.search(text))


def _user_turns(history: Iterable[dict]) -> list[str]:
    return [m.get("content") or "" for m in history if m.get("role") == "user"]


def detect(
    text: str,
    history: list[dict] | None = None,
    last_seen: datetime | None = None,
    now: datetime | None = None,
) -> SignalResult:
    """判定本轮行为信号。

    优先级（从高到低）：wants_human > not_understood > price_focus > terse > dormant > neutral
    高优先级命中后不再看低优先级，避免"既想转人工又被判敷衍"这种自相矛盾。
    """
    text = (text or "").strip()
    history = history or []
    hits: dict[str, list[str]] = {}

    # ① 明确要人工 —— 客户已经用自然语言说清楚了，不该再让模型犹豫
    if HUMAN_RE.search(text):
        hits["wants_human"] = ["命中人工/投诉关键词"]

    # ② 重复提问 —— 同一个意思问第二遍，说明上一轮没被理解
    prev = _user_turns(history)
    if text and prev:
        dup = [m for m in prev[-5:] if _similar(m, text) >= 0.8]
        if dup:
            hits["not_understood"] = [f"与历史消息高度重复（{len(dup)} 条）"]

    # ③ 连续追价 —— 最近几轮都在问价，且没提供新信息
    recent = (_user_turns(history)[-3:] + [text]) if text else _user_turns(history)[-3:]
    price_turns = [m for m in recent if PRICE_RE.search(m)]
    if len(price_turns) >= 2 and PRICE_RE.search(text or ""):
        hits["price_focus"] = [f"最近 {len(recent)} 轮中有 {len(price_turns)} 轮在问价格"]

    # ④ 敷衍 / 极短回复
    # 注意：单个 "?" "。" 这类纯标点本身没有信息量，即便带问号也算敷衍；
    # 但 "有货吗" 这种短句是真提问，不能误判。
    core = _norm(text)
    # 含业务关键词的短句不算敷衍："价格太贵了" 是投诉信号，不是不想聊
    has_info = bool(PRICE_RE.search(text)) or bool(HUMAN_RE.search(text))
    if text and not has_info and (
        len(core) <= 1 or (len(text) < 8 and not _is_question(text))
    ):
        hits["terse"] = [f"回复仅 {len(text)} 字且无业务信息"]

    # ⑤ 长时间沉默
    if last_seen is not None:
        base = now or datetime.now(timezone.utc)
        seen = last_seen if last_seen.tzinfo else last_seen.replace(tzinfo=timezone.utc)
        if base - seen > timedelta(hours=24):
            hits["dormant"] = [f"距上次回复超过 24 小时"]

    for code in PRIORITY:
        if code in hits:
            return SignalResult(code=code, reasons=hits[code], action=dict(ACTIONS[code]))

    return SignalResult(code="neutral", reasons=["未命中任何行为信号"], action=dict(ACTIONS["neutral"]))


def guidance_note(signal: SignalResult) -> str:
    """把信号翻译成给模型的一句话约束，注入 system prompt。"""
    if signal.code == "neutral":
        return ""
    hints = {
        "wants_human": "客户已明确要求人工。不要继续追问任何信息，简短告知已转接。",
        "not_understood": "客户重复了之前的问题，说明上一次没被理解。不要再次追问新字段，换一种说法把原问题答清楚。",
        "price_focus": "客户连续询问价格。直接给出价格区间或说明报价方式，暂缓追问目的地、货代等信息。",
        "terse": "客户回复很短，说明不想被打扰。回答要简短，不要重复长篇说明，不要追加多个问题。",
        "dormant": "客户长时间未回复。不要主动追问，只做简短回应。",
    }
    return hints.get(signal.code, "")
