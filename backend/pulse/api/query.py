import csv
import io
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from pulse.ai import translator as ai_translator
from pulse.api.dependencies import resolve_query_scope
from pulse.core.rate_limit import RateLimitExceeded, check_ai_rate_limit
from pulse.query import service as query_service
from pulse.query.spec import DiscriminatedInsightSpec, FunnelSpec, RetentionSpec, TrendSpec

QueryFormat = Literal["json", "csv"]


def _csv_response(results: list[dict[str, object]], filename: str) -> Response:
    """SPEC.md's "export of query results" -- these results are already
    small and fully materialized (unlike raw-event export, which streams),
    so an ordinary in-memory CSV body is enough; no new route needed, since
    this just adds a response format to the routes that already compute the
    result. Column set comes from the first row: every row in one result
    list is the same shape (one insight kind per call)."""
    fieldnames = list(results[0].keys()) if results else []
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(results)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


router = APIRouter(
    prefix="/api/v1/orgs/{org_id}/projects/{project_id}/query",
    tags=["query"],
    dependencies=[Depends(resolve_query_scope)],
)


def _too_many_queries(exc: RateLimitExceeded) -> HTTPException:
    wait = exc.retry_after
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            "Too many queries for this organization right now"
            + (f" -- try again in {wait} seconds." if wait else " -- try again shortly.")
        ),
        headers={"Retry-After": str(wait)} if wait else None,
    )


def _too_many_ai_requests(exc: RateLimitExceeded) -> HTTPException:
    wait = exc.retry_after
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            "Too many natural-language questions for this organization right now"
            + (f" -- try again in {wait} seconds." if wait else " -- try again shortly.")
        ),
        headers={"Retry-After": str(wait)} if wait else None,
    )


class TrendResponse(BaseModel):
    results: list[dict[str, object]]
    cached: bool
    # "rollup" if answered from the hourly rollup, "raw" if from the events table.
    source: str
    # True when a unique-user count is the rollup's approximate sketch (large windows).
    approximate: bool


class FunnelResponse(BaseModel):
    results: list[dict[str, object]]
    cached: bool


class RetentionResponse(BaseModel):
    results: list[dict[str, object]]
    cached: bool


class NLQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class NLQueryResponse(BaseModel):
    # "ok": `spec` is set, shown to the user before they run it themselves
    # against /trend, /funnel, or /retention -- this endpoint only
    # translates, it never executes (SPEC.md #6.15's "interpreted spec shown
    # before running").
    # "clarify": `message` is set instead -- the question couldn't be turned
    # into one of the three insight kinds confidently enough to guess.
    status: str
    spec: DiscriminatedInsightSpec | None = None
    message: str | None = None
    warnings: list[str] = Field(default_factory=list)


@router.post("/trend", response_model=TrendResponse)
async def query_trend(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    spec: TrendSpec,
    refresh: bool = False,
    format: QueryFormat = "json",
) -> TrendResponse | Response:
    try:
        result = await query_service.run_trend(spec, org_id, project_id, refresh=refresh)
    except RateLimitExceeded as exc:
        raise _too_many_queries(exc) from exc
    except query_service.QueryTooExpensive as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except query_service.ProjectNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        ) from exc
    if format == "csv":
        return _csv_response(result.results, "trend.csv")
    return TrendResponse(
        results=result.results,
        cached=result.cached,
        source=result.source,
        approximate=result.approximate,
    )


@router.post("/funnel", response_model=FunnelResponse)
async def query_funnel(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    spec: FunnelSpec,
    refresh: bool = False,
    format: QueryFormat = "json",
) -> FunnelResponse | Response:
    try:
        result = await query_service.run_funnel(spec, org_id, project_id, refresh=refresh)
    except RateLimitExceeded as exc:
        raise _too_many_queries(exc) from exc
    except query_service.QueryTooExpensive as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except query_service.ProjectNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        ) from exc
    if format == "csv":
        return _csv_response(result.results, "funnel.csv")
    return FunnelResponse(results=result.results, cached=result.cached)


@router.post("/retention", response_model=RetentionResponse)
async def query_retention(
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    spec: RetentionSpec,
    refresh: bool = False,
    format: QueryFormat = "json",
) -> RetentionResponse | Response:
    try:
        result = await query_service.run_retention(spec, org_id, project_id, refresh=refresh)
    except RateLimitExceeded as exc:
        raise _too_many_queries(exc) from exc
    except query_service.QueryTooExpensive as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
    except query_service.ProjectNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        ) from exc
    if format == "csv":
        return _csv_response(result.results, "retention.csv")
    return RetentionResponse(results=result.results, cached=result.cached)


@router.post("/nl", response_model=NLQueryResponse)
async def query_nl(
    org_id: uuid.UUID, project_id: uuid.UUID, body: NLQueryRequest
) -> NLQueryResponse:
    """Translates only -- never executes. The caller runs the returned spec
    itself against /trend, /funnel, or /retention once they've seen it,
    which is what makes "interpreted spec shown before running" literally
    true rather than a flag on a single auto-run call."""
    try:
        await check_ai_rate_limit(str(org_id))
    except RateLimitExceeded as exc:
        raise _too_many_ai_requests(exc) from exc

    try:
        result = await ai_translator.translate_question(body.question, org_id, project_id)
    except ai_translator.ProjectNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Project not found"
        ) from exc
    except ai_translator.TranslationFailed as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    return NLQueryResponse(
        status=result.status, spec=result.spec, message=result.message, warnings=result.warnings
    )
