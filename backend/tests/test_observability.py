"""Phase 23 DoD: "a single trace follows an event from SDK to ClickHouse" and
"the health dashboards are live". Real Redis + ClickHouse throughout; spans
are captured with an in-memory exporter (no collector needed), Sentry with a
capturing transport (no account needed)."""

import asyncio
import json
import socket
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import clickhouse_connect
import httpx
import pytest
import sentry_sdk
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY
from sentry_sdk.transport import Transport

from alembic import command
from alembic.config import Config
from pulse.clickhouse_migrations.runner import migrate
from pulse.core.config import Settings, get_settings
from pulse.ingest.schemas import IngestEvent
from pulse.ingest.service import buffer_batch
from pulse.main import app
from pulse.models import ApiKey
from pulse.observability import metrics, tracing
from pulse.observability.sentry import init_sentry
from pulse.repositories.clickhouse import get_client as get_clickhouse_client
from pulse.repositories.redis import get_client as get_redis_client
from pulse.worker.consumer import ensure_consumer_group, read_batch
from pulse.worker.processing import process_batch
from tests.clickhouse_schema import drop_event_schema

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _BACKEND_ROOT / "pulse" / "clickhouse_migrations" / "migrations"
_PASSWORD = "correct horse battery staple"


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


@pytest.fixture(scope="module", autouse=True)
def _control_plane_schema() -> Any:
    config = _alembic_config()
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


async def _with_fresh_clickhouse_client(body: Any) -> None:
    settings = get_settings()
    client = await clickhouse_connect.get_async_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
        secure=settings.clickhouse_secure,
    )
    try:
        await body(client)
    finally:
        await client.close()


@pytest.fixture(scope="module", autouse=True)
def _events_table() -> Any:
    asyncio.run(_with_fresh_clickhouse_client(lambda client: migrate(client, _MIGRATIONS_DIR)))
    yield
    asyncio.run(_with_fresh_clickhouse_client(drop_event_schema))


