import hashlib
import json
import uuid

from redis.asyncio import Redis

from pulse.query.spec import TrendSpec

_KEY_PREFIX = "query:cache:"


def cache_key(spec: TrendSpec, org_id: uuid.UUID, project_id: uuid.UUID) -> str:
    """Keyed by (spec, org_id, project_id), per SPEC.md #6: a validated
    spec's own JSON dump is already deterministic for equivalent input (field
    order follows the model's declaration, not the client's), so no extra
    canonicalization is needed beyond folding in the tenant scope."""
    payload = spec.model_dump_json() + str(org_id) + str(project_id)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


async def get_cached(redis_client: Redis, key: str) -> list[dict[str, object]] | None:
    raw = await redis_client.get(key)
    if raw is None:
        return None
    result: list[dict[str, object]] = json.loads(raw)
    return result


async def set_cached(
    redis_client: Redis, key: str, results: list[dict[str, object]], ttl_seconds: int
) -> None:
    await redis_client.set(key, json.dumps(results, default=str), ex=ttl_seconds)
