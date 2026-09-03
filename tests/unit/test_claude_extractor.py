"""ClaudeExtractor — Anthropic Claude implementation of LLMExtractor.

Covers:
- empty description short-circuits (no API call)
- happy path — parsed_output translates to RevisedSlots with the right
  values/confidence/notes
- schema field selection — only whitelist fields land in `values`; None
  fields are omitted, not passed as None
- `system` prompt is a list with `cache_control: ephemeral` so the
  prompt caches across calls
- API errors raise TransientEnrichmentError (retriable)
- API status errors raise PermanentEnrichmentError (non-retriable)
- Pydantic validation errors raise PermanentEnrichmentError

The Anthropic client is mocked — the network is never touched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
import pytest
from pydantic import ValidationError

from fg_voice.enrichment.errors import (
    PermanentEnrichmentError,
    TransientEnrichmentError,
)
from fg_voice.enrichment.extractors.claude_llm import (
    ClaudeExtractedSlots,
    ClaudeExtractor,
    build_claude_extractor,
)


def _fake_parsed_response(
    *,
    parsed_output: ClaudeExtractedSlots | None,
    input_tokens: int = 200,
    output_tokens: int = 50,
    cache_read: int = 0,
    cache_creation: int = 0,
) -> MagicMock:
    """Build the object shape `client.messages.parse()` returns —
    just the fields the extractor reads."""
    resp = MagicMock()
    resp.parsed_output = parsed_output
    resp.stop_reason = "end_turn"
    resp.usage = MagicMock(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )
    return resp


def _client_with(parse_side_effect: Any) -> MagicMock:
    """Fake AsyncAnthropic — `client.messages.parse` is an AsyncMock
    with the given return value or side effect."""
    client = MagicMock(spec=anthropic.AsyncAnthropic)
    client.messages = MagicMock()
    if isinstance(parse_side_effect, Exception) or (
        isinstance(parse_side_effect, type) and issubclass(parse_side_effect, BaseException)
    ):
        client.messages.parse = AsyncMock(side_effect=parse_side_effect)
    else:
        client.messages.parse = AsyncMock(return_value=parse_side_effect)
    return client


# ─── Empty / whitespace description short-circuits ───────────────────


@pytest.mark.asyncio
async def test_empty_description_skips_api_call():
    client = _client_with(_fake_parsed_response(parsed_output=None))
    extractor = ClaudeExtractor(client=client)

    result = await extractor.extract(None)
    assert result.values == {}
    assert result.confidence == 0.0
    client.messages.parse.assert_not_called()


@pytest.mark.asyncio
async def test_whitespace_only_description_skips_api_call():
    client = _client_with(_fake_parsed_response(parsed_output=None))
    extractor = ClaudeExtractor(client=client)

    result = await extractor.extract("   \n\t  ")
    assert result.values == {}
    client.messages.parse.assert_not_called()


# ─── Happy path ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_maps_parsed_output_to_revised_slots():
    parsed = ClaudeExtractedSlots(
        hazard_type="storm_surge",
        severity="extreme",
        water_depth_cm=130,
        confidence=0.92,
        reasoning="caller said 'water is up to my chest'",
    )
    client = _client_with(_fake_parsed_response(parsed_output=parsed))
    extractor = ClaudeExtractor(client=client)

    result = await extractor.extract("waves crashing over the road, water is up to my chest")
    assert result.values == {
        "hazard_type": "storm_surge",
        "severity": "extreme",
        "water_depth_cm": 130,
    }
    assert result.confidence == pytest.approx(0.92)
    assert "chest" in result.notes


@pytest.mark.asyncio
async def test_partial_extraction_omits_null_fields():
    """Fields the model returned None for must NOT appear in `values`
    as None — persist would blindly overwrite the row with None
    otherwise. The whitelist should only carry set fields."""
    parsed = ClaudeExtractedSlots(
        hazard_type=None,
        severity="moderate",
        water_depth_cm=None,
        confidence=0.75,
        reasoning="only severity was clear",
    )
    client = _client_with(_fake_parsed_response(parsed_output=parsed))
    extractor = ClaudeExtractor(client=client)

    result = await extractor.extract("water is somewhere between light and heavy")
    assert result.values == {"severity": "moderate"}
    assert "hazard_type" not in result.values
    assert "water_depth_cm" not in result.values


@pytest.mark.asyncio
async def test_all_null_confident_extraction_returns_empty_values():
    """Model can decline to extract anything but still self-report a
    confidence; that's fine — `values` empty means persist has nothing
    to apply."""
    parsed = ClaudeExtractedSlots(
        hazard_type=None, severity=None, water_depth_cm=None, confidence=0.3, reasoning=""
    )
    client = _client_with(_fake_parsed_response(parsed_output=parsed))
    extractor = ClaudeExtractor(client=client)

    result = await extractor.extract("uh hello is anyone there")
    assert result.values == {}
    assert result.confidence == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_none_parsed_output_returns_empty_slots(caplog):
    """If parse() returns a response with parsed_output=None (refusal,
    empty content), we return empty RevisedSlots — never raise."""
    client = _client_with(_fake_parsed_response(parsed_output=None))
    extractor = ClaudeExtractor(client=client)

    result = await extractor.extract("some text")
    assert result.values == {}


# ─── Prompt caching wired correctly ──────────────────────────────────


@pytest.mark.asyncio
async def test_system_prompt_carries_cache_control():
    """The `system` payload must be a list with a `cache_control`
    marker on the last text block — otherwise the shared preamble
    doesn't cache and every call pays full input price."""
    parsed = ClaudeExtractedSlots(
        hazard_type=None, severity=None, water_depth_cm=None, confidence=0.0, reasoning=""
    )
    client = _client_with(_fake_parsed_response(parsed_output=parsed))
    extractor = ClaudeExtractor(client=client, model="claude-opus-4-7")

    await extractor.extract("waves onto the road")

    client.messages.parse.assert_called_once()
    kwargs = client.messages.parse.call_args.kwargs
    assert kwargs["model"] == "claude-opus-4-7"
    assert kwargs["output_format"] is ClaudeExtractedSlots
    system = kwargs["system"]
    assert isinstance(system, list)
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    # The description ends up on the user turn, not injected into the
    # (cached) system prompt — otherwise the cache would invalidate
    # every call.
    assert "waves onto the road" not in system[-1]["text"]
    user_msg = kwargs["messages"][0]
    assert user_msg["role"] == "user"
    assert "waves onto the road" in user_msg["content"]


