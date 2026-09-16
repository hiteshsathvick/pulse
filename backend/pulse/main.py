from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from pulse.api.health import router as health_router
from pulse.repositories import clickhouse, postgres
from pulse.repositories import redis as redis_repo


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    yield
    await clickhouse.close()
    await redis_repo.close()
    await postgres.close()


app = FastAPI(title="Pulse API", lifespan=lifespan)

app.include_router(health_router)
