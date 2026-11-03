"""应用入口。

改动点：
1. 去掉原来重复 include products.router 两次的问题。
2. 建表从"import 时同步执行"改为 lifespan 启动钩子（同步 + 异步两套 metadata 一致）。
3. 挂载新的 /chat 路由（Function Calling），原有路由一个都没动。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .config import settings
from .database import Base, engine
from .db_async import init_models
from .routers import ai, batch, chat, metrics, products

logging.basicConfig(
    level=getattr(logging, str(settings.log_level).upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 同步引擎建表（保证原有单测 / 老代码直接可用）
    Base.metadata.create_all(bind=engine)
    # 异步引擎建表（新表；SQLite/MySQL 均幂等）
    try:
        await init_models()
    except Exception as exc:  # 生产环境通常用 alembic，这里失败不阻塞启动
        logger.warning("异步建表跳过: %s", exc)
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(products.router)
app.include_router(metrics.router)
app.include_router(batch.router)
app.include_router(ai.router)
app.include_router(chat.router)


@app.get("/")
@limiter.limit("30/minute")
def root(request: Request):
    return {"message": "跨境商品API服务运行中", "version": settings.app_version}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": settings.ai_model,
        "tool_mode": settings.ai_tool_mode,
        "async_db": settings.async_url.split("://")[0],
    }
