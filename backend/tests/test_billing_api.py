"""RBAC, the "not configured yet" degradation, and the Stripe-mocked happy
paths for pulse/api/billing.py. Every Stripe SDK call is monkeypatched --
this file proves the API wiring, not the Stripe SDK itself (which
pulse/billing/stripe_client.py is a thin, untested-here pass-through over)."""

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from alembic import command
from alembic.config import Config
from pulse.billing import stripe_client
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


async def test_checkout_is_a_clean_503_when_stripe_is_not_configured() -> None:
    async with _client() as client:
        ctx = await _org(client)
        response = await client.post(f"{ctx['base']}/checkout", headers=ctx["headers"])
        assert response.status_code == 503


async def test_invoices_is_an_empty_list_with_no_stripe_customer_yet() -> None:
    """No customer_id at all means no Stripe call is even attempted -- this
    is a 200/[] "nothing to show yet", not a 503."""
    async with _client() as client:
        ctx = await _org(client)
        response = await client.get(f"{ctx['base']}/invoices", headers=ctx["headers"])
        assert response.status_code == 200
        assert response.json() == []


async def test_portal_is_a_404_with_no_stripe_customer_yet() -> None:
    async with _client() as client:
        ctx = await _org(client)
        response = await client.post(f"{ctx['base']}/portal", headers=ctx["headers"])
        assert response.status_code == 404


async def test_checkout_succeeds_end_to_end_with_stripe_mocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_create_customer(**kwargs: Any) -> str:
        assert kwargs["org_name"] == "Billing Org"
        return "cus_fake_123"

    async def _fake_create_checkout_session(**kwargs: Any) -> str:
        assert kwargs["customer_id"] == "cus_fake_123"
        return "https://checkout.stripe.com/fake-session"

    monkeypatch.setattr(stripe_client, "create_customer", _fake_create_customer)
    monkeypatch.setattr(stripe_client, "create_checkout_session", _fake_create_checkout_session)

    async with _client() as client:
        ctx = await _org(client)
        response = await client.post(f"{ctx['base']}/checkout", headers=ctx["headers"])
        assert response.status_code == 200
        assert response.json()["url"] == "https://checkout.stripe.com/fake-session"

        # The customer id is persisted -- a second checkout doesn't re-create one.
        subscription = await client.get(f"{ctx['base']}/subscription", headers=ctx["headers"])
        assert subscription.status_code == 200


async def test_portal_succeeds_end_to_end_once_a_customer_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_create_customer(**kwargs: Any) -> str:
        return "cus_fake_456"

    async def _fake_create_checkout_session(**kwargs: Any) -> str:
        return "https://checkout.stripe.com/fake-session"

    async def _fake_create_portal_session(**kwargs: Any) -> str:
        assert kwargs["customer_id"] == "cus_fake_456"
        return "https://billing.stripe.com/fake-portal"

    monkeypatch.setattr(stripe_client, "create_customer", _fake_create_customer)
    monkeypatch.setattr(stripe_client, "create_checkout_session", _fake_create_checkout_session)
    monkeypatch.setattr(stripe_client, "create_portal_session", _fake_create_portal_session)

    async with _client() as client:
        ctx = await _org(client)
        # First a checkout, to give the org a stripe_customer_id.
        await client.post(f"{ctx['base']}/checkout", headers=ctx["headers"])

        response = await client.post(f"{ctx['base']}/portal", headers=ctx["headers"])
        assert response.status_code == 200
        assert response.json()["url"] == "https://billing.stripe.com/fake-portal"


async def test_invoices_succeeds_end_to_end_once_a_customer_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_create_customer(**kwargs: Any) -> str:
        return "cus_fake_789"

    async def _fake_create_checkout_session(**kwargs: Any) -> str:
        return "https://checkout.stripe.com/fake-session"

    async def _fake_list_invoices(**kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs["customer_id"] == "cus_fake_789"
        return [
            {
                "id": "in_1",
                "status": "paid",
                "amount_due": 2000,
                "currency": "usd",
                "hosted_invoice_url": "https://invoice.stripe.com/fake",
                "created": 1_700_000_000,
            }
        ]

    monkeypatch.setattr(stripe_client, "create_customer", _fake_create_customer)
    monkeypatch.setattr(stripe_client, "create_checkout_session", _fake_create_checkout_session)
    monkeypatch.setattr(stripe_client, "list_invoices", _fake_list_invoices)

    async with _client() as client:
        ctx = await _org(client)
        await client.post(f"{ctx['base']}/checkout", headers=ctx["headers"])

        response = await client.get(f"{ctx['base']}/invoices", headers=ctx["headers"])
        assert response.status_code == 200
        invoices = response.json()
        assert len(invoices) == 1
        assert invoices[0]["id"] == "in_1"
        assert invoices[0]["amount_due"] == 2000
