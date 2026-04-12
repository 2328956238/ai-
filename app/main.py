from fastapi import FastAPI, Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from .database import engine, Base
from .routers import products, metrics, batch, ai  # 添加 batch,ai
# ...

# 创建限流器
limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="跨境商品管理API", version="1.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.include_router(products.router)
app.include_router(metrics.router)
app.include_router(batch.router)
app.include_router(ai.router)
Base.metadata.create_all(bind=engine)

app.include_router(products.router)

@app.get("/")
@limiter.limit("30/minute")  # 根路径也限流
def root(request: Request):
    return {"message": "跨境商品API服务运行中"}