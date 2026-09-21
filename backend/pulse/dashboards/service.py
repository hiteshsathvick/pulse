import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from pulse.dashboards.schemas import (
    DEFAULT_LAYOUT,
    DEFAULT_RANGE,
    DashboardRange,
    ItemInput,
    validate_layout,
)
from pulse.models import Dashboard, DashboardItem, DashboardScope, Insight, MembershipRole
from pulse.repositories.postgres import session_scope
from pulse.services import audit

_ADMIN_ROLES = {MembershipRole.ADMIN, MembershipRole.OWNER}


class DashboardForbidden(Exception):
    """The caller can see this dashboard but may not change it."""


class InvalidLayout(Exception):
    """The requested set of tiles isn't a valid layout; str(exc) is safe to show."""


@dataclass(frozen=True)
class Actor:
    user_id: uuid.UUID
    role: MembershipRole


@dataclass
class Bundle:
    dashboard: Dashboard
    items: list[tuple[DashboardItem, Insight]]
    can_edit: bool


@dataclass
class Summary:
    dashboard: Dashboard
    item_count: int
    can_edit: bool


def can_view(dashboard: Dashboard, actor: Actor) -> bool:
    """PRIVATE is the creator's alone -- not even org admins see it, so a
    person can keep a scratch dashboard without it being browsable. ORG is
    visible to every member of the org."""
    return dashboard.shared_scope == DashboardScope.ORG or dashboard.created_by == actor.user_id


def can_edit(dashboard: Dashboard, actor: Actor) -> bool:
    """Viewers never edit. Otherwise the creator always can, and an admin or
    owner can edit any dashboard they can see (i.e. an org-shared one)."""
    if actor.role == MembershipRole.VIEWER:
        return False
    if dashboard.created_by == actor.user_id:
        return True
    return dashboard.shared_scope == DashboardScope.ORG and actor.role in _ADMIN_ROLES


def _range_json(value: DashboardRange) -> dict[str, object]:
    return value.model_dump(mode="json", by_alias=True)


def _position_key(item: DashboardItem) -> tuple[int, int]:
    position = item.position
    return (int(position["y"]), int(position["x"]))  # type: ignore[call-overload]


async def _load_bundle(session: AsyncSession, dashboard: Dashboard, actor: Actor) -> Bundle:
    rows = await session.execute(
        select(DashboardItem, Insight)
        .join(Insight, Insight.id == DashboardItem.insight_id)
        .where(DashboardItem.dashboard_id == dashboard.id)
    )
    items = sorted(((i, ins) for i, ins in rows.all()), key=lambda pair: _position_key(pair[0]))
    return Bundle(dashboard=dashboard, items=items, can_edit=can_edit(dashboard, actor))


async def create_dashboard(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    name: str,
    shared_scope: DashboardScope,
    default_range: DashboardRange | None,
    actor: Actor,
) -> Bundle:
    async with session_scope(org_id=org_id) as session:
        dashboard = Dashboard(
            org_id=org_id,
            project_id=project_id,
            name=name,
            layout=dict(DEFAULT_LAYOUT),
            default_range=_range_json(default_range or DEFAULT_RANGE),
            shared_scope=shared_scope,
            created_by=actor.user_id,
        )
        session.add(dashboard)
        await session.flush()
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor.user_id,
            action="dashboard.created",
            target=str(dashboard.id),
            metadata={"shared_scope": shared_scope.value},
        )
        await session.refresh(dashboard)
        bundle = await _load_bundle(session, dashboard, actor)
        await session.commit()
        return bundle


async def list_dashboards(org_id: uuid.UUID, project_id: uuid.UUID, actor: Actor) -> list[Summary]:
    async with session_scope(org_id=org_id) as session:
        rows = await session.execute(
            select(Dashboard, func.count(DashboardItem.id))
            .outerjoin(DashboardItem, DashboardItem.dashboard_id == Dashboard.id)
            .where(
                and_(
                    Dashboard.project_id == project_id,
                    or_(
                        Dashboard.shared_scope == DashboardScope.ORG,
                        Dashboard.created_by == actor.user_id,
                    ),
                )
            )
            .group_by(Dashboard.id)
            .order_by(Dashboard.updated_at.desc(), Dashboard.id)
        )
        return [
            Summary(dashboard=d, item_count=count, can_edit=can_edit(d, actor))
            for d, count in rows.all()
        ]


