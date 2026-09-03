"""Per-node EOT overrides on the conversation graph.

Coverage:
- Node dataclass accepts optional eot_threshold_override and
  eot_timeout_ms_override (defaults None)
- effective_eot returns the override when set
- effective_eot falls back to the default when the override is None
- Known-value spot checks on the shipped graph — LOCATION has an
  extended timeout, CONFIRM_SUMMARY has a tight one, ASK_INTENT has
  no override (uses default)
- EotConfig is frozen (safety — a stray mutation would silently
  change EOT for one call and not the next)
"""

from __future__ import annotations

import pytest

from fg_voice.conversation.graph import EotConfig, build_graph
from fg_voice.conversation.state import NodeId


@pytest.fixture(scope="module")
def graph():
    return build_graph()


# ─── Default vs override behaviour ───────────────────────────────────


def test_effective_eot_falls_back_to_default_when_no_override(graph):
    """Nodes with no override (e.g. ASK_INTENT) return the caller-
    supplied defaults verbatim."""
    result = graph.effective_eot(
        NodeId.ASK_INTENT,
        default_threshold=0.7,
        default_timeout_ms=1200,
    )
    assert result == EotConfig(threshold=0.7, timeout_ms=1200)


def test_effective_eot_uses_threshold_override(graph):
    """ASK_LOCATION has a lower threshold override so Flux doesn't
    fire EOT during a mid-utterance pause."""
    result = graph.effective_eot(
        NodeId.ASK_LOCATION,
        default_threshold=0.7,
        default_timeout_ms=1200,
    )
    assert result.threshold == 0.6  # override
    assert result.timeout_ms == 2000  # override


def test_effective_eot_uses_timeout_override_only(graph):
    """ASK_DESCRIPTION overrides only the timeout — threshold stays
    at the caller default. Verifies the two overrides are
    independent."""
    result = graph.effective_eot(
        NodeId.ASK_DESCRIPTION,
        default_threshold=0.7,
        default_timeout_ms=1200,
    )
    assert result.threshold == 0.7  # default
    assert result.timeout_ms == 1800  # override


def test_effective_eot_confirmation_nodes_are_tight(graph):
    """CONFIRM_SUMMARY + CONFIRM_LOCATION_LOW_CONF are yes/no answers
    — short + fast. Both get a tighter threshold + shorter timeout
    than the default so the caller isn't left hanging after a
    one-syllable reply."""
    for node_id in (NodeId.CONFIRM_SUMMARY, NodeId.CONFIRM_LOCATION_LOW_CONF):
        result = graph.effective_eot(
            node_id,
            default_threshold=0.7,
            default_timeout_ms=1200,
        )
        assert result.threshold > 0.7, f"{node_id} threshold not tightened"
        assert result.timeout_ms < 1200, f"{node_id} timeout not tightened"


# ─── Independence of overrides ───────────────────────────────────────


def test_eot_defaults_propagate_when_both_overrides_unset(graph):
    """No node uses ONLY the timeout override without the threshold —
    a change to defaults must therefore propagate to nodes without
    overrides. Sanity that no accidental hard-coding leaked in."""
    for node_id in (
        NodeId.ASK_INTENT,
        NodeId.ASK_HAZARD_TYPE,
        NodeId.ASK_SEVERITY,
        NodeId.ASK_DEPTH,
    ):
        r1 = graph.effective_eot(node_id, default_threshold=0.5, default_timeout_ms=500)
        r2 = graph.effective_eot(node_id, default_threshold=0.9, default_timeout_ms=3000)
        assert r1.threshold != r2.threshold, f"{node_id} pinned threshold"
        assert r1.timeout_ms != r2.timeout_ms, f"{node_id} pinned timeout"


# ─── EotConfig invariants ────────────────────────────────────────────


def test_eot_config_is_frozen():
    """A stray mutation on the returned EotConfig would silently change
    EOT for one call and not the next (if the object were cached).
    Frozen dataclass guards against it."""
    cfg = EotConfig(threshold=0.7, timeout_ms=1200)
    with pytest.raises((AttributeError, TypeError)):
        cfg.threshold = 0.5  # type: ignore[misc]


# ─── Machine nodes ───────────────────────────────────────────────────


def test_machine_nodes_still_resolve_eot(graph):
    """`RESOLVE_LOCATION` + `SUBMIT` are machine nodes — they never
    listen to the caller, so EOT technically doesn't matter. But
    `effective_eot` must still return the defaults rather than
    crash — the runner might call it defensively before checking
    is_machine."""
    result = graph.effective_eot(
        NodeId.RESOLVE_LOCATION,
        default_threshold=0.7,
        default_timeout_ms=1200,
    )
    assert result == EotConfig(threshold=0.7, timeout_ms=1200)


def test_unknown_node_raises(graph):
    """Sanity that `effective_eot` bubbles the same UnknownNodeError
    that `node()` raises — a stale caller reaching in with a bad id
    should get a loud failure, not a defaults-fallback."""
    from fg_voice.conversation.graph import UnknownNodeError

    with pytest.raises(UnknownNodeError):
        graph.effective_eot(
            "does_not_exist",  # type: ignore[arg-type]
            default_threshold=0.7,
            default_timeout_ms=1200,
        )
