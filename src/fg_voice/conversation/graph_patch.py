"""Repair for a defect in the recovered `graph` bytecode.

`conversation/graph.py` (and driver/nodes/runner, plus api/routes_gather)
had no surviving source in this tree — only `__pycache__` bytecode, which
was restored as sourceless `.pyc` modules. One of those carries a bug:

The single edge out of ASK_HAZARD_TYPE is guarded on the hazard slot
being in

    frozenset({'erosion', 'abnormal_tide', 'storm', 'other', 'sludge_oil'})

which omits `flood` and `tsunami`. Those two therefore fill the slot but
match no transition, so the driver re-prompts, exhausts its attempts, and
hangs up — the two most important hazards on a coastal hotline are the
only two that cannot be reported.

Nothing else in the graph depends on that set: the flood-specific depth
question hangs off ASK_SEVERITY -> ASK_DEPTH, which is wired correctly.
So the fix is simply to widen the guard to the full hazard vocabulary
declared in `prompt_bank.VALID_DTMF_VALUES`.

The guard is a closure over the frozenset; rebinding the cell preserves
the original comparison logic exactly rather than reimplementing it.
Delete this module once `graph.py` is restored from source control with
the set corrected at its definition.
"""

from __future__ import annotations

from fg_voice.conversation.prompt_bank import VALID_DTMF_VALUES
from fg_voice.conversation.state import NodeId
from fg_voice.obs.logging import get_logger

log = get_logger(__name__)

ALL_HAZARD_VALUES = frozenset(VALID_DTMF_VALUES["hazard_type"])

# `routes_gather` builds a fresh graph per call, so the repair runs on
# every request. Log the (unchanging) finding once instead of per call.
_logged = False


def repair_graph(graph: object) -> object:
    """Widen the ASK_HAZARD_TYPE guard in-place; returns the same graph."""
    global _logged
    node = graph.node(NodeId.ASK_HAZARD_TYPE)  # type: ignore[attr-defined]
    for edge in node.transitions:
        for cell in edge.guard.__closure__ or ():
            value = cell.cell_contents
            if not isinstance(value, frozenset) or not value:
                continue
            if not value < ALL_HAZARD_VALUES:
                continue
            missing = sorted(ALL_HAZARD_VALUES - value)
            cell.cell_contents = ALL_HAZARD_VALUES
            if not _logged:
                _logged = True
                log.warning(
                    "graph.hazard_guard_repaired",
                    edge=edge.label,
                    added=missing,
                    note="recovered bytecode omitted these hazard values",
                )
    return graph


def apply() -> None:
    """Make `routes_gather` build repaired graphs.

    `routes_gather` is sourceless bytecode. Its `_graph_builder` is the
    original `build_graph` function object, bound at import and called per
    request — so the module attribute `build_graph` is never consulted and
    rebinding it has no effect. `_graph_builder` is the real seam."""
    from fg_voice.api import routes_gather

    original = routes_gather._graph_builder

    def build_graph_repaired():  # type: ignore[no-untyped-def]
        return repair_graph(original())

    routes_gather._graph_builder = build_graph_repaired
    routes_gather.build_graph = build_graph_repaired


__all__ = ["ALL_HAZARD_VALUES", "apply", "repair_graph"]
