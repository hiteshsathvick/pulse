"""SPEC.md Phase 20 DoD: "webhook handling." Two layers: pure event-handling
logic (pulse/billing/webhooks.py::handle_event, given plain dict fixtures --
no real Stripe SDK objects or network needed) and the real
POST /api/v1/webhooks/stripe route's signature verification (a real HMAC
computed locally against a test secret -- Stripe's signing scheme needs
nothing but the shared secret, so this never touches a real Stripe account
or the network either)."""

import hashlib
import hmac
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from alembic import command
from alembic.config import Config
from pulse.billing.webhooks import handle_event
from pulse.core.config import get_settings
from pulse.main import app
from pulse.models import Subscription, SubscriptionPlan, SubscriptionStatus, User
from pulse.repositories.postgres import session_scope
from pulse.services import orgs as orgs_service

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


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


async def _create_org() -> uuid.UUID:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "Webhook Test Org", f"webhook-org-{uuid.uuid4().hex[:8]}", user.id
    )
    return org.id


async def _get_subscription(org_id: uuid.UUID) -> Subscription:
    from sqlalchemy import select

    async with session_scope(org_id=org_id) as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.org_id == org_id)
        )
        assert subscription is not None
        return subscription


def _subscription_event(event_type: str, **data_overrides: Any) -> dict[str, Any]:
    # `id` (the provider's subscription id) is unique per call, not a shared
    # literal -- subscriptions.payment_subscription_id has a real unique
    # constraint (two provider subscriptions never share an id), and this
    # module-scoped test file's Postgres rows persist across every test in
    # it, so a hardcoded id here would collide with an earlier test's row.
    data: dict[str, Any] = {
        "id": f"sub_{uuid.uuid4().hex[:16]}",
        "status": "active",
        "metadata": {},
        "current_period_start": 1_700_000_000,
        "current_period_end": 1_702_592_000,
    }
    data.update(data_overrides)
    return {"id": "evt_1", "type": event_type, "data": {"object": data}}


# --- pure event-handling logic -----------------------------------------


async def test_a_subscription_updated_event_upgrades_the_org_to_pro() -> None:
    org_id = await _create_org()
    event = _subscription_event(
        "customer.subscription.updated", status="active", metadata={"org_id": str(org_id)}
    )

    await handle_event(event)

    subscription = await _get_subscription(org_id)
    assert subscription.plan == SubscriptionPlan.PRO
    assert subscription.status == SubscriptionStatus.ACTIVE
    assert subscription.payment_subscription_id == event["data"]["object"]["id"]
    assert subscription.current_period_start is not None
    assert subscription.current_period_end is not None


async def test_a_past_due_event_updates_status_without_downgrading_the_plan() -> None:
    org_id = await _create_org()
    # First, a real activation (as Checkout's webhook would send).
    await handle_event(
        _subscription_event(
            "customer.subscription.updated", status="active", metadata={"org_id": str(org_id)}
        )
    )
    # Then a payment hiccup.
    await handle_event(
        _subscription_event(
            "customer.subscription.updated", status="past_due", metadata={"org_id": str(org_id)}
        )
    )

    subscription = await _get_subscription(org_id)
    assert subscription.status == SubscriptionStatus.PAST_DUE
    assert subscription.plan == SubscriptionPlan.PRO  # not silently downgraded


async def test_a_subscription_deleted_event_reverts_to_free() -> None:
    org_id = await _create_org()
    await handle_event(
        _subscription_event(
            "customer.subscription.updated", status="active", metadata={"org_id": str(org_id)}
        )
    )
    await handle_event(
        _subscription_event("customer.subscription.deleted", metadata={"org_id": str(org_id)})
    )

    subscription = await _get_subscription(org_id)
    assert subscription.status == SubscriptionStatus.CANCELED
    assert subscription.plan == SubscriptionPlan.FREE


async def test_an_event_missing_org_id_metadata_is_ignored_not_raised() -> None:
    event = _subscription_event("customer.subscription.updated", metadata={})
    await handle_event(event)  # must not raise


async def test_an_event_for_an_unknown_org_is_ignored_not_raised() -> None:
    event = _subscription_event(
        "customer.subscription.updated", metadata={"org_id": str(uuid.uuid4())}
    )
    await handle_event(event)  # must not raise


async def test_an_unhandled_event_type_is_a_no_op() -> None:
    event = {"id": "evt_x", "type": "invoice.paid", "data": {"object": {}}}
    await handle_event(event)  # must not raise


# --- the real webhook route: signature verification ---------------------

_SECRET = "whsec_test_secret_for_this_suite_only"


def _sign(payload: bytes, secret: str = _SECRET) -> str:
    """Reproduces Stripe's own signing scheme (t=<ts>,v1=<hmac>) locally --
    verification needs nothing but the shared secret, so this never touches
    a real Stripe account or the network."""
    timestamp = int(time.time())
    signed_payload = f"{timestamp}.{payload.decode()}".encode()
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={signature}"


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture
def configured_webhook_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "stripe_webhook_secret", _SECRET)


async def test_a_validly_signed_webhook_is_processed(configured_webhook_secret: None) -> None:
    org_id = await _create_org()
    payload = json.dumps(
        _subscription_event(
            "customer.subscription.updated", status="active", metadata={"org_id": str(org_id)}
        )
    ).encode()

    async with _client() as client:
        response = await client.post(
            "/api/v1/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": _sign(payload), "Content-Type": "application/json"},
        )

    assert response.status_code == 200
    assert response.json() == {"received": True}
    subscription = await _get_subscription(org_id)
    assert subscription.plan == SubscriptionPlan.PRO


async def test_an_invalid_signature_is_rejected(configured_webhook_secret: None) -> None:
    payload = json.dumps(_subscription_event("customer.subscription.updated")).encode()

    async with _client() as client:
        response = await client.post(
            "/api/v1/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": "t=1,v1=deadbeef", "Content-Type": "application/json"},
        )

    assert response.status_code == 400


async def test_a_tampered_payload_is_rejected_even_with_a_signature_header(
    configured_webhook_secret: None,
) -> None:
    payload = json.dumps(_subscription_event("customer.subscription.updated")).encode()
    signature = _sign(payload)
    tampered = payload + b"tampered"

    async with _client() as client:
        response = await client.post(
            "/api/v1/webhooks/stripe",
            content=tampered,
            headers={"Stripe-Signature": signature, "Content-Type": "application/json"},
        )

    assert response.status_code == 400


async def test_missing_signature_header_is_a_422() -> None:
    async with _client() as client:
        response = await client.post(
            "/api/v1/webhooks/stripe", content=b"{}", headers={"Content-Type": "application/json"}
        )
    assert response.status_code == 422


async def test_webhook_without_a_configured_secret_is_a_clean_503() -> None:
    payload = json.dumps(_subscription_event("customer.subscription.updated")).encode()
    async with _client() as client:
        response = await client.post(
            "/api/v1/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": _sign(payload), "Content-Type": "application/json"},
        )
    assert response.status_code == 503
