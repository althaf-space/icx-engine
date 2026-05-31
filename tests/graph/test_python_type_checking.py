"""Tests for python_type_checking resolver."""
from __future__ import annotations
from pathlib import Path
import pytest


_FIXTURE = Path(__file__).parent / "eval" / "fixtures" / "python_types_sample"


def _node_id(rel: str) -> str:
    import re, unicodedata
    combined = rel.replace("/", "_").replace(".", "_").replace("-", "_")
    combined = unicodedata.normalize("NFKC", combined)
    cleaned = re.sub(r"[^\w]+", "_", combined, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned.strip("_").casefold()


def _make_extraction(root: Path) -> dict:
    nodes = []
    for py in (root / "myapp").rglob("*.py"):
        rel = py.relative_to(root).as_posix()
        nodes.append({
            "id": _node_id(rel),
            "label": py.name,
            "source_file": str(py),
            "file_type": "code",
        })
    protocols_id = _node_id("myapp/protocols.py")
    email_id = _node_id("myapp/email_notifier.py")
    barrel_id = _node_id("myapp/barrel.py")
    edges = [
        {"source": email_id, "target": protocols_id, "relation": "imports",
         "source_file": str(root / "myapp" / "email_notifier.py"), "confidence_score": 1.0},
        {"source": barrel_id, "target": protocols_id, "relation": "imports",
         "source_file": str(root / "myapp" / "barrel.py"), "confidence_score": 1.0},
        {"source": barrel_id, "target": email_id, "relation": "imports",
         "source_file": str(root / "myapp" / "barrel.py"), "confidence_score": 1.0},
    ]
    return {"nodes": nodes, "edges": edges}


def test_all_barrel_emits_exports_edges():
    from icx_engine.graph.parser.resolvers.python_type_checking import (
        extract_python_type_checking_edges,
    )

    if not _FIXTURE.exists():
        pytest.skip("fixture not found")

    files = list((_FIXTURE / "myapp").rglob("*.py"))
    extraction = _make_extraction(_FIXTURE)
    edges = extract_python_type_checking_edges(files, _FIXTURE, extraction)

    exports = [e for e in edges if e.get("relation") == "exports"]
    assert len(exports) >= 2, f"expected >=2 exports edges from barrel, got {exports}"


def test_exports_edge_tagged_python_exports():
    from icx_engine.graph.parser.resolvers.python_type_checking import (
        extract_python_type_checking_edges,
    )

    if not _FIXTURE.exists():
        pytest.skip("fixture not found")

    files = list((_FIXTURE / "myapp").rglob("*.py"))
    extraction = _make_extraction(_FIXTURE)
    edges = extract_python_type_checking_edges(files, _FIXTURE, extraction)

    exports = [e for e in edges if e.get("relation") == "exports"]
    for e in exports:
        assert e.get("confidence_source") == "python_exports"


def test_protocol_impl_emits_implements_protocol_edge():
    from icx_engine.graph.parser.resolvers.python_type_checking import (
        extract_python_type_checking_edges,
    )

    if not _FIXTURE.exists():
        pytest.skip("fixture not found")

    files = list((_FIXTURE / "myapp").rglob("*.py"))
    extraction = _make_extraction(_FIXTURE)

    # Add inherits edge from email_notifier to protocols (simulating python_jedi output)
    email_id = _node_id("myapp/email_notifier.py")
    protocols_id = _node_id("myapp/protocols.py")
    extraction["edges"].append({
        "source": email_id,
        "target": protocols_id,
        "relation": "inherits",
        "source_file": str(_FIXTURE / "myapp" / "email_notifier.py"),
        "confidence_score": 1.0,
    })

    edges = extract_python_type_checking_edges(files, _FIXTURE, extraction)

    proto_edges = [e for e in edges if e.get("relation") == "implements_protocol"]
    assert len(proto_edges) >= 1, f"expected implements_protocol edge, got {edges}"


def test_dataclass_field_emits_uses_edge():
    from icx_engine.graph.parser.resolvers.python_type_checking import (
        extract_python_type_checking_edges,
    )

    if not _FIXTURE.exists():
        pytest.skip("fixture not found")

    files = list((_FIXTURE / "myapp").rglob("*.py"))
    extraction = _make_extraction(_FIXTURE)
    edges = extract_python_type_checking_edges(files, _FIXTURE, extraction)

    dc_edges = [e for e in edges if e.get("confidence_source") == "python_dataclass"]
    assert len(dc_edges) >= 1, f"expected python_dataclass uses edge, got {edges}"
