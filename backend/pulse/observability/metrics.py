"""Phase 23 Prometheus metrics -- exactly the signals SPEC.md's Grafana
dashboards name: ingestion lag, batch size, events/sec, DLQ rate, and query
p50/p95/p99. Percentiles are not computed here: a Histogram exposes buckets
and Prometheus's histogram_quantile() derives p50/p95/p99 from them, which
is also why buckets are chosen around the latency targets in SPEC.md #1.4.

Served on a dedicated port per process (Settings.metrics_port), never on the
public API's own port -- a metrics endpoint leaks route names and traffic
shape, so it stays off the surface customers and browsers can reach."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("pulse.observability.metrics")

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_BATCH_SIZE_BUCKETS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 5000)

HTTP_DURATION = Histogram(
    "pulse_http_request_duration_seconds",
    "API request latency. `route` is the path template, never the raw path, so "
    "cardinality stays bounded no matter how many ids appear in URLs.",
    ["method", "route", "status"],
    buckets=_LATENCY_BUCKETS,
)

INGEST_ACCEPTED = Counter(
    "pulse_ingest_events_accepted_total", "Events accepted by POST /ingest and buffered."
)

WORKER_EVENTS = Counter(
    "pulse_worker_events_total",
    "Stream entries the ingest worker handled, by outcome. `poisoned` is the DLQ rate.",
    ["outcome"],  # inserted | duplicate | poisoned
)
WORKER_BATCH_SIZE = Histogram(
    "pulse_worker_batch_size",
    "Stream entries per worker batch.",
    buckets=_BATCH_SIZE_BUCKETS,
)
WORKER_BATCH_SECONDS = Histogram(
    "pulse_worker_batch_duration_seconds",
    "Wall time to process one worker batch (parse, dedup, insert, archive, ack).",
    buckets=_LATENCY_BUCKETS,
)
LANDING_DELAY = Histogram(
    "pulse_ingest_landing_delay_seconds",
    "Time from an event being accepted by /ingest (received_at) to being written "
    "to ClickHouse -- the end-to-end ingestion lag a user actually experiences.",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 300, 900),
)

STREAM_LENGTH = Gauge("pulse_ingest_stream_length", "Entries currently in the ingest stream.")
CONSUMER_LAG = Gauge(
    "pulse_ingest_consumer_lag",
    "Entries in the stream the worker consumer group has not yet been delivered.",
)
PENDING_ENTRIES = Gauge(
    "pulse_ingest_pending_entries",
    "Entries delivered to a worker but not yet acknowledged.",
)
DLQ_LENGTH = Gauge("pulse_dlq_length", "Entries sitting in the dead-letter stream.")

_server_started = False


def start_metrics_server(port: int | None) -> bool:
    """Idempotent, and deliberately tolerant. Idempotent because two
    components in one process must not both try to bind the same port (a
    second bind silently killing a worker's startup is a real failure mode);
    tolerant because a metrics port already being taken (e.g. an API run with
    several uvicorn workers) must never stop the service from serving."""
    global _server_started
    if port is None or _server_started:
        return False
    try:
        start_http_server(port)
    except OSError:
        logger.warning("metrics: port %s unavailable, continuing without /metrics", port)
        return False
    _server_started = True
    logger.info("metrics: serving Prometheus metrics on :%s", port)
    return True


async def update_stream_gauges(
    client: Redis, stream_key: str, group: str, dlq_stream_key: str
) -> None:
    """Sampled once per worker cycle. Best-effort: a Redis hiccup while
    *measuring* must never take down the thing being measured."""
    try:
        length = await client.xlen(stream_key)
        STREAM_LENGTH.set(length)
        DLQ_LENGTH.set(await client.xlen(dlq_stream_key))
        for info in await client.xinfo_groups(stream_key):
            if _text(info.get("name")) == group:
                pending = info.get("pending") or 0
                PENDING_ENTRIES.set(pending)
                # Derived, not read from XINFO's own `lag`: that field goes
                # null once entries have been deleted from the stream (which
                # worker.consumer.ack now does), and reading null as 0 would
                # silently report a healthy queue. Acked entries are deleted,
                # so what remains is exactly undelivered + delivered-unacked.
                CONSUMER_LAG.set(max(length - pending, 0))
                break
    except Exception:
        logger.debug("metrics: stream gauge update failed", exc_info=True)


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Records latency by method, route template and status. The route is read
    from the matched route after routing has run -- the raw path would put
    every org/project/insight id into a label and blow up cardinality."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            HTTP_DURATION.labels(request.method, template, str(status)).observe(
                time.perf_counter() - start
            )
