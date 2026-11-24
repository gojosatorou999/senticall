"""Per-question `speechTimeout` selection (conversation/eot_patch.py).

"auto" stops at the first pause in speech (fast, right for one-word
answers); a number waits that many seconds of silence (patient, right for
questions that invite a sentence). These tests pin that direction — an
earlier revision had it inverted and slowed down every short answer.

The classifier reads prompt text, so the tests read the real prompt bank
rather than restating the wording. A prompt reworded past its anchor
fails here instead of silently regressing on a live call.
"""

from __future__ import annotations

import pytest

from fg_voice.conversation.eot_patch import speech_timeout_for_prompt
from fg_voice.conversation.prompt_bank import load_prompt_bank

# Single-word or single-digit answers — nothing follows the first pause,
# so stop there.
CLOSED_PROMPT_IDS = [
    "ask_intent",
    "reprompt_intent_1",
    "reprompt_intent_2",
    "ask_hazard_type",
    "reprompt_hazard_type_1",
    "reprompt_hazard_type_2",
    "ask_severity",
    "reprompt_severity_1",
    "reprompt_severity_2",
    "ask_depth",
    "reprompt_depth_1",
    "reprompt_confirm_1",
]

# Open-ended — the caller draws breath mid-sentence, and "auto" would
# stop on that breath. Truncating a hazard description is worse than
# waiting for it.
OPEN_PROMPT_IDS = [
    "ask_description",
    "reprompt_description_1",
    "reprompt_description_2",
    "ask_location",
    "reprompt_location_1",
    "reprompt_location_2",
]


@pytest.fixture(scope="module")
def bank():
    return load_prompt_bank()


@pytest.mark.parametrize("prompt_id", CLOSED_PROMPT_IDS)
def test_closed_questions_stop_at_the_first_pause(bank, prompt_id: str) -> None:
    assert speech_timeout_for_prompt(bank.get(prompt_id).text, 3) == "auto"


@pytest.mark.parametrize("prompt_id", OPEN_PROMPT_IDS)
def test_open_questions_get_a_patient_window(bank, prompt_id: str) -> None:
    assert speech_timeout_for_prompt(bank.get(prompt_id).text, 3) == "3"


def test_confirm_summary_is_closed_after_substitution(bank) -> None:
    """confirm_summary carries variables, so classify the rendered text."""
    text = bank.render(
        "confirm_summary",
        hazard_type_spoken="flooding",
        location_spoken="Kakinada beach",
        severity_spoken="moderate",
    )
    assert speech_timeout_for_prompt(text, 3) == "auto"


def test_low_confidence_location_confirmation_is_closed(bank) -> None:
    """A yes/no readback, unlike the location question that precedes it."""
    text = bank.render("confirm_location_low_conf", location_candidate="Kakinada")
    assert speech_timeout_for_prompt(text, 3) == "auto"


def test_zero_forces_auto_everywhere(bank) -> None:
    """GATHER_OPEN_SPEECH_TIMEOUT_SEC=0 is the revert-without-a-deploy
    switch."""
    assert speech_timeout_for_prompt(bank.get("ask_description").text, 0) == "auto"


def test_unknown_prompt_falls_back_to_auto() -> None:
    """A reworded or new prompt degrades to the fast default rather than
    silently making every turn wait."""
    assert speech_timeout_for_prompt("Something nobody has anchored yet.", 3) == "auto"


def test_configured_value_is_used_verbatim(bank) -> None:
    assert speech_timeout_for_prompt(bank.get("ask_description").text, 4) == "4"


def test_open_ended_anchor_wins_over_closed_overlap() -> None:
    """reprompt_description_2 shares "what are you seeing" with
    ask_hazard_type; the open-ended reading must win."""
    assert speech_timeout_for_prompt("Even a few words helps. What are you seeing?", 3) == "3"
