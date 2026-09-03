"""Hybrid LLM-primary + keyword-fallback extractor.

Coverage:
- LLM success returns the LLM value + provider label
- LLM timeout / error → keyword fallback
- Empty utterance → immediate None (no LLM call)
- Provider=None → keyword fallback directly
- Whitelist hint per slot name matches conversation.state.Slot values
- HybridExtractorStats counters increment correctly
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import pytest

from fg_voice.extraction.hybrid_extractor import (
    HybridExtractor,
    HybridExtractorStats,
    build_provider,
)
from fg_voice.extraction.llm_extractor import (
    LlmExtractionResult,
    LlmProviderError,
)

# ─── Fake providers ────────────────────────────────────────────────


@dataclass
class _ScriptedLlmProvider:
    """Returns queued responses in order. Empty queue → raise."""

    responses: list[LlmExtractionResult | Exception]
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def extract(self, slot: str, utterance: str) -> LlmExtractionResult:
        self.calls.append((slot, utterance))
        if not self.responses:
            raise LlmProviderError("no more scripted responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@dataclass
class _SlowLlmProvider:
    """Sleeps forever so the hybrid extractor's timeout must fire."""

    async def extract(self, slot: str, utterance: str) -> LlmExtractionResult:
        await asyncio.sleep(10)
        # Never reached; placate the return type.
        return LlmExtractionResult(slot=slot, value=None, confidence=0.0, provider="anthropic")


# ─── Happy path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_success_returns_llm_value() -> None:
    provider = _ScriptedLlmProvider(
        responses=[
            LlmExtractionResult(slot="intent", value="yes", confidence=0.92, provider="anthropic")
        ]
    )
    hybrid = HybridExtractor(provider=provider)
    result = await hybrid.extract_slot("intent", "yes I am reporting")
    assert result.value == "yes"
    assert result.provider == "anthropic"
    assert result.confidence == pytest.approx(0.92)
    assert hybrid.stats.llm_successes == 1
    assert hybrid.stats.llm_failures == 0


# ─── Fallbacks ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_error_falls_back_to_keyword() -> None:
    provider = _ScriptedLlmProvider(responses=[LlmProviderError("provider down")])
    hybrid = HybridExtractor(provider=provider)
    result = await hybrid.extract_slot("intent", "yes I am")
    # Keyword rule extracts "yes" from "yes I am".
    assert result.value == "yes"
    assert result.provider == "keyword_fallback"
    assert hybrid.stats.llm_failures == 1
    assert hybrid.stats.keyword_hits == 1


@pytest.mark.asyncio
async def test_llm_timeout_falls_back_to_keyword() -> None:
    hybrid = HybridExtractor(provider=_SlowLlmProvider(), timeout_ms=50)
    result = await hybrid.extract_slot("intent", "yes I am reporting")
    assert result.provider == "keyword_fallback"
    assert result.value == "yes"
    assert hybrid.stats.llm_failures == 1


@pytest.mark.asyncio
async def test_llm_value_none_falls_through_to_keyword() -> None:
    """When the LLM reports value=None (no confidence), the hybrid
    extractor MUST still try the keyword rule — the LLM might have
    missed a match the rule catches."""
    provider = _ScriptedLlmProvider(
        responses=[
            LlmExtractionResult(slot="intent", value=None, confidence=0.0, provider="anthropic")
        ]
    )
    hybrid = HybridExtractor(provider=provider)
    result = await hybrid.extract_slot("intent", "yes")
    # Keyword catches "yes".
    assert result.value == "yes"
    assert result.provider == "keyword_fallback"


@pytest.mark.asyncio
async def test_provider_none_uses_keyword_directly() -> None:
    hybrid = HybridExtractor(provider=None)
    result = await hybrid.extract_slot("severity", "moderate")
    assert result.value == "moderate"
    assert result.provider == "keyword_fallback"
    # No LLM stats moved.
    assert hybrid.stats.llm_successes == 0
    assert hybrid.stats.llm_failures == 0


@pytest.mark.asyncio
async def test_empty_utterance_short_circuits() -> None:
    provider = _ScriptedLlmProvider(responses=[])
    hybrid = HybridExtractor(provider=provider)
    result = await hybrid.extract_slot("intent", "   ")
    assert result.value is None
    assert result.provider == "keyword_fallback"
    # No LLM call happened.
    assert provider.calls == []


# ─── Free-text slots use passthrough ───────────────────────────────


@pytest.mark.asyncio
async def test_description_free_text_passthrough_via_keyword_fallback() -> None:
    """description/location aren't in the keyword rule dispatch —
    the fallback just returns the utterance."""
    hybrid = HybridExtractor(provider=None)
    result = await hybrid.extract_slot("description", "trees are down everywhere")
    assert result.value == "trees are down everywhere"
    assert result.confidence == pytest.approx(0.6)


# ─── Stats ─────────────────────────────────────────────────────────


def test_stats_default_zero() -> None:
    stats = HybridExtractorStats()
    assert stats.llm_successes == 0
    assert stats.llm_failures == 0
    assert stats.keyword_hits == 0
    assert stats.keyword_unclear == 0


# ─── Builder ───────────────────────────────────────────────────────


def test_build_provider_none_type_returns_none() -> None:
    assert build_provider(provider_type="none") is None


def test_build_provider_anthropic_without_key_returns_none() -> None:
    """Empty ANTHROPIC_API_KEY → None, pipeline still runs via
    keyword fallback rather than crashing at boot."""
    assert build_provider(provider_type="anthropic", anthropic_api_key="") is None


def test_build_provider_bedrock_without_model_returns_none() -> None:
    assert build_provider(provider_type="bedrock", bedrock_model_id="") is None
