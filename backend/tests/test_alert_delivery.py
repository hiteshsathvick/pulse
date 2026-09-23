"""Delivery-honored tests for pulse.alerts.delivery (SPEC.md #6.16 DoD:
"delivery honored"). Never touches a real network or a real mail server --
ConsoleEmailProvider does no I/O by design, and the webhook sender is
tested against a mocked httpx transport, never a live endpoint. Alert rows
here are plain in-memory model instances, never persisted -- delivery.py
only ever reads a handful of attributes off them."""

import json
import uuid
from typing import Any

import httpx
import pytest

from pulse.alerts import delivery
from pulse.alerts.rules import AlertChannels
from pulse.core.config import get_settings
from pulse.models import Alert


def _fake_alert(**overrides: Any) -> Alert:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "org_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "insight_id": uuid.uuid4(),
        "name": "Test alert",
        "rule": {},
        "channels": {},
        "created_by": uuid.uuid4(),
    }
    defaults.update(overrides)
    return Alert(**defaults)


class _RecordingTransport(httpx.MockTransport):
    def __init__(self, status_code: int = 200) -> None:
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return httpx.Response(status_code)

        super().__init__(handler)


def _patch_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.BaseTransport) -> None:
    """No code in pulse/alerts/delivery.py accepts an injectable transport --
    it just constructs httpx.AsyncClient(...) itself, matching how every
    real caller will use it. Patching AsyncClient.__init__ to always attach
    a mock transport tests the real code path without a real network."""
    original_init = httpx.AsyncClient.__init__

    def patched_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = transport
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


async def test_console_email_provider_reports_success_without_any_network_call() -> None:
    provider = delivery.ConsoleEmailProvider()
    assert await provider.send(to=["a@example.com"], subject="hi", body="body") is True


async def test_deliver_sends_email_when_the_channel_is_configured() -> None:
    alert = _fake_alert()
    channels = AlertChannels(email=["a@example.com"], in_app=False)
    delivered = await delivery.deliver(alert, "the metric moved", channels)
    assert delivered == {"email": "sent"}


async def test_an_email_provider_exception_is_recorded_as_failed_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BoomProvider(delivery.EmailProvider):
        name = "boom"

        async def send(self, *, to: list[str], subject: str, body: str) -> bool:
            raise RuntimeError("smtp exploded")

    monkeypatch.setattr(delivery, "get_email_provider", lambda: _BoomProvider())
    alert = _fake_alert()
    channels = AlertChannels(email=["a@example.com"], in_app=False)

    delivered = await delivery.deliver(alert, "message", channels)
    assert delivered == {"email": "failed"}


async def test_deliver_records_in_app_with_no_network_call_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _RecordingTransport()
    _patch_transport(monkeypatch, transport)
    alert = _fake_alert()
    channels = AlertChannels(in_app=True)

    delivered = await delivery.deliver(alert, "the metric moved", channels)
    assert delivered == {"in_app": "recorded"}
    assert transport.requests == []


async def test_deliver_sends_a_webhook_and_records_success(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _RecordingTransport(status_code=200)
    _patch_transport(monkeypatch, transport)
    alert = _fake_alert(name="Checkout drop")
    channels = AlertChannels(webhook_url="https://example.com/hook", in_app=False)

    delivered = await delivery.deliver(alert, "checkouts dropped", channels)

    assert delivered == {"webhook": "sent"}
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert str(request.url) == "https://example.com/hook"
    body = json.loads(request.content)
    assert body == {
        "alert_id": str(alert.id),
        "alert_name": "Checkout drop",
        "message": "checkouts dropped",
    }


async def test_deliver_records_failure_on_a_non_2xx_webhook_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "alert_webhook_retry_backoff_seconds", 0.0)
    transport = _RecordingTransport(status_code=500)
    _patch_transport(monkeypatch, transport)
    alert = _fake_alert()
    channels = AlertChannels(webhook_url="https://example.com/hook", in_app=False)

    delivered = await delivery.deliver(alert, "message", channels)
    assert delivered == {"webhook": "failed"}


async def test_deliver_records_failure_without_raising_on_a_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "alert_webhook_retry_backoff_seconds", 0.0)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    _patch_transport(monkeypatch, httpx.MockTransport(handler))
    alert = _fake_alert()
    channels = AlertChannels(webhook_url="https://example.com/hook", in_app=False)

    delivered = await delivery.deliver(alert, "message", channels)
    assert delivered == {"webhook": "failed"}


