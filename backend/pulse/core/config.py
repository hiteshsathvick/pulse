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


@lru_cache
def get_settings() -> Settings:
    return Settings()
