"""Call-scoped state for a single voice conversation.

Every field a per-turn handler needs is here. The object is persisted to
Redis after every transition (`SETEX`, TTL 2 h) so that a worker crash
mid-call leaves enough context for the post-call enrichment DAG to run
even though the call itself has dropped. See spec §8.3.

Kept intentionally free of I/O and external types — this module has to
be importable by the property tests without pulling Redis, Twilio, or
the audio path along."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class Slot(StrEnum):
    """The six v1 slots plus one conditional. Strings match the JSON
    payload persisted to Redis and, later, the `reports` columns."""

    INTENT = "intent"
    HAZARD_TYPE = "hazard_type"
    DESCRIPTION = "description"
    LOCATION = "location"
    SEVERITY = "severity"
    WATER_DEPTH_CM = "water_depth_cm"
    CONFIRMATION = "confirmation"


SlotSource = Literal["asr", "dtmf", "rag", "keyword", "llm"]


class NodeId(StrEnum):
    """Every reachable node in the DAG (§8.1). String values are what
    persist to `CallState.current_node` and appear in observability."""

    START = "START"
    CONSENT = "CONSENT"
    ASK_INTENT = "ASK_INTENT"
    NOT_REPORTING = "NOT_REPORTING"
    ASK_HAZARD_TYPE = "ASK_HAZARD_TYPE"
    ASK_DESCRIPTION = "ASK_DESCRIPTION"
    ASK_LOCATION = "ASK_LOCATION"
    RESOLVE_LOCATION = "RESOLVE_LOCATION"
    CONFIRM_LOCATION_LOW_CONF = "CONFIRM_LOCATION_LOW_CONF"
    DISAMBIGUATE_LOCATION = "DISAMBIGUATE_LOCATION"
    ASK_SEVERITY = "ASK_SEVERITY"
    ASK_DEPTH = "ASK_DEPTH"
    CONFIRM_SUMMARY = "CONFIRM_SUMMARY"
    START_OVER = "START_OVER"
    SUBMIT = "SUBMIT"
    SUBMITTED = "SUBMITTED"
    EMERGENCY_REDIRECT = "EMERGENCY_REDIRECT"
    TIMEOUT_EXIT = "TIMEOUT_EXIT"
    FATAL_FALLBACK = "FATAL_FALLBACK"
    END = "END"


TERMINAL_NODES: frozenset[NodeId] = frozenset(
    {
        NodeId.NOT_REPORTING,
        NodeId.SUBMITTED,
        NodeId.TIMEOUT_EXIT,
        NodeId.FATAL_FALLBACK,
        NodeId.END,
    }
)


# Hazard classes that unlock the conditional ASK_DEPTH node. Kept as a
# frozenset so a hostile edit cannot mutate it at runtime.
FLOOD_CLASS_HAZARDS: frozenset[str] = frozenset(
    {"abnormal_tide", "storm", "flood", "tsunami"}
)


class SlotValue(BaseModel):
    """The typed contents of a filled slot. `value` is the canonical
    (post-normalise) form — enum string for categorical slots, int for
    `water_depth_cm`, free text for `description`/`location`."""

    model_config = ConfigDict(frozen=True)

    value: str | int
    confidence: float = Field(ge=0.0, le=1.0)
    source: SlotSource
    raw_transcript: str | None = None
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TurnMetrics(BaseModel):
    """Per-turn latency breakdown. Populated by the pipeline as each
    stage finishes; unfilled stages stay None so absence is visible in
    dashboards rather than silently zero."""

    model_config = ConfigDict(frozen=False)

    asr_ms: int | None = None
    extract_ms: int | None = None
    resolve_ms: int | None = None
    tts_ttfb_ms: int | None = None
    total_ms: int | None = None


class Turn(BaseModel):
    """One caller/agent exchange. Appended in order; never mutated
    once ended, so the post-call DAG can trust the transcript."""

    model_config = ConfigDict(frozen=False)

    turn_index: int
    node_id: NodeId
    agent_prompt_id: str
    caller_utterance: str | None = None
    extractor_output: SlotValue | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    metrics: TurnMetrics = Field(default_factory=TurnMetrics)


class CallState(BaseModel):
    """Everything a worker needs to resume a call after a crash. Kept
    Pydantic-first so `model_dump_json()` / `model_validate_json()` are
    the on-the-wire format to Redis."""

    model_config = ConfigDict(frozen=False)

    call_sid: str
    report_id: UUID = Field(default_factory=uuid4)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    caller_hash: str
    direction: Literal["inbound", "outbound"] = "inbound"
    current_node: NodeId = NodeId.START
    attempt: int = 0
    slots: dict[Slot, SlotValue] = Field(default_factory=dict)
    turns: list[Turn] = Field(default_factory=list)
    flags: set[str] = Field(default_factory=set)
    keyterms: list[str] = Field(default_factory=list)
    metrics: TurnMetrics = Field(default_factory=TurnMetrics)
    # Set on entering EMERGENCY_REDIRECT so the graph can restore the
    # slot-collection node the caller was on. None until first tripwire.
    resume_after_emergency: NodeId | None = None
    # Populated when the driver writes the report on entering SUBMIT.
    # Consumed by the terminal `submitted` prompt so the caller hears
    # a real DB-minted reference, not a client-side placeholder.
    short_ref: str | None = None

    def set_slot(self, slot: Slot, value: SlotValue) -> None:
        self.slots[slot] = value

    def get_slot(self, slot: Slot) -> SlotValue | None:
        return self.slots.get(slot)

    def add_flag(self, flag: str) -> None:
        self.flags.add(flag)

    def hazard_is_flood_class(self) -> bool:
        hz = self.slots.get(Slot.HAZARD_TYPE)
        return hz is not None and isinstance(hz.value, str) and hz.value in FLOOD_CLASS_HAZARDS

    def is_terminal(self) -> bool:
        return self.current_node in TERMINAL_NODES

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, blob: str) -> CallState:
        return cls.model_validate_json(blob)


__all__ = [
    "FLOOD_CLASS_HAZARDS",
    "TERMINAL_NODES",
    "CallState",
    "NodeId",
    "Slot",
    "SlotSource",
    "SlotValue",
    "Turn",
    "TurnMetrics",
]
