"""Redis 访问封装。

原代码在模块导入时直接 `redis.Redis.from_url(os.getenv("REDIS_URL"))`，
一旦没配环境变量就是 None，import 阶段直接崩。这里改成惰性初始化 + 全局降级：
Redis 不可用时返回 None，业务继续走数据库，只丢缓存，不丢功能。
"""

from __future__ import annotations

import logging

from .config import settings

logger = logging.getLogger(__name__)

_client = None
_checked = False


def get_redis():
    global _client, _checked
    if _checked:
        return _client
    _checked = True
    if not settings.redis_enabled:
        return None
    try:
        import redis  # 同步客户端，供原有路由使用

        _client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=settings.redis_timeout,
            socket_connect_timeout=settings.redis_timeout,
        )
        _client.ping()
    except Exception as exc:
        logger.warning("Redis 不可用，缓存功能已降级: %s", exc)
        _client = None
    return _client


def safe_call(fn, *args, default=None, **kwargs):
    """执行一个 Redis 操作，任何异常都吞掉并返回 default。"""
    client = get_redis()
    if client is None:
        return default
    try:
        return getattr(client, fn)(*args, **kwargs)
    except Exception as exc:
        logger.warning("Redis 操作 %s 失败（已忽略）: %s", fn, exc)
        return default
