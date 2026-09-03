"""Anthropic Claude implementation of `LLMExtractor`.

Deep post-call extraction pass — given the raw caller description,
returns structured slot revisions the flow will apply to the report
row (subject to `deep_extract`'s confidence gate + `persist`'s
whitelist).

Design choices:

- **Structured outputs via `messages.parse()`** — the Anthropic SDK
  validates the response against a Pydantic schema and hands back a
  `.parsed_output`. Eliminates the "did the LLM return valid JSON"
  question entirely; a schema violation raises before this function
  returns.
- **Adaptive thinking OFF** — Opus 4.7 defaults `thinking` to off; a
  bounded classification-shaped task like this doesn't benefit from
  extended reasoning. Latency and cost both come down. Ops can flip
  to `{"type": "adaptive"}` here if a calibration study shows nuanced
  cases benefit.
- **Prompt caching on the system prompt** — the system prompt is
  identical across every call, so caching it after the first pays
  back at ~2 calls (write is 1.25x, read is 0.1x base input price).
  The caller description is short (typically < 200 tokens); everything
  after the cache breakpoint is uncached.
- **CLAUDE.md invariant #1 preserved** — the system prompt is explicit
  that this call NEVER produces caller-facing prose, NEVER routes the
  conversation, and NEVER emits anything outside the fixed schema.
  The parse step enforces the schema; the system prompt is a
  belt-and-suspenders.
- **Whitelist-consistent output** — the Pydantic schema only exposes
  `hazard_type`, `severity`, `water_depth_cm` (the same set persist's
  `_REVISABLE_SLOTS` allows). Description, location, and identity
  columns are deliberately absent from the schema so the LLM can't
  even propose them.

Errors:
- Any Anthropic API error (network / 5xx / rate limit / etc.) is
  raised as `TransientEnrichmentError` so the outbox relay's retry
  ladder kicks in.
- A schema-validation error from `messages.parse()` bubbles as
  `PermanentEnrichmentError` — the model returned something the
  Pydantic schema couldn't accept, which won't fix itself on retry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final, Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from fg_voice.enrichment.errors import (
    PermanentEnrichmentError,
    TransientEnrichmentError,
)
from fg_voice.enrichment.tasks.extract import RevisedSlots
from fg_voice.obs.logging import get_logger

log = get_logger(__name__)

# The whitelist of hazard types the caller-facing prompts already accept
# (see `conversation/prompts.yaml`). Keeping the LLM output constrained
# to this set means a downstream revision never lands a category the
# rest of the system doesn't recognise.
_HazardType = Literal[
    "flood",
    "storm_surge",
    "coastal_erosion",
    "cyclone",
    "tsunami",
    "high_tide",
    "unknown",
]
_Severity = Literal["light", "moderate", "extreme"]

# Model default. Per skill guidance, default to Opus 4.7 — operators
# can swap to Haiku 4.5 via config for higher throughput / lower cost
# once real-call calibration data justifies it.
DEFAULT_MODEL: Final[str] = "claude-opus-4-7"

# Bounded output — the schema is small. 512 tokens is plenty of
# headroom for the JSON payload + a short reasoning field.
_MAX_TOKENS: Final[int] = 512

# System prompt. Frozen — every byte here must be stable across calls
# or prompt caching won't hit. See `shared/prompt-caching.md` §Silent
# invalidators.
_SYSTEM_PROMPT: Final[str] = """\
You are a post-call slot-extraction step for a coastal-hazard voice hotline in India.
Your ONLY job is to return a JSON object matching the schema you're given.

STRICT RULES:
- Never generate caller-facing prose. You do NOT talk to the caller.
  Nothing you output is ever spoken back to a person.
- Never route the conversation. The conversation is already over; this
  runs post-call.
- Only extract values that are UNAMBIGUOUSLY stated in the caller's
  description. If the description is vague, leave the field null.
- Do not infer, embellish, or fill gaps from prior knowledge. If the
  caller said "water is coming in", do not guess a depth in cm.
- If the description is empty, gibberish, or entirely off-topic, return
  all fields null with a low confidence.

FIELD SEMANTICS:
- hazard_type: only revise if the caller's description clearly points
  to a different category than the in-call keyword extractor would
  have caught. If the description just describes flooding, don't revise.
- severity: only revise if the caller used explicit intensity language
  ("water is up to my chest", "waves are massive"), NOT if you're
  inferring severity from context.
- water_depth_cm: only extract if the caller stated a numeric depth or
  a body-part reference that maps unambiguously to cm (ankle ~ 15,
  knee ~ 50, waist ~ 100, chest ~ 130). Never guess.

CONFIDENCE:
- 0.9+ : caller stated the value verbatim
- 0.7-0.9: caller used body-part reference or explicit intensity language
- Below 0.7: don't set the field at all; return null instead.

