import clickhouse_connect
from clickhouse_connect.driver.asyncclient import AsyncClient

from pulse.core.config import get_settings

_client: AsyncClient | None = None


async def get_client() -> AsyncClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = await clickhouse_connect.get_async_client(
            host=settings.clickhouse_host,
            port=settings.clickhouse_port,
            username=settings.clickhouse_user,
            password=settings.clickhouse_password,
            database=settings.clickhouse_database,
            secure=settings.clickhouse_secure,
        )
    return _client


async def check_connection() -> bool:
    """Pings the event-plane store to prove ClickHouse is reachable."""
    client = await get_client()
    return await client.ping()


async def close() -> None:
    global _client
    if _client is not None:
        await _client.close()
    _client = None
