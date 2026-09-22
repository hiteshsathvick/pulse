import hashlib
import json
import uuid
from dataclasses import dataclass

from redis.asyncio import Redis

from pulse.query.spec import InsightSpec

# v2: the cached value became {"results", "source", "approximate"} instead of a
# bare list, so the answer's provenance survives a cache hit.
_KEY_PREFIX = "query:cache:v2:"


@dataclass(frozen=True)
class CachedResult:
    results: list[dict[str, object]]
    source: str = "raw"
    approximate: bool = False


def cache_key(spec: InsightSpec, org_id: uuid.UUID, project_id: uuid.UUID) -> str:
    """Keyed by (spec, org_id, project_id), per SPEC.md #6: a validated
    spec's own JSON dump is already deterministic for equivalent input (field
    order follows the model's declaration, not the client's), so no extra
    canonicalization is needed beyond folding in the tenant scope."""
    payload = spec.model_dump_json() + str(org_id) + str(project_id)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"{_KEY_PREFIX}{digest}"


async def get_cached(redis_client: Redis, key: str) -> CachedResult | None:
    raw = await redis_client.get(key)
    if raw is None:
        return None
    data = json.loads(raw)
    return CachedResult(
        results=data["results"],
        source=data.get("source", "raw"),
        approximate=data.get("approximate", False),
    )


async def set_cached(
    redis_client: Redis,
    key: str,
    results: list[dict[str, object]],
    ttl_seconds: int,
    *,
    source: str = "raw",
    approximate: bool = False,
) -> None:
    payload = {"results": results, "source": source, "approximate": approximate}
    await redis_client.set(key, json.dumps(payload, default=str), ex=ttl_seconds)
