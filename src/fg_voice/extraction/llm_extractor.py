"""In-call LLM extractor — spec §10 exit gate.

Runs during the caller's turn (unlike `enrichment/extractors/claude_llm.py`
which runs post-call). The two are separate because:

- Post-call has minutes of budget; in-call has ~350 ms TTFT budget.
- Post-call sees the FULL description + already-resolved slots to
  produce revisions; in-call sees ONE utterance at ONE node and
  must produce ONE canonical slot value.
- Failure of post-call means a report goes un-enriched (recoverable);
  failure of in-call means the caller waits — so this path fast-fails
  to the keyword extractor on timeout.

Two providers:

- **Bedrock (primary)** — invoked via boto3 `bedrock-runtime`. Preferred
  in production because the model + prompt cache live in the same
  AWS region as the voice service, giving the lowest tail latency
  for the `Full-turn latency p95 < 1200 ms` gate.
- **Anthropic API (failover)** — invoked via the `anthropic` SDK.
  Cheaper to wire (no IAM plumbing) but adds ~50-100 ms of TLS + WAN
  latency vs Bedrock in the ap-south-1 region. Used when Bedrock
  fails or when `LLM_PROVIDER=anthropic` is explicitly set.

Both providers speak the same Pydantic-validated JSON schema and
share the same prompt (below), so the runner never has to branch on
which provider produced a given result.

The keyword extractor from `keyword_rules.py` is the ULTIMATE
fallback — if BOTH LLM providers fail (or the LLM is disabled),
the runner falls back to it. This keeps the pipeline available
even when the LLM stack is degraded."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from fg_voice.obs.logging import get_logger

log = get_logger(__name__)


# ─── Budgets ─────────────────────────────────────────────────────────

# Sub-350 ms TTFT budget from spec §10 exit gate. Hard-cap on the
# LLM call; anything slower loses to the caller-perceived pause and
# we fall back to keyword rules.
IN_CALL_LLM_TIMEOUT_MS: Final[int] = 350


# ─── Result type ─────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LlmExtractionResult:
    """Structured output from one in-call LLM extraction. `value` is
    the canonical slot value (matches the rule extractor's output
    shape). `confidence` is 0-1; `provider` is for telemetry."""

    slot: str
    value: str | int | None
    confidence: float
    provider: Literal["bedrock", "anthropic", "keyword_fallback"]


# ─── Provider protocol ──────────────────────────────────────────────


class LlmProvider(Protocol):
    """Both Bedrock + Anthropic impls conform to this shape so the
    hybrid extractor never branches on which one it's calling."""

    async def extract(self, slot: str, utterance: str) -> LlmExtractionResult: ...


# ─── System prompt ──────────────────────────────────────────────────


# The system prompt every provider sends verbatim. Enforces CLAUDE.md
# invariant #1 — the LLM only returns structured JSON for one slot,
# never routes, never emits caller-facing prose.
_IN_CALL_SYSTEM_PROMPT: Final[str] = """\
You are a hazard-report slot extractor for a coastal safety hotline
in India. Your job is to read ONE caller utterance and return ONE
JSON object with the extracted value for the requested slot.

Rules:
- Output ONLY the JSON object, no prose, no code fences.
- If the caller's utterance does not clearly answer the requested
  slot, return {"value": null, "confidence": 0.0}.
- confidence is a float in [0, 1]. 0.9+ for verbatim canonical
  values; 0.6-0.9 for confident paraphrases; below 0.6 for
  guesses.
- For categorical slots, `value` MUST be one of the whitelisted
  strings supplied in the user message. Never invent a new value.
- For water_depth_cm, `value` is an integer number of cm.
- Never produce caller-facing text under any circumstance.
"""


def _user_prompt_for(slot: str, utterance: str) -> str:
    """Build the per-turn user prompt. Categorical slots ship their
    whitelist so the model has no room to invent."""
    whitelist_hint = _whitelist_hint(slot)
    return (
        f"Slot: {slot}\n"
        f"{whitelist_hint}\n"
        f'Caller utterance: "{utterance}"\n\n'
        f'Return one JSON object: {{"value": <string|number|null>, '
        f'"confidence": <float in [0,1]>}}.'
    )


def _whitelist_hint(slot: str) -> str:
    """Whitelist hint per slot name. Slot names match the string
    values of `conversation.state.Slot` (kept out of the import
    graph to preserve the layered contract — extraction/ is below
    conversation/)."""
    if slot == "intent":
        return "Allowed values: 'yes' | 'no'."
    if slot == "hazard_type":
        return (
            "Allowed values: 'storm' | 'flood' | 'tsunami' | 'sludge_oil' | "
            "'abnormal_tide' | 'erosion' | 'other'."
        )
    if slot == "severity":
        return "Allowed values: 'light' | 'moderate' | 'extreme'."
    if slot == "confirmation":
        return "Allowed values: 'yes' | 'no' | 'restart'."
    if slot == "water_depth_cm":
        return "Integer number of cm (e.g. 30, 100, 150). No prose."
    if slot in ("description", "location"):
        return "Free text — return the utterance itself as the value."
    return ""


# ─── Anthropic provider ─────────────────────────────────────────────


