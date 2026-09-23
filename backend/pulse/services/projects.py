import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from pulse.models import Project
from pulse.repositories.postgres import session_scope
from pulse.services import audit


class ProjectSlugAlreadyTaken(Exception):
    pass


# Distinguishes "retention_days wasn't in this PATCH body at all" (leave
# unchanged) from "it was explicitly sent as null" (clear an override, fall
# back to Organization.retention_days) -- plain `None` can't carry both
# meanings for a field whose real value IS `int | None`.
UNSET = object()


async def create_project(
    org_id: uuid.UUID, name: str, slug: str, timezone: str, actor_id: uuid.UUID
) -> Project:
    async with session_scope(org_id=org_id) as session:
        existing = await session.scalar(
            select(Project).where(Project.org_id == org_id, Project.slug == slug)
        )
        if existing is not None:
            raise ProjectSlugAlreadyTaken()

        project = Project(org_id=org_id, name=name, slug=slug, timezone=timezone)
        session.add(project)
        await session.flush()
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="project.created",
            target=str(project.id),
        )
        await session.commit()
        return project


async def list_projects(org_id: uuid.UUID) -> list[Project]:
    async with session_scope(org_id=org_id) as session:
        result = await session.execute(select(Project).where(Project.org_id == org_id))
        return list(result.scalars().all())


async def get_project(org_id: uuid.UUID, project_id: uuid.UUID) -> Project | None:
    async with session_scope(org_id=org_id) as session:
        project = await session.get(Project, project_id)
        # RLS already guarantees a cross-org project_id comes back None; this
        # is belt-and-braces, not the actual boundary.
        return project if project is not None and project.org_id == org_id else None


async def update_project(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    name: str | None = None,
    timezone: str | None = None,
    retention_days: int | None | object = UNSET,
) -> Project | None:
    async with session_scope(org_id=org_id) as session:
        project = await session.get(Project, project_id)
        if project is None or project.org_id != org_id:
            return None
        if name is not None:
            project.name = name
        if timezone is not None:
            project.timezone = timezone
        if retention_days is not UNSET:
            project.retention_days = retention_days  # type: ignore[assignment]
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="project.updated",
            target=str(project_id),
        )
        await session.commit()
        return project


async def delete_project(org_id: uuid.UUID, project_id: uuid.UUID, actor_id: uuid.UUID) -> bool:
    async with session_scope(org_id=org_id) as session:
        project = await session.get(Project, project_id)
        if project is None or project.org_id != org_id:
            return False
        project.deleted_at = datetime.now(UTC)
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="project.deleted",
            target=str(project_id),
        )
        await session.commit()
        return True
