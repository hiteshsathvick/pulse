"""Pure unit tests for pulse.billing.providers -- no database, no network,
no external account. MockPaymentProvider is the default (confirmed with the
user first), so this is the path every fresh clone and CI run exercises."""

from pulse.billing.providers import MockPaymentProvider, get_payment_provider
from pulse.core.config import Settings


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg,arg-type]


async def test_get_payment_provider_defaults_to_mock() -> None:
    provider = get_payment_provider(_settings())
    assert provider.name == "mock"
    assert isinstance(provider, MockPaymentProvider)


async def test_mock_create_customer_returns_a_distinct_id_each_time() -> None:
    provider = MockPaymentProvider(_settings())
    a = await provider.create_customer(org_id="org-1", org_name="Acme", email=None)
    b = await provider.create_customer(org_id="org-1", org_name="Acme", email=None)
    assert a != b
    assert a.startswith("mock_cus_")


async def test_mock_checkout_and_portal_both_point_at_the_local_billing_page() -> None:
    settings = _settings(frontend_base_url="http://localhost:3000")
    provider = MockPaymentProvider(settings)
    checkout_url = await provider.create_checkout_session(customer_id="mock_cus_1", org_id="org-1")
    portal_url = await provider.create_portal_session(customer_id="mock_cus_1", org_id="org-1")
    assert checkout_url == "http://localhost:3000/orgs/org-1/billing/mock-checkout"
    assert portal_url == checkout_url


async def test_mock_checkout_url_respects_a_different_frontend_base_url() -> None:
    settings = _settings(frontend_base_url="https://app.example.com")
    provider = MockPaymentProvider(settings)
    url = await provider.create_checkout_session(customer_id="mock_cus_1", org_id="org-1")
    assert url == "https://app.example.com/orgs/org-1/billing/mock-checkout"


async def test_mock_list_invoices_uses_the_configured_price() -> None:
    settings = _settings(mock_pro_price_cents=1234)
    provider = MockPaymentProvider(settings)
    invoices = await provider.list_invoices(customer_id="mock_cus_1")
    assert len(invoices) == 1
    assert invoices[0].amount_due == 1234
    assert invoices[0].currency == "usd"
    assert invoices[0].status == "paid"


async def test_mock_list_invoices_respects_the_limit() -> None:
    provider = MockPaymentProvider(_settings())
    invoices = await provider.list_invoices(customer_id="mock_cus_1", limit=0)
    assert invoices == []
