"""Hybrid extractor — LLM primary, keyword rules fallback.

Contract: same shape as `keyword_rules.extract_*` — utterance in,
canonical value / None out. When `Settings.in_call_llm_enabled=True`
the LLM provider is tried first with a hard timeout; on any failure
(timeout, provider error, invalid JSON) the keyword rule fires as
backup. This preserves the CLAUDE.md invariant that the caller
never hears "sorry, I couldn't process that" — the ladder always
has a fallback.

The runner calls `HybridExtractor.extract_slot(slot, utterance)`
which is async (needs to await the LLM provider). The old sync
`nodes.run_extractor` still exists for the keyword-only path;
this module doesn't replace it, it adds a superset that the runner
uses when the flag is on.

Providers:
- **primary**: `LlmProvider` (Bedrock or Anthropic per config)
- **fallback**: functions from `extraction.keyword_rules`

The dispatch mapping between slot name → keyword function lives
here (not in the LLM module) so the LLM module stays pure-provider.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fg_voice.extraction import keyword_rules
from fg_voice.extraction.llm_extractor import (
    IN_CALL_LLM_TIMEOUT_MS,
    LlmExtractionResult,
    LlmProvider,
    LlmProviderError,
    extract_with_timeout,
)
from fg_voice.extraction.normalize import depth_to_cm
from fg_voice.obs.logging import get_logger

log = get_logger(__name__)


# ─── Keyword-fallback dispatch ──────────────────────────────────────


# slot name (matches conversation.state.Slot values) → keyword rule
# function returning an object with `.value` / `.confidence` /
# `.evidence` attrs. String slot names to preserve the layered
# contract (extraction/ can't import from conversation/).
_KEYWORD_DISPATCH: dict[str, Callable[[str], Any]] = {
    "intent": keyword_rules.extract_intent,
    "hazard_type": keyword_rules.extract_hazard_type,
    "severity": keyword_rules.extract_severity,
    "confirmation": keyword_rules.extract_confirmation,
    "depth": keyword_rules.extract_depth,
}


# ─── Hybrid extractor ───────────────────────────────────────────────


@dataclass(slots=True)
class HybridExtractor:
    """LLM-primary extractor with keyword fallback. Instance per
    process (safe to share across calls — no per-call state)."""

    provider: LlmProvider | None
    timeout_ms: int = IN_CALL_LLM_TIMEOUT_MS
    stats: HybridExtractorStats = field(default_factory=lambda: HybridExtractorStats())

    async def extract_slot(self, slot: str, utterance: str) -> LlmExtractionResult:
        """Try LLM; on ANY failure, fall back to the keyword rule.

        Returns a `LlmExtractionResult` in both cases — the caller
        (runner) doesn't need to know which path fired. `provider`
        field on the result attributes the source for telemetry."""
        if not utterance or not utterance.strip():
            return LlmExtractionResult(
                slot=slot, value=None, confidence=0.0, provider="keyword_fallback"
            )

        if self.provider is not None:
            try:
                result = await extract_with_timeout(
                    self.provider, slot, utterance, timeout_ms=self.timeout_ms
                )
                # Only accept the LLM result when it named a value.
                # A `value=None` LLM result means "the utterance
                # didn't answer this slot"; falling through to the
                # keyword rule may still recover a match.
                if result.value is not None:
                    self.stats.llm_successes += 1
                    return result
            except LlmProviderError as exc:
                self.stats.llm_failures += 1
                log.info(
                    "hybrid.llm_failed_falling_back",
                    slot=slot,
                    error=str(exc),
                )

        # Keyword fallback path.
        return _keyword_fallback(slot, utterance, self.stats)


def _keyword_fallback(
    slot: str, utterance: str, stats: HybridExtractorStats
) -> LlmExtractionResult:
    """Delegate to `keyword_rules.extract_*` and shape as a
    LlmExtractionResult so the caller path is uniform."""
    fn = _KEYWORD_DISPATCH.get(slot)
    if fn is None:
        # No keyword rule for this slot (description/location) — the
        # runner separately does free-text passthrough.
        return LlmExtractionResult(
            slot=slot, value=utterance.strip(), confidence=0.6, provider="keyword_fallback"
        )
    extraction = fn(utterance)
    value: str | int | None
    if getattr(extraction, "value", None) in (None, "unclear"):
        stats.keyword_unclear += 1
        return LlmExtractionResult(
            slot=slot, value=None, confidence=0.0, provider="keyword_fallback"
        )
    # Depth needs cm normalisation.
    if slot == "depth":
        try:
            value = depth_to_cm(extraction.value)
        except Exception:
            value = None
    else:
        value = extraction.value
    stats.keyword_hits += 1
    return LlmExtractionResult(
        slot=slot,
        value=value,
        confidence=float(extraction.confidence),
        provider="keyword_fallback",
    )


@dataclass(slots=True)
class HybridExtractorStats:
    """Per-process counters. Useful for /metrics — llm_success_rate
    = llm_successes / (llm_successes + llm_failures)."""

    llm_successes: int = 0
    llm_failures: int = 0
    keyword_hits: int = 0
    keyword_unclear: int = 0


# ─── Provider builder ──────────────────────────────────────────────


def build_provider(
    *,
    provider_type: str,
    anthropic_api_key: str = "",
    anthropic_model: str = "claude-haiku-4-5",
    bedrock_model_id: str = "",
    bedrock_region: str = "ap-south-1",
) -> LlmProvider | None:
    """Factory for the configured LLM provider. Returns None when
    provider_type=='none' or when the required credential is empty
    (safer than raising: pipeline still works via keyword fallback)."""
    from fg_voice.extraction.llm_extractor import (
        AnthropicLlmProvider,
        BedrockLlmProvider,
    )

    if provider_type == "anthropic":
        if not anthropic_api_key:
            log.warning("hybrid.anthropic_disabled", reason="ANTHROPIC_API_KEY empty")
            return None
        return AnthropicLlmProvider(api_key=anthropic_api_key, model=anthropic_model)
    if provider_type == "bedrock":
        if not bedrock_model_id:
            log.warning("hybrid.bedrock_disabled", reason="LLM_MODEL_ID empty")
            return None
        return BedrockLlmProvider(model_id=bedrock_model_id, region=bedrock_region)
    return None


__all__ = [
    "HybridExtractor",
    "HybridExtractorStats",
    "build_provider",
]


# Small unused import cleanup helper — keeps ruff quiet without
# imports that would otherwise be flagged as unused when this
# module is imported for its dispatch table only.
_ = Awaitable
