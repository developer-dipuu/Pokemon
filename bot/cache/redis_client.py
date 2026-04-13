from __future__ import annotations

from typing import Any
import redis.asyncio as redis

from bot.config import REDIS_URL

try:
    import fakeredis.aioredis as fakeredis
except ImportError:
    fakeredis = None

_redis_client: redis.Redis | Any | None = None


def _create_fallback_client() -> Any:
    if fakeredis is None:
        raise RuntimeError("fakeredis is required for Redis fallback but is not installed.")
    return fakeredis.FakeRedis(decode_responses=True)


def get_redis_client() -> redis.Redis | Any:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis_client


async def ping_redis() -> bool:
    global _redis_client
    try:
        client = get_redis_client()
        await client.ping()
        return True
    except Exception:
        if fakeredis is not None:
            _redis_client = _create_fallback_client()
            return True
        return False


async def incr_with_expire(key: str, expire_seconds: int) -> int:
    client = get_redis_client()
    value = await client.incr(key)
    if value == 1:
        await client.expire(key, expire_seconds)
    return value


async def set_expire(key: str, value: Any, expire_seconds: int) -> bool:
    client = get_redis_client()
    return await client.set(key, value, ex=expire_seconds)
