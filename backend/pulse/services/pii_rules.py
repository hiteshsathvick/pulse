import uuid

from sqlalchemy import select

from pulse.models import PiiAction, PiiRule
from pulse.repositories.postgres import session_scope
from pulse.services import audit


class DuplicatePiiRule(Exception):
    """A rule for this property_key already exists on this project --
    update or delete it instead of creating a second one."""


async def create_pii_rule(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    property_key: str,
    action: PiiAction,
    actor_id: uuid.UUID,
) -> PiiRule:
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
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="pii_rule.created",
            target=str(rule.id),
            metadata={"property_key": property_key, "action": action.value},
        )
        await session.commit()
        return rule


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
