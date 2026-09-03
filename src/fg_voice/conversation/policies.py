"""Re-prompt ladder, confidence gates, and per-node timing (§7.3).

Kept as pure data + pure functions: no I/O, no network, no clock.
Consumers pass `attempt` in and get an action back — testing the ladder
does not require simulating time or Redis."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from fg_voice.conversation.state import NodeId


class RepromptAction(StrEnum):
    """What to do next when a slot did not fill on this attempt."""

    RETRY_SOFT = "retry_soft"  # attempt 1 — reprompt_<slot>_1
    RETRY_WITH_OPTIONS = "retry_options"  # attempt 2 — reprompt_<slot>_2 + DTMF armed
    DTMF_ONLY = "dtmf_only"  # attempt 3 — categorical only: force DTMF
    ACCEPT_LOW_CONF = "accept_low"  # attempt 3 — free-text: keep what we heard
    TIMEOUT_EXIT = "timeout_exit"  # attempt 4+ — hang up gracefully


@dataclass(frozen=True, slots=True)
class NodeTiming:
    """Per-node knobs from §7.3 and §9.2. `eot_threshold` and
    `eot_timeout_ms` are consumed by the STT layer via
    Flux config updates (§9.2); the rest are enforced by the graph
    runner and the turn watchdog."""

    no_input_timeout_ms: int = 6_000
    max_turn_ms: int = 15_000
    eot_threshold: float = 0.7
    eot_timeout_ms: int = 1_200


# Global caps that override any per-node value.
MAX_CALL_DURATION_SEC: Final[int] = 300
MAX_ATTEMPTS_CATEGORICAL: Final[int] = 3
MAX_ATTEMPTS_FREE_TEXT: Final[int] = 3

# The categorical slots collect from a bounded enum — after attempt 3
# we can force DTMF-only. Free-text slots (description, location) must
# accept low-confidence input rather than trap the caller (§7.3 row 3).
CATEGORICAL_NODES: Final[frozenset[NodeId]] = frozenset(
    {
        NodeId.ASK_INTENT,
        NodeId.ASK_HAZARD_TYPE,
        NodeId.ASK_SEVERITY,
        NodeId.ASK_DEPTH,
        NodeId.CONFIRM_SUMMARY,
    }
)

FREE_TEXT_NODES: Final[frozenset[NodeId]] = frozenset(
    {
        NodeId.ASK_DESCRIPTION,
        NodeId.ASK_LOCATION,
    }
)


# Per-node timing. `ask_description` gets a longer EOT because callers
# pause mid-sentence when describing damage; categorical slots get the
# tighter default so short answers ("moderate") don't wait unnecessarily.
NODE_TIMING: Final[dict[NodeId, NodeTiming]] = {
    NodeId.ASK_DESCRIPTION: NodeTiming(
        no_input_timeout_ms=8_000,
        max_turn_ms=25_000,
        eot_threshold=0.8,
        eot_timeout_ms=2_500,
    ),
    NodeId.ASK_LOCATION: NodeTiming(
        no_input_timeout_ms=7_000,
        max_turn_ms=15_000,
        eot_threshold=0.75,
        eot_timeout_ms=1_500,
    ),
}


def timing_for(node_id: NodeId) -> NodeTiming:
    return NODE_TIMING.get(node_id, NodeTiming())


# Reprompt ladder: which prompt_id to play for a given collection node
# on attempt N (N counted from 1). If the ladder does not have an entry
# for the attempt number, `next_action` returns TIMEOUT_EXIT.
REPROMPT_LADDER: Final[dict[NodeId, tuple[str, ...]]] = {
    NodeId.ASK_INTENT: ("reprompt_intent_1", "reprompt_intent_2"),
    NodeId.ASK_HAZARD_TYPE: ("reprompt_hazard_type_1", "reprompt_hazard_type_2"),
    NodeId.ASK_DESCRIPTION: ("reprompt_description_1", "reprompt_description_2"),
    NodeId.ASK_LOCATION: ("reprompt_location_1", "reprompt_location_2"),
    NodeId.ASK_SEVERITY: ("reprompt_severity_1", "reprompt_severity_2"),
    NodeId.ASK_DEPTH: ("reprompt_depth_1",),
    NodeId.CONFIRM_SUMMARY: ("reprompt_confirm_1",),
}


def next_action(node_id: NodeId, attempt: int) -> RepromptAction:
    """`attempt` is the count of prior failed attempts on this node."""
    if attempt <= 0:
        return RepromptAction.RETRY_SOFT
    max_attempts = (
        MAX_ATTEMPTS_CATEGORICAL if node_id in CATEGORICAL_NODES else MAX_ATTEMPTS_FREE_TEXT
    )
    if attempt == 1:
        return RepromptAction.RETRY_WITH_OPTIONS
    if attempt < max_attempts:
        if node_id in CATEGORICAL_NODES:
            return RepromptAction.DTMF_ONLY
        return RepromptAction.ACCEPT_LOW_CONF
    if attempt == max_attempts:
        if node_id in CATEGORICAL_NODES:
            return RepromptAction.DTMF_ONLY
        return RepromptAction.ACCEPT_LOW_CONF
    return RepromptAction.TIMEOUT_EXIT


def reprompt_id_for(node_id: NodeId, attempt: int) -> str | None:
    """Return the prompt_id to play on this attempt, or None if the
    caller has exhausted the ladder and we should exit."""
    ladder = REPROMPT_LADDER.get(node_id, ())
    idx = attempt - 1
    if 0 <= idx < len(ladder):
        return ladder[idx]
    return None


# ─── Confidence gates (§9.4) ─────────────────────────────────────────

STT_TURN_CONFIDENCE_THRESHOLD: Final[float] = 0.55
EXTRACTION_CONFIDENCE_THRESHOLD: Final[float] = 0.60
GAZETTEER_ACCEPT_THRESHOLD: Final[float] = 0.85
GAZETTEER_CONFIRM_THRESHOLD: Final[float] = 0.60
GAZETTEER_MARGIN_THRESHOLD: Final[float] = 0.10
HAZARD_KNN_MARGIN_THRESHOLD: Final[float] = 0.15


def is_extraction_confident(confidence: float) -> bool:
    return confidence >= EXTRACTION_CONFIDENCE_THRESHOLD


def is_stt_turn_confident(confidence: float) -> bool:
    return confidence >= STT_TURN_CONFIDENCE_THRESHOLD


__all__ = [
    "CATEGORICAL_NODES",
    "EXTRACTION_CONFIDENCE_THRESHOLD",
    "FREE_TEXT_NODES",
    "GAZETTEER_ACCEPT_THRESHOLD",
    "GAZETTEER_CONFIRM_THRESHOLD",
    "GAZETTEER_MARGIN_THRESHOLD",
    "HAZARD_KNN_MARGIN_THRESHOLD",
    "MAX_ATTEMPTS_CATEGORICAL",
    "MAX_ATTEMPTS_FREE_TEXT",
    "MAX_CALL_DURATION_SEC",
    "NODE_TIMING",
    "REPROMPT_LADDER",
    "STT_TURN_CONFIDENCE_THRESHOLD",
    "NodeTiming",
    "RepromptAction",
    "is_extraction_confident",
    "is_stt_turn_confident",
    "next_action",
    "reprompt_id_for",
    "timing_for",
]
