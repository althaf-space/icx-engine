"""Tests for graph/mcp_tools.py dispatch - focused on the truncation fix for
graph_call_chain/graph_impact (previously returned raw uncapped payloads, unlike
graph_find_context's own char-budget truncation, so a high-fan-out node like a central
entity would blow past the MCP host's own hard token cap and fail outright with no partial
answer at all)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_graphs_root(tmp_path, monkeypatch):
    graphs_root = tmp_path / "graphs"
    graphs_root.mkdir()
    monkeypatch.setattr("icx_engine.graph.storage._graphs_root", lambda: graphs_root)


def _write_fanout_graph(tmp_path: Path, n_direct: int = 5, n_transitive: int = 300) -> tuple[str, str]:
    """A "central" node with n_direct direct upstream callers, each of which (for the
    first direct caller only) is itself called by n_transitive further-upstream nodes -
    mirrors a real central entity's fan-in shape (a handful of direct dependents, a much
    larger transitive set)."""
    from icx_engine.graph import storage as st

    project_dir = tmp_path / "proj"
    project_dir.mkdir(exist_ok=True)
    project_id = st.derive_project_id(project_dir.resolve())

    nodes = [{"id": "central", "label": "CentralEntity", "source_file": "src/central.py",
              "community": 0, "role_tag": "", "importance": 0.9}]
    links = []
    for i in range(n_direct):
        nid = f"direct_{i}"
        nodes.append({"id": nid, "label": f"Direct{i}", "source_file": f"src/direct_{i}.py",
                      "community": 0, "role_tag": ""})
        links.append({"source": nid, "target": "central", "relation": "calls",
                      "confidence_score": 0.95, "confidence_source": "x", "resolver_tag": "x"})
    for i in range(n_transitive):
        nid = f"trans_{i}"
        # Long label/path so each entry has real weight against a small token_budget.
        nodes.append({
            "id": nid,
            "label": f"VeryLongTransitiveDependentClassName{i}",
            "source_file": f"src/very/deeply/nested/package/structure/trans_{i}.py",
            "community": 0, "role_tag": "",
        })
        links.append({"source": nid, "target": "direct_0", "relation": "calls",
                      "confidence_score": 0.95, "confidence_source": "x", "resolver_tag": "x"})

    graph = {"nodes": nodes, "links": links, "communities": {"0": [n["id"] for n in nodes]}}
    gpath = st.graph_path(project_id)
    gpath.parent.mkdir(parents=True, exist_ok=True)
    gpath.write_text(json.dumps(graph), encoding="utf-8")
    return str(project_dir), project_id


def _patch_resolve(monkeypatch, project_dir: str, project_id: str):
    from icx_engine.graph import mcp_tools as gmt
    monkeypatch.setattr(
        gmt, "_resolve_graph_path",
        lambda raw_path: (Path(project_dir), project_id, None),
    )


# -- graph_impact ----------------------------------------------------------------------

async def test_graph_impact_truncates_transitive_but_preserves_true_total(monkeypatch, tmp_path):
    from icx_engine.graph import mcp_tools as gmt

    project_dir, project_id = _write_fanout_graph(tmp_path, n_direct=5, n_transitive=300)
    _patch_resolve(monkeypatch, project_dir, project_id)

    result = await gmt.dispatch_graph_tool("graph_impact", {
        "project_path": project_dir, "node_id": "central",
        "min_confidence": 0.5, "token_budget": 200,
    })
    data = json.loads(result[0].text)

    assert data["status"] == "ok"
    assert data["truncated"] is True
    assert len(data["direct"]) == 5, "direct dependents must always be returned in full"
    assert len(data["transitive"]) < 300, "transitive list must be capped under a tiny budget"
    assert data["total"] == 305, "true total (direct + full transitive) must never be altered"
    assert data["transitive_total"] == 300
    assert data["transitive_returned"] == len(data["transitive"])
    assert "note" in data and "token_budget" in data["note"]


async def test_graph_impact_no_truncation_note_when_small(monkeypatch, tmp_path):
    from icx_engine.graph import mcp_tools as gmt

    project_dir, project_id = _write_fanout_graph(tmp_path, n_direct=2, n_transitive=1)
    _patch_resolve(monkeypatch, project_dir, project_id)

    result = await gmt.dispatch_graph_tool("graph_impact", {
        "project_path": project_dir, "node_id": "central", "min_confidence": 0.5,
    })
    data = json.loads(result[0].text)

    assert "truncated" not in data
    assert len(data["transitive"]) == 1
    assert data["total"] == 3


async def test_graph_impact_result_is_json_serializable_under_default_budget(monkeypatch, tmp_path):
    """Regression guard for the original bug: a large fan-out node must always produce a
    parseable, bounded JSON response - never an unbounded payload."""
    from icx_engine.graph import mcp_tools as gmt

    project_dir, project_id = _write_fanout_graph(tmp_path, n_direct=5, n_transitive=2000)
    _patch_resolve(monkeypatch, project_dir, project_id)

    result = await gmt.dispatch_graph_tool("graph_impact", {
        "project_path": project_dir, "node_id": "central", "min_confidence": 0.5,
    })
    text = result[0].text
    data = json.loads(text)  # must not raise
    assert data["total"] == 2005
    assert len(text) < 8000 * 4 * 1.5  # bounded, with slack for payload overhead


# -- graph_call_chain ------------------------------------------------------------------

async def test_graph_call_chain_truncates_upstream_but_preserves_true_total(monkeypatch, tmp_path):
    from icx_engine.graph import mcp_tools as gmt

    project_dir, project_id = _write_fanout_graph(tmp_path, n_direct=5, n_transitive=300)
    _patch_resolve(monkeypatch, project_dir, project_id)

    result = await gmt.dispatch_graph_tool("graph_call_chain", {
        "project_path": project_dir, "node_id": "central",
        "depth": 3, "min_confidence": 0.5, "token_budget": 200,
    })
    data = json.loads(result[0].text)

    assert data["status"] == "ok"
    assert data["truncated"] is True
    assert data["upstream_total"] == 305   # 5 direct + 300 transitive, all within depth=3
    assert len(data["upstream"]) < 305
    assert data["downstream_total"] == 0
    assert "note" in data


async def test_graph_call_chain_no_truncation_when_small(monkeypatch, tmp_path):
    from icx_engine.graph import mcp_tools as gmt

    project_dir, project_id = _write_fanout_graph(tmp_path, n_direct=2, n_transitive=0)
    _patch_resolve(monkeypatch, project_dir, project_id)

    result = await gmt.dispatch_graph_tool("graph_call_chain", {
        "project_path": project_dir, "node_id": "central", "depth": 3, "min_confidence": 0.5,
    })
    data = json.loads(result[0].text)

    assert "truncated" not in data
    assert len(data["upstream"]) == 2
    assert data["upstream_total"] == 2


async def test_graph_call_chain_keeps_nearest_depth_first_when_capped(monkeypatch, tmp_path):
    """The two direct (depth=1) callers must survive truncation ahead of the many
    depth=2 transitive ones - nearest-first is the priority order this fix chose."""
    from icx_engine.graph import mcp_tools as gmt

    project_dir, project_id = _write_fanout_graph(tmp_path, n_direct=2, n_transitive=300)
    _patch_resolve(monkeypatch, project_dir, project_id)

    result = await gmt.dispatch_graph_tool("graph_call_chain", {
        "project_path": project_dir, "node_id": "central",
        "depth": 3, "min_confidence": 0.5, "token_budget": 50,
    })
    data = json.loads(result[0].text)
    kept_depths = [n["depth"] for n in data["upstream"]]
    assert kept_depths == sorted(kept_depths)
    assert kept_depths[0] == 1
