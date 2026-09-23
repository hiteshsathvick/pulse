"""Payment provider abstraction (SPEC.md #6.18) -- mirrors pulse/ai/provider.py's
shape exactly: an ABC + a Mock implementation (the default, needing zero
external account) + a real Stripe implementation kept behind the same
interface for later. Confirmed with the user first: Stripe test mode needed
a real account this session couldn't set up, and a paid/external dependency
wasn't wanted for this piece either -- so `MockPaymentProvider` is the
default and the only one CI or a fresh clone ever exercises. Real Stripe is
a swap-in (`Settings.payment_provider = "stripe"`), not a requirement."""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from pulse.core.config import Settings


@dataclass(frozen=True)
class Invoice:
    id: str
    status: str | None
    amount_due: int
    currency: str
    hosted_invoice_url: str | None
    created: int


class PaymentProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def create_customer(self, *, org_id: str, org_name: str, email: str | None) -> str:
        """Returns a provider-specific customer id, stored on
        Subscription.payment_customer_id."""

    @abc.abstractmethod
    async def create_checkout_session(self, *, customer_id: str, org_id: str) -> str:
        """Returns a URL the caller redirects to -- a provider-hosted page
        for a real provider, or one of Pulse's own pages for the mock."""

    @abc.abstractmethod
    async def create_portal_session(self, *, customer_id: str, org_id: str) -> str:
        """Returns a URL for managing an existing subscription (plan
        changes, cancellation, payment method)."""

    @abc.abstractmethod
    async def list_invoices(self, *, customer_id: str, limit: int = 20) -> list[Invoice]: ...


class MockPaymentProvider(PaymentProvider):
    """No network, no external account, ever. Checkout and Portal both
    point back at Pulse's own mock-checkout page (a local, in-app page that
    simulates the hosted redirect a real processor would show) instead of a
    processor-hosted URL -- confirming a subscription there calls
    POST .../billing/mock/subscribe, which applies the exact same
    event-application logic (pulse/billing/webhooks.py::handle_event) a
    real payment processor's webhook would eventually trigger, so that one
    piece of logic is never duplicated between "real" and "mock" paths."""

    name = "mock"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def create_customer(self, *, org_id: str, org_name: str, email: str | None) -> str:
        return f"mock_cus_{uuid.uuid4().hex[:16]}"

    async def create_checkout_session(self, *, customer_id: str, org_id: str) -> str:
        return f"{self._settings.frontend_base_url}/orgs/{org_id}/billing/mock-checkout"

    async def create_portal_session(self, *, customer_id: str, org_id: str) -> str:
        # The same page doubles as "manage" -- it shows an Upgrade or a
        # Cancel action depending on the org's current plan, so there's no
        # separate mock "portal" page to maintain in parallel.
        return f"{self._settings.frontend_base_url}/orgs/{org_id}/billing/mock-checkout"

    async def list_invoices(self, *, customer_id: str, limit: int = 20) -> list[Invoice]:
        # One representative invoice for the current period -- there's no
        # real billing history to synthesize beyond what the org's own
        # Subscription already records, and the point is to prove the
        # "list invoices" UI path works, not to model real invoice history.
        now = datetime.now(UTC)
        return [
            Invoice(
                id=f"mock_in_{uuid.uuid4().hex[:16]}",
                status="paid",
                amount_due=self._settings.mock_pro_price_cents,
                currency="usd",
                hosted_invoice_url=None,
                created=int(now.timestamp()),
            )
        ][:limit]


def get_payment_provider(settings: Settings) -> PaymentProvider:
    if settings.payment_provider == "stripe":
        from pulse.billing.stripe_client import StripePaymentProvider

        return StripePaymentProvider(settings)
    return MockPaymentProvider(settings)
