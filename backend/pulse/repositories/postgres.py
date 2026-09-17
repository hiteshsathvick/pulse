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

# Sentinels for "no org/user scope", used instead of NULL/unset. A real id is
# always a gen_random_uuid() value, so it can never legitimately equal these.
_NO_ORG_SCOPE = "00000000-0000-0000-0000-000000000000"
_NO_USER_SCOPE = "00000000-0000-0000-0000-000000000000"


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


async def set_session_scope(
    session: AsyncSession, *, org_id: UUID | None = None, user_id: UUID | None = None
) -> None:
    """Re-scopes an already-open session mid-transaction. Used when a single
    transaction needs to change tenant context partway through -- e.g.
    creating an org (unscoped, since Organization carries no org_id) then,
    in the same transaction, inserting its owner Membership (which does)."""
    await session.execute(
        text("SELECT set_config('app.current_org_id', :org_id, true)"),
        {"org_id": str(org_id) if org_id is not None else _NO_ORG_SCOPE},
    )
    await session.execute(
        text("SELECT set_config('app.current_user_id', :user_id, true)"),
        {"user_id": str(user_id) if user_id is not None else _NO_USER_SCOPE},
    )


@asynccontextmanager
async def session_scope(
    org_id: UUID | None = None, user_id: UUID | None = None
) -> AsyncIterator[AsyncSession]:
    """With `org_id`, every RLS-protected table (see pulse/models) is scoped to
    that org for the lifetime of this session's transaction -- both what it can
    read (USING) and what it's allowed to write (WITH CHECK). Without it, RLS
    policies default-deny: the GUC is set to a sentinel that no real row's
    org_id can ever equal. `set_config(..., true)` (not raw `SET LOCAL`) so
    the value is a bound parameter, not string-built SQL.

    `user_id` sets a second GUC (`app.current_user_id`), which exists purely
    for the "which orgs am I in" query: Membership rows are normally only
    visible within one org's scope, but that's inherently cross-org from a
    user's own perspective. A second, `FOR SELECT`-only policy on memberships
    (migration 0004) allows visibility by user_id in addition to org_id --
    scoped to SELECT specifically so it can never loosen the org-scoped
    WITH CHECK on INSERT, which would let a user self-grant membership in any
    org just by setting their own user_id.

    Always sets concrete, valid-UUID-shaped values, on every call -- the
    physical connection underneath this session comes from a pool and may
    have been scoped to *different* org/user a moment ago. Passing NULL to
    set_config performs a RESET, but for a custom (unregistered) GUC that
    doesn't reliably leave `current_setting(..., true)` reading back as NULL
    once the name has been touched on that connection -- it can come back as
    '', which then fails a policy's ::uuid cast instead of just not matching.
    Sentinel values sidestep that NULL/'' ambiguity entirely.
    """
    async with get_session_factory()() as session:
        await set_session_scope(session, org_id=org_id, user_id=user_id)
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
