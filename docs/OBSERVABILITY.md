# Observability

Phase 23. Three things, all opt-in and all a no-op when unconfigured: **traces** (one event, one
trace, across the Redis hop), **metrics + dashboards** (Prometheus and Grafana), and **error
tracking** (Sentry). With none of the settings below set, the app behaves exactly as it did before
this phase.

## Run it locally

```bash
cd infra/docker
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d --build
```

| What | Where |
|---|---|
| Grafana (dashboard **Pulse – ingestion & query health**, anonymous admin, localhost only) | http://localhost:3001 |
| Prometheus | http://localhost:9090 |
| Tempo (trace API) | http://localhost:3200 |
| OTLP/HTTP collector | http://localhost:4318 |

The override file sets `OTEL_EXPORTER_OTLP_ENDPOINT` and `METRICS_PORT` on the API and every
worker. Without it those variables are simply unset, so nothing exports and nothing listens.

## Traces: one event, one trace

```
SDK ──traceparent──▶ POST /ingest ──▶ ingest.buffer ═(Redis stream)═▶ worker.process_event ──▶ clickhouse.insert
   (mints trace id)    (server span)    (injects traceparent            (parent = ingest.buffer)  (parent = process_event)
                                         into each entry)
```

HTTP hops propagate context on their own. The Redis stream does not, so `/ingest` writes the W3C
`traceparent` into every stream entry and the worker reads it back as the parent of its spans.
Because a worker batch mixes events from many requests, each event gets **its own** pair of spans in
**its own** trace, recording the shared insert's real timing — traces are never merged.

- **The SDKs originate the trace id** (both send a fresh `traceparent` per flush). They have no
  OpenTelemetry dependency on purpose, so the SDK's own span is never exported: in Tempo the API's
  server span is the first hop, and its parent shows as missing.
- An entry with no `traceparent` (an untraced producer) simply produces no worker spans.
- Tracing can never break ingestion: span emission is wrapped so a failure is logged and ignored.

Find a trace by id: `GET http://localhost:3200/api/traces/<32-hex-trace-id>`, or use the dashboard's
"Recent event traces" table.

## Metrics

Each process serves Prometheus metrics on its own port (`METRICS_PORT`), **never** on the public API
port — a metrics endpoint leaks route names and traffic shape.

| Metric | Meaning |
|---|---|
| `pulse_ingest_events_accepted_total` | Events `/ingest` accepted and buffered |
| `pulse_worker_events_total{outcome}` | `inserted` / `duplicate` / `poisoned` — `poisoned` **is** the DLQ rate |
| `pulse_worker_batch_size`, `pulse_worker_batch_duration_seconds` | Batch size and processing time |
| `pulse_ingest_landing_delay_seconds` | Accepted → in ClickHouse: the end-to-end freshness a user sees |
| `pulse_ingest_stream_length` / `_consumer_lag` / `_pending_entries` | Queue depth, undelivered, delivered-unacked |
| `pulse_dlq_length` | Dead-letter stream depth |
| `pulse_http_request_duration_seconds{method,route,status}` | API latency; `route` is the **template**, never the raw path |

p50/p95/p99 are derived by Prometheus (`histogram_quantile`) from the buckets, not computed in the
app. `test_every_metric_the_dashboard_queries_actually_exists` fails if a dashboard query references a
metric that isn't exported.

**Consumer lag is derived** (`stream length − pending`), not read from Redis's `XINFO … lag`, which
turns null once entries have been deleted from a stream — reading that as 0 would report a healthy
queue exactly when it isn't.

**Deployed, the API's metrics are a bearer-token route instead.** Where a second port isn't reachable
(Render exposes only a web service's primary port privately) the API also serves `GET /metrics` on its
primary port -- but only when `METRICS_TOKEN` is set. Unset (the default) the route doesn't exist and
returns the same plain 404 as any unknown path; set, it requires `Authorization: Bearer <token>`,
compared in constant time, and a wrong or missing token gets a 401 with no metrics in the body. It is
not in the OpenAPI schema and is excluded from tracing. See `docs/DEPLOYMENT.md` ("Deployed
observability") for how Prometheus and Grafana run on Render.

The API's metrics are **per process**. Under `uvicorn --workers N` only one process can bind the
port, so run one metrics-serving API process per container (as the compose file does) or add
Prometheus multiprocess mode before scaling workers in one container.

## Sentry

Set `SENTRY_DSN` (and optionally `SENTRY_ENVIRONMENT`, `SENTRY_TRACES_SAMPLE_RATE`). Unset — the
default — Sentry is never initialized: no network call, no capture.

Privacy is configured deliberately: **request bodies are never sent** (`max_request_body_size:
"never"`), **stack-frame local variables are never sent** (`include_local_variables: False` — the SDK's
default is on, and a frame is exactly where an event batch or password sits in a variable), and default
PII collection is off. This app ingests arbitrary customer event payloads and
accepts passwords on `/auth`; Sentry's defaults would ship those to a third party the moment
something errored, undoing Phase 22's PII controls. `test_sentry_captures_an_exception_and_never_
ships_request_bodies_or_pii` pins both settings.

## What this phase's dashboard has already caught

The stream-length panel showed 5,000+ entries and only ever rising, with zero pending and zero lag:
acknowledged entries were never being deleted, so the ingest stream grew without bound. Fixed in
`worker/consumer.py::ack` (ack **and** delete, in one transaction) with a regression test — and it
mattered because the deployed Redis runs `noeviction`, where a full Redis means `/ingest` fails
permanently. See `docs/DEPLOYMENT.md`.
