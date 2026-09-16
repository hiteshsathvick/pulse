from pulse.core.config import get_settings
from pulse.repositories.redis import get_client


class RateLimitExceeded(Exception):
    pass


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
