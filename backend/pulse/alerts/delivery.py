"""Delivery for a fired alert (pulse/alerts/evaluate.py). The email side
mirrors pulse/ai/provider.py's shape: an ABC + a default that does no real
network I/O, so CI and every test run for free without a mail server.
Confirmed with the user first: only a console/log EmailProvider ships this
phase -- a real SMTP provider is a documented follow-up once one is
actually chosen, the same deferral Phase 4's invite emails already made
(pulse/services/invites.py). The webhook sender IS real: an outbound POST
needs no new infra, and it's HMAC-signed (X-Pulse-Signature: sha256=...,
the Stripe/GitHub convention) since there's no existing signing pattern in
this codebase to mirror -- this establishes one. In-app delivery has no
send step at all; the AlertEvent row pulse.alerts.evaluate writes right
after this returns IS the in-app notification."""

from __future__ import annotations

import abc
import asyncio
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from pulse.alerts.rules import AlertChannels
from pulse.core.config import get_settings
from pulse.models import Alert

logger = logging.getLogger("pulse.alerts.delivery")


class EmailProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def send(self, *, to: list[str], subject: str, body: str) -> bool:
        """True if the provider considers the message delivered."""


class ConsoleEmailProvider(EmailProvider):
    """The only provider that ships this phase -- logs instead of sending,
    exactly like invites' raw-token-instead-of-an-email stand-in (Phase 4:
    "no email provider chosen yet"). Always reports success: it can only
    speak to whether the app did its part, never to a real inbox's outcome,
    which this provider has no way to observe."""

    name = "console"

    async def send(self, *, to: list[str], subject: str, body: str) -> bool:
        logger.info("alert email (console provider) to=%s subject=%s\n%s", to, subject, body)
        return True


def get_email_provider() -> EmailProvider:
    return ConsoleEmailProvider()


def sign_webhook_payload(payload: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


@dataclass
class WebhookDeliveryResult:
    delivered: bool
    status_code: int | None


async def send_webhook_with_result(url: str, payload: dict[str, Any]) -> WebhookDeliveryResult:
    """One attempt, then retries with doubling backoff -- but only on a
    transient failure (timeout, connection error, 5xx). A 4xx means the
    receiver itself rejected the request; retrying the identical payload
    won't change that, so it fails immediately instead of wasting attempts.
    `status_code` is the last response actually received (None if every
    attempt raised, e.g. connection refused/timeout throughout)."""
    settings = get_settings()
    body = json.dumps(payload, sort_keys=True).encode()
    headers = {"Content-Type": "application/json"}
    if settings.alert_webhook_secret:
        headers["X-Pulse-Signature"] = sign_webhook_payload(body, settings.alert_webhook_secret)

    attempts = settings.alert_webhook_max_retries + 1
    backoff = settings.alert_webhook_retry_backoff_seconds
    last_status: int | None = None
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.alert_webhook_timeout_seconds) as client:
                response = await client.post(url, content=body, headers=headers)
            last_status = response.status_code
            if response.status_code < 300:
                return WebhookDeliveryResult(delivered=True, status_code=last_status)
            if response.status_code < 500:
                logger.warning(
                    "webhook delivery to %s rejected with %s, not retrying",
                    url,
                    response.status_code,
                )
                return WebhookDeliveryResult(delivered=False, status_code=last_status)
        except httpx.HTTPError:
            logger.warning(
                "webhook delivery attempt %s/%s failed for %s",
                attempt,
                attempts,
                url,
                exc_info=True,
            )

        if attempt < attempts:
            await asyncio.sleep(backoff * (2 ** (attempt - 1)))

    logger.warning("webhook delivery to %s failed after %s attempts", url, attempts)
    return WebhookDeliveryResult(delivered=False, status_code=last_status)


async def _send_webhook(url: str, payload: dict[str, Any]) -> bool:
    return (await send_webhook_with_result(url, payload)).delivered


async def deliver(alert: Alert, message: str, channels: AlertChannels) -> dict[str, str]:
    """Returns a per-channel status dict, stored verbatim on the AlertEvent
    row (`delivered`). Never raises: a delivery failure on one channel must
    not stop the others, or stop the fire from being recorded at all --
    "delivery honored" is a claim about what was attempted and reported, not
    a guarantee every channel always succeeds."""
    delivered: dict[str, str] = {}

    if channels.email:
        try:
            ok = await get_email_provider().send(
                to=channels.email, subject=f"Pulse alert: {alert.name}", body=message
            )
            delivered["email"] = "sent" if ok else "failed"
        except Exception:
            logger.exception("email delivery failed for alert %s", alert.id)
            delivered["email"] = "failed"

    if channels.webhook_url:
        ok = await _send_webhook(
            channels.webhook_url,
            {"alert_id": str(alert.id), "alert_name": alert.name, "message": message},
        )
        delivered["webhook"] = "sent" if ok else "failed"

    if channels.in_app:
        delivered["in_app"] = "recorded"

    return delivered
