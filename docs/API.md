# API 接口说明

Base URL：`http://localhost:8000`

## 原有接口（未改动）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | 服务探活 |
| POST | `/products/` | 创建商品 |
| GET | `/products/{id}` | 查商品（带 Redis 缓存） |
| PUT | `/products/{id}` | 更新商品（失效缓存） |
| DELETE | `/products/{id}` | 删除商品 |
| GET | `/products/` | 商品列表 |
| POST | `/products/ai/enrich` | AI 批量生成描述与分类 |
| POST | `/batch/products` | 批量导入，返回 task_id |
| GET | `/batch/{task_id}/status` | 批量任务进度 |
| GET | `/batch/{task_id}/errors` | 下载错误 Excel |
| GET | `/metrics/` | 缓存命中率（新增 `redis_available` 字段） |

## 新增接口

### POST /chat/messages

发送消息，触发 Agent（可能多次调用工具）。

**请求头**

| 头 | 必填 | 说明 |
|---|---|---|
| `X-Customer-No` | 是 | 客户编号，如 `CUST001`（真实项目请换 JWT） |

**请求体**

```json
{
  "message": "iPhone 15 还有货吗",
  "session_id": "可选，不传则新建会话"
}
```

**响应 200**

```json
{
  "session_id": "3f2b1c9e-...",
  "reply": "这款还有 37 台现货，单价 4299 元。",
  "session_status": "active",
  "tool_calls": [
    {
      "name": "query_inventory",
      "arguments": {"query": "iPhone 15"},
      "ok": true,
      "result": {
        "found": true,
        "product": {"product_id": "P-IP15-128", "title": "iPhone 15 128GB 黑色", "price": 4299.0},
        "total_available": 37,
        "in_stock": true,
        "warehouses": [{"sku": "SKU-IP15-128-BK", "warehouse": "深圳仓", "available": 37}]
      },
      "error": null,
      "latency_ms": 12
    }
  ],
  "handoff": null,
  "signal": {
    "code": "neutral",
    "reasons": ["未命中任何行为信号"],
    "action": {"force_handoff": false, "ask_next": true, "long_explain": true}
  }
}
```

**转人工时的 handoff 字段**

```json
{
  "reply": "已经帮您转接人工客服，请稍等。",
  "session_status": "awaiting_human",
  "handoff": {
    "reason": "客户明确要求人工",
    "reason_code": "customer_request",
    "status": "pending",
    "created_at": "2026-11-02T13:20:31",
    "duplicated": false
  }
}
```

**错误码**

| 状态码 | 场景 |
|---|---|
| 401 | 缺少 / 无效 `X-Customer-No` |
| 403 | 用别人的 `session_id` |
| 422 | 请求体校验失败（message 为空 / 超长） |

---

### GET /chat/tools

查看当前白名单工具，用于验收与排查。

```json
{"tools": ["query_inventory", "query_order", "transfer_to_human"]}
```

---

### POST /chat/handoffs/{session_id}/accept

人工客服接管（为后续工作台预留）。

| 参数 | 位置 | 说明 |
|---|---|---|
| `agent` | query | 客服工号 |
| `note` | query | 备注，可选 |

响应：

```json
{"ok": true, "session_status": "human", "agent": "agent-007"}
```

接管后 AI 对该会话永久静默。

---

### POST /chat/handoffs/{session_id}/close

关闭会话，状态置为 `closed`。

---

## 三个工具的调用契约

### query_inventory

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| query | string | 是 | 商品名称 / 商品ID / SKU |
| query_type | enum | 否 | `auto`(默认) / `name` / `product_id` / `sku` |

返回 `found=false` 时 `reason` 可能是：`product_not_found` / `empty_query` / `database_error`。

### query_order

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| order_no | string | 是 | 订单号 |

**没有 customer_id 参数** —— 身份由后端注入。
查询别人订单返回 `{"found": false, "reason": "not_found"}`，与"不存在"完全一致。

### transfer_to_human

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| reason | string | 是 | 具体原因，给人工客服看 |
| reason_code | enum | 否 | `customer_request` / `ai_limit`(默认) / `emotion` / `out_of_scope` / `other` |

同一会话重复调用返回 `duplicated: true` 和同一条 `handoff_id`，不会重复建单。

---

## curl 速查

```bash
# 查库存
curl -X POST http://localhost:8000/chat/messages \
  -H "Content-Type: application/json" \
  -H "X-Customer-No: CUST001" \
  -d '{"message":"iPhone 15 还有货吗"}'

# 查订单
curl -X POST http://localhost:8000/chat/messages \
  -H "Content-Type: application/json" \
  -H "X-Customer-No: CUST001" \
  -d '{"message":"帮我查一下订单 ORD-2026-0001"}'

# 越权测试（应查不到）
curl -X POST http://localhost:8000/chat/messages \
  -H "Content-Type: application/json" \
  -H "X-Customer-No: CUST001" \
  -d '{"message":"查订单 ORD-2026-0003"}'

# 转人工
curl -X POST http://localhost:8000/chat/messages \
  -H "Content-Type: application/json" \
  -H "X-Customer-No: CUST001" \
  -d '{"message":"我要人工客服"}'
```


---

## 行为信号（signal）

每轮回复都会带回本轮命中的行为信号。它**不预测情绪**，只判定可观察的行为，
并且信号直接绑定动作。

| code | 触发条件 | 动作 |
|---|---|---|
| `wants_human` | 命中"人工/真人/转人工/投诉"等 | **后端直接转人工**，不等模型决定 |
| `not_understood` | 与最近 5 轮内某条消息相似度 ≥ 0.8 | 不追问新字段，换说法重答 |
| `price_focus` | 最近 4 轮中 ≥2 轮问价，且本轮仍问价 | 给价格区间，暂缓追问目的地/货代 |
| `terse` | 少于 8 字且不含提问 | 不重复长解释，不追加多个问题 |
| `dormant` | 距上次回复超过 24 小时 | 不主动追问 |
| `neutral` | 未命中 | 正常流程 |

优先级从高到低：`wants_human > not_understood > price_focus > terse > dormant > neutral`，
高优先级命中后不再看低优先级——避免"既要转人工又被判敷衍"这种自相矛盾。

**为什么 `wants_human` 由后端强制触发**：客户已经用自然语言说清楚了，
再交给模型判断一次，就是拿明确的意图去赌模型的准确率。这条规则在
`app/services/tool_service.py` 里，不依赖模型是否调用工具。
