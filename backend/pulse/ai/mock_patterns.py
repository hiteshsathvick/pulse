"""Deterministic phrase-matching behind MockProvider (pulse/ai/provider.py).
Free, offline, exercised for real in CI via backend/tests/nl_eval/. This is a
small, fixed set of templates, not a general NL-understanding engine -- every
phrasing it recognizes is listed in the eval fixtures. Anything else falls
through to a clarify response, which is itself a real, tested code path
(SPEC.md Phase 18 DoD: "ambiguous -> clarify"), not a bug in the matcher."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Any


def _today() -> date:
    return datetime.now(UTC).date()


def _clarify() -> dict[str, Any]:
    return {
        "status": "clarify",
        "message": (
            "I'm not sure how to turn that into a trend, funnel, or retention query. "
            'Try something like "how many times did <event> happen last week" or '
            '"funnel from <event> to <event> to <event>".'
        ),
    }


def _resolve_range(phrase: str) -> dict[str, str] | None:
    phrase = phrase.strip().rstrip("?").strip().lower()
    today = _today()

    if phrase == "today":
        return {"from": today.isoformat(), "to": today.isoformat()}
    if phrase == "yesterday":
        d = today - timedelta(days=1)
        return {"from": d.isoformat(), "to": d.isoformat()}
    if phrase in ("last week", "the last week", "the past week"):
        return {"from": (today - timedelta(days=7)).isoformat(), "to": today.isoformat()}
    if phrase in ("last month", "the last month", "the past 30 days", "last 30 days"):
        return {"from": (today - timedelta(days=30)).isoformat(), "to": today.isoformat()}

    match = re.fullmatch(r"(?:the )?last (\d+) days?", phrase)
    if match:
        n = int(match.group(1))
        return {"from": (today - timedelta(days=n)).isoformat(), "to": today.isoformat()}

    match = re.fullmatch(
        r"(?:between |from )?(\d{4}-\d{2}-\d{2}) (?:and|to) (\d{4}-\d{2}-\d{2})", phrase
    )
    if match:
        return {"from": match.group(1), "to": match.group(2)}

    return None


# Anchors the trailing "when" clause to a finite, known set of phrases,
# rather than a generic `(.+)`. A generic capture would leave the regex
# engine free to split a multi-word event name and the time phrase in the
# wrong place (e.g. "onboarding finished last week" ambiguously splitting
# between "onboarding" and "onboarding finished"); anchoring to the exact
# phrases _resolve_range actually understands means the preceding event-name
# group is forced to absorb everything else, unambiguously.
_WHEN = (
    r"(?:today|yesterday"
    r"|last week|the last week|the past week"
    r"|last month|the last month|the past 30 days|last 30 days"
    r"|(?:the )?last \d+ days?"
    r"|(?:between |from )?\d{4}-\d{2}-\d{2} (?:and|to) \d{4}-\d{2}-\d{2})"
)

_TREND_FILTERED = re.compile(
    rf"^how many users completed (.+?) on (\w+) ({_WHEN})\??$", re.IGNORECASE
)
_TREND_UNIQUE = re.compile(
    rf"^how many unique users (?:did|completed|triggered) (.+?) ({_WHEN})\??$", re.IGNORECASE
)
_TREND_COUNT = re.compile(rf"^how many times did (.+?) happen ({_WHEN})\??$", re.IGNORECASE)
_FUNNEL = re.compile(
    r"^(?:show me the )?funnel from (.+?) to (.+?) to (.+?)"
    r"(?: within (\d+) (hours?|days?))?\??$",
    re.IGNORECASE,
)
_RETENTION = re.compile(
    r"^(?:what'?s the )?(day|week)ly retention for users who (.+?) "
    r"and (?:came back to|returned to) (.+?) over (\d+) (?:days?|weeks?)\??$",
    re.IGNORECASE,
)


def match(question: str) -> dict[str, Any]:
    q = question.strip()

    m = _TREND_FILTERED.match(q)
    if m:
        event, platform, when = m.groups()
        date_range = _resolve_range(when)
        if date_range is None:
            return _clarify()
        return {
            "status": "ok",
            "spec": {
                "kind": "trend",
                "version": 1,
                "events": [event.strip()],
                "measure": "unique_users",
                "filters": [{"key": "platform", "op": "eq", "value": platform.strip()}],
                "range": date_range,
                "granularity": "day",
            },
        }

    m = _TREND_UNIQUE.match(q)
    if m:
        event, when = m.groups()
        date_range = _resolve_range(when)
        if date_range is None:
            return _clarify()
        return {
            "status": "ok",
            "spec": {
                "kind": "trend",
                "version": 1,
                "events": [event.strip()],
                "measure": "unique_users",
                "range": date_range,
                "granularity": "day",
            },
        }

    m = _TREND_COUNT.match(q)
    if m:
        event, when = m.groups()
        date_range = _resolve_range(when)
        if date_range is None:
            return _clarify()
        return {
            "status": "ok",
            "spec": {
                "kind": "trend",
                "version": 1,
                "events": [event.strip()],
                "measure": "count",
                "range": date_range,
                "granularity": "day",
            },
        }

    m = _FUNNEL.match(q)
    if m:
        step1, step2, step3, value, unit = m.groups()
        window = (
            {"value": int(value), "unit": unit.rstrip("s")}
            if value
            else {
                "value": 7,
                "unit": "day",
            }
        )
        return {
            "status": "ok",
            "spec": {
                "kind": "funnel",
                "version": 1,
                "steps": [
                    {"event": step1.strip()},
                    {"event": step2.strip()},
                    {"event": step3.strip()},
                ],
                "window": window,
                "range": {
                    "from": (_today() - timedelta(days=30)).isoformat(),
                    "to": _today().isoformat(),
                },
            },
        }

    m = _RETENTION.match(q)
    if m:
        period, born, returned, periods = m.groups()
        return {
            "status": "ok",
            "spec": {
                "kind": "retention",
                "version": 1,
                "born_event": born.strip(),
                "return_event": returned.strip(),
                "period": period.lower(),
                "periods": int(periods),
                "range": {
                    "from": (_today() - timedelta(days=90)).isoformat(),
                    "to": _today().isoformat(),
                },
            },
        }

    return _clarify()
