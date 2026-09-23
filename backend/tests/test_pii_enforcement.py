"""Phase 22 DoD: "PII fields never reach ClickHouse." Pure unit tests for
apply_pii_rules() (no DB at all), plus a real end-to-end pass through
process_batch proving a project's rules actually keep a marked property out
of ClickHouse -- both the row itself and, separately, an unmarked property
on the same event still arrives untouched."""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import clickhouse_connect
import pytest

from alembic import command
from alembic.config import Config
from pulse.clickhouse_migrations.runner import migrate
from pulse.core.config import get_settings
from pulse.events.repository import query_events
from pulse.models import PiiAction
from pulse.repositories.clickhouse import get_client as get_clickhouse_client
from pulse.repositories.redis import get_client as get_redis_client
from pulse.worker.consumer import ensure_consumer_group
from pulse.worker.processing import ParsedEvent, apply_pii_rules, process_batch
from tests.clickhouse_schema import drop_event_schema

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _BACKEND_ROOT / "pulse" / "clickhouse_migrations" / "migrations"


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


@pytest.fixture(scope="module", autouse=True)
def _control_plane_schema() -> Any:
    config = _alembic_config()
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


async def _with_fresh_clickhouse_client(body: Any) -> None:
    settings = get_settings()
    client = await clickhouse_connect.get_async_client(
        host=settings.clickhouse_host,
        port=settings.clickhouse_port,
        username=settings.clickhouse_user,
        password=settings.clickhouse_password,
        database=settings.clickhouse_database,
        secure=settings.clickhouse_secure,
    )
    try:
        await body(client)
    finally:
        await client.close()


@pytest.fixture(scope="module", autouse=True)
def _events_table() -> Any:
    asyncio.run(_with_fresh_clickhouse_client(lambda client: migrate(client, _MIGRATIONS_DIR)))
    yield
    asyncio.run(_with_fresh_clickhouse_client(drop_event_schema))


def _parsed_event(**overrides: Any) -> ParsedEvent:
    defaults: dict[str, Any] = {
        "org_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "event_id": uuid.uuid4(),
        "event_name": "signed up",
        "user_id": "u1",
        "anonymous_id": "",
        "timestamp": datetime.now(UTC),
        "received_at": datetime.now(UTC),
        "properties": {"email": "alice@example.com", "plan": "pro"},
        "raw_properties": {"email": "alice@example.com", "plan": "pro"},
    }
    defaults.update(overrides)
    return ParsedEvent(**defaults)


def test_no_rules_returns_the_event_unchanged() -> None:
    parsed = _parsed_event()
    assert apply_pii_rules(parsed, {}) is parsed


def test_a_rule_for_an_absent_key_changes_nothing() -> None:
    parsed = _parsed_event()
    result = apply_pii_rules(parsed, {"ssn": PiiAction.DROP})
    assert result is parsed


def test_drop_removes_the_key_from_both_properties_and_raw_properties() -> None:
    parsed = _parsed_event()
    result = apply_pii_rules(parsed, {"email": PiiAction.DROP})
    assert "email" not in result.properties
    assert "email" not in result.raw_properties
    assert result.properties == {"plan": "pro"}
    assert result.raw_properties == {"plan": "pro"}


def test_hash_replaces_the_value_deterministically_in_both_dicts() -> None:
    parsed = _parsed_event()
    result = apply_pii_rules(parsed, {"email": PiiAction.HASH})
    assert result.properties["email"] != "alice@example.com"
    assert result.properties["email"] == result.raw_properties["email"]
    # Same input -> same hash, so unique-user-style grouping still works.
    other = _parsed_event(event_id=uuid.uuid4())
    other_result = apply_pii_rules(other, {"email": PiiAction.HASH})
    assert result.properties["email"] == other_result.properties["email"]


def test_hash_uses_the_configured_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = _parsed_event()
    first = apply_pii_rules(parsed, {"email": PiiAction.HASH}).properties["email"]
    monkeypatch.setattr(get_settings(), "pii_hash_secret", "a-different-secret")
    second = apply_pii_rules(parsed, {"email": PiiAction.HASH}).properties["email"]
    assert first != second


def test_unmarked_properties_on_the_same_event_are_untouched() -> None:
    parsed = _parsed_event()
    result = apply_pii_rules(parsed, {"email": PiiAction.DROP})
    assert result.properties["plan"] == "pro"


def test_apply_pii_rules_never_mutates_the_original_event() -> None:
    parsed = _parsed_event()
    original_properties = dict(parsed.properties)
    apply_pii_rules(parsed, {"email": PiiAction.DROP})
    assert parsed.properties == original_properties


def test_a_dropped_value_that_was_never_in_raw_properties_does_not_error() -> None:
    """A property present in the stringified `properties` (ClickHouse-bound)
    but absent from `raw_properties` shouldn't happen in real parsing, but
    apply_pii_rules must not assume it always will -- see
    `pop(key, None)` vs a plain `del`."""
    parsed = _parsed_event(properties={"email": "a@example.com"}, raw_properties={})
    result = apply_pii_rules(parsed, {"email": PiiAction.DROP})
    assert result.properties == {}
    assert result.raw_properties == {}


