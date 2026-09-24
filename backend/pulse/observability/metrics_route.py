"""Phase 24: the API's own `/metrics`, for deployments where a separate metrics
port isn't reachable.

Locally the API serves metrics on a dedicated port (`METRICS_PORT`), off the
public surface. On Render only a web service's primary HTTP port is reachable over
the private network, so there the API's metrics have to live on that port. A
metrics endpoint leaks route names and traffic shape, and that port is public, so
the route is:

  * **absent** (a plain 404, indistinguishable from any unknown path) unless
    `METRICS_TOKEN` is set -- the default, and what every existing deployment gets;
  * otherwise **bearer-token only**, compared in constant time.

Workers don't need this: they serve `/metrics` on their own port as private
services."""

import hmac

from fastapi import APIRouter, Header, HTTPException, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

from pulse.core.config import get_settings

router = APIRouter(include_in_schema=False)


@router.get("/metrics")
async def metrics(authorization: str | None = Header(default=None)) -> Response:
    token = get_settings().metrics_token
    if not token:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")

    scheme, _, presented = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented.encode(), token.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing metrics token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
