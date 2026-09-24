"""The raw event archive (SPEC.md #6.7): every worker batch, good and poison
entries alike, as JSON in object storage -- the replay/backfill source of truth.

Phase 24 changed the layout so that GDPR subject erasure can reach it. It used
to be one object per *batch* (`raw/YYYY/MM/DD/<batch>.json`), which mixes every
tenant's events, so finding one subject's entries meant reading the whole
archive. It is now one object per (org, project) *per batch*:

    raw/<org_id>/<project_id>/YYYY/MM/DD/<batch>.json

so erasing a subject reads only that project's prefix. Objects written under the
old layout are still found and cleaned (`_legacy_keys`), so nothing goes
un-erased just because it predates the change.

Entries whose org/project can't be parsed as UUIDs (a poison entry with garbage
tenant fields) go under `raw/_unattributed/`. They cannot be tied to a tenant,
so erasure does not touch them -- documented in docs/THREAT_MODEL.md."""

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pulse.repositories import object_storage

logger = logging.getLogger("pulse.archive")

_ROOT = "raw/"
_UNATTRIBUTED = "_unattributed"
_YEAR_SEGMENT = re.compile(r"^\d{4}$")


def tenant_prefix(org_id: uuid.UUID, project_id: uuid.UUID) -> str:
    return f"{_ROOT}{org_id}/{project_id}/"


def _tenant_of(entry: dict[str, str]) -> tuple[uuid.UUID, uuid.UUID] | None:
    try:
        return uuid.UUID(entry["org_id"]), uuid.UUID(entry["project_id"])
    except (KeyError, ValueError):
        return None


async def write_batch(ingest_batch: uuid.UUID, raw_events: list[dict[str, str]]) -> None:
    """Archives the entire raw batch, split per tenant. Keys are built only from
    parsed UUIDs, never from raw entry text, so an entry can't choose its path."""
    if not raw_events:
        return
    groups: dict[str, list[dict[str, str]]] = {}
    for entry in raw_events:
        tenant = _tenant_of(entry)
        prefix = tenant_prefix(*tenant) if tenant else f"{_ROOT}{_UNATTRIBUTED}/"
        groups.setdefault(prefix, []).append(entry)

    stamp = f"{datetime.now(UTC):%Y/%m/%d}"
    for prefix, entries in groups.items():
        await object_storage.put_object(
            f"{prefix}{stamp}/{ingest_batch}.json", json.dumps(entries).encode()
        )


@dataclass(frozen=True)
class ErasureReport:
    entries_removed: int
    objects_rewritten: int
    objects_deleted: int
    # Objects that could not be read or parsed. Reported, never silently
    # skipped: an unreadable object might hold the subject's data.
    unreadable: int


async def _legacy_keys() -> list[str]:
    """Keys under the pre-Phase-24 `raw/YYYY/...` layout. Enumerated one year
    prefix at a time rather than listing all of `raw/`, so the tenant-partitioned
    objects (the bulk, going forward) are not walked."""
    keys: list[str] = []
    for top in await object_storage.list_prefixes(_ROOT):
        segment = top.removeprefix(_ROOT).rstrip("/")
        if _YEAR_SEGMENT.match(segment):
            keys.extend(await object_storage.list_keys(top))
    return keys


def _matches(
    entry: dict[str, str],
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    user_id: str | None,
    anonymous_id: str | None,
) -> bool:
    if entry.get("org_id") != str(org_id) or entry.get("project_id") != str(project_id):
        return False
    return bool(
        (user_id and entry.get("user_id") == user_id)
        or (anonymous_id and entry.get("anonymous_id") == anonymous_id)
    )


async def erase_subject(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    user_id: str | None = None,
    anonymous_id: str | None = None,
) -> ErasureReport:
    """Removes a subject's entries from the archive, scoped to one project. An
    object left with no entries is deleted; otherwise it is rewritten in place
    without them. The tenant match is re-checked per entry (not just per prefix)
    so a legacy, tenant-mixed object never loses another tenant's events."""
    keys = await object_storage.list_keys(tenant_prefix(org_id, project_id))
    keys += await _legacy_keys()

    removed = rewritten = deleted = unreadable = 0
    for key in keys:
        try:
            entries = json.loads(await object_storage.get_bytes(key))
            if not isinstance(entries, list):
                raise ValueError("archive object is not a list")
        except Exception:
            logger.exception("archive object %s could not be read for erasure", key)
            unreadable += 1
            continue

        kept = [
            e
            for e in entries
            if not (isinstance(e, dict) and _matches(e, org_id, project_id, user_id, anonymous_id))
        ]
        if len(kept) == len(entries):
            continue
        removed += len(entries) - len(kept)
        if kept:
            await object_storage.put_object(key, json.dumps(kept).encode())
            rewritten += 1
        else:
            await object_storage.remove_object(key)
            deleted += 1

    return ErasureReport(
        entries_removed=removed,
        objects_rewritten=rewritten,
        objects_deleted=deleted,
        unreadable=unreadable,
    )
