# Function Calling 调用流程

## 0. 先明确一件事

Function Calling **不是**让模型执行 Python 代码。

模型只做两件事：
1. 判断"这次要不要查数据"；
2. 如果要，给出**工具名 + 参数**（结构化 JSON）。

真正的函数执行、SQL 查询、权限校验，全部由 FastAPI 后端完成。
模型拿不到数据库连接，也拿不到别人的订单。

---

## 1. 完整时序（10 步）

```
① 客户 POST /chat/messages  {"message": "iPhone 15 还有货吗"}
        │  Header: X-Customer-No: CUST001
        ▼
② chat.py → get_current_customer()
        用 customer_no 查库拿到 Customer（身份只来自服务端，模型无法伪造）
        ▼
③ tool_service.run_chat_turn()
        取或建会话 → 读最近 N 轮历史 → 落库用户消息
        ▼
④ ai_service.agent_turn()
        组装 messages = [system] + 历史 + 本轮
        ▼
⑤ ai_client.chat(messages, tools=[3 个工具 schema], tool_choice="auto")
        发往 OpenAI 兼容的 /chat/completions
        ▼
⑥ 模型返回 tool_calls
        {"name": "query_inventory", "arguments": {"query": "iPhone 15"}}
        ▼
⑦ tool_registry.execute()  ← 白名单校验
        · 名字不在白名单 → 直接拒绝，不派发
        · 参数用 pydantic 校验（extra=forbid）
        · 通过 → 调用 inventory_tools._handle
        ▼
⑧ inventory_service.query_inventory()
        · 查 Redis（失败自动降级）
        · 查 MySQL：products + inventory
        · 返回真实库存 {total_available: 37, ...}
        ▼
⑨ 结果回填给模型
        messages += [
          {"role": "assistant", "tool_calls": [...]},
          {"role": "tool", "tool_call_id": "call_1", "content": "{...37...}"}
        ]
        ──→ 回到 ⑤，再次调用模型
        ▼
⑩ 模型给出最终自然语言回复
        "这款还有 37 台现货，单价 4299 元。"
        → 落库 assistant 消息 → 返回给客户
```

对应代码：

| 步骤 | 文件 |
|---|---|
| ② | `app/routers/chat.py` |
| ③⑩ | `app/services/tool_service.py` |
| ④⑤⑨ | `app/services/ai_service.py` |
| ⑤ | `app/services/ai_client.py` |
| ⑦ | `app/tools/tool_registry.py` |
| ⑧ | `app/services/inventory_service.py` / `order_service.py` / `human_service.py` |

---

## 2. 两种模式（重要）

| 模式 | 原理 | 适用 |
|---|---|---|
| `native` | 标准 `tools` / `tool_calls` 字段 | 支持 Function Calling 的模型（DeepSeek-V3、GPT-4o 等） |
| `prompt` | 用系统提示约定"要调工具就只输出一行 JSON"，后端解析 | **任何** chat 模型，包括不支持 tools 的蒸馏模型 |
| `auto`（默认） | 先试 native，网关报"不支持 tools"就自动降级并记住 | 不确定网关能力时用 |

### 为什么必须准备 prompt 兜底

你原项目的配置是：

```
AI_BASE_URL = https://api.scnet.cn/api/llm/v1
AI_MODEL    = DeepSeek-R1-Distill-Qwen-7B
```

- `DeepSeek-R1-Distill-Qwen-7B` 是**蒸馏小模型**，Function Calling 能力弱且不稳定；
- 超算中心（SCNet）的 OpenAI 兼容层**没有官方承诺**透传 `tools` / `tool_choice`。

**不能仅凭"模型名字里有 DeepSeek"就认定它支持 tools。** 所以默认 `auto`：
先按标准协议发一次，若网关返回"未知参数 / 不支持"，捕获后切成 prompt 模式，后续请求不再重复撞墙。

### 怎么快速验证你的网关到底支不支持

```bash
curl -s https://你的地址/v1/chat/completions \
  -H "Authorization: Bearer $AI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "'"$AI_MODEL"'",
    "messages": [{"role":"user","content":"查一下 SKU-IP15-128-BK 的库存"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "query_inventory",
        "description": "查询商品库存",
        "parameters": {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}
      }
    }],
    "tool_choice": "auto"
  }' | jq '.choices[0].message'
```

判读结果：