async def test_a_webhook_secret_signs_the_payload_and_the_signature_verifies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "alert_webhook_secret", "shh-its-a-secret")
    transport = _RecordingTransport()
    _patch_transport(monkeypatch, transport)
    alert = _fake_alert()
    channels = AlertChannels(webhook_url="https://example.com/hook", in_app=False)

    await delivery.deliver(alert, "message", channels)

    request = transport.requests[0]
    signature = request.headers["X-Pulse-Signature"]
    assert signature == delivery.sign_webhook_payload(request.content, "shh-its-a-secret")


async def test_no_webhook_secret_means_no_signature_header(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "alert_webhook_secret", None)
    transport = _RecordingTransport()
    _patch_transport(monkeypatch, transport)
    alert = _fake_alert()
    channels = AlertChannels(webhook_url="https://example.com/hook", in_app=False)

    await delivery.deliver(alert, "message", channels)
    assert "X-Pulse-Signature" not in transport.requests[0].headers


async def test_email_and_webhook_and_in_app_all_deliver_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _RecordingTransport(status_code=200)
    _patch_transport(monkeypatch, transport)
    alert = _fake_alert()
    channels = AlertChannels(
        email=["a@example.com"], webhook_url="https://example.com/hook", in_app=True
    )

    delivered = await delivery.deliver(alert, "message", channels)
    assert delivered == {"email": "sent", "webhook": "sent", "in_app": "recorded"}


class _FlakyTransport(httpx.MockTransport):
    """Fails with a transient error/status the first `fail_times` requests,
    then succeeds -- for proving retry actually recovers, not just that it
    eventually gives up."""

    def __init__(self, fail_times: int, fail_status: int | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._remaining_failures = fail_times
        self._fail_status = fail_status

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if self._remaining_failures > 0:
                self._remaining_failures -= 1
                if self._fail_status is not None:
                    return httpx.Response(self._fail_status)
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200)

        super().__init__(handler)


async def test_a_connection_error_retries_and_eventually_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "alert_webhook_retry_backoff_seconds", 0.0)
    transport = _FlakyTransport(fail_times=2)
    _patch_transport(monkeypatch, transport)

    result = await delivery.send_webhook_with_result("https://example.com/hook", {"a": 1})

    assert result == delivery.WebhookDeliveryResult(delivered=True, status_code=200)
    assert len(transport.requests) == 3


async def test_a_5xx_response_retries_and_eventually_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "alert_webhook_retry_backoff_seconds", 0.0)
    transport = _FlakyTransport(fail_times=2, fail_status=503)
    _patch_transport(monkeypatch, transport)

    result = await delivery.send_webhook_with_result("https://example.com/hook", {"a": 1})

    assert result == delivery.WebhookDeliveryResult(delivered=True, status_code=200)
    assert len(transport.requests) == 3


async def test_a_4xx_response_fails_immediately_without_retrying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "alert_webhook_retry_backoff_seconds", 0.0)
    transport = _RecordingTransport(status_code=400)
    _patch_transport(monkeypatch, transport)

    result = await delivery.send_webhook_with_result("https://example.com/hook", {"a": 1})

    assert result == delivery.WebhookDeliveryResult(delivered=False, status_code=400)
    assert len(transport.requests) == 1


async def test_exhausting_all_retries_reports_failure_with_the_last_status_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "alert_webhook_retry_backoff_seconds", 0.0)
    monkeypatch.setattr(settings, "alert_webhook_max_retries", 2)
    transport = _RecordingTransport(status_code=503)
    _patch_transport(monkeypatch, transport)

    result = await delivery.send_webhook_with_result("https://example.com/hook", {"a": 1})

    assert result == delivery.WebhookDeliveryResult(delivered=False, status_code=503)
    assert len(transport.requests) == 3  # 1 initial attempt + 2 retries


async def test_exhausting_all_retries_on_connection_errors_reports_no_status_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "alert_webhook_retry_backoff_seconds", 0.0)
    monkeypatch.setattr(settings, "alert_webhook_max_retries", 1)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    _patch_transport(monkeypatch, httpx.MockTransport(handler))

    result = await delivery.send_webhook_with_result("https://example.com/hook", {"a": 1})

    assert result == delivery.WebhookDeliveryResult(delivered=False, status_code=None)
