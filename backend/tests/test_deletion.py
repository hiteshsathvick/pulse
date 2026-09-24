"""Phase 22 DoD: "a deletion request removes a subject's events." Real
end-to-end tests against ClickHouse (pulse/services/deletion.py) plus API
RBAC for the Owner-only endpoint."""

import asyncio
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import clickhouse_connect
import httpx
import pytest
from sqlalchemy import select

from alembic import command
from alembic.config import Config
from pulse import archive
from pulse.clickhouse_migrations.runner import migrate
from pulse.core.config import get_settings
from pulse.events.fixtures import generate_fake_event
from pulse.events.repository import insert_events, query_events
from pulse.main import app
from pulse.models import AuditLog, User
from pulse.repositories import object_storage
from pulse.repositories.clickhouse import get_client as get_clickhouse_client
from pulse.repositories.postgres import session_scope
from pulse.repositories.redis import get_client as get_redis_client
from pulse.services import deletion as deletion_service
from pulse.services import orgs as orgs_service
from pulse.services import projects as projects_service
from pulse.worker.consumer import ensure_consumer_group, read_batch
from pulse.worker.processing import process_batch
from tests.clickhouse_schema import drop_event_schema

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS_DIR = _BACKEND_ROOT / "pulse" / "clickhouse_migrations" / "migrations"
_PASSWORD = "correct horse battery staple"


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


