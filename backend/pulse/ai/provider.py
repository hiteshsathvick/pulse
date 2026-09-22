"""LLMProvider abstraction for Phase 18's NL-to-query translation (SPEC.md
#2: "AI: provider abstraction (OpenAI/Anthropic/local)"). Pulse has no
sibling project to import this from, so it's rebuilt here to the same idea:
translator.py depends only on this interface, never on a concrete SDK, and
the real provider is a single setting away from being swapped or turned off.

Only one thing ever crosses this boundary: a JSON-shaped dict matching
`submit_insight_query`'s tool arguments (pulse/ai/schema.py). A provider is
not trusted with anything more than producing that dict -- deciding whether
it's actually a valid InsightSpec happens one layer up, in translator.py.
"""

from __future__ import annotations

import abc
from typing import Any

from pulse.ai.schema import SUBMIT_INSIGHT_QUERY_TOOL
from pulse.core.config import Settings


class LLMProviderError(Exception):
    """The provider could not produce a tool call at all: a network/API
    error, or the model declining to call the tool. Both are handled
    identically by the caller (translator.py) -- there is nothing to
    recover from mid-request either way."""


class LLMProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def translate(self, *, system: str, question: str) -> dict[str, Any]:
        """Returns the raw dict a provider produced for the
        submit_insight_query tool call. Never pre-validated here --
        translator.py is the only place that decides whether it's a real
        InsightSpec, so every provider implementation is held to the exact
        same standard."""


class MockProvider(LLMProvider):
    """Deterministic, free, no network call -- the default, matching
    Settings.ai_provider's default and this codebase's cost-aware posture.
    Pattern-matches a small, fixed set of phrasings (pulse/ai/mock_patterns.py)
    so the translator, the API route, and the eval harness are all exercised
    for real in CI without spending anything or depending on network access.
    Not a general NL-understanding engine: a question outside its known
    patterns gets a clarify response, which is itself a real, tested outcome
    of this phase, not a failure of the mock."""

    name = "mock"

    async def translate(self, *, system: str, question: str) -> dict[str, Any]:
        from pulse.ai import mock_patterns

        return mock_patterns.match(question)


class AnthropicProvider(LLMProvider):
    """The real provider -- only ever constructed when Settings.ai_provider
    is "anthropic" (pulse/ai/provider.py's get_provider), and only ever
    called from a live pytest.mark.llm_live run or real traffic, never from
    the default CI suite. `tool_choice` forces the one tool, so a
    prompt-injection attempt inside the question has no free-text channel to
    answer through in the first place -- the JSON it returns is still fully
    re-validated by translator.py regardless, since a constrained tool call
    is a strong hint, not a proof, that the arguments are well-formed."""

    name = "anthropic"

    def __init__(self, settings: Settings) -> None:
        if not settings.anthropic_api_key:
            raise LLMProviderError("ANTHROPIC_API_KEY is not configured")
        # Imported lazily so the mock-only (default) path never needs the
        # `anthropic` package importable at process startup.
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = settings.ai_model
        self._max_tokens = settings.ai_max_output_tokens

    async def translate(self, *, system: str, question: str) -> dict[str, Any]:
        import anthropic

        try:
            # SUBMIT_INSIGHT_QUERY_TOOL/tool_choice are plain dicts (schema.py
            # keeps this module provider-agnostic) rather than the SDK's own
            # ToolParam/ToolChoiceToolParam TypedDicts, which the SDK accepts
            # at runtime but mypy can't match against its typed overloads.
            response = await self._client.messages.create(  # type: ignore[call-overload]
                model=self._model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": question}],
                tools=[SUBMIT_INSIGHT_QUERY_TOOL],
                tool_choice={"type": "tool", "name": SUBMIT_INSIGHT_QUERY_TOOL["name"]},
            )
        except anthropic.AnthropicError as exc:
            raise LLMProviderError(str(exc)) from exc

        for block in response.content:
            if block.type == "tool_use" and block.name == SUBMIT_INSIGHT_QUERY_TOOL["name"]:
                input_value = block.input
                if not isinstance(input_value, dict):
                    raise LLMProviderError("tool call arguments were not a JSON object")
                return input_value
        raise LLMProviderError("model did not call submit_insight_query")


def get_provider(settings: Settings) -> LLMProvider:
    if settings.ai_provider == "anthropic":
        return AnthropicProvider(settings)
    return MockProvider()