@pytest.fixture
def spans() -> Any:
    """An in-memory-exporting provider swapped in for the test's duration."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with tracing.use_provider(provider):
        yield exporter


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


def _stream_key() -> str:
    return f"test:obs:stream:{uuid.uuid4().hex[:8]}"


def _ingest_event(**overrides: Any) -> IngestEvent:
    payload: dict[str, Any] = {
        "event_id": uuid.uuid4(),
        "event": "signed up",
        "user_id": "u1",
        "properties": {"plan": "pro"},
    }
    payload.update(overrides)
    return IngestEvent(**payload)


def _api_key() -> ApiKey:
    return ApiKey(org_id=uuid.uuid4(), project_id=uuid.uuid4())


async def _drain_through_worker(stream_key: str, group: str) -> Any:
    redis_client = get_redis_client()
    settings = get_settings()
    entries = await read_batch(redis_client, stream_key, group, "c1", count=100, block_ms=10)
    return await process_batch(
        redis_client=redis_client,
        clickhouse_client=await get_clickhouse_client(),
        entries=entries,
        stream_key=stream_key,
        group=group,
        dlq_stream_key=f"{stream_key}:dlq",
        dedup_ttl_seconds=settings.worker_dedup_ttl_seconds,
    )


# --- Tracing: the DoD sentence -------------------------------------------------


async def test_one_trace_follows_an_event_from_ingest_through_the_stream_to_clickhouse(
    spans: InMemorySpanExporter,
) -> None:
    stream_key, group = _stream_key(), f"g-{uuid.uuid4().hex[:6]}"
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)

    # Stands in for the SDK/API entry point: everything downstream must hang
    # off this one trace.
    with tracing.tracer().start_as_current_span("test.request") as root:
        await buffer_batch(redis_client, stream_key, _api_key(), [_ingest_event()])
        root_trace_id = root.get_span_context().trace_id

    await _drain_through_worker(stream_key, group)

    finished = {s.name: s for s in spans.get_finished_spans()}
    assert {"ingest.buffer", "worker.process_event", "clickhouse.insert"} <= set(finished)

    # One trace id across the process boundary...
    assert {finished[n].context.trace_id for n in finished} == {root_trace_id}
    # ...with the right parent chain: request -> buffer -> worker -> insert.
    assert finished["ingest.buffer"].parent.span_id == root.get_span_context().span_id
    assert finished["worker.process_event"].parent.span_id == (
        finished["ingest.buffer"].context.span_id
    )
    assert finished["clickhouse.insert"].parent.span_id == (
        finished["worker.process_event"].context.span_id
    )
    insert = finished["clickhouse.insert"]
    assert insert.attributes["db.system"] == "clickhouse"
    assert insert.attributes["db.sql.table"] == "events"
    assert insert.kind == trace.SpanKind.CLIENT


async def test_two_requests_in_one_worker_batch_keep_separate_traces(
    spans: InMemorySpanExporter,
) -> None:
    """A worker batch mixes events from different requests -- each event's
    spans must land in *its own* trace, not one merged trace."""
    stream_key, group = _stream_key(), f"g-{uuid.uuid4().hex[:6]}"
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)

    trace_ids = []
    for _ in range(2):
        with tracing.tracer().start_as_current_span("test.request") as root:
            await buffer_batch(redis_client, stream_key, _api_key(), [_ingest_event()])
            trace_ids.append(root.get_span_context().trace_id)
    await _drain_through_worker(stream_key, group)

    inserts = [s for s in spans.get_finished_spans() if s.name == "clickhouse.insert"]
    assert sorted(s.context.trace_id for s in inserts) == sorted(trace_ids)


async def test_an_untraced_entry_produces_no_worker_spans(spans: InMemorySpanExporter) -> None:
    stream_key, group = _stream_key(), f"g-{uuid.uuid4().hex[:6]}"
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)
    now = datetime.now(UTC).isoformat()
    await redis_client.xadd(
        stream_key,
        {
            "org_id": str(uuid.uuid4()),
            "project_id": str(uuid.uuid4()),
            "event_id": str(uuid.uuid4()),
            "event_name": "x",
            "user_id": "u",
            "anonymous_id": "",
            "timestamp": now,
            "received_at": now,
            "properties": json.dumps({}),
        },  # type: ignore[arg-type]
    )
    result = await _drain_through_worker(stream_key, group)

    assert result.processed == 1
    assert not [s for s in spans.get_finished_spans() if s.name.startswith("worker.")]


def test_extract_context_rejects_a_missing_or_malformed_traceparent() -> None:
    assert tracing.extract_context({}) is None
    assert tracing.extract_context({"traceparent": "not-a-traceparent"}) is None
    assert (
        tracing.extract_context({"traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-01"}) is None
    )


async def test_ingest_forwards_an_incoming_traceparent_into_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API half of the chain: a `traceparent` header an SDK sent must end
    up, same trace id, on the stream entry the worker will read."""
    stream_key = _stream_key()
    monkeypatch.setattr(get_settings(), "ingest_stream_key", stream_key)
    incoming_trace_id = "0af7651916cd43dd8448eb211c80319c"
    header = f"00-{incoming_trace_id}-b7ad6b7169203331-01"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        email = f"obs-{uuid.uuid4().hex[:8]}@example.com"
        await client.post(
            "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": "O"}
        )
        login = await client.post(
            "/api/v1/auth/login", json={"email": email, "password": _PASSWORD}
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        org = await client.post(
            "/api/v1/orgs",
            json={"name": "Obs Org", "slug": f"obs-{uuid.uuid4().hex[:8]}"},
            headers=headers,
        )
        org_id = org.json()["id"]
        project = await client.post(
            f"/api/v1/orgs/{org_id}/projects", json={"name": "Web", "slug": "web"}, headers=headers
        )
        key = await client.post(
            f"/api/v1/orgs/{org_id}/projects/{project.json()['id']}/keys",
            json={"type": "write"},
            headers=headers,
        )

        response = await client.post(
            "/ingest",
            json={"batch": [{"event_id": str(uuid.uuid4()), "event": "e", "user_id": "u"}]},
            headers={"X-API-Key": key.json()["key"], "traceparent": header},
        )
        assert response.status_code == 202

    entries = await get_redis_client().xrange(stream_key)
    assert len(entries) == 1
    forwarded = {k.decode(): v.decode() for k, v in entries[0][1].items()}["traceparent"]
    assert forwarded.split("-")[1] == incoming_trace_id


# --- Metrics -------------------------------------------------------------------


async def test_worker_metrics_count_inserted_duplicate_and_poisoned_entries() -> None:
    stream_key, group = _stream_key(), f"g-{uuid.uuid4().hex[:6]}"
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)
    inserted = ("pulse_worker_events_total", {"outcome": "inserted"})
    duplicate = ("pulse_worker_events_total", {"outcome": "duplicate"})
    poisoned = ("pulse_worker_events_total", {"outcome": "poisoned"})
    before = [_sample(*m) for m in (inserted, duplicate, poisoned)]

    api_key = _api_key()
    event = _ingest_event()
    await buffer_batch(redis_client, stream_key, api_key, [event])
    await redis_client.xadd(stream_key, {"garbage": "1"})  # type: ignore[arg-type]
    await _drain_through_worker(stream_key, group)

    # Duplicates are only detected *across* batches: the dedup mark is written
    # after a batch lands, so the same id twice within one batch would both be
    # inserted (ReplacingMergeTree is the backstop for that case). Hence a
    # second batch for the repeat.
    await buffer_batch(redis_client, stream_key, api_key, [event])
    await _drain_through_worker(stream_key, group)

    after = [_sample(*m) for m in (inserted, duplicate, poisoned)]
    assert [a - b for a, b in zip(after, before, strict=True)] == [1, 1, 1]


async def test_landing_delay_and_batch_size_are_recorded() -> None:
    stream_key, group = _stream_key(), f"g-{uuid.uuid4().hex[:6]}"
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)
    delay_before = _sample("pulse_ingest_landing_delay_seconds_count")
    batches_before = _sample("pulse_worker_batch_size_count")

    await buffer_batch(redis_client, stream_key, _api_key(), [_ingest_event(), _ingest_event()])
    await _drain_through_worker(stream_key, group)

    assert _sample("pulse_ingest_landing_delay_seconds_count") - delay_before == 2
    assert _sample("pulse_worker_batch_size_count") - batches_before == 1


