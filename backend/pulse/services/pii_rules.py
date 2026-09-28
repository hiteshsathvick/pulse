import logging
import uuid

from sqlalchemy import select

from pulse import archive
from pulse.core.config import get_settings
from pulse.models import PiiAction, PiiRule
from pulse.repositories.postgres import session_scope
from pulse.services import audit

logger = logging.getLogger("pulse.services.pii_rules")


class DuplicatePiiRule(Exception):
    """A rule for this property_key already exists on this project --
    update or delete it instead of creating a second one."""


async def create_pii_rule(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    property_key: str,
    action: PiiAction,
    actor_id: uuid.UUID,
) -> tuple[PiiRule, archive.RewriteReport | None]:
    """Also retroactively scrubs the property from this project's already-archived
    events (pulse/archive.py::apply_pii_rules) -- Phase 24 closed this gap for
    subject *deletion*; Phase 25 closes it for PII *rules*, which until now only
    protected new events going forward. The rule commits first, in its own short
    transaction, so it exists and is enforced (pulse/worker/processing.py) even
    if the archive rewrite -- which walks every one of the project's archive
    objects and can be slow for a large one -- fails outright (a total object-
    storage outage, say). That failure is best-effort, not fatal: the return
    value is `None` rather than a report when it happens, and the caller (the
    API) still returns 201, since the rule itself is durable either way. Per-
    object read failures inside a successful rewrite are still reported, in
    `RewriteReport.unreadable`, not swallowed."""
    async with session_scope(org_id=org_id) as session:
        existing = await session.scalar(
            select(PiiRule).where(
                PiiRule.project_id == project_id, PiiRule.property_key == property_key
            )
        )
        if existing is not None:
            raise DuplicatePiiRule()
        rule = PiiRule(
            org_id=org_id, project_id=project_id, property_key=property_key, action=action
        )
        session.add(rule)
        await session.flush()
        await session.commit()

    rewrite_report: archive.RewriteReport | None
    try:
        rewrite_report = await archive.apply_pii_rules(
            org_id, project_id, {property_key: action}, get_settings().pii_hash_secret
        )
    except Exception:
        logger.exception("archive PII rewrite failed for rule %s", rule.id)
        rewrite_report = None

    async with session_scope(org_id=org_id) as session:
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="pii_rule.created",
            target=str(rule.id),
            metadata={
                "property_key": property_key,
                "action": action.value,
                "archive_entries_scrubbed": (
                    rewrite_report.entries_changed if rewrite_report else None
                ),
                "archive_objects_rewritten": (
                    rewrite_report.objects_rewritten if rewrite_report else None
                ),
                "archive_unreadable_objects": rewrite_report.unreadable if rewrite_report else None,
                "archive_rewrite_failed": rewrite_report is None,
            },
        )
        await session.commit()

    return rule, rewrite_report


async def list_pii_rules(org_id: uuid.UUID, project_id: uuid.UUID) -> list[PiiRule]:
    async with session_scope(org_id=org_id) as session:
        result = await session.execute(
            select(PiiRule).where(PiiRule.project_id == project_id).order_by(PiiRule.property_key)
        )
        return list(result.scalars().all())


async def delete_pii_rule(
    org_id: uuid.UUID, project_id: uuid.UUID, rule_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    async with session_scope(org_id=org_id) as session:
        rule = await session.get(PiiRule, rule_id)
        if rule is None or rule.project_id != project_id:
            return False
        await session.delete(rule)
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="pii_rule.deleted",
            target=str(rule_id),
        )
        await session.commit()
        return True


async def get_rules_for_project(org_id: uuid.UUID, project_id: uuid.UUID) -> dict[str, PiiAction]:
    """The lookup shape the ingest pipeline actually wants: property_key ->
    action, for one project. Called once per batch (pulse/worker/processing.py),
    not per event."""
    rules = await list_pii_rules(org_id, project_id)
    return {rule.property_key: rule.action for rule in rules}