| 返回 | 结论 |
|---|---|
| `message.tool_calls` 有内容 | 支持 native，设 `AI_TOOL_MODE=native` |
| `message.content` 是普通文本，没有 tool_calls | 不真正支持（模型没被训练用工具），用 `prompt` |
| 400 / 422 报参数不支持 | 完全不支持 tools，用 `prompt` |

---

## 3. 安全设计

### 3.1 工具白名单

`_REGISTRY` 是唯一的派发表。模型给出 `delete_all_products` 这类名字时：

```python
spec = get_spec(name)
if spec is None:
    return ToolExecution(name=name, ok=False, error=f"unknown_tool: {name}")
```

**不尝试解释执行、不 eval、不 exec**。

### 3.2 参数校验

每个工具都有 pydantic 模型，`extra="forbid"`：

```python
execute("query_inventory", {"query": "iPhone", "evil": "1=1"}, ctx)
# → ok=False, error="invalid_arguments: evil: Extra inputs are not permitted"
```

### 3.3 客户数据隔离

`query_order` 的 SQL 永远是：

```sql
SELECT * FROM orders WHERE order_no = :order_no
```

然后在 Python 里比对 `order.customer_id == ctx.customer_id`。
不匹配时返回 `{"found": false, "reason": "not_found"}` ——
**和"订单不存在"返回完全一样**，防止有人用错误信息枚举别人订单。

`customer_id` 来自 `ToolContext`（服务端注入），**不在工具参数里**，
模型无法通过任何 prompt 注入指定"查别人"。

### 3.4 转人工的幂等

`human_handoffs.session_id` 上有唯一约束，且先查后插：

```python
existing = await db.execute(select(HumanHandoff).where(...))
if existing: return {..., "duplicated": True}
```

模型在一轮里调用两次 `transfer_to_human`，数据库里也只有一条，
会话状态只会从 `active → awaiting_human` 走一次。

---

## 4. 异常与降级

| 故障 | 行为 |
|---|---|
| Redis 挂了 | 缓存失效，直接查库；`/metrics` 返回 `redis_available: false` |
| 工具内部抛异常 | 捕获 → `ok=False` → 错误回填模型 → 模型正常回复"稍等" |
| 模型 API 超时 / 5xx | tenacity 重试 3 次（指数退避），仍失败返回兜底话术 |
| 模型一直要工具不停 | 达到 `AI_MAX_TOOL_ROUNDS`（默认 5）后强制转人工，避免死循环 |
| 未配置 API Key | 返回"客服系统还在配置中"，不 500 |
| 会话已转人工 | AI 静默，不再调用模型 |

---

## 5. 多轮对话

`ChatMessage` 表按 `session_id` 存 user / assistant / tool 三类消息。

- 上下文回放只带 `user` 和 `assistant`（工具消息不回放，避免历史 token 膨胀）；
- 工具调用记录单独落库，人工接管时可以看到"AI 刚才查了什么、返回什么"；
- 客户端只需回传 `session_id` 就能延续对话。

---

## 6. 一次真实请求长什么样（native 模式）

第一次请求：

```json
POST /v1/chat/completions
{
  "model": "deepseek-chat",
  "messages": [
    {"role": "system", "content": "你是一名跨境电商平台的智能客服助理..."},
    {"role": "user", "content": "iPhone 15 还有货吗"}
  ],
  "tools": [
    {"type": "function", "function": {"name": "query_inventory", "parameters": {...}}},
    {"type": "function", "function": {"name": "query_order", "parameters": {...}}},
    {"type": "function", "function": {"name": "transfer_to_human", "parameters": {...}}}
  ],
  "tool_choice": "auto"
}
```

模型返回：

```json
{"role": "assistant", "content": null,
 "tool_calls": [{"id": "call_1", "type": "function",
                 "function": {"name": "query_inventory",
                              "arguments": "{\"query\": \"iPhone 15\"}"}}]}
```

后端执行后第二次请求（节选）：

```json
"messages": [
  {"role": "user", "content": "iPhone 15 还有货吗"},
  {"role": "assistant", "content": "", "tool_calls": [...]},
  {"role": "tool", "tool_call_id": "call_1",
   "content": "{\"found\": true, \"total_available\": 37, \"product\": {...}}"}
]
```

模型最终输出：

```json
{"role": "assistant", "content": "这款还有 37 台现货，单价 4299 元。"}
```
