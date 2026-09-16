from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from pulse.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# Sentinel for "no org scope", used instead of NULL/unset. A real org_id is
# always a gen_random_uuid() value, so it can never legitimately equal this.
_NO_ORG_SCOPE = "00000000-0000-0000-0000-000000000000"


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


@asynccontextmanager
async def session_scope(org_id: UUID | None = None) -> AsyncIterator[AsyncSession]:
    """With `org_id`, every RLS-protected table (see pulse/models) is scoped to
    that org for the lifetime of this session's transaction -- both what it can
    read (USING) and what it's allowed to write (WITH CHECK). Without it, RLS
    policies default-deny: the GUC is set to a sentinel that no real row's
    org_id can ever equal. `set_config(..., true)` (not raw `SET LOCAL`) so
    the value is a bound parameter, not string-built SQL.

    Always sets a concrete, valid-UUID-shaped value, on every call -- the
    physical connection underneath this session comes from a pool and may
    have been scoped to a *different* org a moment ago. Passing NULL to
    set_config performs a RESET, but for a custom (unregistered) GUC that
    doesn't reliably leave `current_setting(..., true)` reading back as NULL
    once the name has been touched on that connection -- it can come back as
    '', which then fails the policy's ::uuid cast instead of just not
    matching. A sentinel value sidesteps that NULL/'' ambiguity entirely.
    """
    async with get_session_factory()() as session:
        await session.execute(
            text("SELECT set_config('app.current_org_id', :org_id, true)"),
            {"org_id": str(org_id) if org_id is not None else _NO_ORG_SCOPE},
        )
        yield session


async def check_connection() -> bool:
    """Round-trips a trivial query to prove the control-plane database is reachable."""
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def close() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
