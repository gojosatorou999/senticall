"""Static invariants on the conversation DAG (§8.2).

These are the checks that would fail at boot if the graph diverged
from the prompt bank or introduced an orphan node."""

from __future__ import annotations

import pytest

from fg_voice.conversation.graph import (
    ExtractorId,
    build_graph,
    is_terminal_node,
)
from fg_voice.conversation.nodes import slot_for
from fg_voice.conversation.prompt_bank import load_prompt_bank
from fg_voice.conversation.state import NodeId


@pytest.fixture(scope="module")
def graph():
    return build_graph()


@pytest.fixture(scope="module")
def bank():
    return load_prompt_bank()


def test_every_declared_nodeid_has_a_node(graph):
    for nid in NodeId:
        assert nid in graph.nodes, f"NodeId {nid} declared but not present in graph"


def test_every_node_reachable_from_start(graph):
    reachable = graph.reachable_from_start()
    orphans = set(graph.nodes) - set(reachable)
    assert not orphans, f"orphan nodes in the DAG: {orphans}"


def test_every_prompt_id_exists_in_bank(graph, bank):
    ids = bank.ids()
    for node in graph.nodes.values():
        if node.prompt_id is None:
            continue
        assert node.prompt_id in ids, f"node {node.id} references missing prompt {node.prompt_id!r}"


def test_terminal_nodes_present(graph):
    terminals = {nid for nid, n in graph.nodes.items() if is_terminal_node(n)}
    assert NodeId.NOT_REPORTING in terminals
    assert NodeId.SUBMITTED in terminals
    assert NodeId.TIMEOUT_EXIT in terminals
    assert NodeId.FATAL_FALLBACK in terminals
    assert NodeId.END in terminals


def test_end_has_no_outgoing_edges(graph):
    assert graph.node(NodeId.END).transitions == ()


def test_terminal_nodes_go_only_to_end(graph):
    for nid in (NodeId.NOT_REPORTING, NodeId.SUBMITTED, NodeId.TIMEOUT_EXIT, NodeId.FATAL_FALLBACK):
        for edge in graph.node(nid).transitions:
            assert edge.target is NodeId.END, (
                f"{nid} should terminate at END, edge targets {edge.target}"
            )


def test_start_is_a_machine_node_that_reaches_consent(graph):
    n = graph.node(NodeId.START)
    assert n.is_machine
    targets = {e.target for e in n.transitions}
    assert NodeId.CONSENT in targets


def test_extractor_dispatch_covers_every_extractor(graph):
    seen: set[ExtractorId] = set()
    for node in graph.nodes.values():
        seen.add(node.extractor)
    # Every extractor referenced by the graph must have a slot mapping
    # (except NONE, which is machine/terminal).
    for extractor in seen - {ExtractorId.NONE}:
        assert slot_for(extractor) is not None, f"extractor {extractor!r} has no slot mapping"


def test_no_prompted_node_has_zero_transitions(graph):
    """A collection node that can't advance is a soft-lock — always a bug.
    Terminal nodes are allowed to have a single →END transition."""
    for node in graph.nodes.values():
        if node.prompt_id is None:
            continue
        assert node.transitions, f"node {node.id} has no outgoing edges"


def test_confirm_summary_has_yes_no_restart_branches(graph):
    node = graph.node(NodeId.CONFIRM_SUMMARY)
    targets = {e.target for e in node.transitions}
    assert NodeId.SUBMIT in targets
    assert NodeId.START_OVER in targets
