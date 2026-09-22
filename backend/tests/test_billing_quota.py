"""SPEC.md Phase 20 DoD: "quota enforcement." Tests the real /ingest
endpoint's soft/hard quota behavior. UsageRecord rows are written directly
via Postgres (bypassing ClickHouse/the billing-worker, both already covered
by test_billing_usage.py) so this file can drive exact quota-boundary
scenarios without actually ingesting thousands of real events."""

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from alembic import command
from alembic.config import Config
from pulse.billing.plans import PLANS, Plan
from pulse.billing.service import current_period
from pulse.core.config import get_settings
from pulse.main import app
from pulse.models import ApiKeyType, Subscription, SubscriptionPlan, UsageRecord, User
from pulse.repositories.postgres import session_scope
from pulse.repositories.redis import get_client as get_redis_client
from pulse.services import api_keys as api_keys_service
from pulse.services import orgs as orgs_service
from pulse.services import projects as projects_service

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def _alembic_config() -> Config:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return config


@pytest.fixture(scope="module", autouse=True)
def _control_plane_schema() -> Any:
    config = _alembic_config()
    command.upgrade(config, "head")
    yield
    command.downgrade(config, "base")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _small_free_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tiny quota (10 events) so tests don't need to ingest thousands of
    real events to cross soft/hard thresholds."""
    monkeypatch.setitem(PLANS, SubscriptionPlan.FREE, Plan(SubscriptionPlan.FREE, "Free", 10))


async def _org_project_and_key() -> tuple[uuid.UUID, uuid.UUID, str]:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "Quota Test Org", f"quota-org-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", "UTC", user.id)
    _, raw_key = await api_keys_service.create_api_key(
        org.id, project.id, ApiKeyType.WRITE, user.id
    )
    return org.id, project.id, raw_key


async def _set_usage(org_id: uuid.UUID, events_ingested: int) -> None:
    period = current_period()
    async with session_scope(org_id=org_id) as session:
        session.add(
            UsageRecord(
                org_id=org_id,
                period=period,
                events_ingested=events_ingested,
                mtu=events_ingested,
            )
        )
        await session.commit()


def _event() -> dict[str, str]:
    return {"event_id": str(uuid.uuid4()), "event": "button clicked", "user_id": "u1"}


async def test_no_usage_yet_is_ok() -> None:
    _, _, write_key = await _org_project_and_key()
    async with _client() as client:
        response = await client.post(
            "/ingest", json={"batch": [_event()]}, headers={"X-API-Key": write_key}
        )
    assert response.status_code == 202
    assert response.json()["quota_warning"] is None


async def test_usage_below_the_soft_threshold_is_ok() -> None:
    org_id, _, write_key = await _org_project_and_key()
    await _set_usage(org_id, 5)  # 50% of quota=10; soft threshold is 80%
    async with _client() as client:
        response = await client.post(
            "/ingest", json={"batch": [_event()]}, headers={"X-API-Key": write_key}
        )
    assert response.status_code == 202
    assert response.json()["quota_warning"] is None


async def test_usage_past_the_soft_threshold_warns_but_still_accepts() -> None:
    org_id, _, write_key = await _org_project_and_key()
    await _set_usage(org_id, 8)  # 80% of quota=10
    async with _client() as client:
        response = await client.post(
            "/ingest", json={"batch": [_event()]}, headers={"X-API-Key": write_key}
        )
    assert response.status_code == 202
    assert response.json()["accepted"] == 1
    assert "8 of 10" in response.json()["quota_warning"]


async def test_usage_at_the_hard_limit_rejects_with_402() -> None:
    org_id, _, write_key = await _org_project_and_key()
    await _set_usage(org_id, 10)  # == quota
    async with _client() as client:
        response = await client.post(
            "/ingest", json={"batch": [_event()]}, headers={"X-API-Key": write_key}
        )
    assert response.status_code == 402
    assert "quota" in response.json()["error"]["message"].lower()


async def test_usage_over_the_hard_limit_rejects_with_402() -> None:
    org_id, _, write_key = await _org_project_and_key()
    await _set_usage(org_id, 15)
    async with _client() as client:
        response = await client.post(
            "/ingest", json={"batch": [_event()]}, headers={"X-API-Key": write_key}
        )
    assert response.status_code == 402


async def test_a_rejected_batch_never_reaches_the_buffer() -> None:
    org_id, _, write_key = await _org_project_and_key()
    settings = get_settings()
    redis_client = get_redis_client()
    before = await redis_client.xlen(settings.ingest_stream_key)

    await _set_usage(org_id, 10)
    async with _client() as client:
        await client.post("/ingest", json={"batch": [_event()]}, headers={"X-API-Key": write_key})

    after = await redis_client.xlen(settings.ingest_stream_key)
    assert after == before


async def test_each_organization_has_its_own_quota() -> None:
    org_a, _, key_a = await _org_project_and_key()
    _, _, key_b = await _org_project_and_key()
    await _set_usage(org_a, 10)  # org A is at its hard limit; org B has none yet

    async with _client() as client:
        blocked = await client.post(
            "/ingest", json={"batch": [_event()]}, headers={"X-API-Key": key_a}
        )
        allowed = await client.post(
            "/ingest", json={"batch": [_event()]}, headers={"X-API-Key": key_b}
        )

    assert blocked.status_code == 402
    assert allowed.status_code == 202


async def test_a_pro_plan_is_not_yet_limited_by_the_frees_quota() -> None:
    """Proves the check reads the org's own plan, not a hardcoded number --
    upgrades the org's Subscription to pro directly (bypassing Stripe
    entirely, which test_billing_webhooks.py covers) and confirms the same
    usage that hard-limits a free org doesn't limit a pro one."""
    org_id, _, write_key = await _org_project_and_key()
    async with session_scope(org_id=org_id) as session:
        subscription = await session.scalar(
            select(Subscription).where(Subscription.org_id == org_id)
        )
        assert subscription is not None
        subscription.plan = SubscriptionPlan.PRO
        await session.commit()

    await _set_usage(org_id, 10)  # would hard-limit a free org
    async with _client() as client:
        response = await client.post(
            "/ingest", json={"batch": [_event()]}, headers={"X-API-Key": write_key}
        )
    assert response.status_code == 202
