import json
import logging

from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from pulse.core.error_handlers import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)


def _request(request_id: str | None = "req-abc") -> Request:
    request = Request(scope={"type": "http", "method": "GET", "path": "/whatever", "headers": []})
    if request_id is not None:
        request.state.request_id = request_id
    return request


async def test_http_exception_handler_wraps_detail_in_the_envelope() -> None:
    exc = StarletteHTTPException(status_code=404, detail="project not found")

    response = await http_exception_handler(_request(), exc)

    assert response.status_code == 404
    body = _body(response)
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == "project not found"
    assert body["error"]["request_id"] == "req-abc"


async def test_http_exception_handler_falls_back_to_generic_code_for_unmapped_status() -> None:
    exc = StarletteHTTPException(status_code=418, detail="I'm a teapot")

    response = await http_exception_handler(_request(), exc)

    assert response.status_code == 418
    assert _body(response)["error"]["code"] == "http_error"


async def test_validation_exception_handler_returns_422_with_field_errors() -> None:
    exc = RequestValidationError(
        errors=[
            {
                "type": "missing",
                "loc": ("body", "event_name"),
                "msg": "Field required",
                "input": {},
            }
        ]
    )

    response = await validation_exception_handler(_request(), exc)

    assert response.status_code == 422
    body = _body(response)
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["fields"][0]["loc"] == ["body", "event_name"]


async def test_unhandled_exception_handler_never_leaks_the_real_message(caplog) -> None:
    caplog.set_level(logging.ERROR, logger="pulse.errors")

    response = await unhandled_exception_handler(_request(), ValueError("db password is hunter2"))

    assert response.status_code == 500
    body = _body(response)
    assert body["error"]["code"] == "internal_error"
    assert "hunter2" not in body["error"]["message"]
    # but it IS logged server-side, with the traceback, for debugging
    assert any("hunter2" in r.getMessage() or "hunter2" in str(r.exc_info) for r in caplog.records)


def _body(response: JSONResponse) -> dict[str, object]:
    result: dict[str, object] = json.loads(response.body)
    return result