# --- End-to-end through process_batch (real ClickHouse, no Postgres) ---


def _stream_key() -> str:
    return f"test:pii:stream:{uuid.uuid4().hex[:8]}"


def _group() -> str:
    return f"test-group-{uuid.uuid4().hex[:8]}"


def _raw_fields(org_id: uuid.UUID, project_id: uuid.UUID, **overrides: str) -> dict[str, str]:
    now = datetime.now(UTC).isoformat()
    fields = {
        "org_id": str(org_id),
        "project_id": str(project_id),
        "event_id": str(uuid.uuid4()),
        "event_name": "signed up",
        "user_id": "u1",
        "anonymous_id": "",
        "timestamp": now,
        "received_at": now,
        "properties": json.dumps({"email": "alice@example.com", "plan": "pro"}),
    }
    fields.update(overrides)
    return fields


async def test_a_project_s_rules_keep_the_marked_property_out_of_clickhouse() -> None:
    stream_key, group = _stream_key(), _group()
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)

    org_id, project_id = uuid.uuid4(), uuid.uuid4()
    fields = _raw_fields(org_id, project_id)
    entry = (b"1-1", {k.encode(): v.encode() for k, v in fields.items()})

    async def fetch_rules(_org_id: uuid.UUID, _project_id: uuid.UUID) -> dict[str, PiiAction]:
        return {"email": PiiAction.DROP}

    settings = get_settings()
    await process_batch(
        redis_client=redis_client,
        clickhouse_client=await get_clickhouse_client(),
        entries=[entry],
        stream_key=stream_key,
        group=group,
        dlq_stream_key=f"{stream_key}:dlq",
        dedup_ttl_seconds=settings.worker_dedup_ttl_seconds,
        pii_rules_fetcher=fetch_rules,
    )

    ch_client = await get_clickhouse_client()
    await ch_client.command("OPTIMIZE TABLE events FINAL")
    rows = await query_events(ch_client, org_id, project_id)
    assert len(rows) == 1
    assert "email" not in rows[0]["properties"]
    assert rows[0]["properties"]["plan"] == "pro"


async def test_a_different_project_with_no_rules_is_unaffected() -> None:
    """The fetcher is called per (org_id, project_id) -- a project with no
    rules configured gets `{}` back and every property survives untouched,
    even in the same batch as a project that does have a rule."""
    stream_key, group = _stream_key(), _group()
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)

    protected_org, protected_project = uuid.uuid4(), uuid.uuid4()
    plain_org, plain_project = uuid.uuid4(), uuid.uuid4()
    entries = [
        (
            b"1-1",
            {
                k.encode(): v.encode()
                for k, v in _raw_fields(protected_org, protected_project).items()
            },
        ),
        (
            b"1-2",
            {k.encode(): v.encode() for k, v in _raw_fields(plain_org, plain_project).items()},
        ),
    ]

    async def fetch_rules(org_id: uuid.UUID, _project_id: uuid.UUID) -> dict[str, PiiAction]:
        return {"email": PiiAction.DROP} if org_id == protected_org else {}

    settings = get_settings()
    await process_batch(
        redis_client=redis_client,
        clickhouse_client=await get_clickhouse_client(),
        entries=entries,
        stream_key=stream_key,
        group=group,
        dlq_stream_key=f"{stream_key}:dlq",
        dedup_ttl_seconds=settings.worker_dedup_ttl_seconds,
        pii_rules_fetcher=fetch_rules,
    )

    ch_client = await get_clickhouse_client()
    await ch_client.command("OPTIMIZE TABLE events FINAL")
    plain_rows = await query_events(ch_client, plain_org, plain_project)
    assert plain_rows[0]["properties"]["email"] == "alice@example.com"


async def test_no_fetcher_at_all_is_a_no_op_and_touches_no_postgres() -> None:
    """The default (None) is what every pre-Phase-22 test already relies
    on -- must remain a true no-op, not an accidental behavior change."""
    stream_key, group = _stream_key(), _group()
    redis_client = get_redis_client()
    await ensure_consumer_group(redis_client, stream_key, group)

    org_id, project_id = uuid.uuid4(), uuid.uuid4()
    fields = _raw_fields(org_id, project_id)
    entry = (b"1-1", {k.encode(): v.encode() for k, v in fields.items()})

    settings = get_settings()
    result = await process_batch(
        redis_client=redis_client,
        clickhouse_client=await get_clickhouse_client(),
        entries=[entry],
        stream_key=stream_key,
        group=group,
        dlq_stream_key=f"{stream_key}:dlq",
        dedup_ttl_seconds=settings.worker_dedup_ttl_seconds,
    )
    assert result.processed == 1

    ch_client = await get_clickhouse_client()
    await ch_client.command("OPTIMIZE TABLE events FINAL")
    rows = await query_events(ch_client, org_id, project_id)
    assert rows[0]["properties"]["email"] == "alice@example.com"
