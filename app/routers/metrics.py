from fastapi import APIRouter, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from ..cache import safe_call

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/")
@limiter.limit("60/minute")
def get_metrics(request: Request):
    """缓存命中率。Redis 不可用时返回 0，而不是 500。"""
    hits = int(safe_call("get", "cache_hits", default=0) or 0)
    misses = int(safe_call("get", "cache_misses", default=0) or 0)
    total = hits + misses
    return {
        "cache_hits": hits,
        "cache_misses": misses,
        "total_requests": total,
        "hit_rate": round(hits / total, 4) if total else 0.0,
        "redis_available": safe_call("ping", default=False) is True,
    }
