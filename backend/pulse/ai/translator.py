"""Phase 18: NL -> InsightSpec, never NL -> SQL (SPEC.md #6.15). The only
thing a provider's output is ever turned into is one of pulse.query.spec's
existing, already-tenant-safe spec types -- validated by the exact same
Pydantic union pulse/api/insights.py uses for a human-built spec. Nothing
here ever touches ClickHouse or Postgres write paths; the resulting spec
still has to go through the ordinary /trend, /funnel, /retention endpoints
(and their existing caps, cache, and rate limiting) to actually run."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, ValidationError

from pulse.ai.provider import LLMProviderError, get_provider
from pulse.ai.schema import build_system_prompt
from pulse.core.config import Settings, get_settings
from pulse.models import SchemaStatus
from pulse.query.spec import DiscriminatedInsightSpec, FunnelSpec, InsightSpec, TrendSpec
from pulse.registry import service as registry_service
from pulse.services import projects as projects_service

# How much of a project's taxonomy is embedded in the prompt. A real
# question is overwhelmingly likely to reference one of the most-seen
# events; an enormous project (thousands of registered event names) would
# otherwise blow the model's context for marginal benefit.
_MAX_GROUNDING_EVENTS = 200
_MAX_MESSAGE_LENGTH = 500

_spec_adapter: TypeAdapter[Any] = TypeAdapter(DiscriminatedInsightSpec)


class ProjectNotFound(Exception):
    pass


class TranslationFailed(Exception):
    """The provider's output could not be trusted. The message is always a
    generic, safe-to-show string -- never the provider's raw output, so a
    crafted response can't use this as a channel back to the caller."""


@dataclass
class NLQueryResult:
    status: Literal["ok", "clarify"]
    spec: InsightSpec | None = None
    message: str | None = None
    warnings: list[str] = field(default_factory=list)


async def translate_question(
    question: str,
    org_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    settings: Settings | None = None,
) -> NLQueryResult:
    settings = settings or get_settings()

    project = await projects_service.get_project(org_id, project_id)
    if project is None:
        raise ProjectNotFound()

    events = await registry_service.list_events(org_id, project_id)
    active_events = [e for e in events if e.status == SchemaStatus.ACTIVE]
    known_event_names = {e.event_name for e in active_events}

    grounding_events = [
        (e.event_name, e.volume_estimate)
        for e in sorted(active_events, key=lambda e: -e.volume_estimate)[:_MAX_GROUNDING_EVENTS]
    ]
    today = datetime.now(ZoneInfo(project.timezone)).date()
    system = build_system_prompt(
        grounding_events=grounding_events, project_timezone=project.timezone, today=today
    )

    try:
        provider = get_provider(settings)
        raw = await provider.translate(system=system, question=question)
    except LLMProviderError as exc:
        raise TranslationFailed(str(exc)) from exc

    return _parse_result(raw, known_event_names)


def _parse_result(raw: Any, known_event_names: set[str]) -> NLQueryResult:
    if not isinstance(raw, dict):
        raise TranslationFailed("The model returned a response that wasn't a JSON object.")

    status = raw.get("status")

    if status == "clarify":
        message = raw.get("message")
        if not isinstance(message, str) or not message.strip():
            raise TranslationFailed("The model's clarify response was missing a message.")
        return NLQueryResult(status="clarify", message=message.strip()[:_MAX_MESSAGE_LENGTH])

    if status != "ok":
        raise TranslationFailed(f"The model returned an unrecognized status ({status!r}).")

    spec_payload = raw.get("spec")
    if not isinstance(spec_payload, dict):
        raise TranslationFailed("The model's 'ok' response was missing a spec.")

    try:
        spec = _spec_adapter.validate_python(spec_payload)
    except ValidationError as exc:
        raise TranslationFailed("The model produced an insight spec that didn't validate.") from exc

    warnings = _grounding_warnings(spec, known_event_names)
    return NLQueryResult(status="ok", spec=spec, warnings=warnings)


def _spec_event_names(spec: InsightSpec) -> list[str]:
    if isinstance(spec, TrendSpec):
        return list(spec.events)
    if isinstance(spec, FunnelSpec):
        return [step.event for step in spec.steps]
    return [spec.born_event, spec.return_event]


def _grounding_warnings(spec: InsightSpec, known_event_names: set[str]) -> list[str]:
    """Soft, non-blocking -- an event name outside the registry is still a
    valid spec (Phase 9's own precedent: "unknown events still ingest"), so
    this never rejects, only flags it for the user to double-check before
    running."""
    warnings: list[str] = []
    seen: set[str] = set()
    for name in _spec_event_names(spec):
        if name in known_event_names or name in seen:
            continue
        seen.add(name)
        warnings.append(
            f"\"{name}\" hasn't been recorded in this project's event history yet -- the "
            "query will still run, but check the spelling if this wasn't intentional."
        )
    return warnings
