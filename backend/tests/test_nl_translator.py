import uuid
from pathlib import Path
from typing import Any

import pytest

from alembic import command
from alembic.config import Config
from pulse.ai import provider as ai_provider
from pulse.ai import translator
from pulse.models import User
from pulse.registry.service import RegistryObservation, register_batch
from pulse.repositories.postgres import session_scope
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


async def _create_org_and_project(
    timezone: str = "UTC",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_scope() as session:
        user = User(
            email=f"owner-{uuid.uuid4().hex[:8]}@example.com",
            password_hash="not-a-real-hash",
            name="Owner",
        )
        session.add(user)
        await session.commit()

    org = await orgs_service.create_organization(
        "NL Test Org", f"nl-org-{uuid.uuid4().hex[:8]}", user.id
    )
    project = await projects_service.create_project(org.id, "Web", "web", timezone, user.id)
    return org.id, project.id, user.id


class _FakeProvider(ai_provider.LLMProvider):
    """Stands in for a real provider so translator.py's parsing/validation
    can be tested against exact, crafted outputs -- including malformed ones
    a mock/live provider would never produce on its own."""

    name = "fake"

    def __init__(self, response: dict[str, Any] | BaseException) -> None:
        self._response = response

    async def translate(self, *, system: str, question: str) -> dict[str, Any]:
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


def _patch_provider(
    monkeypatch: pytest.MonkeyPatch, response: dict[str, Any] | BaseException
) -> None:
    from pulse.ai import translator as translator_module

    monkeypatch.setattr(translator_module, "get_provider", lambda settings: _FakeProvider(response))


async def test_raises_project_not_found_for_an_unknown_project() -> None:
    org_id, _project_id, _user_id = await _create_org_and_project()
    with pytest.raises(translator.ProjectNotFound):
        await translator.translate_question("how many signups", org_id, uuid.uuid4())


async def test_mock_provider_translates_a_known_phrasing() -> None:
    """The default provider (Settings.ai_provider == "mock") end to end --
    no monkeypatching, the exact path a real request takes."""
    org_id, project_id, _user_id = await _create_org_and_project()
    result = await translator.translate_question(
        "how many times did checkout completed happen today", org_id, project_id
    )
    assert result.status == "ok"
    assert result.spec is not None
    assert result.spec.kind == "trend"
    assert result.spec.events == ["checkout completed"]


async def test_unrecognized_phrasing_clarifies_instead_of_guessing() -> None:
    org_id, project_id, _user_id = await _create_org_and_project()
    result = await translator.translate_question(
        "what's the weather like today", org_id, project_id
    )
    assert result.status == "clarify"
    assert result.spec is None
    assert result.message


async def test_grounding_warns_about_an_unregistered_event_but_still_returns_a_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, project_id, _user_id = await _create_org_and_project()
    await register_batch(
        [
            RegistryObservation(
                org_id=org_id,
                project_id=project_id,
                event_name="checkout completed",
                raw_properties={},
            )
        ]
    )
    _patch_provider(
        monkeypatch,
        {
            "status": "ok",
            "spec": {
                "kind": "trend",
                "version": 1,
                "events": ["totally made up event"],
                "measure": "count",
                "range": {"from": "2026-01-01", "to": "2026-01-31"},
            },
        },
    )
    result = await translator.translate_question("how many made up events", org_id, project_id)
    assert result.status == "ok"
    assert result.warnings and "totally made up event" in result.warnings[0]


async def test_a_registered_event_produces_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    org_id, project_id, _user_id = await _create_org_and_project()
    await register_batch(
        [
            RegistryObservation(
                org_id=org_id,
                project_id=project_id,
                event_name="checkout completed",
                raw_properties={},
            )
        ]
    )
    _patch_provider(
        monkeypatch,
        {
            "status": "ok",
            "spec": {
                "kind": "trend",
                "version": 1,
                "events": ["checkout completed"],
                "measure": "count",
                "range": {"from": "2026-01-01", "to": "2026-01-31"},
            },
        },
    )
    result = await translator.translate_question("checkouts this month", org_id, project_id)
    assert result.status == "ok"
    assert result.warnings == []


async def test_a_malformed_spec_from_the_provider_fails_translation_not_silently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, project_id, _user_id = await _create_org_and_project()
    _patch_provider(
        monkeypatch,
        {"status": "ok", "spec": {"kind": "trend", "events": []}},  # missing required fields
    )
    with pytest.raises(translator.TranslationFailed):
        await translator.translate_question("bad spec please", org_id, project_id)


async def test_a_spec_with_an_unknown_kind_fails_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, project_id, _user_id = await _create_org_and_project()
    _patch_provider(
        monkeypatch,
        {"status": "ok", "spec": {"kind": "raw_sql", "query": "SELECT * FROM events"}},
    )
    with pytest.raises(translator.TranslationFailed):
        await translator.translate_question("give me raw sql", org_id, project_id)


async def test_a_non_object_response_fails_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    org_id, project_id, _user_id = await _create_org_and_project()

    class _NonDictProvider(ai_provider.LLMProvider):
        name = "fake"

        async def translate(self, *, system: str, question: str) -> Any:
            return "not a dict"

    monkeypatch.setattr(translator, "get_provider", lambda settings: _NonDictProvider())
    with pytest.raises(translator.TranslationFailed):
        await translator.translate_question("anything", org_id, project_id)


async def test_a_clarify_response_without_a_message_fails_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, project_id, _user_id = await _create_org_and_project()
    _patch_provider(monkeypatch, {"status": "clarify"})
    with pytest.raises(translator.TranslationFailed):
        await translator.translate_question("anything", org_id, project_id)


async def test_provider_error_is_wrapped_as_translation_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, project_id, _user_id = await _create_org_and_project()
    _patch_provider(monkeypatch, ai_provider.LLMProviderError("network exploded"))
    with pytest.raises(translator.TranslationFailed):
        await translator.translate_question("anything", org_id, project_id)


async def test_a_spec_with_a_smuggled_tenant_field_drops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A spec type has no org_id/project_id field at all (SPEC.md #6.8) -- if
    a provider ever tried to smuggle one in, it must never survive
    validation as something the caller could act on."""
    org_id, project_id, _user_id = await _create_org_and_project()
    _patch_provider(
        monkeypatch,
        {
            "status": "ok",
            "spec": {
                "kind": "trend",
                "version": 1,
                "events": ["checkout completed"],
                "measure": "count",
                "range": {"from": "2026-01-01", "to": "2026-01-31"},
                "org_id": str(uuid.uuid4()),
            },
        },
    )
    result = await translator.translate_question("checkouts", org_id, project_id)
    assert result.status == "ok"
    assert not hasattr(result.spec, "org_id")
