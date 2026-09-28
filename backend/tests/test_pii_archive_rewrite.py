"""Phase 25 DoD: creating a PII rule scrubs the property from a project's
ALREADY-archived events, not just events ingested from then on (the gap
Phase 24 closed for subject deletion but left open for rules -- see
pulse/services/pii_rules.py). Service-layer tests against real Postgres and
object storage, plus one through the real HTTP API for the response shape."""

import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from alembic import command
from alembic.config import Config
from pulse import archive
from pulse.main import app
from pulse.models import PiiAction
from pulse.repositories import object_storage
from pulse.services import pii_rules as pii_rules_service
from tests.test_deletion import _create_org_and_project

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
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


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _register_and_login(client: httpx.AsyncClient, email: str) -> dict[str, str]:
    await client.post(
        "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": email}
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _org_and_project(client: httpx.AsyncClient) -> dict[str, Any]:
    owner_email = f"pii-rewrite-owner-{uuid.uuid4().hex[:8]}@example.com"
    headers = await _register_and_login(client, owner_email)
    org = await client.post(
        "/api/v1/orgs",
        json={"name": "PII Rewrite Org", "slug": f"pii-rewrite-{uuid.uuid4().hex[:8]}"},
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
        "base": f"/api/v1/orgs/{org_id}/projects/{project_id}/pii-rules",
    }


def _archived_entry(
    org_id: uuid.UUID, project_id: uuid.UUID, **properties: object
) -> dict[str, str]:
    return {
        "org_id": str(org_id),
        "project_id": str(project_id),
        "event_id": str(uuid.uuid4()),
        "event_name": "signed up",
        "user_id": "u1",
        "anonymous_id": "",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "received_at": "2026-01-01T00:00:00+00:00",
        "properties": json.dumps(properties),
    }


async def _archived_properties(org_id: uuid.UUID, project_id: uuid.UUID) -> list[dict[str, object]]:
    keys = await object_storage.list_keys(archive.tenant_prefix(org_id, project_id))
    out: list[dict[str, object]] = []
    for key in keys:
        for entry in json.loads(await object_storage.get_bytes(key)):
            out.append(json.loads(entry["properties"]))
    return out


# --- Service layer ---


async def test_creating_a_drop_rule_scrubs_the_property_from_the_existing_archive() -> None:
    org_id, project_id, actor_id = await _create_org_and_project()
    batch = uuid.uuid4()
    await archive.write_batch(
        batch,
        [_archived_entry(org_id, project_id, email="alice@example.com", plan="pro")],
    )

    rule, report = await pii_rules_service.create_pii_rule(
        org_id, project_id, "email", PiiAction.DROP, actor_id
    )

    assert rule.property_key == "email"
    assert report is not None
    assert (report.entries_changed, report.objects_rewritten, report.unreadable) == (1, 1, 0)
    properties = await _archived_properties(org_id, project_id)
    assert properties == [{"plan": "pro"}]


async def test_creating_a_hash_rule_scrubs_the_property_deterministically() -> None:
    org_id, project_id, actor_id = await _create_org_and_project()
    await archive.write_batch(
        uuid.uuid4(), [_archived_entry(org_id, project_id, email="alice@example.com")]
    )

    _, report = await pii_rules_service.create_pii_rule(
        org_id, project_id, "email", PiiAction.HASH, actor_id
    )

    assert report is not None and report.entries_changed == 1
    (properties,) = await _archived_properties(org_id, project_id)
    assert properties["email"] != "alice@example.com"


async def test_the_rewrite_never_touches_another_projects_archive() -> None:
    org_id, project_a, actor_id = await _create_org_and_project()
    _, project_b, _ = await _create_org_and_project()
    await archive.write_batch(
        uuid.uuid4(), [_archived_entry(org_id, project_a, email="a@example.com")]
    )
    await archive.write_batch(
        uuid.uuid4(), [_archived_entry(org_id, project_b, email="b@example.com")]
    )

    await pii_rules_service.create_pii_rule(org_id, project_a, "email", PiiAction.DROP, actor_id)

    assert await _archived_properties(org_id, project_a) == [{}]
    (other,) = await _archived_properties(org_id, project_b)
    assert other["email"] == "b@example.com"


async def test_an_archive_object_with_nothing_to_scrub_is_left_alone() -> None:
    org_id, project_id, actor_id = await _create_org_and_project()
    await archive.write_batch(uuid.uuid4(), [_archived_entry(org_id, project_id, plan="pro")])

    _, report = await pii_rules_service.create_pii_rule(
        org_id, project_id, "email", PiiAction.DROP, actor_id
    )

    assert report is not None
    assert (report.entries_changed, report.objects_rewritten) == (0, 0)


async def test_a_project_with_no_archived_events_yet_is_a_clean_no_op() -> None:
    org_id, project_id, actor_id = await _create_org_and_project()

    _, report = await pii_rules_service.create_pii_rule(
        org_id, project_id, "email", PiiAction.DROP, actor_id
    )

    assert report is not None
    assert (report.entries_changed, report.objects_rewritten, report.unreadable) == (0, 0, 0)


async def test_a_legacy_tenant_mixed_archive_object_is_rewritten_without_losing_other_tenants() -> (
    None
):
    org_a, project_a, actor_id = await _create_org_and_project()
    org_b, project_b, _ = await _create_org_and_project()
    key = "raw/2026/01/01/" + str(uuid.uuid4()) + ".json"
    entries = [
        _archived_entry(org_a, project_a, email="a@example.com"),
        _archived_entry(org_b, project_b, email="b@example.com"),
    ]
    await object_storage.put_object(key, json.dumps(entries).encode())
    try:
        _, report = await pii_rules_service.create_pii_rule(
            org_a, project_a, "email", PiiAction.DROP, actor_id
        )

        assert report is not None and report.entries_changed == 1
        left = json.loads(await object_storage.get_bytes(key))
        left_properties = [json.loads(e["properties"]) for e in left]
        assert {"email": "b@example.com"} in left_properties
        assert {} in left_properties
    finally:
        await object_storage.remove_object(key)


# --- Through the real HTTP API ---


async def test_the_api_response_reports_the_archive_rewrite() -> None:
    async with _client() as client:
        ctx = await _org_and_project(client)
        org_id, project_id = uuid.UUID(ctx["org_id"]), uuid.UUID(ctx["project_id"])
        await archive.write_batch(
            uuid.uuid4(), [_archived_entry(org_id, project_id, email="a@example.com")]
        )

        response = await client.post(
            ctx["base"],
            json={"property_key": "email", "action": "drop"},
            headers=ctx["headers"],
        )

        assert response.status_code == 201
        body = response.json()
        assert body["archive_rewrite"] == {
            "entries_scrubbed": 1,
            "objects_rewritten": 1,
            "unreadable_objects": 0,
        }
        assert await _archived_properties(org_id, project_id) == [{}]
