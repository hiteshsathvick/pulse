"""SPEC.md Phase 18 DoD: "a fixed eval set of NL->spec pairs gates
regressions." Two tests share tests/nl_eval/cases.py:

- test_mock_eval_set_meets_its_accuracy_threshold runs unconditionally in
  CI against the free, deterministic MockProvider -- this is the harness
  actually gating every push, at 100% (mock matching is exact-or-broken,
  not approximate).
- test_live_eval_set_meets_its_accuracy_threshold is skipped unless you set
  PULSE_TEST_LLM_LIVE=1 and ANTHROPIC_API_KEY, so you can run the same
  fixtures against the real model locally whenever you want a true accuracy
  check, without spending anything or risking flakiness in ordinary CI runs.
"""

import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from alembic import command
from alembic.config import Config
from pulse.ai import translator
from pulse.core.config import Settings, get_settings
from pulse.models import User
from pulse.repositories.postgres import session_scope
from pulse.services import orgs as orgs_service
from pulse.services import projects as projects_service
from tests.nl_eval.cases import CASES

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_MOCK_THRESHOLD = 1.0
_LIVE_THRESHOLD = 0.8


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


async def _create_org_and_project() -> tuple[uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        user = User(
            email=f"eval-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Eval",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "Eval Org", f"eval-org-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", "UTC", user.id)
    return org.id, project.id


async def _run_eval(settings: Settings | None = None) -> list[tuple[str, bool, str]]:
    """Returns (question, passed, detail) for every case -- printed by the
    caller on failure so a regression is diagnosable from CI output alone,
    not just a bare pass/fail count."""
    org_id, project_id = await _create_org_and_project()
    results: list[tuple[str, bool, str]] = []

    for case in CASES:
        try:
            result = await translator.translate_question(
                case.question, org_id, project_id, settings=settings
            )
        except translator.TranslationFailed as exc:
            results.append((case.question, False, f"translation failed: {exc}"))
            continue

        if result.status != case.expected_status:
            detail = f"expected status={case.expected_status!r}, got {result.status!r}"
            results.append((case.question, False, detail))
            continue

        if case.expected_status == "ok":
            assert result.spec is not None
            if case.check is not None and not case.check(result.spec):
                results.append((case.question, False, f"spec shape mismatch: {result.spec!r}"))
                continue

        results.append((case.question, True, "ok"))

    return results


def _assert_threshold(results: list[tuple[str, bool, str]], threshold: float) -> None:
    passed = sum(1 for _, ok, _ in results if ok)
    accuracy = passed / len(results)
    failures = "\n".join(f"  - {q!r}: {detail}" for q, ok, detail in results if not ok)
    assert accuracy >= threshold, (
        f"NL eval accuracy {accuracy:.0%} ({passed}/{len(results)}) is below the "
        f"{threshold:.0%} threshold. Failures:\n{failures}"
    )


async def test_mock_eval_set_meets_its_accuracy_threshold() -> None:
    results = await _run_eval()
    _assert_threshold(results, _MOCK_THRESHOLD)


_live_requested = os.environ.get("PULSE_TEST_LLM_LIVE") == "1"
_live_configured = bool(get_settings().anthropic_api_key)


@pytest.mark.llm_live
@pytest.mark.skipif(
    not (_live_requested and _live_configured),
    reason=(
        "set PULSE_TEST_LLM_LIVE=1 and ANTHROPIC_API_KEY to run the NL eval set "
        "against the real Anthropic provider"
    ),
)
async def test_live_eval_set_meets_its_accuracy_threshold() -> None:
    live_settings = get_settings().model_copy(update={"ai_provider": "anthropic"})
    results = await _run_eval(settings=live_settings)
    _assert_threshold(results, _LIVE_THRESHOLD)
