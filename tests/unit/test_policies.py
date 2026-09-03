"""Reprompt ladder + confidence gates."""

from __future__ import annotations

import pytest

from fg_voice.conversation.policies import (
    EXTRACTION_CONFIDENCE_THRESHOLD,
    REPROMPT_LADDER,
    RepromptAction,
    is_extraction_confident,
    is_stt_turn_confident,
    next_action,
    reprompt_id_for,
    timing_for,
)
from fg_voice.conversation.state import NodeId


@pytest.mark.parametrize(
    "node,attempt,expected",
    [
        (NodeId.ASK_INTENT, 0, RepromptAction.RETRY_SOFT),
        (NodeId.ASK_INTENT, 1, RepromptAction.RETRY_WITH_OPTIONS),
        (NodeId.ASK_INTENT, 2, RepromptAction.DTMF_ONLY),
        (NodeId.ASK_INTENT, 3, RepromptAction.DTMF_ONLY),
        (NodeId.ASK_INTENT, 4, RepromptAction.TIMEOUT_EXIT),
        # free-text: no DTMF-only rung
        (NodeId.ASK_DESCRIPTION, 2, RepromptAction.ACCEPT_LOW_CONF),
        (NodeId.ASK_DESCRIPTION, 3, RepromptAction.ACCEPT_LOW_CONF),
        (NodeId.ASK_DESCRIPTION, 4, RepromptAction.TIMEOUT_EXIT),
        (NodeId.ASK_LOCATION, 3, RepromptAction.ACCEPT_LOW_CONF),
    ],
)
def test_next_action_ladder(node, attempt, expected):
    assert next_action(node, attempt) is expected


def test_reprompt_id_for_returns_none_past_ladder():
    ladder_len = len(REPROMPT_LADDER[NodeId.ASK_INTENT])
    assert reprompt_id_for(NodeId.ASK_INTENT, ladder_len + 1) is None


def test_reprompt_id_for_returns_ladder_entries():
    assert reprompt_id_for(NodeId.ASK_INTENT, 1) == "reprompt_intent_1"
    assert reprompt_id_for(NodeId.ASK_INTENT, 2) == "reprompt_intent_2"


def test_reprompt_ladder_prompt_ids_all_exist_in_bank():
    """Ladder entries must be real prompt IDs; catches typos before they
    fail on a live call."""
    from fg_voice.conversation.prompt_bank import load_prompt_bank

    bank_ids = load_prompt_bank().ids()
    for node, ladder in REPROMPT_LADDER.items():
        for prompt_id in ladder:
            assert prompt_id in bank_ids, f"{node} ladder → missing prompt {prompt_id!r}"


def test_timing_defaults_and_overrides():
    default = timing_for(NodeId.ASK_INTENT)
    assert default.eot_timeout_ms == 1_200
    desc = timing_for(NodeId.ASK_DESCRIPTION)
    assert desc.eot_timeout_ms == 2_500
    assert desc.max_turn_ms >= default.max_turn_ms


def test_confidence_gates():
    assert is_extraction_confident(EXTRACTION_CONFIDENCE_THRESHOLD)
    assert is_extraction_confident(0.9)
    assert not is_extraction_confident(0.0)
    assert is_stt_turn_confident(0.9)
    assert not is_stt_turn_confident(0.1)
