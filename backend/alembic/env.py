import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from pulse.core.config import get_settings

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers defaults to True, which would silently disable
    # every logger already created elsewhere in the process (e.g. pulse's own
    # loggers, when alembic runs in-process during tests) -- not just alembic's.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# No ORM models exist yet -- control-plane models land in Phase 2, at which
# point this becomes their metadata for autogenerate support.
target_metadata = None


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


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        {"sqlalchemy.url": get_settings().database_url},
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
