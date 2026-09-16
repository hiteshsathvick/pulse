from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from pulse.api.health import router as health_router
from pulse.core.error_handlers import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from pulse.core.logging import configure_logging
from pulse.core.middleware import RequestIdMiddleware
from pulse.repositories import clickhouse, postgres
from pulse.repositories import redis as redis_repo

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await clickhouse.close()
    await redis_repo.close()
    await postgres.close()


app = FastAPI(title="Pulse API", lifespan=lifespan)

app.add_middleware(RequestIdMiddleware)

app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(health_router)
