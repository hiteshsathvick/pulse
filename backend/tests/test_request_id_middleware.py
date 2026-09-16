import json
import logging
import uuid

import httpx

from pulse.core.logging import JsonFormatter, RequestIdFilter
from pulse.main import app


async def test_generates_request_id_when_none_supplied() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    request_id = response.headers["X-Request-ID"]
    assert uuid.UUID(request_id)  # raises if not a valid UUID


async def test_echoes_a_client_supplied_request_id() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health", headers={"X-Request-ID": "given-id-123"})

    assert response.headers["X-Request-ID"] == "given-id-123"


async def test_access_log_line_is_json_and_carries_the_request_id(caplog) -> None:
    # caplog's own handler doesn't have the app's RequestIdFilter attached, so
    # attach one directly to the logger (filters at the logger level run for
    # every handler) to prove the same correlation the real handler gets.
    logger = logging.getLogger("pulse.request")
    request_id_filter = RequestIdFilter()
    logger.addFilter(request_id_filter)
    caplog.set_level(logging.INFO, logger="pulse.request")

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health", headers={"X-Request-ID": "trace-me"})
    finally:
        logger.removeFilter(request_id_filter)

    matching = [r for r in caplog.records if r.name == "pulse.request"]
    assert matching, "expected the request middleware to log a completion line"

    rendered = json.loads(JsonFormatter().format(matching[-1]))
    assert rendered["request_id"] == "trace-me"
    assert rendered["status_code"] == response.status_code
    assert rendered["path"] == "/health"
