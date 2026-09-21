import copy
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter

from alembic import command
from alembic.config import Config
from pulse.insights import service as insights_service
from pulse.main import app
from pulse.query.spec import InsightSpec

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_PASSWORD = "correct horse battery staple"

_TREND_SPEC: dict[str, Any] = {
    "kind": "trend",
    "events": ["checkout completed"],
    "measure": "property_sum:revenue",
    "filters": [{"key": "platform", "op": "eq", "value": "ios"}],
    "breakdown": "country",
    "range": {"from": "2026-01-01", "to": "2026-01-31", "tz": "project"},
    "granularity": "week",
}
_FUNNEL_SPEC: dict[str, Any] = {
    "kind": "funnel",
    "steps": [{"event": "signup"}, {"event": "activate"}],
    "window": {"value": 7, "unit": "day"},
    "range": {"from": "2026-01-01", "to": "2026-01-31"},
}
_RETENTION_SPEC: dict[str, Any] = {
    "kind": "retention",
    "born_event": "signup",
    "return_event": "signup",
    "period": "week",
    "periods": 8,
    "range": {"from": "2026-01-01", "to": "2026-03-01"},
}


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


async def _register_and_login(client: httpx.AsyncClient, email: str) -> dict[str, Any]:
    await client.post(
        "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": email}
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return login.json()


