"""Boot-time invariants for `prompts.yaml`. Per spec §2.2 these MUST
fail at load, not at runtime with a caller on the line."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from fg_voice.conversation.prompt_bank import (
    DTMF_PROMPT_SLOTS,
    PROMPT_VARIABLES,
    VALID_DTMF_VALUES,
    ExtraTemplateVariableError,
    InvalidPromptBankError,
    MissingTemplateVariableError,
    UnknownPromptError,
    load_prompt_bank,
)


@pytest.fixture(scope="module")
def bank():
    return load_prompt_bank()


def test_shipped_bank_loads(bank):
    # Every prompt from §7.2 should be present. Guard against silent
    # deletion of a prompt the graph depends on.
    expected = {
        "consent_notice",
        "ask_intent",
        "reprompt_intent_1",
        "reprompt_intent_2",
        "not_reporting",
        "emergency_redirect",
        "ask_hazard_type",
        "reprompt_hazard_type_1",
        "reprompt_hazard_type_2",
        "ask_description",
        "reprompt_description_1",
        "reprompt_description_2",
        "ask_location",
        "reprompt_location_1",
        "reprompt_location_2",
        "confirm_location_low_conf",
        "disambiguate_location",
        "ask_severity",
        "reprompt_severity_1",
        "reprompt_severity_2",
        "ask_depth",
        "reprompt_depth_1",
        "skip_depth",
        "confirm_summary",
        "reprompt_confirm_1",
        "start_over",
        "submitted",
        "submitted_queued",
        "sms_pin_offer",
        "timeout_exit",
        "fatal_fallback",
    }
    assert expected.issubset(bank.ids())


def test_backchannel_pool_loaded(bank):
    assert bank.backchannels.variants
    assert bank.backchannels.max_duration_ms > 0


def test_consent_and_emergency_disable_barge_in(bank):
    assert bank.get("consent_notice").barge_in is False
    assert bank.get("emergency_redirect").barge_in is False


def test_emergency_side_effects_include_flag_and_notify(bank):
    p = bank.get("emergency_redirect")
    assert "flag:life_safety" in p.side_effects
    assert "notify:ops_immediate" in p.side_effects


def test_terminal_prompts_marked(bank):
    for pid in ("not_reporting", "submitted", "submitted_queued", "timeout_exit", "fatal_fallback"):
        assert bank.get(pid).terminal, f"{pid} must be terminal"


def test_static_prompts_have_no_variables(bank):
    # ask_* prompts don't take dynamic values; catches accidental drift
    # of a placeholder into the prompt text.
    for pid in ("ask_intent", "ask_hazard_type", "ask_description", "ask_location", "ask_severity"):
        assert bank.get(pid).variables == frozenset()


def test_dynamic_prompts_declare_expected_vars(bank):
    assert bank.get("confirm_location_low_conf").variables == frozenset({"location_candidate"})
    assert bank.get("disambiguate_location").variables == frozenset({"option_a", "option_b"})
    assert bank.get("confirm_summary").variables == frozenset(
        {"hazard_type_spoken", "location_spoken", "severity_spoken"}
    )
    assert bank.get("submitted").variables == frozenset({"short_ref"})


def test_render_substitutes_variables(bank):
    rendered = bank.render(
        "confirm_summary",
        hazard_type_spoken="storm damage",
        location_spoken="RK Beach",
        severity_spoken="moderate",
    )
    assert "storm damage" in rendered
    assert "RK Beach" in rendered
    assert "moderate" in rendered


def test_render_missing_variable_raises(bank):
    with pytest.raises(MissingTemplateVariableError):
        bank.render("submitted")


def test_render_extra_variable_raises(bank):
    with pytest.raises(ExtraTemplateVariableError):
        bank.render("ask_intent", short_ref="FG-XYZ")


def test_unknown_prompt_id_raises(bank):
    with pytest.raises(UnknownPromptError):
        bank.get("nonexistent")


def test_dtmf_values_are_in_slot_vocabulary(bank):
    for prompt_id, slot in DTMF_PROMPT_SLOTS.items():
        p = bank.get(prompt_id)
        assert p.dtmf is not None, f"{prompt_id} should have a DTMF map"
        for digit, value in p.dtmf.items():
            assert digit in "0123456789*#"
            assert value in VALID_DTMF_VALUES[slot], (
                f"{prompt_id} digit {digit} maps to {value!r} which is not in the {slot} vocabulary"
            )


def test_prompt_variables_whitelist_matches_shipped_bank(bank):
    # Every {var} in a shipped prompt must be in PROMPT_VARIABLES —
    # if this ever fails, a prompt introduced a placeholder without
    # registering it in the whitelist.
    seen: set[str] = set()
    for p in bank.prompts.values():
        seen |= p.variables
    assert seen <= PROMPT_VARIABLES


# ─── Loader failure modes ────────────────────────────────────────────


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "prompts.yaml"
    p.write_text(dedent(body), encoding="utf-8")
    return p


def test_missing_file_raises(tmp_path):
    with pytest.raises(InvalidPromptBankError):
        load_prompt_bank(tmp_path / "does-not-exist.yaml")


def test_unknown_placeholder_fails_boot(tmp_path):
    path = _write(
        tmp_path,
        """
        meta: { locale: en-IN }
        prompts:
          bogus:
            text: "Hi {not_a_real_var}"
          backchannel_pool:
            variants: ["Okay."]
            prerender: true
            max_duration_ms: 400
        """,
    )
    with pytest.raises(InvalidPromptBankError, match="unregistered template variables"):
        load_prompt_bank(path)


def test_dtmf_on_unregistered_prompt_fails(tmp_path):
    path = _write(
        tmp_path,
        """
        meta: { locale: en-IN }
        prompts:
          weird_prompt:
            text: "Press one"
            dtmf: { "1": "yes" }
          backchannel_pool:
            variants: ["Okay."]
            prerender: true
            max_duration_ms: 400
        """,
    )
    with pytest.raises(InvalidPromptBankError, match="not registered in DTMF_PROMPT_SLOTS"):
        load_prompt_bank(path)


def test_dtmf_value_outside_slot_vocab_fails(tmp_path):
    path = _write(
        tmp_path,
        """
        meta: { locale: en-IN }
        prompts:
          reprompt_intent_2:
            text: "Press 1 for yes, 2 for maybe"
            dtmf: { "1": "yes", "2": "maybe" }
          backchannel_pool:
            variants: ["Okay."]
            prerender: true
            max_duration_ms: 400
        """,
    )
    with pytest.raises(InvalidPromptBankError, match="not in intent vocabulary"):
        load_prompt_bank(path)


def test_missing_backchannel_pool_fails(tmp_path):
    path = _write(
        tmp_path,
        """
        meta: { locale: en-IN }
        prompts:
          ask_intent:
            text: "Reporting?"
        """,
    )
    with pytest.raises(InvalidPromptBankError, match="backchannel_pool"):
        load_prompt_bank(path)
