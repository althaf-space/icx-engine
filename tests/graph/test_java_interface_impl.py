"""Tests for java_interface_impl resolver."""
from __future__ import annotations
from pathlib import Path
import pytest


_FIXTURE = Path(__file__).parent / "eval" / "fixtures" / "java_interface_sample"
_SRC = _FIXTURE / "src" / "main" / "java" / "com" / "example"


def _node_id_for(rel: str) -> str:
    import re, unicodedata
    combined = rel.replace("/", "_").replace(".", "_").replace("-", "_")
    combined = unicodedata.normalize("NFKC", combined)
    cleaned = re.sub(r"[^\w]+", "_", combined, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned.strip("_").casefold()


def _make_extraction(root: Path) -> dict:
    files = list(root.rglob("*.java"))
    nodes = []
    for jf in files:
        rel = jf.relative_to(root).as_posix()
        nid = _node_id_for(rel)
        nodes.append({
            "id": nid,
            "label": jf.name,
            "source_file": str(jf),
            "file_type": "code",
        })
    svc_iface_id = _node_id_for("src/main/java/com/example/OrderService.java")
    ctrl_id = _node_id_for("src/main/java/com/example/OrderController.java")
    edges = [
        {
            "source": ctrl_id,
            "target": svc_iface_id,
            "relation": "depends_on",
            "source_file": "src/main/java/com/example/OrderController.java",
            "confidence_score": 0.95,
            "confidence_source": "spring_resolver",
        }
    ]
    return {"nodes": nodes, "edges": edges}


def test_injects_edge_emitted_for_interface_di():
    from icx_engine.graph.parser.resolvers.java_interface_impl import (
        extract_java_interface_impl_edges,
    )

    if not _FIXTURE.exists():
        pytest.skip("fixture not found")

    files = list(_FIXTURE.rglob("*.java"))
    extraction = _make_extraction(_FIXTURE)
    edges = extract_java_interface_impl_edges(files, _FIXTURE, extraction)

    injects = [e for e in edges if e.get("relation") == "injects"]
    assert len(injects) >= 1, f"expected injects edge, got: {edges}"


def test_injects_edge_target_is_impl_not_interface():
    from icx_engine.graph.parser.resolvers.java_interface_impl import (
        extract_java_interface_impl_edges,
    )

    if not _FIXTURE.exists():
        pytest.skip("fixture not found")

    files = list(_FIXTURE.rglob("*.java"))
    extraction = _make_extraction(_FIXTURE)
    edges = extract_java_interface_impl_edges(files, _FIXTURE, extraction)

    injects = [e for e in edges if e.get("relation") == "injects"]
    assert injects, "no injects edges"
    impl_id = _node_id_for("src/main/java/com/example/OrderServiceImpl.java")
    targets = {e["target"] for e in injects}
    assert impl_id in targets, f"expected impl node {impl_id!r} in targets {targets}"


def test_injects_confidence_is_framework_resolved():
    from icx_engine.graph.parser.resolvers.java_interface_impl import (
        extract_java_interface_impl_edges,
    )

    if not _FIXTURE.exists():
        pytest.skip("fixture not found")

    files = list(_FIXTURE.rglob("*.java"))
    extraction = _make_extraction(_FIXTURE)
    edges = extract_java_interface_impl_edges(files, _FIXTURE, extraction)

    injects = [e for e in edges if e.get("relation") == "injects"]
    for e in injects:
        assert e.get("confidence_score") == 0.95
        assert e.get("confidence_source") == "java_interface_impl"


def test_no_injects_when_no_impl_exists():
    from icx_engine.graph.parser.resolvers.java_interface_impl import (
        extract_java_interface_impl_edges,
    )

    if not _FIXTURE.exists():
        pytest.skip("fixture not found")

    svc_iface_id = _node_id_for("src/main/java/com/example/OrderService.java")
    nodes = [{"id": svc_iface_id, "label": "OrderService.java",
              "source_file": str(_SRC / "OrderService.java"),
              "file_type": "code"}]
    edges = [{"source": "ctrl", "target": svc_iface_id, "relation": "depends_on",
              "source_file": "ctrl", "confidence_score": 0.95}]
    extraction = {"nodes": nodes, "edges": edges}

    result = extract_java_interface_impl_edges(
        [_SRC / "OrderService.java"],
        _FIXTURE,
        extraction,
    )
    injects = [e for e in result if e.get("relation") == "injects"]
    assert len(injects) == 0
