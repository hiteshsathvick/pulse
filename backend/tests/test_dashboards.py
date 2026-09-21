import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from alembic import command
from alembic.config import Config
from pulse.dashboards import service as dashboards_service
from pulse.dashboards.service import Actor
from pulse.main import app
from pulse.models import MembershipRole

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


async def _register_and_login(client: httpx.AsyncClient, email: str) -> dict[str, Any]:
    await client.post(
        "/api/v1/auth/register", json={"email": email, "password": _PASSWORD, "name": email}
    )
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": _PASSWORD})
    return login.json()


def _auth(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


_ALL_ROLES = {"viewer": "viewer", "member": "member", "member2": "member", "admin": "admin"}


async def _build_org(
    client: httpx.AsyncClient, roles: dict[str, str] | None = None
) -> dict[str, Any]:
    """An org with an owner and one user per requested role label."""
    owner_headers = _auth(await _register_and_login(client, f"owner-{uuid.uuid4().hex[:8]}@x.com"))
    org = await client.post(
        "/api/v1/orgs",
        json={"name": "Dash Org", "slug": f"dash-{uuid.uuid4().hex[:8]}"},
        headers=owner_headers,
    )
    org_id = org.json()["id"]
    project = await client.post(
        f"/api/v1/orgs/{org_id}/projects",
        json={"name": "Web", "slug": "web"},
        headers=owner_headers,
    )
    headers = {"owner": owner_headers}
    user_ids = {"owner": (await client.get("/api/v1/auth/me", headers=owner_headers)).json()["id"]}
    for label, role in (roles or {}).items():
        email = f"{label}-{uuid.uuid4().hex[:8]}@x.com"
        invite = await client.post(
            f"/api/v1/orgs/{org_id}/invites",
            json={"email": email, "role": role},
            headers=owner_headers,
        )
        user_headers = _auth(await _register_and_login(client, email))
        await client.post(
            "/api/v1/invites/accept", json={"token": invite.json()["token"]}, headers=user_headers
        )
        headers[label] = user_headers
        user_ids[label] = (await client.get("/api/v1/auth/me", headers=user_headers)).json()["id"]
    return {
        "org_id": org_id,
        "project_id": project.json()["id"],
        "headers": headers,
        "user_ids": user_ids,
    }


def _dash(ctx: dict[str, Any], suffix: str = "") -> str:
    return f"/api/v1/orgs/{ctx['org_id']}/projects/{ctx['project_id']}/dashboards{suffix}"


def _insights_url(ctx: dict[str, Any]) -> str:
    return f"/api/v1/orgs/{ctx['org_id']}/projects/{ctx['project_id']}/insights"


async def _make_insights(client: httpx.AsyncClient, ctx: dict[str, Any], count: int) -> list[str]:
    ids = []
    for i in range(count):
        spec = {
            "kind": "trend",
            "events": [f"event {i}"],
            "measure": "count",
            "range": {"from": "2026-01-01", "to": "2026-01-31"},
        }
        response = await client.post(
            _insights_url(ctx),
            json={"name": f"Insight {i}", "spec": spec},
            headers=ctx["headers"]["owner"],
        )
        ids.append(response.json()["id"])
    return ids


def _tile(insight_id: str, x: int, y: int, w: int = 4, h: int = 3) -> dict[str, Any]:
    return {"insight_id": insight_id, "position": {"x": x, "y": y, "w": w, "h": h}}


async def _create(
    client: httpx.AsyncClient, ctx: dict[str, Any], who: str = "owner", **body: Any
) -> dict[str, Any]:
    response = await client.post(
        _dash(ctx), json={"name": "Board", **body}, headers=ctx["headers"][who]
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_crud_with_sensible_defaults() -> None:
    async with _client() as client:
        ctx = await _build_org(client)
        headers = ctx["headers"]["owner"]

        created = await _create(client, ctx)
        assert created["shared_scope"] == "private"
        assert created["default_range"] == {"type": "relative", "days": 30}
        assert created["layout"] == {"columns": 12}
        assert created["items"] == []
        assert created["can_edit"] is True

        patched = await client.patch(
            _dash(ctx, f"/{created['id']}"),
            json={
                "name": "Renamed",
                "shared_scope": "org",
                "default_range": {"type": "absolute", "from": "2026-01-01", "to": "2026-01-31"},
            },
            headers=headers,
        )
        assert patched.status_code == 200, patched.text
        body = patched.json()
        assert (body["name"], body["shared_scope"]) == ("Renamed", "org")
        assert body["default_range"] == {
            "type": "absolute",
            "from": "2026-01-01",
            "to": "2026-01-31",
        }

        listed = (await client.get(_dash(ctx), headers=headers)).json()
        assert [(d["id"], d["item_count"]) for d in listed] == [(created["id"], 0)]

        assert (
            await client.delete(_dash(ctx, f"/{created['id']}"), headers=headers)
        ).status_code == 204
        assert (
            await client.get(_dash(ctx, f"/{created['id']}"), headers=headers)
        ).status_code == 404
        assert (
            await client.delete(_dash(ctx, f"/{created['id']}"), headers=headers)
        ).status_code == 404


@pytest.mark.parametrize(
    "bad_range",
    [
        {"type": "relative", "days": 0},
        {"type": "relative", "days": 1000},
        {"type": "absolute", "from": "2026-02-01", "to": "2026-01-01"},
        {"type": "nonsense"},
    ],
)
async def test_invalid_default_range_is_rejected(bad_range: dict[str, Any]) -> None:
    async with _client() as client:
        ctx = await _build_org(client)
        response = await client.post(
            _dash(ctx),
            json={"name": "x", "default_range": bad_range},
            headers=ctx["headers"]["owner"],
        )
        assert response.status_code == 422


async def test_layout_persists_and_reloads_in_reading_order() -> None:
    """DoD: layout persistence."""
    async with _client() as client:
        ctx = await _build_org(client)
        headers = ctx["headers"]["owner"]
        a, b, c = await _make_insights(client, ctx, 3)
        dash = await _create(client, ctx)
        url = _dash(ctx, f"/{dash['id']}")

        # Sent out of reading order on purpose.
        layout = [_tile(c, 0, 3, 12, 4), _tile(b, 4, 0, 8, 3), _tile(a, 0, 0, 4, 3)]
        saved = await client.patch(url, json={"items": layout}, headers=headers)
        assert saved.status_code == 200, saved.text

        for source in (saved.json(), (await client.get(url, headers=headers)).json()):
            assert [i["insight"]["id"] for i in source["items"]] == [a, b, c]
            assert [i["position"] for i in source["items"]] == [
                {"x": 0, "y": 0, "w": 4, "h": 3},
                {"x": 4, "y": 0, "w": 8, "h": 3},
                {"x": 0, "y": 3, "w": 12, "h": 4},
            ]
            assert source["items"][0]["insight"]["spec"]["kind"] == "trend"
        assert (await client.get(_dash(ctx), headers=headers)).json()[0]["item_count"] == 3

        # A later save replaces the layout wholesale -- including re-placing an
        # insight that was already on it.
        moved = await client.patch(
            url, json={"items": [_tile(a, 6, 0, 6, 2), _tile(b, 0, 0, 6, 2)]}, headers=headers
        )
        assert moved.status_code == 200, moved.text
        reloaded = (await client.get(url, headers=headers)).json()
        assert [(i["insight"]["id"], i["position"]["x"]) for i in reloaded["items"]] == [
            (b, 0),
            (a, 6),
        ]

        cleared = await client.patch(url, json={"items": []}, headers=headers)
        assert cleared.json()["items"] == []


async def test_a_layout_only_change_still_bumps_updated_at() -> None:
    async with _client() as client:
        ctx = await _build_org(client)
        headers = ctx["headers"]["owner"]
        (a,) = await _make_insights(client, ctx, 1)
        dash = await _create(client, ctx)
        saved = await client.patch(
            _dash(ctx, f"/{dash['id']}"), json={"items": [_tile(a, 0, 0)]}, headers=headers
        )
        assert datetime.fromisoformat(saved.json()["updated_at"]) > datetime.fromisoformat(
            dash["updated_at"]
        )


async def test_an_invalid_layout_is_rejected_and_the_saved_one_is_untouched() -> None:
    async with _client() as client:
        ctx = await _build_org(client)
        headers = ctx["headers"]["owner"]
        a, b = await _make_insights(client, ctx, 2)
        dash = await _create(client, ctx)
        url = _dash(ctx, f"/{dash['id']}")
        good = [_tile(a, 0, 0, 6, 3), _tile(b, 6, 0, 6, 3)]
        assert (await client.patch(url, json={"items": good}, headers=headers)).status_code == 200

        too_many = await _make_insights(client, ctx, 21)
        bad_layouts = {
            "overlap": [_tile(a, 0, 0, 6, 3), _tile(b, 3, 1, 6, 3)],
            "past the right edge": [_tile(a, 8, 0, 6, 3)],
            "zero height": [_tile(a, 0, 0, 4, 0)],
            "too wide": [_tile(a, 0, 0, 13, 3)],
            "duplicate insight": [_tile(a, 0, 0), _tile(a, 4, 0)],
            "unknown insight": [_tile(str(uuid.uuid4()), 0, 0)],
            "over the item cap": [
                _tile(i, (n % 3) * 4, (n // 3) * 3) for n, i in enumerate(too_many)
            ],
        }
        for label, layout in bad_layouts.items():
            response = await client.patch(url, json={"items": layout}, headers=headers)
            assert response.status_code == 422, f"{label}: {response.status_code} {response.text}"

        after = (await client.get(url, headers=headers)).json()
        assert [(i["insight"]["id"], i["position"]["x"]) for i in after["items"]] == [
            (a, 0),
            (b, 6),
        ]


async def test_an_insight_from_another_project_cannot_be_placed() -> None:
    async with _client() as client:
        ctx = await _build_org(client)
        headers = ctx["headers"]["owner"]
        sibling = await client.post(
            f"/api/v1/orgs/{ctx['org_id']}/projects",
            json={"name": "Mobile", "slug": "mobile"},
            headers=headers,
        )
        spec = {
            "kind": "trend",
            "events": ["x"],
            "measure": "count",
            "range": {"from": "2026-01-01", "to": "2026-01-31"},
        }
        elsewhere = await client.post(
            f"/api/v1/orgs/{ctx['org_id']}/projects/{sibling.json()['id']}/insights",
            json={"name": "Mobile insight", "spec": spec},
            headers=headers,
        )
        dash = await _create(client, ctx)
        response = await client.patch(
            _dash(ctx, f"/{dash['id']}"),
            json={"items": [_tile(elsewhere.json()["id"], 0, 0)]},
            headers=headers,
        )
        assert response.status_code == 422

        other_base = f"/api/v1/orgs/{ctx['org_id']}/projects/{sibling.json()['id']}/dashboards"
        assert (await client.get(f"{other_base}/{dash['id']}", headers=headers)).status_code == 404


async def test_deleting_an_insight_removes_it_from_dashboards() -> None:
    async with _client() as client:
        ctx = await _build_org(client)
        headers = ctx["headers"]["owner"]
        a, b = await _make_insights(client, ctx, 2)
        dash = await _create(client, ctx)
        url = _dash(ctx, f"/{dash['id']}")
        await client.patch(url, json={"items": [_tile(a, 0, 0), _tile(b, 4, 0)]}, headers=headers)

        assert (
            await client.delete(f"{_insights_url(ctx)}/{a}", headers=headers)
        ).status_code == 204
        remaining = (await client.get(url, headers=headers)).json()["items"]
        assert [i["insight"]["id"] for i in remaining] == [b]


async def test_sharing_rules_private_org_and_role() -> None:
    """DoD: permission-scoped sharing -- who sees a dashboard, who may change it."""
    async with _client() as client:
        ctx = await _build_org(client, _ALL_ROLES)
        h = ctx["headers"]
        (a,) = await _make_insights(client, ctx, 1)

        private = await _create(client, ctx, "member", name="Private", shared_scope="private")
        shared = await _create(client, ctx, "member", name="Shared", shared_scope="org")
        p_url, s_url = _dash(ctx, f"/{private['id']}"), _dash(ctx, f"/{shared['id']}")

        def names(response: httpx.Response) -> set[str]:
            return {d["name"] for d in response.json()}

        # Visibility. A private dashboard is the creator's alone -- not even the
        # org owner or an admin sees it, and it reads as "not found", not "forbidden".
        assert names(await client.get(_dash(ctx), headers=h["member"])) == {"Private", "Shared"}
        for who in ("member2", "viewer", "admin", "owner"):
            assert names(await client.get(_dash(ctx), headers=h[who])) == {"Shared"}, who
            assert (await client.get(p_url, headers=h[who])).status_code == 404, who
            assert (await client.patch(p_url, json={"name": "x"}, headers=h[who])).status_code in (
                403,
                404,
            ), who
        for who in ("member2", "admin", "owner"):
            assert (
                await client.patch(p_url, json={"name": "x"}, headers=h[who])
            ).status_code == 404

        # Read-only for a teammate: a viewer, and a member who isn't the creator.
        for who in ("viewer", "member2"):
            got = await client.get(s_url, headers=h[who])
            assert got.status_code == 200 and got.json()["can_edit"] is False, who
            for attempt in (
                await client.patch(s_url, json={"name": "hacked"}, headers=h[who]),
                await client.patch(s_url, json={"items": [_tile(a, 0, 0)]}, headers=h[who]),
                await client.patch(s_url, json={"shared_scope": "private"}, headers=h[who]),
                await client.delete(s_url, headers=h[who]),
            ):
                assert attempt.status_code == 403, (who, attempt.text)
        assert (await client.get(s_url, headers=h["member"])).json()["name"] == "Shared"

        # Creator, and admin/owner on an org-shared dashboard, can edit.
        for who in ("member", "admin", "owner"):
            got = await client.get(s_url, headers=h[who])
            assert got.json()["can_edit"] is True, who
        assert (
            await client.patch(s_url, json={"name": "By admin"}, headers=h["admin"])
        ).status_code == 200

        # Viewers can't create at all.
        assert (
            await client.post(_dash(ctx), json={"name": "nope"}, headers=h["viewer"])
        ).status_code == 403

        # Changing scope changes who sees it, immediately.
        await client.patch(p_url, json={"shared_scope": "org"}, headers=h["member"])
        assert (await client.get(p_url, headers=h["viewer"])).json()["can_edit"] is False
        await client.patch(s_url, json={"shared_scope": "private"}, headers=h["member"])
        assert (await client.get(s_url, headers=h["viewer"])).status_code == 404
        assert (await client.get(s_url, headers=h["admin"])).status_code == 404


async def test_dashboards_are_tenant_isolated() -> None:
    """Mandatory tenant-leakage test (CLAUDE.md #4): API and, independently, RLS."""
    async with _client() as client:
        org_a = await _build_org(client)
        org_b = await _build_org(client)
        (insight_a,) = await _make_insights(client, org_a, 1)
        dash = await _create(client, org_a, shared_scope="org", name="A only")
        url = _dash(org_a, f"/{dash['id']}")
        await client.patch(
            url, json={"items": [_tile(insight_a, 0, 0)]}, headers=org_a["headers"]["owner"]
        )

        b = org_b["headers"]["owner"]
        assert (await client.get(url, headers=b)).status_code == 404
        assert (await client.get(_dash(org_a), headers=b)).status_code == 404
        assert (await client.patch(url, json={"name": "pwned"}, headers=b)).status_code == 404
        assert (await client.delete(url, headers=b)).status_code == 404
        # Org B's own URL with org A's dashboard id.
        assert (await client.get(_dash(org_b, f"/{dash['id']}"), headers=b)).status_code == 404

        # Org B cannot pull org A's insight onto its own dashboard.
        own = await _create(client, org_b)
        response = await client.patch(
            _dash(org_b, f"/{own['id']}"), json={"items": [_tile(insight_a, 0, 0)]}, headers=b
        )
        assert response.status_code == 422

        # Below the router, RLS alone hides it: scoped to org B, nothing of org A's exists.
        actor = Actor(user_id=uuid.uuid4(), role=MembershipRole.OWNER)
        org_b_id, project_a = uuid.UUID(org_b["org_id"]), uuid.UUID(org_a["project_id"])
        assert await dashboards_service.list_dashboards(org_b_id, project_a, actor) == []
        assert (
            await dashboards_service.get_dashboard(
                org_b_id, project_a, uuid.UUID(dash["id"]), actor
            )
            is None
        )

        still = await client.get(url, headers=org_a["headers"]["owner"])
        assert still.json()["name"] == "A only" and len(still.json()["items"]) == 1
