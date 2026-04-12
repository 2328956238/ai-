# 跨境商品智能处理平台（后端 API）

一个面向跨境电商场景的高性能后端服务，提供商品管理、Redis 缓存、异步批量导入、AI 智能优化、监控与部署脚本等完整功能。

## 技术栈

- **Python 3.9+**
- **FastAPI** – 异步 Web 框架
- **MySQL** – 商品数据持久化（SQLAlchemy ORM）
- **Redis** – 缓存 + 限流（滑动窗口）
- **Docker / Docker Compose** – 快速启动 MySQL、Redis
- **Gunicorn + Uvicorn** – 生产级 ASGI 服务器
- **SiliconFlow / OpenAI API** – AI 生成商品描述与分类
- **asyncio + httpx** – 并发调用 AI，提升处理效率

## 主要功能

### ✅ 商品 CRUD + 缓存
- RESTful API 管理商品（增删改查）
- Redis 旁路缓存，5 分钟 TTL，写操作自动失效缓存
- 单商品查询延迟降低约 80%

### ✅ 限流保护
- 基于 Redis 的滑动窗口限流
- 默认每个 IP 每分钟 30 次请求（批量接口更严格）
- 超出返回 `429 Too Many Requests`

### ✅ 批量异步导入
- `POST /batch/products` 接收最多 1000 条商品
- 立即返回 `task_id`，后台异步写入 MySQL
- `GET /batch/{task_id}/status` 查询进度（总数/成功/失败）
- 失败记录自动生成 Excel 错误报告并支持下载

### ✅ AI 智能优化（并发调用）
- `POST /products/ai/enrich` 批量生成优化描述与商品类别
- 使用 `asyncio.gather` + 信号量控制并发（默认 5 个）
- 支持重试（`tenacity`）、超时控制、降级默认值
- 处理 50 个商品从串行 30 秒降至并发 6 秒

### ✅ 监控与可观测性
- `GET /monitor` 返回：
  - 最近 1 小时新增商品数量
  - Redis 内存使用情况
  - 数据库连接池状态

### ✅ Linux 生产部署
- 一键部署脚本 `deploy.sh`
- 自动安装 Docker、Python 依赖、启动服务
- Crontab 保活（每分钟检测进程，异常自动拉起）

## 快速开始

### 前置要求
- Docker & Docker Compose
- Python 3.9+
- AI API Key（免费从 [SiliconFlow](https://siliconflow.cn) 获取）

### 1. 克隆项目
```bash
git clone https://github.com/2328956238/ai-.git
cd ai-
