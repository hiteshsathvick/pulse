import uuid

from sqlalchemy import select

from pulse.models import Insight, InsightKind
from pulse.query.spec import InsightSpec
from pulse.repositories.postgres import session_scope
from pulse.services import audit


def _serialize(spec: InsightSpec) -> dict[str, object]:
    # by_alias so DateRange's `from_` is stored as "from" -- the exact shape
    # the query endpoints accept, so a stored spec can be POSTed back as-is.
    return spec.model_dump(mode="json", by_alias=True)


async def create_insight(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    name: str,
    spec: InsightSpec,
    actor_id: uuid.UUID,
) -> Insight:
    async with session_scope(org_id=org_id) as session:
        insight = Insight(
            org_id=org_id,
            project_id=project_id,
            name=name,
            kind=InsightKind(spec.kind),
            spec=_serialize(spec),
            created_by=actor_id,
        )
        session.add(insight)
        await session.flush()
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="insight.created",
            target=str(insight.id),
            metadata={"kind": insight.kind.value},
        )
        # Refresh before commit, not after: the org scope is transaction-local,
        # so a post-commit refresh would open a new, unscoped transaction and
        # RLS would reject the read.
        await session.refresh(insight)
        await session.commit()
        return insight


async def list_insights(org_id: uuid.UUID, project_id: uuid.UUID) -> list[Insight]:
    async with session_scope(org_id=org_id) as session:
        result = await session.scalars(
            select(Insight)
            .where(Insight.project_id == project_id)
            .order_by(Insight.updated_at.desc(), Insight.id)
        )
        return list(result)


async def get_insight(
    org_id: uuid.UUID, project_id: uuid.UUID, insight_id: uuid.UUID
) -> Insight | None:
    async with session_scope(org_id=org_id) as session:
        insight = await session.get(Insight, insight_id)
        if insight is None or insight.org_id != org_id or insight.project_id != project_id:
            return None
        return insight


async def update_insight(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    insight_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    name: str | None = None,
    spec: InsightSpec | None = None,
) -> Insight | None:
    async with session_scope(org_id=org_id) as session:
        insight = await session.get(Insight, insight_id)
        if insight is None or insight.org_id != org_id or insight.project_id != project_id:
            return None
        if name is not None:
            insight.name = name
        if spec is not None:
            insight.kind = InsightKind(spec.kind)
            insight.spec = _serialize(spec)
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="insight.updated",
            target=str(insight_id),
            metadata={"kind": insight.kind.value},
        )
        # Flush so the server-side updated_at is written, then refresh to load it
        # -- both inside the scoped transaction (see create_insight).
        await session.flush()
        await session.refresh(insight)
        await session.commit()
        return insight


async def delete_insight(
    org_id: uuid.UUID, project_id: uuid.UUID, insight_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    async with session_scope(org_id=org_id) as session:
        insight = await session.get(Insight, insight_id)
        if insight is None or insight.org_id != org_id or insight.project_id != project_id:
            return False
        await session.delete(insight)
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="insight.deleted",
            target=str(insight_id),
        )
        await session.commit()
        return True
