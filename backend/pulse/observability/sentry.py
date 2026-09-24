"""Phase 23 error tracking. Env-gated (confirmed with the user first): with
SENTRY_DSN unset -- the default -- sentry_sdk is never initialized, so there
is no network call and no capture, exactly like an unset Stripe or Anthropic
key. Plug a DSN in whenever a Sentry project exists; nothing else changes.

Privacy is configured on purpose, not left to defaults. This app ingests
arbitrary customer event payloads and accepts passwords on /auth, and Sentry's
FastAPI integration would by default attach request bodies to an error event
and ship them to a third party -- undoing the PII controls from Phase 22 at
the moment something goes wrong. Bodies are therefore disabled outright, and
default PII collection is off.

Local variables are off too. The SDK captures the value of every local in each
stack frame by default, and in this codebase a frame is exactly where an event
batch, a request body or a password lives (a variable named `body`, `batch`,
`properties`). Its built-in key denylist only redacts obvious names like
`password`, so relying on it would leak the rest. Stack traces and messages
are still captured -- what's lost is only the variable values, the price of
not sending customer data to a third party."""

from __future__ import annotations

import logging
from typing import Any

import sentry_sdk

from pulse.core.config import Settings

logger = logging.getLogger("pulse.observability.sentry")


def init_sentry(service_name: str, settings: Settings, **overrides: Any) -> bool:
    """Returns whether Sentry was actually initialized. `overrides` exists for
    tests (a capturing `transport`), never used by real entrypoints."""
    if not settings.sentry_dsn:
        return False
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        send_default_pii=False,
        max_request_body_size="never",
        include_local_variables=False,
        server_name=service_name,
        **overrides,
    )
    sentry_sdk.set_tag("service", service_name)
    logger.info("sentry: initialized for %s (%s)", service_name, settings.sentry_environment)
    return True
