"""RBAC and the real happy paths for pulse/api/billing.py against the
default MockPaymentProvider (needs zero external account -- confirmed with
the user first, see pulse/billing/providers.py), plus the "not configured"
degradation for the optional real-Stripe swap-in path."""

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from alembic import command
from alembic.config import Config
from pulse.core.config import get_settings
from pulse.main import app

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


async def _org(client: httpx.AsyncClient) -> dict[str, Any]:
    owner_email = f"billing-owner-{uuid.uuid4().hex[:8]}@example.com"
    headers = await _register_and_login(client, owner_email)
    org = await client.post(
        "/api/v1/orgs",
        json={"name": "Billing Org", "slug": f"billing-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    org_id = org.json()["id"]
    return {"org_id": org_id, "headers": headers, "base": f"/api/v1/orgs/{org_id}/billing"}


async def test_get_subscription_returns_the_auto_created_free_plan() -> None:
    async with _client() as client:
        ctx = await _org(client)
        response = await client.get(f"{ctx['base']}/subscription", headers=ctx["headers"])
        assert response.status_code == 200
        body = response.json()
        assert body["plan"] == "free"
        assert body["status"] == "active"
        assert body["quota_events_per_month"] == 10_000


async def test_get_usage_defaults_to_zero_before_any_usage_computed() -> None:
    async with _client() as client:
        ctx = await _org(client)
        response = await client.get(f"{ctx['base']}/usage", headers=ctx["headers"])
        assert response.status_code == 200
        body = response.json()
        assert body["events_ingested"] == 0
        assert body["mtu"] == 0
        assert body["quota_events_per_month"] == 10_000


async def test_a_member_cannot_view_billing_only_an_admin_can() -> None:
    async with _client() as client:
        ctx = await _org(client)
        member_email = f"member-{uuid.uuid4().hex[:8]}@example.com"
        member_headers = await _register_and_login(client, member_email)
        invite = await client.post(
            f"/api/v1/orgs/{ctx['org_id']}/invites",
            json={"email": member_email, "role": "member"},
            headers=ctx["headers"],
        )
        await client.post(
            "/api/v1/invites/accept",
            json={"token": invite.json()["token"]},
            headers=member_headers,
        )

        response = await client.get(f"{ctx['base']}/subscription", headers=member_headers)
        assert response.status_code == 403


async def test_a_member_of_another_org_gets_404_not_403() -> None:
    async with _client() as client:
        ctx = await _org(client)
        outsider_headers = await _register_and_login(
            client, f"outsider-{uuid.uuid4().hex[:8]}@example.com"
        )
        response = await client.get(f"{ctx['base']}/subscription", headers=outsider_headers)
        assert response.status_code == 404


# --- the default mock provider: no external account needed at all --------


async def test_checkout_returns_a_local_mock_checkout_url_by_default() -> None:
    async with _client() as client:
        ctx = await _org(client)
        response = await client.post(f"{ctx['base']}/checkout", headers=ctx["headers"])
        assert response.status_code == 200
        url = response.json()["url"]
        assert url == f"http://localhost:3000/orgs/{ctx['org_id']}/billing/mock-checkout"


async def test_checkout_persists_a_customer_id_so_a_second_call_reuses_it() -> None:
    async with _client() as client:
        ctx = await _org(client)
        first = await client.post(f"{ctx['base']}/checkout", headers=ctx["headers"])
        second = await client.post(f"{ctx['base']}/checkout", headers=ctx["headers"])
        assert first.status_code == second.status_code == 200
        assert first.json()["url"] == second.json()["url"]


async def test_portal_is_a_404_with_no_customer_yet() -> None:
    async with _client() as client:
        ctx = await _org(client)
        response = await client.post(f"{ctx['base']}/portal", headers=ctx["headers"])
        assert response.status_code == 404


async def test_portal_returns_a_local_mock_url_once_a_customer_exists() -> None:
    async with _client() as client:
        ctx = await _org(client)
        await client.post(f"{ctx['base']}/checkout", headers=ctx["headers"])
        response = await client.post(f"{ctx['base']}/portal", headers=ctx["headers"])
        assert response.status_code == 200
        assert (
            response.json()["url"]
            == f"http://localhost:3000/orgs/{ctx['org_id']}/billing/mock-checkout"
        )


async def test_invoices_is_an_empty_list_with_no_customer_yet() -> None:
    async with _client() as client:
        ctx = await _org(client)
        response = await client.get(f"{ctx['base']}/invoices", headers=ctx["headers"])
        assert response.status_code == 200
        assert response.json() == []


async def test_invoices_is_still_empty_on_the_free_plan_even_with_a_customer_id() -> None:
    """A customer id alone (created the moment "Upgrade" is first clicked)
    isn't proof of a paid period -- only mock/subscribe (or a real
    completed checkout) moving the org to Pro should produce an invoice."""
    async with _client() as client:
        ctx = await _org(client)
        await client.post(f"{ctx['base']}/checkout", headers=ctx["headers"])
        response = await client.get(f"{ctx['base']}/invoices", headers=ctx["headers"])
        assert response.status_code == 200
        assert response.json() == []


async def test_mock_subscribe_upgrades_the_org_to_pro() -> None:
    async with _client() as client:
        ctx = await _org(client)
        await client.post(f"{ctx['base']}/checkout", headers=ctx["headers"])

        response = await client.post(f"{ctx['base']}/mock/subscribe", headers=ctx["headers"])
        assert response.status_code == 200
        assert response.json() == {"plan": "pro", "status": "active"}

        subscription = await client.get(f"{ctx['base']}/subscription", headers=ctx["headers"])
        body = subscription.json()
        assert body["plan"] == "pro"
        assert body["current_period_start"] is not None
        assert body["current_period_end"] is not None


async def test_mock_subscribe_works_even_without_a_prior_checkout() -> None:
    """mock/subscribe only needs the (always-present) Subscription row, not
    a customer id -- unlike a real provider, there's no separate "create a
    customer" step it depends on."""
    async with _client() as client:
        ctx = await _org(client)
        response = await client.post(f"{ctx['base']}/mock/subscribe", headers=ctx["headers"])
        assert response.status_code == 200


async def test_invoices_shows_one_after_mock_subscribing() -> None:
    async with _client() as client:
        ctx = await _org(client)
        await client.post(f"{ctx['base']}/checkout", headers=ctx["headers"])
        await client.post(f"{ctx['base']}/mock/subscribe", headers=ctx["headers"])

        response = await client.get(f"{ctx['base']}/invoices", headers=ctx["headers"])
        assert response.status_code == 200
        invoices = response.json()
        assert len(invoices) == 1
        assert invoices[0]["status"] == "paid"
        assert invoices[0]["amount_due"] == 2900
        assert invoices[0]["currency"] == "usd"


async def test_mock_cancel_reverts_to_free() -> None:
    async with _client() as client:
        ctx = await _org(client)
        await client.post(f"{ctx['base']}/checkout", headers=ctx["headers"])
        await client.post(f"{ctx['base']}/mock/subscribe", headers=ctx["headers"])

        response = await client.post(f"{ctx['base']}/mock/cancel", headers=ctx["headers"])
        assert response.status_code == 200
        assert response.json() == {"plan": "free", "status": "canceled"}

        subscription = await client.get(f"{ctx['base']}/subscription", headers=ctx["headers"])
        assert subscription.json()["plan"] == "free"

        # And invoices go back to empty -- no longer on Pro.
        invoices = await client.get(f"{ctx['base']}/invoices", headers=ctx["headers"])
        assert invoices.json() == []


async def test_a_member_cannot_call_mock_subscribe_only_an_admin_can() -> None:
    async with _client() as client:
        ctx = await _org(client)
        member_email = f"member-{uuid.uuid4().hex[:8]}@example.com"
        member_headers = await _register_and_login(client, member_email)
        invite = await client.post(
            f"/api/v1/orgs/{ctx['org_id']}/invites",
            json={"email": member_email, "role": "member"},
            headers=ctx["headers"],
        )
        await client.post(
            "/api/v1/invites/accept",
            json={"token": invite.json()["token"]},
            headers=member_headers,
        )

        response = await client.post(f"{ctx['base']}/mock/subscribe", headers=member_headers)
        assert response.status_code == 403


# --- the optional real-Stripe swap-in path --------------------------------


@pytest.fixture
def stripe_provider_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "payment_provider", "stripe")


async def test_checkout_is_a_clean_503_when_stripe_is_selected_but_not_configured(
    stripe_provider_selected: None,
) -> None:
    async with _client() as client:
        ctx = await _org(client)
        response = await client.post(f"{ctx['base']}/checkout", headers=ctx["headers"])
        assert response.status_code == 503


async def test_mock_actions_are_disabled_when_stripe_is_selected(
    stripe_provider_selected: None,
) -> None:
    async with _client() as client:
        ctx = await _org(client)
        response = await client.post(f"{ctx['base']}/mock/subscribe", headers=ctx["headers"])
        assert response.status_code == 400
