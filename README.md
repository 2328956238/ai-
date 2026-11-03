# 跨境商品智能处理平台（后端 API）

面向跨境电商场景的后端服务：商品管理、Redis 缓存、批量导入、AI 生成，以及本次新增的
**真正的 Function Calling 客服 Agent**（库存查询 / 订单查询 / 转人工）。

> 本次为**在原项目上增量改造**：原有商品 CRUD、批量导入、AI 生成描述、缓存、限流全部保留，
> 没有重写、没有删功能。改动点集中在修复配置错误 + 新增工具调用闭环。

---

## 一、升级概览

| 项目 | 升级前 | 升级后 |
|---|---|---|
| AI 配置 | `os.getenv("sk-aaaa")` 等 3 处读错，全部为 None | 统一 `pydantic-settings`，`AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` 全部走环境变量 |
| 工具调用 | 无（只有 Prompt 生成文本） | 三工具 Function Calling 闭环，native + prompt 双模式 |
| 数据库 | 同步 SQLAlchemy | 同步保留 + 新增异步（`asyncmy` / `aiosqlite`） |
| 库存 | 无库存字段 | 独立 `inventory` 表，可售量 = 总量 - 锁定 - 安全库存 |
| 订单 | 无 | `orders` + `order_items`，含物流字段，按客户隔离 |
| 会话 | 无 | `chat_sessions` + `chat_messages`，多轮上下文 |
| 转人工 | 无 | `human_handoffs`，唯一约束防重复，含接管接口 |
| 行为信号 | 无 | `signals.py`：敷衍/比价/重复提问/要人工 → 调整追问节奏或强制转人工 |
| 依赖清单 | 缺 6 个必需包，装完起不来 | `requirements.txt` 完整 |
| 测试 | 无 | pytest 31 项全通过 |

---

## 二、快速开始

### 方式一：Docker Compose（推荐）

```bash
cp .env.example .env
# 编辑 .env，至少填入 AI_API_KEY
docker compose up -d --build
```

启动后：

- API 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 方式二：本地运行

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/Mac: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 本地想用 SQLite 玩：把 .env 里改成
#   DATABASE_URL=sqlite:///./product.db
#   ASYNC_DATABASE_URL=sqlite+aiosqlite:///./product.db

python scripts/seed_demo.py --reset
uvicorn app.main:app --reload --port 8000
```

### 试一句

```bash
curl -X POST http://localhost:8000/chat/messages \
  -H "Content-Type: application/json" \
  -H "X-Customer-No: CUST001" \
  -d '{"message":"iPhone 15 还有货吗"}'
```

---

## 三、项目结构

```
ai-/
├── app/
│   ├── config.py                 ★ 新增：集中配置（修环境变量 bug）
│   ├── database.py               ~ 修改：连接串走配置 + SQLite 兜底
│   ├── db_async.py               ★ 新增：异步引擎 / Session / 建表
│   ├── cache.py                  ★ 新增：Redis 惰性初始化 + 降级
│   ├── models.py                 ~ 修改：追加 5 组新表，原表未动
│   ├── schemas.py                ~ 修改：追加 chat / 工具参数模型
│   ├── crud.py                     未改
│   ├── main.py                   ~ 修改：去重复 include，加 /health 与 lifespan
│   ├── routers/
│   │   ├── products.py           ~ 修改：缓存降级，接口不变
│   │   ├── batch.py                未改
│   │   ├── metrics.py            ~ 修改：Redis 不可用返回 0 而非 500
│   │   ├── ai.py                   未改（仍走 /products/ai/enrich）
│   │   └── chat.py               ★ 新增：对话入口 + 人工接管
│   ├── services/
│   │   ├── ai_client.py          ★ 新增：LLM 传输层（重试/超时/可注入）
│   │   ├── ai_service.py         ~ 修改：修配置 + 新增 Agent 主循环
│   │   ├── tool_service.py       ★ 新增：编排（会话/上下文/落库）
│   │   ├── inventory_service.py  ★ 新增
│   │   ├── order_service.py      ★ 新增
│   │   ├── human_service.py      ★ 新增
│   │   ├── signals.py            ★ 新增：行为信号（不猜情绪，只认行为）
│   │   └── session_service.py    ★ 新增
│   ├── tools/
│   │   ├── tool_registry.py      ★ 新增：白名单 + 参数校验 + 执行日志
│   │   ├── inventory_tools.py    ★ 新增：query_inventory
│   │   ├── order_tools.py        ★ 新增：query_order
│   │   └── human_tools.py        ★ 新增：transfer_to_human
│   └── tasks/batch_processor.py    未改
├── docs/
│   ├── FUNCTION_CALLING.md       ★ 调用流程 + 兼容性验证
│   ├── API.md                    ★ 接口说明
│   └── DATABASE.md               ★ 表结构与迁移
├── scripts/seed_demo.py          ★ 建表 + 演示数据
├── tests/                        ★ 31 项测试
├── .env.example                  ★
├── requirements.txt              ★ 完整依赖
├── Dockerfile / docker-compose.yml  ★
└── pytest.ini                    ★
```

图例：`★` 新增，`~` 修改，其余保持原样。

---

## 四、Function Calling 核心流程

```
用户消息 → 模型判断是否需要工具 → 返回 tool_calls
        → 后端白名单派发 → 查真实数据库 → 结果回填
        → 模型基于真实数据生成回复
