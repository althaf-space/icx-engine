"""Tests for local-variable-declaration type-reference extraction in java_symbols.py.

Real javalang parsing against files on disk, mirroring test_java_inferred_upgrade.py's style -
this is the entry point (extract_java_edges), not the internal _emit_body_refs closure, so it
exercises the real regression: a local variable declared with a generic type argument
(`CriteriaQuery<UserMaster> cq = ...`) must emit a "uses" edge to the generic argument's type,
the same way a field/param/return-type declaration already does.

Java sources use imports + simple type names throughout (never a fully-qualified inline type
like `java.util.List<UserMaster>`), matching both the originally reported code shape and
avoiding an unrelated javalang parsing ambiguity around qualifier-chain + generic-argument
combinations that has nothing to do with the bug under test here.
"""
from __future__ import annotations
from pathlib import Path


def _extract(tmp_path: Path, imports: str, approval_body: str) -> list[dict]:
    from icx_engine.graph.parser.resolvers.java_symbols import extract_java_edges

    usermaster_path = tmp_path / "UserMaster.java"
    usermaster_path.write_text(
        "package com.example;\n\npublic class UserMaster {\n}\n", encoding="utf-8",
    )

    approval_path = tmp_path / "ApprovalDaoImpl.java"
    approval_path.write_text(
        "package com.example;\n\n"
        f"{imports}"
        "public class ApprovalDaoImpl {\n"
        f"{approval_body}\n"
        "}\n",
        encoding="utf-8",
    )

    nodes = [
        {"id": "approval_class", "label": "ApprovalDaoImpl", "source_file": str(approval_path)},
        {"id": "usermaster_class", "label": "UserMaster", "source_file": str(usermaster_path)},
    ]
    ast_extraction = {"nodes": nodes}

    return extract_java_edges(
        files=[approval_path, usermaster_path],
        project_root=tmp_path,
        ast_extraction=ast_extraction,
    )


def _edges_between(edges: list[dict]) -> list[dict]:
    """Edges from ApprovalDaoImpl to UserMaster, any relation - callers assert relation
    explicitly where it matters (see test_local_var_generic_type_argument_emits_uses_edge)."""
    return [
        e for e in edges
        if e["source"] == "approval_class" and e["target"] == "usermaster_class"
    ]


def test_local_var_generic_type_argument_emits_uses_edge(tmp_path: Path):
    """CriteriaQuery<UserMaster> cq = ...; Root<UserMaster> root = ...; - both are local
    variable declarations whose declared type is generic over UserMaster. Before the fix,
    _emit_body_refs's LocalVariableDeclaration branch never called _emit_type_ref, so this
    edge was silently dropped regardless of confidence threshold."""
    imports = (
        "import javax.persistence.criteria.CriteriaBuilder;\n"
        "import javax.persistence.criteria.CriteriaQuery;\n"
        "import javax.persistence.criteria.Root;\n\n"
    )
    body = (
        "    public void approve(CriteriaBuilder cb) {\n"
        "        CriteriaQuery<UserMaster> cq = cb.createQuery(UserMaster.class);\n"
        "        Root<UserMaster> root = cq.from(UserMaster.class);\n"
        "    }\n"
    )
    edges = _extract(tmp_path, imports, body)

    uses_edges = _edges_between(edges)
    assert uses_edges, f"expected a uses edge from ApprovalDaoImpl to UserMaster, got: {edges}"
    assert uses_edges[0]["relation"] == "uses"


def test_local_var_simple_generic_type_still_emits_edge(tmp_path: Path):
    """A plain generic local variable (List<UserMaster>, imported unqualified) must also emit
    the edge - guards that the fix isn't accidentally specific to the JPA-Criteria shape used
    in the primary regression test."""
    imports = "import java.util.List;\n\n"
    body = (
        "    public void approve() {\n"
        "        List<UserMaster> results = null;\n"
        "    }\n"
    )
    edges = _extract(tmp_path, imports, body)

    uses_edges = _edges_between(edges)
    assert uses_edges, f"expected a uses edge from ApprovalDaoImpl to UserMaster, got: {edges}"


def test_field_level_generic_type_still_works_unchanged(tmp_path: Path):
    """Regression guard: the field-declaration path (already working before this fix, via
    _emit_type_ref called directly at the field-walk site) must be untouched by the new
    LocalVariableDeclaration call - a field with a generic type argument still emits its edge."""
    imports = "import java.util.List;\n\n"
    body = "    private List<UserMaster> cachedResults;\n"
    edges = _extract(tmp_path, imports, body)

    uses_edges = _edges_between(edges)
    assert uses_edges, f"expected the pre-existing field-level uses edge, got: {edges}"


def test_local_var_non_generic_type_still_resolves_var_type_map(tmp_path: Path):
    """Regression guard: the pre-existing var_type_map population (used to resolve later
    qualified method calls like `userMaster.getId()` back to UserMaster's file) must still
    work after adding the _emit_type_ref call alongside it - a non-generic local variable
    declaration (`UserMaster um = ...`) should emit its own direct "uses" edge too."""
    body = (
        "    public void approve() {\n"
        "        UserMaster um = new UserMaster();\n"
        "    }\n"
    )
    edges = _extract(tmp_path, "", body)

    uses_edges = _edges_between(edges)
    assert uses_edges, f"expected a uses/calls edge from ApprovalDaoImpl to UserMaster, got: {edges}"