async def _get_visible(
    session: AsyncSession,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    dashboard_id: uuid.UUID,
    actor: Actor,
) -> Dashboard | None:
    dashboard = await session.get(Dashboard, dashboard_id)
    if (
        dashboard is None
        or dashboard.org_id != org_id
        or dashboard.project_id != project_id
        or not can_view(dashboard, actor)
    ):
        return None
    return dashboard


async def get_dashboard(
    org_id: uuid.UUID, project_id: uuid.UUID, dashboard_id: uuid.UUID, actor: Actor
) -> Bundle | None:
    async with session_scope(org_id=org_id) as session:
        dashboard = await _get_visible(session, org_id, project_id, dashboard_id, actor)
        if dashboard is None:
            return None
        return await _load_bundle(session, dashboard, actor)


async def update_dashboard(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    dashboard_id: uuid.UUID,
    actor: Actor,
    *,
    name: str | None = None,
    shared_scope: DashboardScope | None = None,
    default_range: DashboardRange | None = None,
    items: list[ItemInput] | None = None,
) -> Bundle | None:
    """Returns None if the dashboard doesn't exist *or the caller can't see
    it* (indistinguishable on purpose). Raises DashboardForbidden if they can
    see it but not edit it, InvalidLayout for a bad tile set. `items`, when
    given, replaces the whole layout in this one transaction -- there is no
    partial update, so a saved dashboard is never half old layout, half new."""
    async with session_scope(org_id=org_id) as session:
        dashboard = await _get_visible(session, org_id, project_id, dashboard_id, actor)
        if dashboard is None:
            return None
        if not can_edit(dashboard, actor):
            raise DashboardForbidden()

        changed: list[str] = []
        if name is not None:
            dashboard.name = name
            changed.append("name")
        if shared_scope is not None:
            dashboard.shared_scope = shared_scope
            changed.append("shared_scope")
        if default_range is not None:
            dashboard.default_range = _range_json(default_range)
            changed.append("default_range")
        if items is not None:
            await _replace_items(session, org_id, project_id, dashboard, items)
            changed.append("items")

        # Touched explicitly: a layout-only change doesn't modify the
        # dashboards row itself, so the onupdate default would never fire.
        dashboard.updated_at = datetime.now(UTC)
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor.user_id,
            action="dashboard.updated",
            target=str(dashboard_id),
            metadata={"changed": changed},
        )
        # Flush + refresh + load inside the scoped transaction, before commit
        # (a post-commit read would open an unscoped one and RLS would reject it).
        await session.flush()
        await session.refresh(dashboard)
        bundle = await _load_bundle(session, dashboard, actor)
        await session.commit()
        return bundle


async def _replace_items(
    session: AsyncSession,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    dashboard: Dashboard,
    items: list[ItemInput],
) -> None:
    try:
        validate_layout(items)
    except ValueError as exc:
        raise InvalidLayout(str(exc)) from exc

    wanted = {item.insight_id for item in items}
    if wanted:
        found = set(
            await session.scalars(
                select(Insight.id).where(Insight.project_id == project_id, Insight.id.in_(wanted))
            )
        )
        if found != wanted:
            raise InvalidLayout("one or more insights were not found in this project")

    # A Core DELETE runs immediately; a session.delete() would be ordered
    # *after* the INSERTs of a same-flush re-add and trip the unique constraint.
    await session.execute(delete(DashboardItem).where(DashboardItem.dashboard_id == dashboard.id))
    for item in items:
        session.add(
            DashboardItem(
                org_id=org_id,
                dashboard_id=dashboard.id,
                insight_id=item.insight_id,
                position=item.position.model_dump(),
            )
        )
    await session.flush()


async def delete_dashboard(
    org_id: uuid.UUID, project_id: uuid.UUID, dashboard_id: uuid.UUID, actor: Actor
) -> bool:
    async with session_scope(org_id=org_id) as session:
        dashboard = await _get_visible(session, org_id, project_id, dashboard_id, actor)
        if dashboard is None:
            return False
        if not can_edit(dashboard, actor):
            raise DashboardForbidden()
        await session.delete(dashboard)
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor.user_id,
            action="dashboard.deleted",
            target=str(dashboard_id),
        )
        await session.commit()
        return True