The downstream system already applies a confidence gate (0.7) to your
overall confidence score, and a whitelist to your field selection.
Your job is to be conservative, not clever.
"""


class ClaudeExtractedSlots(BaseModel):
    """Schema handed to `messages.parse()`. Fields are exactly the
    whitelist that `persist._REVISABLE_SLOTS` accepts — anything else
    would be dropped downstream, so we don't let the LLM propose it."""

    hazard_type: _HazardType | None = Field(
        default=None,
        description=(
            "Coastal-hazard category, or null if the description doesn't "
            "unambiguously indicate one."
        ),
    )
    severity: _Severity | None = Field(
        default=None,
        description=(
            "Severity level, or null if the description doesn't contain "
            "explicit intensity language."
        ),
    )
    water_depth_cm: int | None = Field(
        default=None,
        ge=0,
        le=1000,
        description=(
            "Water depth in centimeters. Only set if the caller stated a "
            "numeric depth or an unambiguous body-part reference."
        ),
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "Overall confidence in the extraction, in [0, 1]. If the "
            "description was vague or off-topic, this MUST be low even "
            "if some fields were guessed."
        ),
    )
    reasoning: str = Field(
        default="",
        max_length=500,
        description=(
            "One-sentence justification for the QA audit trail. Do NOT "
            "address the caller; this is for internal review only."
        ),
    )


@dataclass(slots=True)
class ClaudeExtractor:
    """Anthropic Claude implementation of the `LLMExtractor` protocol.

    Instances hold an AsyncAnthropic client — one per process is fine.
    The client is injected in tests so the network is never called
    inside the suite."""

    client: anthropic.AsyncAnthropic
    model: str = DEFAULT_MODEL
    max_tokens: int = _MAX_TOKENS

    async def extract(self, snapshot_description: str | None) -> RevisedSlots:
        """Run the deep-extract pass. Empty description → empty
        RevisedSlots (skip the API call — no signal, no point in
        burning tokens)."""
        if not snapshot_description or not snapshot_description.strip():
            return RevisedSlots()
        try:
            response = await self.client.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                # `system` as a list lets us attach `cache_control` —
                # the shared system prompt caches after the first call.
                system=[
                    {
                        "type": "text",
                        "text": _SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[
                    {
                        "role": "user",
                        "content": (f"Caller description:\n{snapshot_description.strip()}"),
                    }
                ],
                output_format=ClaudeExtractedSlots,
            )
        except (
            anthropic.APIConnectionError,
            anthropic.APITimeoutError,
            anthropic.RateLimitError,
            anthropic.InternalServerError,
        ) as exc:
            raise TransientEnrichmentError(
                f"Claude extractor transient failure: {type(exc).__name__}: {exc}"
            ) from exc
        except anthropic.APIStatusError as exc:
            # 4xx that isn't rate-limit / auth — usually a request
            # shape bug on our side. Non-retriable.
            raise PermanentEnrichmentError(
                f"Claude extractor rejected request: {exc.status_code} {exc}"
            ) from exc
        except ValidationError as exc:
            # Model returned JSON that didn't validate. `messages.parse()`
            # already retries schema mismatches at the SDK level; if it
            # still fails, the schema and the model's calibration are
            # out of sync — retrying at the outbox layer won't help.
            raise PermanentEnrichmentError(f"Claude extractor schema violation: {exc}") from exc

        parsed = response.parsed_output
        if parsed is None:
            # `messages.parse()` returns None when the model didn't
            # emit a schema-shaped tool_use block (refusal, empty
            # content, etc.). Treat as an empty extraction rather
            # than raising — the caller's slots stay as they were.
            log.warning(
                "enrichment.claude_extractor.no_parsed_output",
                stop_reason=response.stop_reason,
            )
            return RevisedSlots()
        values: dict[str, str | int] = {}
        if parsed.hazard_type is not None:
            values["hazard_type"] = parsed.hazard_type
        if parsed.severity is not None:
            values["severity"] = parsed.severity
        if parsed.water_depth_cm is not None:
            values["water_depth_cm"] = parsed.water_depth_cm

        if response.usage:
            log.info(
                "enrichment.claude_extractor.usage",
                model=self.model,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                cache_read=response.usage.cache_read_input_tokens or 0,
                cache_creation=response.usage.cache_creation_input_tokens or 0,
                proposed=sorted(values.keys()),
                confidence=parsed.confidence,
            )

        return RevisedSlots(
            values=values,
            confidence=parsed.confidence,
            notes=parsed.reasoning,
        )


# Alias for use in main.py without dragging in the whole module.
def build_claude_extractor(
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,
) -> ClaudeExtractor:
    """Convenience constructor — hides the AsyncAnthropic wiring so
    `main.py` doesn't need to import anthropic directly."""
    client = anthropic.AsyncAnthropic(api_key=api_key)
    return ClaudeExtractor(client=client, model=model)


# Keep the unused `field` import from ruff-erroring if someone strips
# the `= field(default=...)` from ClaudeExtractor in future refactors.
_ = field
__all__ = [
    "DEFAULT_MODEL",
    "ClaudeExtractedSlots",
    "ClaudeExtractor",
    "build_claude_extractor",
]
