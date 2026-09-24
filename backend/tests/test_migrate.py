"""Phase 23: migrations-on-deploy for BOTH stores (`python -m pulse.migrate`).
Ordering/fail-fast is tested with the two steps stubbed; the real thing is
tested end to end against the live Postgres and ClickHouse, including that a
second run (every deploy after the first) is a clean no-op."""

import asyncio
from pathlib import Path
from typing import Any

import clickhouse_connect
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pulse import migrate
from pulse.core.config import get_settings
from tests.clickhouse_schema import drop_event_schema

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


def test_postgres_is_migrated_before_clickhouse(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_clickhouse() -> None:
        calls.append("clickhouse")

    monkeypatch.setattr(migrate, "upgrade_postgres", lambda: calls.append("postgres"))
    monkeypatch.setattr(migrate, "migrate_clickhouse", fake_clickhouse)

    assert migrate.main() == 0
    assert calls == ["postgres", "clickhouse"]


def test_a_postgres_failure_stops_before_touching_clickhouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def failing_postgres() -> None:
        raise RuntimeError("bad migration")

    async def fake_clickhouse() -> None:
        calls.append("clickhouse")

    monkeypatch.setattr(migrate, "upgrade_postgres", failing_postgres)
    monkeypatch.setattr(migrate, "migrate_clickhouse", fake_clickhouse)

    with pytest.raises(RuntimeError, match="bad migration"):
        migrate.main()
    assert calls == []


def test_project_root_finds_alembic_ini() -> None:
    assert (migrate._project_root() / "alembic.ini").exists()


async def _query_clickhouse(sql: str) -> list[Any]:
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
        return list((await client.query(sql)).result_rows)
    finally:
        await client.close()


async def _drop_events() -> None:
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
        await drop_event_schema(client)
    finally:
        await client.close()


async def _current_postgres_revision() -> str | None:
    # A fresh engine per call: the app's global engine is bound to whichever
    # event loop first used it, and each asyncio.run() here is a new loop.
    engine = create_async_engine(get_settings().database_url)
    try:
        async with engine.connect() as connection:
            return await connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await engine.dispose()


def test_a_real_run_migrates_both_stores_and_a_second_run_is_a_no_op() -> None:
    config = _alembic_config()
    head = ScriptDirectory.from_config(config).get_current_head()
    command.downgrade(config, "base")
    asyncio.run(_drop_events())
    try:
        assert migrate.main() == 0
        tables = asyncio.run(
            _query_clickhouse("SELECT name FROM system.tables WHERE database = currentDatabase()")
        )
        assert "events" in {row[0] for row in tables}
        assert asyncio.run(_current_postgres_revision()) == head

        # Every deploy after the first: nothing to do, and it must not fail.
        assert migrate.main() == 0
        assert asyncio.run(_current_postgres_revision()) == head
    finally:
        command.downgrade(config, "base")
        asyncio.run(_drop_events())