async def _create_org_and_project() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Real Postgres-backed rows, needed here (unlike test_retention.py's
    sweep, which never touches Postgres beyond a read) because delete_subject
    writes an audit log with real FKs to organizations/users."""
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "Deletion Test Org", f"deletion-test-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", "UTC", user.id)
    return org.id, project.id, user.id


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _register_and_login(client: httpx.AsyncClient, email: str) -> dict[str, str]:
    await client.post(
        "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": email}
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _org_and_project(client: httpx.AsyncClient) -> dict[str, Any]:
    owner_email = f"deletion-owner-{uuid.uuid4().hex[:8]}@example.com"
    headers = await _register_and_login(client, owner_email)
    org = await client.post(
        "/api/v1/orgs",
        json={"name": "Deletion Org", "slug": f"deletion-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    org_id = org.json()["id"]
    project = await client.post(
        f"/api/v1/orgs/{org_id}/projects", json={"name": "Web", "slug": "web"}, headers=headers
    )
    project_id = project.json()["id"]
    return {
        "org_id": org_id,
        "project_id": project_id,
        "headers": headers,
        "base": f"/api/v1/orgs/{org_id}/projects/{project_id}/subjects",
    }


# --- Service layer ---


async def test_delete_subject_removes_only_the_matching_user_id() -> None:
    org_id, project_id, actor_id = await _create_org_and_project()
    ch_client = await get_clickhouse_client()
    await insert_events(
        ch_client,
        [
            generate_fake_event(org_id, project_id, user_id="alice"),
            generate_fake_event(org_id, project_id, user_id="bob"),
        ],
    )

    await deletion_service.delete_subject(ch_client, org_id, project_id, actor_id, user_id="alice")
    await ch_client.command("OPTIMIZE TABLE events FINAL")

    remaining = await query_events(ch_client, org_id, project_id, limit=100)
    assert [row["user_id"] for row in remaining] == ["bob"]


async def test_delete_subject_by_anonymous_id() -> None:
    org_id, project_id, actor_id = await _create_org_and_project()
    ch_client = await get_clickhouse_client()
    anon = str(uuid.uuid4())
    # generate_fake_event has no anonymous_id parameter and its own user_id
    # fallback treats "" as "not given" -- built directly for this one case.
    anon_event = generate_fake_event(org_id, project_id)
    anon_event["user_id"] = ""
    anon_event["anonymous_id"] = anon
    await insert_events(
        ch_client,
        [anon_event, generate_fake_event(org_id, project_id, user_id="carol")],
    )

    await deletion_service.delete_subject(
        ch_client, org_id, project_id, actor_id, anonymous_id=anon
    )
    await ch_client.command("OPTIMIZE TABLE events FINAL")

    remaining = await query_events(ch_client, org_id, project_id, limit=100)
    assert [row["user_id"] for row in remaining] == ["carol"]


async def test_delete_subject_never_leaks_across_tenants() -> None:
    org_a, project_a, actor_a = await _create_org_and_project()
    org_b, project_b, _actor_b = await _create_org_and_project()
    ch_client = await get_clickhouse_client()
    await insert_events(ch_client, [generate_fake_event(org_a, project_a, user_id="shared_id")])
    await insert_events(ch_client, [generate_fake_event(org_b, project_b, user_id="shared_id")])

    await deletion_service.delete_subject(ch_client, org_a, project_a, actor_a, user_id="shared_id")
    await ch_client.command("OPTIMIZE TABLE events FINAL")

    assert await query_events(ch_client, org_a, project_a, limit=100) == []
    assert len(await query_events(ch_client, org_b, project_b, limit=100)) == 1


async def test_no_identifier_raises_without_touching_clickhouse() -> None:
    org_id, project_id, actor_id = await _create_org_and_project()
    ch_client = await get_clickhouse_client()
    with pytest.raises(deletion_service.NoIdentifierGiven):
        await deletion_service.delete_subject(ch_client, org_id, project_id, actor_id)


async def test_delete_subject_writes_an_audit_log_entry() -> None:
    org_id, project_id, actor_id = await _create_org_and_project()
    ch_client = await get_clickhouse_client()
    await insert_events(ch_client, [generate_fake_event(org_id, project_id, user_id="dana")])

    await deletion_service.delete_subject(ch_client, org_id, project_id, actor_id, user_id="dana")

    async with session_scope(org_id=org_id) as session:
        rows = list(
            await session.scalars(select(AuditLog).where(AuditLog.action == "subject.deleted"))
        )
    assert len(rows) == 1
    assert rows[0].actor_id == actor_id
    assert rows[0].target == "dana"


async def test_a_deletion_reaches_events_the_rollup_and_the_archive_together() -> None:
    """The Phase 24 DoD in one pass, through the real ingestion worker: a subject's
    events land in ClickHouse, feed the rollup, and are archived to object storage
    -- and one deletion removes them from all three, leaving other users intact."""
    org_id, project_id, actor_id = await _create_org_and_project()
    ch_client = await get_clickhouse_client()
    redis_client = get_redis_client()
    stream_key, group = f"test:erasure:{uuid.uuid4().hex[:8]}", f"g-{uuid.uuid4().hex[:8]}"
    await ensure_consumer_group(redis_client, stream_key, group)

    now = datetime.now(UTC)

    def fields(user: str) -> dict[str, str]:
        return {
            "org_id": str(org_id),
            "project_id": str(project_id),
            "event_id": str(uuid.uuid4()),
            "event_name": "checkout completed",
            "user_id": user,
            "anonymous_id": "",
            "timestamp": now.isoformat(),
            "received_at": now.isoformat(),
            "properties": json.dumps({"plan": "pro"}),
        }

    for user in ("erasure-alice", "erasure-alice", "erasure-bob"):
        await redis_client.xadd(stream_key, fields(user))
    entries = await read_batch(redis_client, stream_key, group, "c1", count=10, block_ms=100)
    settings = get_settings()
    await process_batch(
        redis_client=redis_client,
        clickhouse_client=ch_client,
        entries=entries,
        stream_key=stream_key,
        group=group,
        dlq_stream_key=f"{stream_key}:dlq",
        dedup_ttl_seconds=settings.worker_dedup_ttl_seconds,
    )

    archived_before = [
        e for objs in (await _archive_objects(org_id, project_id)).values() for e in objs
    ]
    assert sorted(e["user_id"] for e in archived_before) == [
        "erasure-alice",
        "erasure-alice",
        "erasure-bob",
    ]

    report = await deletion_service.delete_subject(
        ch_client, org_id, project_id, actor_id, user_id="erasure-alice"
    )
    await ch_client.command("OPTIMIZE TABLE events FINAL")

    remaining = await query_events(ch_client, org_id, project_id, limit=100)
    assert [row["user_id"] for row in remaining] == ["erasure-bob"]
    rollup = await ch_client.query(
        "SELECT sum(events) FROM event_hourly WHERE org_id = {o:UUID} AND project_id = {p:UUID}",
        parameters={"o": str(org_id), "p": str(project_id)},
    )
    assert int(rollup.result_rows[0][0]) == 1
    archived_after = [
        e for objs in (await _archive_objects(org_id, project_id)).values() for e in objs
    ]
    assert [e["user_id"] for e in archived_after] == ["erasure-bob"]
    assert report.rollup_verified and report.archive.entries_removed == 2


async def _archive_objects(org_id: uuid.UUID, project_id: uuid.UUID) -> dict[str, list[Any]]:
    keys = await object_storage.list_keys(archive.tenant_prefix(org_id, project_id))
    return {key: json.loads(await object_storage.get_bytes(key)) for key in keys}


# --- API RBAC ---


async def test_owner_can_delete_a_subject() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        ch_client = await get_clickhouse_client()
        org_id, project_id = uuid.UUID(ctx["org_id"]), uuid.UUID(ctx["project_id"])
        await insert_events(ch_client, [generate_fake_event(org_id, project_id, user_id="eve")])

        response = await client.post(
            f"{ctx['base']}/delete", json={"user_id": "eve"}, headers=ctx["headers"]
        )
        assert response.status_code == 200
        body = response.json()
        assert body["rollup_verified"] is True
        assert body["rollup_buckets_recomputed"] == 1
        assert set(body["archive"]) == {
            "entries_removed",
            "objects_rewritten",
            "objects_deleted",
            "unreadable_objects",
        }


async def test_a_member_cannot_delete_a_subject_only_an_owner_can() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        admin_email = f"admin-{uuid.uuid4().hex[:8]}@example.com"
        admin_headers = await _register_and_login(client, admin_email)
        invite = await client.post(
            f"/api/v1/orgs/{ctx['org_id']}/invites",
            json={"email": admin_email, "role": "admin"},
            headers=ctx["headers"],
        )
        await client.post(
            "/api/v1/invites/accept",
            json={"token": invite.json()["token"]},
            headers=admin_headers,
        )

        response = await client.post(
            f"{ctx['base']}/delete", json={"user_id": "frank"}, headers=admin_headers
        )
        assert response.status_code == 403


async def test_no_identifier_in_the_request_body_is_a_clean_422() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        response = await client.post(f"{ctx['base']}/delete", json={}, headers=ctx["headers"])
        assert response.status_code == 422


async def test_an_unknown_project_is_404() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        url = f"/api/v1/orgs/{ctx['org_id']}/projects/{uuid.uuid4()}/subjects/delete"
        response = await client.post(url, json={"user_id": "x"}, headers=ctx["headers"])
        assert response.status_code == 404
