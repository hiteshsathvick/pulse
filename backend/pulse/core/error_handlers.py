import logging

from fastapi import Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from pulse.core.errors import ErrorDetail, ErrorResponse

logger = logging.getLogger("pulse.errors")

_STATUS_CODE_SLUGS: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE: "payload_too_large",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "validation_error",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
}


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Starlette's add_exception_handler is typed generically over Exception,
    # so it can't express "this handler is only ever called with the type it
    # was registered for" -- true at runtime, just not in the type. Narrow it.
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_CODE_SLUGS.get(exc.status_code, "http_error")
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=str(exc.detail), request_id=_request_id(request))
    )
    return JSONResponse(
        status_code=exc.status_code,
        content=body.model_dump(),
        headers=exc.headers,
    )


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    body = ErrorResponse(
        error=ErrorDetail(
            code="validation_error",
            message="Request validation failed.",
            request_id=_request_id(request),
            fields=jsonable_encoder(exc.errors()),
        )
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=body.model_dump(),
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Never leaks the real exception to the client -- it's logged server-side
    (with the traceback and the request id) and the client gets a generic 500.
    `exc_info=exc` (rather than `logger.exception`, which relies on an ambient
    `sys.exc_info()`) keeps this correct no matter how the handler is invoked."""
    logger.error("unhandled exception", exc_info=exc, extra={"path": request.url.path})
    body = ErrorResponse(
        error=ErrorDetail(
            code="internal_error",
            message="An unexpected error occurred.",
            request_id=_request_id(request),
        )
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=body.model_dump(),
    )
