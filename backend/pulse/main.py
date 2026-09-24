from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from starlette.exceptions import HTTPException as StarletteHTTPException

from pulse.api.alerts import router as alerts_router
from pulse.api.auth import router as auth_router
from pulse.api.billing import router as billing_router
from pulse.api.billing import webhook_router as billing_webhook_router
from pulse.api.dashboards import router as dashboards_router
from pulse.api.deletion import router as deletion_router
from pulse.api.export import router as export_router
from pulse.api.health import router as health_router
from pulse.api.insights import router as insights_router
from pulse.api.invites import invites_router, org_invites_router
from pulse.api.keys import router as keys_router
from pulse.api.orgs import router as orgs_router
from pulse.api.pii_rules import router as pii_rules_router
from pulse.api.projects import router as projects_router
from pulse.api.query import router as query_router
from pulse.api.schema_registry import router as schema_registry_router
from pulse.api.webhooks import router as webhooks_router
from pulse.core.config import get_settings
from pulse.core.error_handlers import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from pulse.core.logging import configure_logging
from pulse.core.middleware import RequestIdMiddleware, SecurityHeadersMiddleware
from pulse.ingest.router import router as ingest_router
from pulse.observability.metrics import MetricsMiddleware
from pulse.observability.setup import setup_observability
from pulse.observability.tracing import get_provider
from pulse.repositories import clickhouse, postgres
from pulse.repositories import redis as redis_repo

configure_logging()
setup_observability("pulse-api")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await clickhouse.close()
    await redis_repo.close()
    await postgres.close()


app = FastAPI(title="Pulse API", lifespan=lifespan)

app.add_middleware(RequestIdMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(MetricsMiddleware)
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
app.include_router(query_router)
app.include_router(insights_router)
app.include_router(dashboards_router)
app.include_router(alerts_router)
app.include_router(billing_router)
app.include_router(billing_webhook_router)
app.include_router(webhooks_router)
app.include_router(export_router)
app.include_router(pii_rules_router)
app.include_router(deletion_router)

# Phase 23: after every router is registered. Reads an incoming `traceparent`
# (an SDK's, or an upstream proxy's) and starts a server span from it; /health
# is excluded so a load balancer's polling doesn't flood the trace backend.
FastAPIInstrumentor.instrument_app(app, tracer_provider=get_provider(), excluded_urls="health")
