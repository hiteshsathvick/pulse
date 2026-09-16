import asyncio
from pathlib import Path

import asyncpg

from alembic import command
from alembic.config import Config
from pulse.core.config import get_settings

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


def _asyncpg_dsn() -> str:
    # asyncpg.connect() wants a plain libpq-style URL, not SQLAlchemy's
    # "+asyncpg" driver-qualified one.
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


async def _table_exists(table_name: str) -> bool:
    conn = await asyncpg.connect(_asyncpg_dsn())
    try:
        exists = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = $1)",
            table_name,
        )
        return bool(exists)
    finally:
        await conn.close()


def test_upgrade_then_downgrade_round_trips_the_baseline_table() -> None:
    """Runs the real Alembic CLI machinery (not just importing env.py) against
    the live test Postgres, proving `alembic upgrade head` / `downgrade base`
    actually work, per Phase 1's DoD.

    Deliberately a plain (non-async) test: alembic's env.py drives its own
    `asyncio.run()` internally, which would collide with an event loop
    pytest-asyncio already has running if this were `async def`.
    """
    config = _alembic_config()

    command.upgrade(config, "head")
    assert asyncio.run(_table_exists("_pulse_schema_baseline"))

    command.downgrade(config, "base")
    assert not asyncio.run(_table_exists("_pulse_schema_baseline"))


def test_control_plane_migration_round_trips_all_four_tables() -> None:
    """Phase 2's DoD: migrations reversible. Covers the control-plane tables
    added on top of Phase 1's baseline."""
    config = _alembic_config()
    tables = ("organizations", "users", "memberships", "projects")

    command.upgrade(config, "head")
    for table in tables:
        assert asyncio.run(_table_exists(table)), f"{table} missing after upgrade"

    command.downgrade(config, "base")
    for table in tables:
        assert not asyncio.run(_table_exists(table)), f"{table} still present after downgrade"