```

模型**永远不执行代码**，只输出"调哪个工具、什么参数"。详见
[docs/FUNCTION_CALLING.md](docs/FUNCTION_CALLING.md)。

### 关于你当前模型的重要提示

原配置指向超算中心的 `DeepSeek-R1-Distill-Qwen-7B`，**该模型与网关不保证支持 `tools`**。
本项目默认 `AI_TOOL_MODE=auto`：先按标准协议调用，失败自动降级到"提示词 JSON 协议"，
两种模式都实现了完整闭环与测试覆盖。

生产建议换成明确支持 Function Calling 的模型（如 `deepseek-chat`），并设
`AI_TOOL_MODE=native`。

---

## 五、测试

```bash
pytest -v
```

当前结果：**46 passed**，覆盖

| 场景 | 对应用例 |
|---|---|
| 正常查库存 | `test_query_inventory_ok` / `test_query_inventory_by_sku` |
| 商品不存在 | `test_query_inventory_not_found` |
| 正常查订单 | `test_query_order_ok` |
| 越权查订单 | `test_query_order_unauthorized` |
| AI 触发库存工具 | `test_native_tool_call_inventory` |
| AI 触发订单工具 | `test_native_tool_call_order` |
| AI 触发转人工 | `test_native_transfer_to_human` / `test_transfer_to_human_is_idempotent` |
| 工具执行失败 | `test_tool_exception_is_contained` / `test_tool_failure_still_answers` |
| 模型 API 失败 | `test_ai_call_failure_returns_graceful_reply` / `test_missing_api_key_returns_config_error` |
| 多轮 Function Calling | `test_multi_turn_keeps_context` / `test_max_rounds_forces_handoff` |
| 模式降级 | `test_prompt_mode_fallback` / `test_auto_mode_downgrades_when_tools_unsupported` |
| 行为信号 | `test_signals.py` 15 项：敷衍/比价/重复提问/要人工/优先级/端到端强制转人工 |
| 安全 | `test_unknown_tool_rejected` / `test_invalid_arguments_rejected` / 会话越权 |

所有测试**不联网**：LLM 传输层用假 transport 注入。

---

## 六、原项目文档（保留）

### 主要功能

- ✅ 商品 CRUD + Redis 旁路缓存（5 分钟 TTL，写操作失效）
- ✅ Redis 滑动窗口限流，默认 30 次/分钟
- ✅ 批量异步导入，最多 1000 条，失败生成 Excel 报告
- ✅ AI 批量生成商品描述与分类（`asyncio.gather` + 信号量并发）
- ✅ 监控：缓存命中率

### 一键部署

```bash
docker compose up -d
```