async def test_stream_gauges_reflect_length_pending_lag_and_the_dlq() -> None:
    stream_key, group = _stream_key(), f"g-{uuid.uuid4().hex[:6]}"
    dlq_key = f"{stream_key}:dlq"
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)
    await buffer_batch(redis_client, stream_key, _api_key(), [_ingest_event() for _ in range(3)])
    await redis_client.xadd(dlq_key, {"error": "x"})  # type: ignore[arg-type]

    await metrics.update_stream_gauges(redis_client, stream_key, group, dlq_key)
    assert _sample("pulse_ingest_stream_length") == 3
    assert _sample("pulse_ingest_consumer_lag") == 3
    assert _sample("pulse_dlq_length") == 1

    # Delivered but not acked -> pending, no longer lag.
    await read_batch(redis_client, stream_key, group, "c1", count=10, block_ms=10)
    await metrics.update_stream_gauges(redis_client, stream_key, group, dlq_key)
    assert _sample("pulse_ingest_pending_entries") == 3
    assert _sample("pulse_ingest_consumer_lag") == 0


async def test_http_latency_is_labelled_by_route_template_not_raw_path() -> None:
    """Cardinality guard: an id in the URL must never become its own label."""
    org_id = uuid.uuid4()
    labels = {"method": "GET", "route": "/api/v1/orgs/{org_id}", "status": "401"}
    before = _sample("pulse_http_request_duration_seconds_count", labels)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.get(f"/api/v1/orgs/{org_id}")

    assert _sample("pulse_http_request_duration_seconds_count", labels) - before == 1
    assert not any(
        str(org_id) in sample.labels.get("route", "")
        for family in REGISTRY.collect()
        for sample in family.samples
    )


def test_the_metrics_server_serves_prometheus_text_and_is_idempotent() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    assert metrics.start_metrics_server(None) is False
    assert metrics.start_metrics_server(port) is True
    assert metrics.start_metrics_server(port) is False  # a second bind is refused, not attempted

    body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
    assert "pulse_worker_events_total" in body
    assert "pulse_http_request_duration_seconds" in body


# --- Sentry --------------------------------------------------------------------


class _CapturingTransport(Transport):
    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.envelopes: list[Any] = []

    def capture_envelope(self, envelope: Any) -> None:
        self.envelopes.append(envelope)


def test_sentry_is_a_true_no_op_without_a_dsn() -> None:
    settings = Settings(_env_file=None, sentry_dsn=None)
    assert init_sentry("pulse-api", settings) is False
    assert sentry_sdk.get_client().is_active() is False


def test_sentry_captures_an_exception_and_never_ships_request_bodies_or_pii() -> None:
    settings = Settings(_env_file=None, sentry_dsn="http://public@localhost/1")
    transport = _CapturingTransport()
    try:
        assert init_sentry("pulse-test", settings, transport=transport) is True
        options = sentry_sdk.get_client().options
        # The Phase 22 privacy posture, asserted: customer event payloads and
        # passwords must not be able to leave for a third party in an error.
        assert options["send_default_pii"] is False
        assert options["max_request_body_size"] == "never"
        # The SDK's default is ON; frame locals would carry event payloads.
        assert options["include_local_variables"] is False

        sentry_sdk.capture_exception(ValueError("boom"))
        sentry_sdk.flush()
        payloads = [
            item.payload.json
            for envelope in transport.envelopes
            for item in envelope.items
            if item.payload.json
        ]
        events = [p for p in payloads if p.get("exception")]
        assert events
        assert events[0]["server_name"] == "pulse-test"
        assert events[0]["exception"]["values"][0]["type"] == "ValueError"
    finally:
        sentry_sdk.init()  # disable again so no other test inherits an active client


# --- The API's token-protected /metrics (Phase 24) ---


async def _get_metrics(authorization: str | None) -> httpx.Response:
    headers = {"Authorization": authorization} if authorization is not None else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get("/metrics", headers=headers)


async def test_the_api_metrics_route_does_not_exist_unless_a_token_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "metrics_token", None)
    # Even a request carrying some bearer token gets the same plain 404 as any
    # unknown path -- the route's existence isn't revealed.
    assert (await _get_metrics("Bearer anything")).status_code == 404
    assert (await _get_metrics(None)).status_code == 404


async def test_the_api_metrics_route_rejects_a_missing_or_wrong_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "metrics_token", "correcttoken123")
    for header in (None, "", "Bearer", "Bearer wrong", "Basic correcttoken123", "correcttoken123"):
        response = await _get_metrics(header)
        assert response.status_code == 401, header
        assert response.headers["www-authenticate"] == "Bearer"
        assert "pulse_" not in response.text  # nothing leaks on the failure path


async def test_the_api_metrics_route_serves_prometheus_text_with_the_right_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "metrics_token", "correcttoken123")
    response = await _get_metrics("Bearer correcttoken123")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "pulse_http_request_duration_seconds" in response.text
