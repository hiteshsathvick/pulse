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


@lru_cache
def get_settings() -> Settings:
    return Settings()
