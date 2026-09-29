import httpx

from pulse.api import health as health_module
from pulse.main import app


async def test_health_reports_all_stores_ok() -> None:
    """Phase 0 acceptance test: /health must return 200 and prove Postgres and
    ClickHouse (and Redis) are actually reachable, not just that the process is up.
    Phase 29 added object storage to the same check."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["clickhouse"] == "ok"
    assert body["checks"]["redis"] == "ok"
    assert body["checks"]["object_storage"] == "ok"


async def test_health_is_503_when_object_storage_is_unreachable(monkeypatch) -> None:
    """No failure path was tested at all before Phase 29 -- every check has
    always been trusted to report an outage correctly, untested. A bad
    connectivity check (the wrong SDK call, say) can look identical to a
    healthy one until it's actually exercised failing -- confirmed live in
    Phase 28, where `bucket_exists` reported AccessDenied against a fully
    working bucket. Patching one check to fail also confirms the others
    still report correctly alongside it, not just that the endpoint as a
    whole goes red."""

    async def _boom() -> bool:
        raise ConnectionError("object store unreachable")

    # Patching pulse.repositories.object_storage.check_connection itself
    # wouldn't reach this: _CHECKS below already holds a direct reference to
    # the function object, bound at import time, not a lazy per-call lookup
    # into the object_storage module. _CHECKS itself is the thing health()
    # actually reads fresh on every call, so that's what has to move.
    patched_checks = tuple(
        (name, _boom if name == "object_storage" else check)
        for name, check in health_module._CHECKS
    )
    monkeypatch.setattr(health_module, "_CHECKS", patched_checks)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    assert "object store unreachable" in body["checks"]["object_storage"]
    assert body["checks"]["postgres"] == "ok"
    assert body["checks"]["clickhouse"] == "ok"
    assert body["checks"]["redis"] == "ok"
