"""Phase 22: per-project data retention. A project's effective window is
its own `retention_days` if set, else its org's default (SPEC.md #6.20).
Enforced by a scheduled sweep (pulse/retention/main.py) issuing a real
ClickHouse mutation -- not native TTL, which is static per table and can't
vary per project without rebuilding a table-wide expression on every
settings change. A project's window takes effect on the next sweep after
the setting changes; nothing is baked in at ingest time."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from clickhouse_connect.driver.asyncclient import AsyncClient
from sqlalchemy import select

from pulse.models import Organization, Project
from pulse.repositories.postgres import session_scope


@dataclass
class RetentionSweepOutcome:
    org_id: str
    project_id: str
    retention_days: int
    cutoff: datetime


def effective_retention_days(project: Project, organization: Organization) -> int:
    if project.retention_days is not None:
        return project.retention_days
    return organization.retention_days


async def _projects_with_organizations() -> list[tuple[Project, Organization]]:
    """Every (project, its org) pair, across every org -- the same
    unscoped-Organization-then-per-org-scoped pattern Phase 19/20
    established, since an unscoped session default-denies every
    RLS-protected table and Organization is the one exception."""
    pairs: list[tuple[Project, Organization]] = []
    async with session_scope() as session:
        org_ids = list(await session.scalars(select(Organization.id)))

    for org_id in org_ids:
        async with session_scope(org_id=org_id) as session:
            organization = await session.get(Organization, org_id)
            assert organization is not None
            projects = list(await session.scalars(select(Project).where(Project.org_id == org_id)))
        pairs.extend((project, organization) for project in projects)
    return pairs


async def sweep_project(
    clickhouse_client: AsyncClient, project: Project, organization: Organization
) -> RetentionSweepOutcome:
    retention_days = effective_retention_days(project, organization)
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    await clickhouse_client.command(
        "ALTER TABLE events DELETE WHERE org_id = {org_id:UUID} "
        "AND project_id = {project_id:UUID} AND timestamp < {cutoff:DateTime64(3)}",
        parameters={
            "org_id": str(project.org_id),
            "project_id": str(project.id),
            "cutoff": cutoff,
        },
        # Waits for this mutation specifically (not every mutation on every
        # replica) so the sweep's own log line, and any test asserting on
        # it, reflects real completed state -- not a mutation still queued.
        settings={"mutations_sync": 1},
    )
    return RetentionSweepOutcome(
        org_id=str(project.org_id),
        project_id=str(project.id),
        retention_days=retention_days,
        cutoff=cutoff,
    )


async def sweep_all_projects(clickhouse_client: AsyncClient) -> list[RetentionSweepOutcome]:
    outcomes: list[RetentionSweepOutcome] = []
    for project, organization in await _projects_with_organizations():
        outcomes.append(await sweep_project(clickhouse_client, project, organization))
    return outcomes
