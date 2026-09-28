"""Suppresses events for a subject that were already accepted into the Redis
ingest stream when a GDPR deletion ran (pulse/services/deletion.py). Without
this, such an event -- POSTed to /ingest a moment before the deletion,
buffered, not yet landed by the worker -- would land in ClickHouse (and the
archive) AFTER the deletion, silently reintroducing exactly the data that
was just erased. Read by the worker (pulse/worker/processing.py) before
landing an event; written by deletion. A leaf module, imported by both, so
neither creates a dependency on the other."""

import uuid

from redis.asyncio import Redis


def _key(org_id: uuid.UUID, project_id: uuid.UUID, kind: str, value: str) -> str:
    return f"suppress:{org_id}:{project_id}:{kind}:{value}"


async def suppress(
    redis_client: Redis,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    user_id: str | None,
    anonymous_id: str | None,
    ttl_seconds: int,
) -> None:
    """Bounded, not permanent: an outage that keeps the worker from draining
    the stream for longer than `ttl_seconds` could let a very late event
    through after all -- a documented residual limit (docs/THREAT_MODEL.md),
    not a solved one. In steady state the worker drains within seconds, so
    the default (Settings.subject_suppression_ttl_seconds, matching the
    ingest dedup TTL) is generous against any realistic catch-up time. It is
    bounded rather than kept forever on purpose: an ever-growing suppression
    list is the same shape of bug Phase 23's unbounded ingest stream was."""
    if not user_id and not anonymous_id:
        return
    pipeline = redis_client.pipeline(transaction=False)
    if user_id:
        pipeline.set(_key(org_id, project_id, "user", user_id), "1", ex=ttl_seconds)
    if anonymous_id:
        pipeline.set(_key(org_id, project_id, "anon", anonymous_id), "1", ex=ttl_seconds)
    await pipeline.execute()


async def is_suppressed(
    redis_client: Redis,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: str,
    anonymous_id: str,
) -> bool:
    keys = []
    if user_id:
        keys.append(_key(org_id, project_id, "user", user_id))
    if anonymous_id:
        keys.append(_key(org_id, project_id, "anon", anonymous_id))
    if not keys:
        return False
    return bool(await redis_client.exists(*keys))
