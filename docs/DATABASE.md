# 数据库表结构 & 迁移方案

## 1. 新增表（5 张）

> 原则：**原有 `products` 与 `batch_tasks` 一个字段都没动**，库存/订单/客户全部用新表，
> 通过外键关联，避免破坏现有 CRUD 和批量导入。

### customers 客户

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INT PK | |
| customer_no | VARCHAR(50) UNIQUE | 客户编号，请求头 `X-Customer-No` 用它换身份 |
| name | VARCHAR(100) | |
| phone | VARCHAR(30) | |
| email | VARCHAR(120) | |
| created_at | DATETIME | |

### inventory 库存

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INT PK | |
| product_ref_id | INT FK → products.id | 关联原商品表，**不给 products 加字段** |
| sku | VARCHAR(64) | |
| warehouse | VARCHAR(64) | 仓库 |
| quantity | INT | 总库存 |
| reserved | INT | 已锁定（下单未发） |
| safety_stock | INT | 安全库存，不计可售 |
| updated_at | DATETIME | |

可售数量 = `quantity - reserved - safety_stock`（代码里是 `Inventory.available` 属性）。

唯一约束：`(sku, warehouse)`。

### orders 订单

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INT PK | |
| order_no | VARCHAR(64) UNIQUE | 订单号 |
| customer_id | INT FK → customers.id | **数据隔离的关键字段** |
| status | ENUM | pending/paid/shipped/delivered/cancelled/refunded |
| total_amount | FLOAT | 订单金额 |
| currency | VARCHAR(8) | 默认 CNY |
| receiver_name / receiver_phone / address | | 收货信息 |
| logistics_company | VARCHAR(80) | 物流公司 |
| tracking_no | VARCHAR(80) | 运单号 |
| logistics_status | VARCHAR(40) | 运输状态 |
| shipped_at / estimated_delivery | DATETIME | 发货时间 / 预计送达 |
| created_at / updated_at | DATETIME | |

> 物流字段直接放订单主表：一是需求要求"返回物流信息"，二是目前一单一件，
> 避免为 4 个字段多建一张表。将来拆包发货再独立成 `shipments` 表。

### order_items 订单明细

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INT PK | |
| order_id | INT FK → orders.id (CASCADE) | |
| product_ref_id | INT FK → products.id (SET NULL) | 商品删除后保留快照 |
| sku | VARCHAR(64) | |
| title | VARCHAR(200) | 下单时标题快照 |
| quantity | INT | |
| unit_price | FLOAT | |

### chat_sessions 会话 / chat_messages 消息 / human_handoffs 转人工

| 表 | 关键字段 | 说明 |
|---|---|---|
| chat_sessions | session_id UNIQUE, customer_id, status, handoff_at | status: active / awaiting_human / human / closed |
| chat_messages | session_id, role(user/assistant/tool/system), content, tool_name, tool_args(JSON), tool_result(JSON) | 工具调用可审计 |
| human_handoffs | session_id **UNIQUE**, customer_id, reason, reason_code, status | 唯一约束 = 防重复转人工 |

---

## 2. 迁移方案

### 方案 A：开发 / 演示（默认，零配置）

启动时 `lifespan` 里执行：

```python
Base.metadata.create_all(bind=engine)   # 同步引擎
await init_models()                     # 异步引擎
```

`create_all` 只建**缺失的表**，不会改已有表，所以对现有 `products` 完全无影响。

### 方案 B：生产（推荐，Alembic）

```bash
pip install alembic
alembic init migrations
```

`migrations/env.py` 关键改动：

```python
from app.database import Base
from app import models  # noqa: F401  确保模型被导入

target_metadata = Base.metadata
```

生成并应用：

```bash
alembic revision --autogenerate -m "add inventory/order/customer/chat tables"
alembic upgrade head
```

### 方案 C：纯 SQL（不方便跑 ORM 时）

```sql
CREATE TABLE customers (
  id INT AUTO_INCREMENT PRIMARY KEY,
  customer_no VARCHAR(50) NOT NULL UNIQUE,
  name VARCHAR(100), phone VARCHAR(30), email VARCHAR(120),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE inventory (
  id INT AUTO_INCREMENT PRIMARY KEY,
  product_ref_id INT NOT NULL,
  sku VARCHAR(64) NOT NULL,
  warehouse VARCHAR(64) NOT NULL DEFAULT 'default',
  quantity INT NOT NULL DEFAULT 0,
  reserved INT NOT NULL DEFAULT 0,
  safety_stock INT NOT NULL DEFAULT 0,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_inventory_sku_warehouse (sku, warehouse),
  KEY ix_inventory_product (product_ref_id),
  CONSTRAINT fk_inventory_product FOREIGN KEY (product_ref_id)
    REFERENCES products(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE orders (
  id INT AUTO_INCREMENT PRIMARY KEY,
  order_no VARCHAR(64) NOT NULL UNIQUE,
  customer_id INT NOT NULL,
  status ENUM('pending','paid','shipped','delivered','cancelled','refunded') NOT NULL DEFAULT 'pending',
  total_amount FLOAT NOT NULL DEFAULT 0,
  currency VARCHAR(8) NOT NULL DEFAULT 'CNY',
  receiver_name VARCHAR(100), receiver_phone VARCHAR(30), address VARCHAR(300),
  logistics_company VARCHAR(80), tracking_no VARCHAR(80), logistics_status VARCHAR(40),
  shipped_at DATETIME NULL, estimated_delivery DATETIME NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  KEY ix_orders_customer (customer_id),
  CONSTRAINT fk_orders_customer FOREIGN KEY (customer_id)
    REFERENCES customers(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE order_items (
  id INT AUTO_INCREMENT PRIMARY KEY,
  order_id INT NOT NULL,
  product_ref_id INT NULL,
  sku VARCHAR(64), title VARCHAR(200) NOT NULL,
  quantity INT NOT NULL DEFAULT 1,
  unit_price FLOAT NOT NULL DEFAULT 0,
  KEY ix_items_order (order_id),
  CONSTRAINT fk_items_order FOREIGN KEY (order_id)
    REFERENCES orders(id) ON DELETE CASCADE,
  CONSTRAINT fk_items_product FOREIGN KEY (product_ref_id)
    REFERENCES products(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

`chat_sessions` / `chat_messages` / `human_handoffs` 同理，完整 DDL 见
`scripts/seed_demo.py` 建表后的实际结构，或用 `alembic --autogenerate` 生成。

---

## 3. 演示数据

```bash
python scripts/seed_demo.py --reset
```

| 客户 | 订单 | 用途 |
|---|---|---|
| CUST001（张三） | ORD-2026-0001（已发货，顺丰 SF123）、ORD-2026-0002（已付款） | 正常查询 |
| CUST002（李四） | ORD-2026-0003（待付款） | **越权测试**：CUST001 查不到这笔 |

商品：iPhone 15（库存 37 可售）、iPhone 16（8 台）、AirPods Pro 2（0 台，用于测"缺货"）、手机壳（500）。
