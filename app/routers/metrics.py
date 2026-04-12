from fastapi import APIRouter, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
import redis
import os
from dotenv import load_dotenv

load_dotenv()
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL"), decode_responses=True)

limiter = Limiter(key_func=get_remote_address)
router = APIRouter(prefix="/metrics", tags=["metrics"])

@router.get("/")
@limiter.limit("60/minute")
def get_metrics(request: Request):
    hits = int(redis_client.get("cache_hits") or 0)
    misses = int(redis_client.get("cache_misses") or 0)
    total = hits + misses
    hit_rate = hits / total if total > 0 else 0.0
    return {
        "cache_hits": hits,
        "cache_misses": misses,
        "total_requests": total,
        "hit_rate": round(hit_rate, 4)
    }