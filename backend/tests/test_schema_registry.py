import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from alembic import command
from alembic.config import Config
from pulse.main import app
from pulse.models import PropertyType, SchemaStatus, User
from pulse.registry.service import RegistryObservation, list_events, list_properties, register_batch
from pulse.repositories.postgres import session_scope
from pulse.services import orgs as orgs_service
from pulse.services import projects as projects_service

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_PASSWORD = "correct horse battery staple"


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


@pytest.fixture(scope="module", autouse=True)
def _control_plane_schema():
    config = _alembic_config()
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _create_org_and_project() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "Registry Test Org", f"registry-org-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", "UTC", user.id)
    return org.id, project.id, user.id


async def _register_and_login(client: httpx.AsyncClient, email: str) -> dict[str, Any]:
    await client.post(
        "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": email}
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return login.json()


def _auth(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _build_org_with_all_roles(client: httpx.AsyncClient) -> dict[str, Any]:
    owner_email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
    owner_tokens = await _register_and_login(client, owner_email)
    owner_headers = _auth(owner_tokens)

    org = await client.post(
        "/api/v1/orgs",
        json={"name": "Registry RBAC Org", "slug": f"registry-rbac-{uuid.uuid4().hex[:8]}"},
        headers=owner_headers,
    )
    org_id = org.json()["id"]

    project = await client.post(
        f"/api/v1/orgs/{org_id}/projects",
        json={"name": "Web", "slug": "web"},
        headers=owner_headers,
    )
    project_id = project.json()["id"]

    role_headers = {"owner": owner_headers}
    for role in ("viewer", "member", "admin"):
        email = f"{role}-{uuid.uuid4().hex[:8]}@example.com"
        invite = await client.post(
            f"/api/v1/orgs/{org_id}/invites",
            json={"email": email, "role": role},
            headers=owner_headers,
        )
        token = invite.json()["token"]
        tokens = await _register_and_login(client, email)
        headers = _auth(tokens)
        await client.post("/api/v1/invites/accept", json={"token": token}, headers=headers)
        role_headers[role] = headers

    return {"org_id": org_id, "project_id": project_id, "headers": role_headers}


async def test_new_event_auto_registers_with_correctly_inferred_property_types() -> None:
    """DoD: new event auto-registers."""
    org_id, project_id, _ = await _create_org_and_project()

    await register_batch(
        [
            RegistryObservation(
                org_id=org_id,
                project_id=project_id,
                event_name="checkout completed",
                raw_properties={
                    "revenue": 19.99,
                    "is_first_purchase": True,
                    "platform": "ios",
                    "occurred_at": "2026-01-01T00:00:00+00:00",
                    "note": None,
                },
            )
        ]
    )

    events = await list_events(org_id, project_id)
    assert len(events) == 1
    event = events[0]
    assert event.event_name == "checkout completed"
    assert event.status == SchemaStatus.ACTIVE
    assert event.volume_estimate == 1

    properties = {p.key: p for p in await list_properties(org_id, event.id)}
    assert properties["revenue"].inferred_type == PropertyType.NUMBER
    assert properties["is_first_purchase"].inferred_type == PropertyType.BOOL
    assert properties["platform"].inferred_type == PropertyType.STRING
    assert properties["occurred_at"].inferred_type == PropertyType.DATETIME
    assert "note" not in properties, "a null-valued property must not be registered"


async def test_volume_estimate_accumulates_across_batches() -> None:
    org_id, project_id, _ = await _create_org_and_project()
    for _ in range(3):
        await register_batch(
            [
                RegistryObservation(
                    org_id=org_id,
                    project_id=project_id,
                    event_name="page viewed",
                    raw_properties={},
                )
            ]
        )

    events = await list_events(org_id, project_id)
    assert events[0].volume_estimate == 3


async def test_type_conflict_is_flagged_but_original_type_is_preserved() -> None:
    """DoD: type-conflict flagged -- not enforced, not silently overwritten."""
    org_id, project_id, _ = await _create_org_and_project()

    await register_batch(
        [
            RegistryObservation(
                org_id=org_id,
                project_id=project_id,
                event_name="signed up",
                raw_properties={"referral_code": 12345},
            )
        ]
    )
    events = await list_events(org_id, project_id)
    properties = await list_properties(org_id, events[0].id)
    assert properties[0].inferred_type == PropertyType.NUMBER
    assert properties[0].type_conflict_detected_at is None

    # Same event, same property key, now a string -- a real conflict.
    await register_batch(
        [
            RegistryObservation(
                org_id=org_id,
                project_id=project_id,
                event_name="signed up",
                raw_properties={"referral_code": "PROMO2026"},
            )
        ]
    )
    properties_after = await list_properties(org_id, events[0].id)
    assert properties_after[0].inferred_type == PropertyType.NUMBER, (
        "original type is not overwritten"
    )
    assert properties_after[0].type_conflict_detected_at is not None


async def test_viewer_can_list_but_not_deprecate_or_mark_pii() -> None:
    async with _client() as client:
        ctx = await _build_org_with_all_roles(client)
        org_id, project_id = uuid.UUID(ctx["org_id"]), uuid.UUID(ctx["project_id"])

        await register_batch(
            [
                RegistryObservation(
                    org_id=org_id,
                    project_id=project_id,
                    event_name="button clicked",
                    raw_properties={"label": "signup"},
                )
            ]
        )
        events = await list_events(org_id, project_id)
        event_id = events[0].id
        property_id = (await list_properties(org_id, event_id))[0].id

        base = f"/api/v1/orgs/{ctx['org_id']}/projects/{ctx['project_id']}/schema"
        h = ctx["headers"]["viewer"]

        listed = await client.get(f"{base}/events", headers=h)
        assert listed.status_code == 200
        assert len(listed.json()) == 1

        listed_props = await client.get(f"{base}/events/{event_id}/properties", headers=h)
        assert listed_props.status_code == 200

        deprecate = await client.patch(
            f"{base}/events/{event_id}", json={"status": "deprecated"}, headers=h
        )
        assert deprecate.status_code == 403

        mark_pii = await client.patch(
            f"{base}/properties/{property_id}", json={"is_pii": True}, headers=h
        )
        assert mark_pii.status_code == 403


async def test_admin_can_deprecate_an_event_and_mark_a_property_pii() -> None:
    async with _client() as client:
        ctx = await _build_org_with_all_roles(client)
        org_id, project_id = uuid.UUID(ctx["org_id"]), uuid.UUID(ctx["project_id"])

        await register_batch(
            [
                RegistryObservation(
                    org_id=org_id,
                    project_id=project_id,
                    event_name="user identified",
                    raw_properties={"email": "user@example.com"},
                )
            ]
        )
        events = await list_events(org_id, project_id)
        event_id = events[0].id
        property_id = (await list_properties(org_id, event_id))[0].id

        base = f"/api/v1/orgs/{ctx['org_id']}/projects/{ctx['project_id']}/schema"
        h = ctx["headers"]["admin"]

        deprecate = await client.patch(
            f"{base}/events/{event_id}", json={"status": "deprecated"}, headers=h
        )
        assert deprecate.status_code == 200
        assert deprecate.json()["status"] == "deprecated"

        mark_pii = await client.patch(
            f"{base}/properties/{property_id}", json={"is_pii": True}, headers=h
        )
        assert mark_pii.status_code == 200
        assert mark_pii.json()["is_pii"] is True

        # deprecating an event never touches ClickHouse/ingestion -- it's
        # purely registry metadata (DoD: unknown/registered events alike
        # still ingest, never blocked by their registry status).
        listed = await client.get(f"{base}/events", headers=h)
        assert listed.json()[0]["status"] == "deprecated"


async def test_events_are_scoped_to_their_own_project() -> None:
    org_a, project_a, _ = await _create_org_and_project()
    org_b, project_b, _ = await _create_org_and_project()

    await register_batch(
        [
            RegistryObservation(
                org_id=org_a, project_id=project_a, event_name="shared name", raw_properties={}
            ),
            RegistryObservation(
                org_id=org_b, project_id=project_b, event_name="shared name", raw_properties={}
            ),
        ]
    )

    events_a = await list_events(org_a, project_a)
    events_b = await list_events(org_b, project_b)
    assert len(events_a) == 1
    assert len(events_b) == 1
    assert events_a[0].id != events_b[0].id
