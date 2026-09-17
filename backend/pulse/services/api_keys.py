import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from pulse.models import ApiKey, ApiKeyType
from pulse.repositories.postgres import session_scope
from pulse.services import audit


class InvalidApiKey(Exception):
    """Unknown key, revoked, or the wrong type for what it's being used for
    (a read key presented where a write key is required, or vice versa) --
    deliberately one error for all of these."""


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _generate_key(key_type: ApiKeyType) -> tuple[str, str, str]:
    """Returns (raw_key, display_prefix, key_hash). The prefix identifies the
    key's type at a glance (e.g. "pulse_write_a1b2c3d4") without exposing
    enough of the secret to matter if it leaks into a log or screenshot."""
    secret = secrets.token_urlsafe(32)
    raw_key = f"pulse_{key_type.value}_{secret}"
    display_prefix = raw_key[: len(f"pulse_{key_type.value}_") + 8]
    return raw_key, display_prefix, _hash_key(raw_key)


async def create_api_key(
    org_id: uuid.UUID, project_id: uuid.UUID, key_type: ApiKeyType, actor_id: uuid.UUID
) -> tuple[ApiKey, str]:
    """Returns the key record and the raw key -- shown to the caller exactly
    once. Only key_hash is ever persisted, same as refresh tokens/invites."""
    raw_key, prefix, key_hash = _generate_key(key_type)
    # api_keys itself isn't RLS-protected, but audit_logs is -- org-scope the
    # session so the audit write below satisfies its WITH CHECK (same fix as
    # invites.py's create_invite/revoke_invite).
    async with session_scope(org_id=org_id) as session:
        key = ApiKey(
            org_id=org_id,
            project_id=project_id,
            type=key_type,
            key_prefix=prefix,
            key_hash=key_hash,
            created_by=actor_id,
        )
        session.add(key)
        await session.flush()
        audit.record(
            session,
            org_id=org_id,
            actor_id=actor_id,
            action="api_key.created",
            target=str(key.id),
            metadata={"type": key_type.value, "project_id": str(project_id)},
        )
        await session.commit()
        return key, raw_key


async def list_api_keys(org_id: uuid.UUID, project_id: uuid.UUID) -> list[ApiKey]:
    async with session_scope() as session:
        result = await session.execute(
            select(ApiKey).where(ApiKey.org_id == org_id, ApiKey.project_id == project_id)
        )
        return list(result.scalars().all())


async def revoke_api_key(org_id: uuid.UUID, key_id: uuid.UUID, actor_id: uuid.UUID) -> bool:
    async with session_scope(org_id=org_id) as session:  # see create_api_key on why
        key = await session.get(ApiKey, key_id)
        if key is None or key.org_id != org_id:
            return False
        key.revoked_at = datetime.now(UTC)
        audit.record(
            session, org_id=org_id, actor_id=actor_id, action="api_key.revoked", target=str(key_id)
        )
        await session.commit()
        return True


async def resolve_api_key(raw_key: str, expected_type: ApiKeyType) -> ApiKey:
    """The redemption path (Phase 7/11 will call this on every ingest/query
    request): looked up by the hash of an opaque secret, before any org
    context exists -- this is why ApiKey isn't RLS-protected."""
    async with session_scope() as session:
        key = await session.scalar(select(ApiKey).where(ApiKey.key_hash == _hash_key(raw_key)))
    if key is None or key.revoked_at is not None or key.type != expected_type:
        raise InvalidApiKey()
    return key
