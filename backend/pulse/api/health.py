from fastapi import APIRouter
from fastapi.responses import JSONResponse

from pulse.repositories import clickhouse, postgres
from pulse.repositories import redis as redis_repo

router = APIRouter()

_CHECKS = (
    ("postgres", postgres.check_connection),
    ("clickhouse", clickhouse.check_connection),
    ("redis", redis_repo.check_connection),
)


@router.get("/health")
async def health() -> JSONResponse:
    """Verifies connectivity to every backing store. Never raises: a store being down
    is a 503, not a 500."""
    checks: dict[str, str] = {}
    healthy = True

    for name, check in _CHECKS:
        try:
            await check()
            checks[name] = "ok"
        except Exception as exc:
            checks[name] = f"error: {exc}"
            healthy = False

    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "error", "checks": checks},
    )
