import asyncio
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from pulse.core.config import get_settings
from pulse.models import Base

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would silently disable
    # every logger already created elsewhere in the process (e.g. pulse's own
    # loggers, when alembic runs in-process during tests) -- not just alembic's.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _ensure_app_role_exists(bootstrap_url: str, app_url: str) -> None:
    """Row-Level Security -- unconditionally, even with FORCE ROW LEVEL
    SECURITY -- is bypassed for superuser roles. The official postgres image's
    POSTGRES_USER is created as the bootstrap superuser, and Postgres refuses
    to ever strip SUPERUSER from that specific role ("the bootstrap user must
    have the SUPERUSER attribute"), so it can't just be demoted in place.
    Instead, the bootstrap role idempotently creates a second, ordinary role
    for the app/migrations to actually connect as.

    Role/database names below are string-built, not bound parameters -- DDL
    doesn't support parameterizing identifiers, and these values come from our
    own Settings, never external input.
    """
    app = make_url(app_url)
    assert app.username and app.password and app.database

    bootstrap_engine = async_engine_from_config(
        {"sqlalchemy.url": bootstrap_url}, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    try:
        async with bootstrap_engine.connect() as connection:
            role_exists = await connection.scalar(
                text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": app.username}
            )
            if not role_exists:
                await connection.execute(
                    text(f"CREATE ROLE \"{app.username}\" WITH LOGIN PASSWORD '{app.password}'")
                )
            await connection.execute(
                text(f'GRANT ALL PRIVILEGES ON DATABASE "{app.database}" TO "{app.username}"')
            )
            # Postgres 15+ no longer grants CREATE on the public schema to
            # everyone by default -- without this, the app role couldn't
            # create any tables at all.
            await connection.execute(text(f'GRANT ALL ON SCHEMA public TO "{app.username}"'))
            await connection.commit()
    finally:
        await bootstrap_engine.dispose()


async def run_migrations_online() -> None:
    settings = get_settings()
    await _ensure_app_role_exists(settings.database_bootstrap_url, settings.database_url)

    connectable = async_engine_from_config(
        {"sqlalchemy.url": settings.database_url},
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
