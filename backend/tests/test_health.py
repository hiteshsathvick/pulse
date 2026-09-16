import httpx

from pulse.main import app


async def test_health_reports_all_stores_ok() -> None:
    """Phase 0 acceptance test: /health must return 200 and prove Postgres and
    ClickHouse (and Redis) are actually reachable, not just that the process is up."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["clickhouse"] == "ok"
    assert body["checks"]["redis"] == "ok"
