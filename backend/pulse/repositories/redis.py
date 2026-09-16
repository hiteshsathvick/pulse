from redis.asyncio import Redis

from pulse.core.config import get_settings

_client: Redis | None = None


def get_client() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(get_settings().redis_url)
    return _client


async def check_connection() -> bool:
    """Pings Redis to prove the buffer/cache store is reachable."""
    return bool(await get_client().ping())


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
