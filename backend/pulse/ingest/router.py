from fastapi import APIRouter, Depends, HTTPException, Request, status

from pulse.api.dependencies import require_write_key
from pulse.billing import service as billing_service
from pulse.core import rate_limit
from pulse.core.config import get_settings
from pulse.ingest.schemas import IngestBatchRequest, IngestBatchResponse
from pulse.ingest.service import buffer_batch
from pulse.models import ApiKey
from pulse.observability.metrics import INGEST_ACCEPTED
from pulse.repositories.redis import get_client

router = APIRouter(prefix="/ingest", tags=["ingest"])


async def _enforce_max_body_size(request: Request) -> None:
    """Checked against the Content-Length header rather than a body read --
    cheap, and doesn't race FastAPI's own body parsing. A client that lies
    about Content-Length (or omits it under chunked transfer) isn't caught
    here; the batch-count cap below is the second, independent guard."""
    settings = get_settings()
    content_length = request.headers.get("content-length")
    if content_length is not None and int(content_length) > settings.ingest_max_body_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"Request body exceeds the {settings.ingest_max_body_bytes}-byte limit",
        )


@router.post(
    "",
    response_model=IngestBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(_enforce_max_body_size)],
)
async def ingest(
    body: IngestBatchRequest,
    api_key: ApiKey = Depends(require_write_key),
) -> IngestBatchResponse:
    """Validates lightly and buffers -- never a synchronous ClickHouse write
    (SPEC.md #3.2). Durability from here on is the Redis Stream's job, not
    this request's; Phase 8's workers are the only thing that ever writes to
    ClickHouse."""
    settings = get_settings()

    try:
        await rate_limit.check_ingest_rate_limit(str(api_key.id))
    except rate_limit.RateLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many ingest requests -- slow down and retry shortly",
        ) from exc

    if len(body.batch) > settings.ingest_max_batch_size:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Batch exceeds the {settings.ingest_max_batch_size}-event limit",
        )

    # Phase 20: a request-rate limiter (above) and a monthly-volume quota
    # are different resources -- this reads the billing-worker's last
    # computed usage, not a live ClickHouse query on every call.
    quota = await billing_service.check_ingest_quota(api_key.org_id)
    if quota.level == billing_service.QuotaLevel.HARD:
        raise HTTPException(status_code=status.HTTP_402_PAYMENT_REQUIRED, detail=quota.message)

    accepted = await buffer_batch(get_client(), settings.ingest_stream_key, api_key, body.batch)
    INGEST_ACCEPTED.inc(accepted)
    return IngestBatchResponse(
        accepted=accepted,
        quota_warning=quota.message if quota.level == billing_service.QuotaLevel.SOFT else None,
    )