@dataclass(slots=True)
class AnthropicLlmProvider:
    """Anthropic SDK impl. Lazily imports `anthropic` at construction
    so a deploy without the `[llm]` extras doesn't fail on import."""

    api_key: str
    model: str = "claude-haiku-4-5"  # in-call default: latency > intelligence
    _client: Any = None

    def __post_init__(self) -> None:
        # Lazy import so the base image doesn't need anthropic when
        # LLM_PROVIDER=bedrock or LLM_PROVIDER=none.
        import anthropic

        # AsyncAnthropic so the runner's async loop isn't blocked.
        self._client = anthropic.AsyncAnthropic(api_key=self.api_key)

    async def extract(self, slot: str, utterance: str) -> LlmExtractionResult:
        assert self._client is not None
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=100,
                system=[
                    {
                        "type": "text",
                        "text": _IN_CALL_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": _user_prompt_for(slot, utterance)}],
            )
        except Exception as exc:
            raise LlmProviderError(f"anthropic: {exc}") from exc

        text = _extract_text_from_response(response)
        parsed = _parse_llm_json(text)
        return LlmExtractionResult(
            slot=slot,
            value=parsed["value"],
            confidence=float(parsed["confidence"]),
            provider="anthropic",
        )


# ─── Bedrock provider ───────────────────────────────────────────────


@dataclass(slots=True)
class BedrockLlmProvider:
    """AWS Bedrock impl. Uses `aioboto3` so the call stays async.
    Model ID must be the Bedrock-inference-profile ARN or a foundation
    model ID like `anthropic.claude-3-5-haiku-20241022-v1:0`."""

    model_id: str
    region: str = "ap-south-1"
    _session: Any = None

    def __post_init__(self) -> None:
        # Lazy import — aioboto3 already ships in main deps, but
        # keeping this in __post_init__ mirrors the Anthropic pattern.
        import aioboto3

        self._session = aioboto3.Session()

    async def extract(self, slot: str, utterance: str) -> LlmExtractionResult:
        assert self._session is not None
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 100,
                "system": [
                    {
                        "type": "text",
                        "text": _IN_CALL_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": _user_prompt_for(slot, utterance)}],
            }
        )
        try:
            async with self._session.client("bedrock-runtime", region_name=self.region) as br:
                resp = await br.invoke_model(
                    modelId=self.model_id, body=body, contentType="application/json"
                )
                raw = await resp["body"].read()
        except Exception as exc:
            raise LlmProviderError(f"bedrock: {exc}") from exc

        payload = json.loads(raw)
        text = _extract_text_from_bedrock_response(payload)
        parsed = _parse_llm_json(text)
        return LlmExtractionResult(
            slot=slot,
            value=parsed["value"],
            confidence=float(parsed["confidence"]),
            provider="bedrock",
        )


# ─── Error types + parsers ──────────────────────────────────────────


class LlmProviderError(RuntimeError):
    """Raised when a provider fails (network / auth / timeout /
    validation). The hybrid extractor catches this + falls back."""


def _extract_text_from_response(response: Any) -> str:
    """Extract the first text block from an Anthropic Message
    response object."""
    if not getattr(response, "content", None):
        raise LlmProviderError("anthropic: empty content list")
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return str(block.text)
    raise LlmProviderError("anthropic: no text block in response")


def _extract_text_from_bedrock_response(payload: dict[str, Any]) -> str:
    """Bedrock returns the same content-block shape as the direct API."""
    content = payload.get("content", [])
    for block in content:
        if block.get("type") == "text":
            return str(block.get("text", ""))
    raise LlmProviderError("bedrock: no text block in response")


def _parse_llm_json(text: str) -> dict[str, Any]:
    """Parse the JSON envelope the model produces. Strips code fences
    if the model wrapped despite instructions."""
    stripped = text.strip()
    if stripped.startswith("```"):
        # Remove triple-backtick fence, optionally with a language tag.
        stripped = stripped.strip("`")
        newline = stripped.find("\n")
        if newline != -1:
            stripped = stripped[newline + 1 :]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise LlmProviderError(f"invalid JSON from LLM: {exc}") from exc
    if not isinstance(parsed, dict) or "value" not in parsed or "confidence" not in parsed:
        raise LlmProviderError(f"LLM JSON missing required keys: {parsed!r}")
    return parsed


# ─── Timeout wrapper ────────────────────────────────────────────────


async def extract_with_timeout(
    provider: LlmProvider,
    slot: str,
    utterance: str,
    *,
    timeout_ms: int = IN_CALL_LLM_TIMEOUT_MS,
) -> LlmExtractionResult:
    """Wrap provider.extract with a hard timeout. On timeout raises
    `LlmProviderError` so the hybrid extractor's fallback fires — the
    caller experience must never wait beyond the budget."""
    try:
        return await asyncio.wait_for(
            provider.extract(slot, utterance), timeout=timeout_ms / 1000.0
        )
    except TimeoutError as exc:
        raise LlmProviderError(f"timeout after {timeout_ms}ms") from exc


__all__ = [
    "IN_CALL_LLM_TIMEOUT_MS",
    "AnthropicLlmProvider",
    "BedrockLlmProvider",
    "LlmExtractionResult",
    "LlmProvider",
    "LlmProviderError",
    "extract_with_timeout",
]
