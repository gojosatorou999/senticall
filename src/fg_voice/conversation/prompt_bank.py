"""Loader and template-renderer for `prompts.yaml`.

The invariant enforced here is spec §2.2: every caller-facing utterance
comes from `prompts.yaml`, and every dynamic variable is on a fixed
whitelist. A prompt with an unknown variable or an unknown DTMF slot
value MUST fail at boot, never at runtime with a caller on the line."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import yaml

# ─── Variable whitelist ──────────────────────────────────────────────
# Every {placeholder} that may appear inside prompt text. Adding a new
# placeholder is a two-step change: register it here, then populate it
# in `render_prompt` callers. `load_prompt_bank` will refuse to boot
# if a template references an unlisted variable — that's the point.
PROMPT_VARIABLES: Final[frozenset[str]] = frozenset(
    {
        # locations resolved by the RAG layer (P4)
        "location_candidate",
        "option_a",
        "option_b",
        # confirmation summary
        "hazard_type_spoken",
        "location_spoken",
        "severity_spoken",
        # submission
        "short_ref",
    }
)

# ─── DTMF slot-value whitelist ───────────────────────────────────────
# Any DTMF map value in `prompts.yaml` must appear in one of these
# lists. Keeps the categorical-slot vocabulary in one place so the
# extractor and the DTMF fallback cannot silently drift apart.
VALID_DTMF_VALUES: Final[dict[str, frozenset[str]]] = {
    "intent": frozenset({"yes", "no"}),
    "hazard_type": frozenset(
        {"storm", "flood", "tsunami", "sludge_oil", "abnormal_tide", "erosion", "other"}
    ),
    "severity": frozenset({"light", "moderate", "extreme"}),
    "depth": frozenset({"ankle", "knee", "waist", "above_waist"}),
    "confirmation": frozenset({"yes", "no", "restart"}),
}

# Map prompt_id → which categorical vocabulary its DTMF must draw from.
# A prompt_id absent from this map is not allowed to have a `dtmf` block.
DTMF_PROMPT_SLOTS: Final[dict[str, str]] = {
    # The opening question carries a keypad map too, not just its second
    # reprompt. Speech recognition on the first turn is the least reliable
    # moment of the call — the audio path may still be settling right after
    # answer, and a caller who talks over the prompt is not heard at all.
    # Without this, a caller whose voice does not register has no way
    # through the first question and gets timed out.
    "ask_intent": "intent",
    "reprompt_intent_2": "intent",
    "reprompt_hazard_type_2": "hazard_type",
    "reprompt_severity_2": "severity",
    "reprompt_depth_1": "depth",
    "reprompt_confirm_1": "confirmation",
}


_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class Prompt:
    """One entry from `prompts.yaml`, post-validation."""

    id: str
    text: str
    barge_in: bool = True
    prerender: bool = True
    terminal: bool = False
    dtmf: dict[str, str] | None = None
    side_effects: tuple[str, ...] = ()
    variables: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class BackchannelPool:
    """The variants played on EndOfTurn to mask the extractor pause."""

    variants: tuple[str, ...]
    prerender: bool
    max_duration_ms: int


@dataclass(frozen=True, slots=True)
class PromptBank:
    """Immutable container for the whole bank. Look up via `.get(id)`
    (raises) or `.render(id, **vars)` (raises)."""

    prompts: dict[str, Prompt]
    backchannels: BackchannelPool
    locale: str
    default_barge_in: bool

    def get(self, prompt_id: str) -> Prompt:
        try:
            return self.prompts[prompt_id]
        except KeyError as exc:
            raise UnknownPromptError(prompt_id) from exc

    def render(self, prompt_id: str, **variables: str) -> str:
        p = self.get(prompt_id)
        missing = p.variables - set(variables)
        if missing:
            raise MissingTemplateVariableError(prompt_id, missing)
        extra = set(variables) - p.variables
        if extra:
            raise ExtraTemplateVariableError(prompt_id, extra)
        return p.text.format(**variables)

    def ids(self) -> frozenset[str]:
        return frozenset(self.prompts.keys())


# ─── Errors ──────────────────────────────────────────────────────────


class PromptBankError(Exception):
    """Base class for boot-time prompt-bank errors."""


class UnknownPromptError(PromptBankError):
    def __init__(self, prompt_id: str) -> None:
        super().__init__(f"unknown prompt_id: {prompt_id!r}")


class MissingTemplateVariableError(PromptBankError):
    def __init__(self, prompt_id: str, missing: frozenset[str] | set[str]) -> None:
        super().__init__(f"prompt {prompt_id!r} missing variables: {sorted(missing)}")


class ExtraTemplateVariableError(PromptBankError):
    def __init__(self, prompt_id: str, extra: frozenset[str] | set[str]) -> None:
        super().__init__(f"prompt {prompt_id!r} got unexpected variables: {sorted(extra)}")


class InvalidPromptBankError(PromptBankError):
    """Structural problem with `prompts.yaml` — thrown at boot."""


# ─── Loader ──────────────────────────────────────────────────────────


DEFAULT_PROMPTS_PATH: Final[Path] = Path(__file__).with_name("prompts.yaml")


def load_prompt_bank(path: Path | None = None) -> PromptBank:
    """Read the YAML, validate every invariant, return an immutable bank.

    Failing here (with a caller-facing prompt problem) is the whole point
    of §2.2 — do it now, at boot, not on a live call."""
    yaml_path = path or DEFAULT_PROMPTS_PATH
    if not yaml_path.exists():
        raise InvalidPromptBankError(f"prompts.yaml not found at {yaml_path}")
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise InvalidPromptBankError("prompts.yaml root must be a mapping")

    meta = raw.get("meta", {})
    if not isinstance(meta, dict):
        raise InvalidPromptBankError("prompts.yaml: `meta` must be a mapping")
    locale = str(meta.get("locale", "en-IN"))
    default_barge_in = bool(meta.get("default_barge_in", True))

    prompts_raw = raw.get("prompts", {})
    if not isinstance(prompts_raw, dict) or not prompts_raw:
        raise InvalidPromptBankError("prompts.yaml: `prompts` must be a non-empty mapping")

    if "backchannel_pool" not in prompts_raw:
        raise InvalidPromptBankError("prompts.yaml: missing required `backchannel_pool`")

    bc_raw = prompts_raw.pop("backchannel_pool")
    if not isinstance(bc_raw, dict):
        raise InvalidPromptBankError("prompts.yaml: `backchannel_pool` must be a mapping")
    variants = bc_raw.get("variants", [])
    if (
        not isinstance(variants, list)
        or not variants
        or not all(isinstance(v, str) for v in variants)
    ):
        raise InvalidPromptBankError(
            "prompts.yaml: `backchannel_pool.variants` must be a non-empty list of strings"
        )
    backchannels = BackchannelPool(
        variants=tuple(variants),
        prerender=bool(bc_raw.get("prerender", True)),
        max_duration_ms=int(bc_raw.get("max_duration_ms", 400)),
    )

    prompts: dict[str, Prompt] = {}
    for prompt_id, body in prompts_raw.items():
        prompts[prompt_id] = _parse_prompt(prompt_id, body, default_barge_in)

    return PromptBank(
        prompts=prompts,
        backchannels=backchannels,
        locale=locale,
        default_barge_in=default_barge_in,
    )


def _parse_prompt(prompt_id: str, body: object, default_barge_in: bool) -> Prompt:
    if not isinstance(body, dict):
        raise InvalidPromptBankError(f"prompt {prompt_id!r}: body must be a mapping")
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        raise InvalidPromptBankError(f"prompt {prompt_id!r}: `text` must be a non-empty string")

    variables = frozenset(_PLACEHOLDER_RE.findall(text))
    unknown_vars = variables - PROMPT_VARIABLES
    if unknown_vars:
        raise InvalidPromptBankError(
            f"prompt {prompt_id!r} references unregistered template variables: "
            f"{sorted(unknown_vars)}. Add them to PROMPT_VARIABLES first."
        )

    dtmf_map = _parse_dtmf(prompt_id, body.get("dtmf"))
    side_effects_raw = body.get("side_effects", [])
    if not isinstance(side_effects_raw, list) or not all(
        isinstance(e, str) for e in side_effects_raw
    ):
        raise InvalidPromptBankError(
            f"prompt {prompt_id!r}: `side_effects` must be a list of strings"
        )

    return Prompt(
        id=prompt_id,
        text=text,
        barge_in=bool(body.get("barge_in", default_barge_in)),
        prerender=bool(body.get("prerender", True)),
        terminal=bool(body.get("terminal", False)),
        dtmf=dtmf_map,
        side_effects=tuple(side_effects_raw),
        variables=variables,
    )


def _parse_dtmf(prompt_id: str, raw: object) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise InvalidPromptBankError(f"prompt {prompt_id!r}: `dtmf` must be a mapping")
    if prompt_id not in DTMF_PROMPT_SLOTS:
        raise InvalidPromptBankError(
            f"prompt {prompt_id!r} declares a `dtmf` map but is not registered in "
            f"DTMF_PROMPT_SLOTS. Register the prompt with its slot first."
        )
    slot = DTMF_PROMPT_SLOTS[prompt_id]
    allowed = VALID_DTMF_VALUES[slot]
    parsed: dict[str, str] = {}
    for digit, value in raw.items():
        if not isinstance(digit, str) or digit not in "0123456789*#":
            raise InvalidPromptBankError(
                f"prompt {prompt_id!r}: DTMF key {digit!r} is not a valid digit"
            )
        if not isinstance(value, str) or value not in allowed:
            raise InvalidPromptBankError(
                f"prompt {prompt_id!r}: DTMF value {value!r} not in {slot} vocabulary "
                f"{sorted(allowed)}"
            )
        parsed[digit] = value
    return parsed


__all__ = [
    "DTMF_PROMPT_SLOTS",
    "PROMPT_VARIABLES",
    "VALID_DTMF_VALUES",
    "BackchannelPool",
    "ExtraTemplateVariableError",
    "InvalidPromptBankError",
    "MissingTemplateVariableError",
    "Prompt",
    "PromptBank",
    "PromptBankError",
    "UnknownPromptError",
    "load_prompt_bank",
]
