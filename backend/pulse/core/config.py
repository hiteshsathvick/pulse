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


@lru_cache
def get_settings() -> Settings:
    return Settings()