def _auth(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _build_org(client: httpx.AsyncClient, *, with_roles: bool = False) -> dict[str, Any]:
    owner_tokens = await _register_and_login(client, f"owner-{uuid.uuid4().hex[:8]}@example.com")
    owner_headers = _auth(owner_tokens)
    org = await client.post(
        "/api/v1/orgs",
        json={"name": "Insights Org", "slug": f"insights-{uuid.uuid4().hex[:8]}"},
        headers=owner_headers,
    )
    org_id = org.json()["id"]
    project = await client.post(
        f"/api/v1/orgs/{org_id}/projects",
        json={"name": "Web", "slug": "web"},
        headers=owner_headers,
    )
    ctx: dict[str, Any] = {
        "org_id": org_id,
        "project_id": project.json()["id"],
        "headers": {"owner": owner_headers},
    }
    if with_roles:
        for role in ("viewer", "member"):
            email = f"{role}-{uuid.uuid4().hex[:8]}@example.com"
            invite = await client.post(
                f"/api/v1/orgs/{org_id}/invites",
                json={"email": email, "role": role},
                headers=owner_headers,
            )
            tokens = await _register_and_login(client, email)
            headers = _auth(tokens)
            await client.post(
                "/api/v1/invites/accept", json={"token": invite.json()["token"]}, headers=headers
            )
            ctx["headers"][role] = headers
    return ctx


def _base(ctx: dict[str, Any]) -> str:
    return f"/api/v1/orgs/{ctx['org_id']}/projects/{ctx['project_id']}/insights"


@pytest.mark.parametrize(
    ("spec", "kind"),
    [(_TREND_SPEC, "trend"), (_FUNNEL_SPEC, "funnel"), (_RETENTION_SPEC, "retention")],
)
async def test_saved_insight_reloads_with_an_identical_spec(
    spec: dict[str, Any], kind: str
) -> None:
    """DoD: saved insights reload -- and what reloads is a spec the query
    engine still accepts, not just an opaque blob."""
    async with _client() as client:
        ctx = await _build_org(client)
        headers = ctx["headers"]["owner"]

        created = await client.post(
            _base(ctx), json={"name": f"My {kind}", "spec": spec}, headers=headers
        )
        assert created.status_code == 201, created.text
        assert created.json()["kind"] == kind

        fetched = await client.get(f"{_base(ctx)}/{created.json()['id']}", headers=headers)
        assert fetched.status_code == 200
        stored = fetched.json()["spec"]

        # Semantic round-trip: what reloads parses back to exactly the spec that
        # was saved (stored form may add defaults such as `version`/`tz`, so
        # compare parsed models, not raw dicts). And the alias survives: the
        # stored range uses "from", the shape the query endpoints accept.
        adapter = TypeAdapter(InsightSpec)
        assert adapter.validate_python(stored) == adapter.validate_python(spec)
        assert "from" in stored["range"]
        assert stored["kind"] == kind

        listed = await client.get(_base(ctx), headers=headers)
        assert [i["id"] for i in listed.json()] == [created.json()["id"]]


@pytest.mark.parametrize(
    "bad_spec",
    [
        {"kind": "nonsense"},
        {**_TREND_SPEC, "measure": "median"},
        {**_TREND_SPEC, "events": []},
        {**_TREND_SPEC, "range": {"from": "2026-02-01", "to": "2026-01-01"}},
        {**_FUNNEL_SPEC, "steps": [{"event": "signup"}]},
        {**_RETENTION_SPEC, "periods": 500},
    ],
)
async def test_invalid_spec_is_rejected_and_nothing_is_stored(bad_spec: dict[str, Any]) -> None:
    async with _client() as client:
        ctx = await _build_org(client)
        headers = ctx["headers"]["owner"]

        response = await client.post(
            _base(ctx), json={"name": "Bad", "spec": bad_spec}, headers=headers
        )
        assert response.status_code == 422

        listed = await client.get(_base(ctx), headers=headers)
        assert listed.json() == []


async def test_update_can_rename_and_change_kind_keeping_kind_in_sync() -> None:
    async with _client() as client:
        ctx = await _build_org(client)
        headers = ctx["headers"]["owner"]
        created = await client.post(
            _base(ctx), json={"name": "Original", "spec": _TREND_SPEC}, headers=headers
        )
        insight_id = created.json()["id"]

        renamed = await client.patch(
            f"{_base(ctx)}/{insight_id}", json={"name": "Renamed"}, headers=headers
        )
        assert renamed.json()["name"] == "Renamed"
        assert renamed.json()["kind"] == "trend"

        respec = await client.patch(
            f"{_base(ctx)}/{insight_id}", json={"spec": _FUNNEL_SPEC}, headers=headers
        )
        assert respec.status_code == 200
        assert respec.json()["kind"] == "funnel"
        assert respec.json()["spec"]["kind"] == "funnel"
        assert respec.json()["name"] == "Renamed"

        empty = await client.patch(f"{_base(ctx)}/{insight_id}", json={}, headers=headers)
        assert empty.status_code == 422


async def test_delete_removes_the_insight() -> None:
    async with _client() as client:
        ctx = await _build_org(client)
        headers = ctx["headers"]["owner"]
        created = await client.post(
            _base(ctx), json={"name": "Doomed", "spec": _TREND_SPEC}, headers=headers
        )
        insight_id = created.json()["id"]

        assert (
            await client.delete(f"{_base(ctx)}/{insight_id}", headers=headers)
        ).status_code == 204
        assert (await client.get(f"{_base(ctx)}/{insight_id}", headers=headers)).status_code == 404
        assert (
            await client.delete(f"{_base(ctx)}/{insight_id}", headers=headers)
        ).status_code == 404


async def test_viewer_can_read_but_not_write() -> None:
    async with _client() as client:
        ctx = await _build_org(client, with_roles=True)
        created = await client.post(
            _base(ctx),
            json={"name": "By member", "spec": _TREND_SPEC},
            headers=ctx["headers"]["member"],
        )
        assert created.status_code == 201
        insight_id = created.json()["id"]

        viewer = ctx["headers"]["viewer"]
        assert (await client.get(_base(ctx), headers=viewer)).status_code == 200
        assert (await client.get(f"{_base(ctx)}/{insight_id}", headers=viewer)).status_code == 200
        assert (
            await client.post(_base(ctx), json={"name": "x", "spec": _TREND_SPEC}, headers=viewer)
        ).status_code == 403
        assert (
            await client.patch(f"{_base(ctx)}/{insight_id}", json={"name": "y"}, headers=viewer)
        ).status_code == 403
        assert (
            await client.delete(f"{_base(ctx)}/{insight_id}", headers=viewer)
        ).status_code == 403


async def test_insights_are_tenant_isolated() -> None:
    """Mandatory tenant-leakage test (CLAUDE.md #4): another org can neither
    see nor touch an insight -- through the API *or* the service layer, since
    RLS is the actual isolation boundary, not just the router's org check."""
    async with _client() as client:
        org_a = await _build_org(client)
        org_b = await _build_org(client)
        created = await client.post(
            _base(org_a),
            json={"name": "Org A secret", "spec": copy.deepcopy(_TREND_SPEC)},
            headers=org_a["headers"]["owner"],
        )
        insight_id = created.json()["id"]

        b_headers = org_b["headers"]["owner"]
        # Org B's owner addressing org A's URL: not a member -> 404, never 200/403.
        assert (
            await client.get(f"{_base(org_a)}/{insight_id}", headers=b_headers)
        ).status_code == 404
        assert (await client.get(_base(org_a), headers=b_headers)).status_code == 404
        assert (
            await client.patch(
                f"{_base(org_a)}/{insight_id}", json={"name": "pwned"}, headers=b_headers
            )
        ).status_code == 404
        assert (
            await client.delete(f"{_base(org_a)}/{insight_id}", headers=b_headers)
        ).status_code == 404

        # Org B's own URL, org A's insight id: not found.
        assert (
            await client.get(f"{_base(org_b)}/{insight_id}", headers=b_headers)
        ).status_code == 404

        # And RLS blocks it below the router, too: scoped to org B, org A's
        # project/insight are simply invisible.
        assert (
            await insights_service.list_insights(
                uuid.UUID(org_b["org_id"]), uuid.UUID(org_a["project_id"])
            )
            == []
        )
        assert (
            await insights_service.get_insight(
                uuid.UUID(org_b["org_id"]), uuid.UUID(org_a["project_id"]), uuid.UUID(insight_id)
            )
            is None
        )

        # Untouched.
        still = await client.get(f"{_base(org_a)}/{insight_id}", headers=org_a["headers"]["owner"])
        assert still.json()["name"] == "Org A secret"


async def test_insight_is_not_reachable_through_a_sibling_project() -> None:
    async with _client() as client:
        ctx = await _build_org(client)
        headers = ctx["headers"]["owner"]
        second = await client.post(
            f"/api/v1/orgs/{ctx['org_id']}/projects",
            json={"name": "Mobile", "slug": "mobile"},
            headers=headers,
        )
        created = await client.post(
            _base(ctx), json={"name": "Web only", "spec": _TREND_SPEC}, headers=headers
        )
        insight_id = created.json()["id"]

        other_base = f"/api/v1/orgs/{ctx['org_id']}/projects/{second.json()['id']}/insights"
        assert (await client.get(f"{other_base}/{insight_id}", headers=headers)).status_code == 404
        assert (await client.get(other_base, headers=headers)).json() == []
