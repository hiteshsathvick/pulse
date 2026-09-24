"""Phase 23 distributed tracing. The point is one trace that follows a single
event across a process boundary that isn't HTTP: SDK -> POST /ingest ->
Redis stream -> ingest worker -> ClickHouse. HTTP hops propagate context on
their own (the FastAPI instrumentation reads `traceparent`); the Redis hop
doesn't, so `/ingest` writes the W3C `traceparent` into each stream entry and
the worker reads it back out as the parent of its own spans.

This module owns its own TracerProvider instead of using OpenTelemetry's
process-global one: the global can only be set once per process, which would
make the trace-continuity tests (which need an in-memory exporter) depend on
import order. Propagation itself is provider-independent -- it only needs a
span in the current context -- so nothing is lost by keeping it local.

Unset OTEL endpoint means spans are still *created* (so propagation through
the stream keeps working and stays testable) but never exported."""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, Tracer

logger = logging.getLogger("pulse.observability.tracing")

TRACEPARENT = "traceparent"

_provider: TracerProvider = TracerProvider(resource=Resource.create({"service.name": "pulse"}))


def get_provider() -> TracerProvider:
    return _provider


def tracer() -> Tracer:
    return _provider.get_tracer("pulse")


def setup_tracing(service_name: str, otlp_endpoint: str | None) -> TracerProvider:
    """Called once at each process's entrypoint (API lifespan, each worker's
    main), never at import time -- importing a library module must not start
    exporting spans."""
    global _provider
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=f"{otlp_endpoint.rstrip('/')}/v1/traces")
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("tracing: exporting %s spans to %s", service_name, otlp_endpoint)
    _provider = provider
    return provider


@contextmanager
def use_provider(provider: TracerProvider) -> Iterator[TracerProvider]:
    """Swaps the active provider for the duration -- what tests use to attach
    an in-memory exporter without touching OpenTelemetry's process global."""
    global _provider
    previous = _provider
    _provider = provider
    try:
        yield provider
    finally:
        _provider = previous


def inject_traceparent(carrier: dict[str, str]) -> None:
    """Writes the current span's context into `carrier`. A no-op when there is
    no valid current span (an ingest with no active trace), which is what
    lets the worker treat a missing `traceparent` as "not traced"."""
    propagate.inject(carrier)


def extract_context(carrier: Mapping[str, str]) -> otel_context.Context | None:
    """The parent context for a span that continues a trace begun elsewhere,
    or None when the carrier has no usable `traceparent`."""
    if TRACEPARENT not in carrier:
        return None
    ctx = propagate.extract(carrier)
    if not trace.get_current_span(ctx).get_span_context().is_valid:
        return None
    return ctx


def start_span_in(
    name: str,
    parent: otel_context.Context,
    *,
    start_time_ns: int | None = None,
    attributes: Mapping[str, str | int | float | bool] | None = None,
    kind: trace.SpanKind = trace.SpanKind.INTERNAL,
) -> Span:
    """A span with an explicit parent and (optionally) an explicit start time.
    The ingest worker uses this after a batch completes: one ClickHouse insert
    served many events from many different traces, so each event's trace gets
    its own span recording that shared insert's real timing, parented into
    that event's own trace."""
    return tracer().start_span(
        name, context=parent, start_time=start_time_ns, attributes=attributes, kind=kind
    )
