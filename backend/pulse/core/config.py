from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # The app/migrations connect as a dedicated non-superuser role, since RLS
    # (even with FORCE) is unconditionally bypassed for superusers.
    database_url: str = "postgresql+asyncpg://pulse_app:pulse_app@localhost:5432/pulse"
    # Used only by alembic/env.py to idempotently ensure that role exists --
    # the postgres image's bootstrap user (which this points at) can never be
    # demoted itself (Postgres refuses to strip SUPERUSER from it), so a
    # second role has to be created instead, and something has to have
    # privileges to create it.
    database_bootstrap_url: str = "postgresql+asyncpg://pulse:pulse@localhost:5432/pulse"

    clickhouse_host: str = "localhost"
    clickhouse_port: int = 8123
    clickhouse_user: str = "pulse"
    clickhouse_password: str = "pulse"
    clickhouse_database: str = "pulse"
    clickhouse_secure: bool = False

    redis_url: str = "redis://localhost:6379/0"

    # Dev-only default, like every other credential in this file -- never used
    # as-is in a real deployment. Signs/verifies access tokens (HS256).
    jwt_secret: str = "dev-insecure-jwt-secret-change-me"
    jwt_access_token_ttl_minutes: int = 15
    jwt_refresh_token_ttl_days: int = 30

    auth_rate_limit_max_attempts: int = 5
    auth_rate_limit_window_seconds: int = 300

    # Phase 7: POST /ingest. Batch-count and body-size caps are independent
    # guards -- either alone would miss a batch that's small in count but
    # huge in property payloads, or vice versa.
    ingest_max_batch_size: int = 500
    ingest_max_body_bytes: int = 512_000
    ingest_rate_limit_max_requests: int = 600
    ingest_rate_limit_window_seconds: int = 60
    # One global stream, not one per project -- keeps Phase 8's consumer-group
    # workers simple as the number of projects grows; tenant scope still lives
    # in each entry's own org_id/project_id fields.
    ingest_stream_key: str = "pulse:ingest:events"
    # Wide open: /ingest auth is a bearer-style write key, not a cookie, so
    # there's no CSRF surface for an open CORS policy to create.
    ingest_cors_allow_origins: list[str] = ["*"]

    # Phase 8: the ingest-worker. A single, fixed consumer name (not one
    # generated per process) so a crashed-and-restarted worker reclaims its
    # own still-pending entries via XREADGROUP ... 0 -- true multi-replica
    # consumer takeover (XCLAIM) is deferred until something actually runs
    # more than one worker.
    worker_consumer_group: str = "ingest-workers"
    worker_consumer_name: str = "worker-1"
    # BLOCK+COUNT on one XREADGROUP call gives both the size and time
    # trigger the DoD asks for -- no separate manual timer/accumulator needed.
    worker_batch_size: int = 500
    worker_block_ms: int = 5000
    # The synchronous idempotency guarantee (SPEC.md #5.1) -- generous
    # relative to any realistic crash-recovery window; ClickHouse's
    # ReplacingMergeTree is the backstop for whatever slips past it.
    worker_dedup_ttl_seconds: int = 86_400
    worker_dlq_stream_key: str = "pulse:ingest:dlq"

    # Object storage (MinIO locally / S3-compatible in prod) -- the raw
    # per-batch archive, per SPEC.md #6.7. Access/secret reuse the same
    # dev-only credentials as every other store in this file.
    s3_endpoint_url: str = "http://localhost:9002"
    s3_access_key: str = "pulse"
    s3_secret_key: str = "pulse12345"
    s3_bucket: str = "pulse-raw-events"
    s3_region: str = "us-east-1"

    # Phase 11: the query engine. Caps are enforced as ClickHouse query
    # settings, not a separate application-level timeout layer -- ClickHouse's
    # own enforcement is the real boundary here.
    query_max_execution_time_seconds: int = 10
    query_max_rows_to_read: int = 10_000_000
    query_result_limit: int = 10_000
    # Short: query results are cached per exact (spec, org, project), so a
    # short TTL still absorbs the common case (a dashboard re-rendering,
    # someone re-running the same trend) without serving badly stale numbers.
    query_cache_ttl_seconds: int = 60

    # Phase 17. Kill switch for the hourly rollup (pulse/query/rollup.py): off
    # routes every query to the raw events table, which is also how the
    # "before" numbers in docs/PERFORMANCE.md were taken.
    query_rollups_enabled: bool = True
    # Unique users are exact from raw events while the window is small (raw is
    # fast there) and come from the rollup's compact approximate sketch once the
    # window holds at least this many events (exact raw would be slow, or hit the
    # row cap). Not simply "bigger is always faster": a sketch merge's cost is
    # proportional to the number of *hourly rollup rows* a query spans, not the
    # event count, so a wide date range with moderate volume can merge more
    # sketch states than a raw scan would read. Load-tested (docs/PERFORMANCE.md,
    # Phase 17) at 5M and 50M events: 2M put wide weekly/monthly trends over the
    # merge-bound edge on the smaller dataset. Set very high to never approximate.
    query_rollup_unique_min_events: int = 5_000_000
    # Per-org, counting only queries that actually reach ClickHouse (a cache
    # hit is nearly free and doesn't count) -- the resource being protected is
    # ClickHouse time, not HTTP requests. A 20-tile dashboard refresh is 20.
    query_rate_limit_max_queries: int = 120
    query_rate_limit_window_seconds: int = 60

    # Phase 18: NL-to-query (pulse/ai/). "mock" is deterministic, free, and
    # offline -- the default, matching this app's cost-aware posture. Only
    # "anthropic" ever makes a real network call, and only once
    # anthropic_api_key is set; nothing here is a secret to commit (None just
    # means "the real provider is off").
    ai_provider: Literal["mock", "anthropic"] = "mock"
    anthropic_api_key: str | None = None
    ai_model: str = "claude-sonnet-5"
    ai_max_output_tokens: int = 1024
    # A separate budget from query_rate_limit_* above: this counts every
    # translate call (a cost-bearing LLM request when ai_provider=anthropic),
    # not just ones that reach ClickHouse -- a clarify response still cost a
    # model call.
    ai_rate_limit_max_requests: int = 30
    ai_rate_limit_window_seconds: int = 60

    # Phase 19: alerts (pulse/alerts/). The alert-worker (pulse/alerts/main.py)
    # evaluates every enabled alert once per interval; each evaluation reuses
    # the unchanged query engine, so it shares query_rate_limit_* with
    # ordinary traffic rather than having a separate ClickHouse-side budget.
    alert_evaluation_interval_seconds: int = 60
    # HMAC-signs the outbound webhook body (X-Pulse-Signature: sha256=...)
    # when set; unset (the default) sends unsigned, since a self-hosted
    # deployment may have no receiver that checks a signature at all.
    alert_webhook_secret: str | None = None
    alert_webhook_timeout_seconds: float = 5.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
