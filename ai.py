from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import List
from slowapi import Limiter
from slowapi.util import get_remote_address
from ..services.ai_service import enrich_product_batch

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/products/ai", tags=["AI"])

class AIEnrichRequest(BaseModel):
    titles: List[str] = Field(..., max_items=50, description="商品标题列表，最多50个")

class AIEnrichResponse(BaseModel):
    results: List[dict]

@router.post("/enrich", response_model=AIEnrichResponse)
@limiter.limit("20/minute")
async def ai_enrich(request: Request, req: AIEnrichRequest):
    """
    批量调用 AI 生成优化描述和分类
    """
    if len(req.titles) == 0:
        raise HTTPException(status_code=400, detail="标题列表不能为空")
    if len(req.titles) > 50:
        raise HTTPException(status_code=400, detail="单次最多处理50个标题")
    
    # 并发调用 AI 服务
    results = await enrich_product_batch(req.titles, concurrency=5)
    return {"results": results}