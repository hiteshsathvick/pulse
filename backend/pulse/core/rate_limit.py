from pulse.core.config import get_settings
from pulse.repositories.redis import get_client


class RateLimitExceeded(Exception):
    def __init__(self, retry_after: int | None = None) -> None:
        super().__init__()
        # Seconds until the window resets, when the limiter knows it.
        self.retry_after = retry_after


async def check_login_rate_limit(identifier: str) -> None:
    """A fixed-window counter in Redis (already a dependency -- no reason to
    pull in a rate-limiting library for this), keyed by client IP. Counts
    every attempt, not just failures: a generous threshold (default 5 per 5
    minutes) means a legitimate user mistyping their password once or twice
    is unaffected, and it protects the endpoint itself, not just wrong-
    password floods."""
    settings = get_settings()
    client = get_client()
    key = f"auth:login_attempts:{identifier}"

    count = await client.incr(key)
    if count == 1:
        await client.expire(key, settings.auth_rate_limit_window_seconds)

    if count > settings.auth_rate_limit_max_attempts:
        raise RateLimitExceeded()


async def check_ingest_rate_limit(api_key_id: str) -> None:
    """Same fixed-window pattern as check_login_rate_limit, but keyed by the
    write key's id rather than client IP -- ingestion traffic for many
    customers can share an IP (CDNs, corporate NAT), and the resource being
    protected is per-project buffer capacity, not a single client's login
    attempts."""
    settings = get_settings()
    client = get_client()
    key = f"ingest:requests:{api_key_id}"

    count = await client.incr(key)
    if count == 1:
        await client.expire(key, settings.ingest_rate_limit_window_seconds)

    if count > settings.ingest_rate_limit_max_requests:
        raise RateLimitExceeded()


async def check_query_rate_limit(org_id: str) -> None:
    """Per-tenant limit on queries that actually run against ClickHouse (the
    caller invokes this only after a cache miss, so cache hits are free).
    Keyed by org -- the tenant -- not by user or API key, so one organization
    can't crowd out the others no matter how many users or keys it has, and
    isn't rewarded for splitting its load across them. Same fixed-window
    Redis counter as the other limiters here.

    Unlike them, it reports how long until the window resets, so the client
    can be told when to retry."""
    settings = get_settings()
    client = get_client()
    key = f"query:executions:{org_id}"

    count = await client.incr(key)
    if count == 1:
        await client.expire(key, settings.query_rate_limit_window_seconds)

    if count > settings.query_rate_limit_max_queries:
        remaining = await client.ttl(key)
        if remaining < 0:
            # The key has no expiry (a crash between INCR and EXPIRE): without
            # this it would block the org forever. Re-arm it.
            await client.expire(key, settings.query_rate_limit_window_seconds)
            remaining = settings.query_rate_limit_window_seconds
        raise RateLimitExceeded(retry_after=max(remaining, 1))
