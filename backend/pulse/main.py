from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from pulse.api.auth import router as auth_router
from pulse.api.health import router as health_router
from pulse.api.invites import invites_router, org_invites_router
from pulse.api.keys import router as keys_router
from pulse.api.orgs import router as orgs_router
from pulse.api.projects import router as projects_router
from pulse.api.schema_registry import router as schema_registry_router
from pulse.core.config import get_settings
from pulse.core.error_handlers import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from pulse.core.logging import configure_logging
from pulse.core.middleware import RequestIdMiddleware
from pulse.ingest.router import router as ingest_router
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
# Applied app-wide for simplicity rather than scoped to just /ingest --
# Starlette's CORSMiddleware has no per-route scoping, and hand-rolling one
# to avoid opening CORS on the JWT-bearer console endpoints would reinvent
# preflight handling for no real gain: nothing in this app relies on cookies,
# so a browser attaches no ambient credentials a cross-origin script could
# ride on regardless of which endpoint it targets. /ingest (write-key bearer
# auth, meant to be called from arbitrary customer domains) is the actual
# reason this exists.
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().ingest_cors_allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)

app.include_router(health_router)
app.include_router(ingest_router)
app.include_router(auth_router)
app.include_router(orgs_router)
app.include_router(projects_router)
app.include_router(org_invites_router)
app.include_router(invites_router)
app.include_router(keys_router)
app.include_router(schema_registry_router)