# ─── Error mapping ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transient_errors_bubble_as_transient_enrichment_error():
    """Retriable API failures MUST surface as TransientEnrichmentError
    so the outbox relay's retry ladder engages."""
    client = _client_with(
        anthropic.APIConnectionError(
            message="peer reset", request=httpx.Request("POST", "https://api.example")
        )
    )
    extractor = ClaudeExtractor(client=client)

    with pytest.raises(TransientEnrichmentError, match="transient failure"):
        await extractor.extract("hello")


@pytest.mark.asyncio
async def test_rate_limit_is_transient():
    """429s go through the same retry ladder — the SDK also retries
    internally, but the outbox retry adds a longer backoff window."""
    resp = httpx.Response(429, request=httpx.Request("POST", "https://api.example"))
    client = _client_with(
        anthropic.RateLimitError(
            message="slow down", response=resp, body={"error": {"type": "rate_limit_error"}}
        )
    )
    extractor = ClaudeExtractor(client=client)

    with pytest.raises(TransientEnrichmentError):
        await extractor.extract("hello")


@pytest.mark.asyncio
async def test_400_class_bubbles_as_permanent():
    """A non-retriable APIStatusError (bad request shape, auth) means
    retrying won't fix anything — surface as permanent so ops sees it
    in the DLQ without the retry-count noise."""
    resp = httpx.Response(400, request=httpx.Request("POST", "https://api.example"))
    client = _client_with(
        anthropic.BadRequestError(
            message="bad request",
            response=resp,
            body={"error": {"type": "invalid_request_error"}},
        )
    )
    extractor = ClaudeExtractor(client=client)

    with pytest.raises(PermanentEnrichmentError, match="rejected request"):
        await extractor.extract("hello")


@pytest.mark.asyncio
async def test_pydantic_validation_error_is_permanent():
    """If the model returns something the schema can't accept, this is
    a calibration issue — retrying at the outbox layer won't help.
    Surface as permanent so ops sees a real bug, not a transient blip."""
    from pydantic_core import InitErrorDetails, PydanticCustomError

    err = ValidationError.from_exception_data(
        title="ClaudeExtractedSlots",
        line_errors=[
            InitErrorDetails(
                type=PydanticCustomError("value_error", "bad"),
                loc=("confidence",),
                input=None,
            )
        ],
    )
    client = _client_with(err)
    extractor = ClaudeExtractor(client=client)

    with pytest.raises(PermanentEnrichmentError, match="schema violation"):
        await extractor.extract("hello")


# ─── Convenience constructor ─────────────────────────────────────────


def test_build_claude_extractor_wires_client():
    extractor = build_claude_extractor(api_key="test-key", model="claude-haiku-4-5")
    assert isinstance(extractor, ClaudeExtractor)
    assert extractor.model == "claude-haiku-4-5"
    assert isinstance(extractor.client, anthropic.AsyncAnthropic)
