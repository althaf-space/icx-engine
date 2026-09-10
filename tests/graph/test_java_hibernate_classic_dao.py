"""Tests for classic (pre-Spring-Data) Hibernate DAO detection in java_symbols.py.

jpa.py only detects Spring Data Repository<T,ID> interfaces and @Query/@NamedQuery JPQL -
it has zero coverage for a plain DAO class calling SessionFactory.getCurrentSession()
directly (session.get/load(Entity.class, id), session.createQuery(hql)), the older, still
extremely common enterprise Hibernate style. Real javalang parsing against files on disk,
mirroring test_java_local_var_generic_refs.py's style - this exercises the entry point
(extract_java_edges), not the internal visit() closure.
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
    return extract_java_edges(
        files=[approval_path, usermaster_path],
        project_root=tmp_path,
        ast_extraction={"nodes": nodes},
    )


def _edges_between(edges: list[dict], relation: str | None = None) -> list[dict]:
    out = [
        e for e in edges
        if e["source"] == "approval_class" and e["target"] == "usermaster_class"
    ]
    if relation is not None:
        out = [e for e in out if e["relation"] == relation]
    return out


_SESSION_IMPORTS = (
    "import org.hibernate.Session;\n"
    "import org.hibernate.SessionFactory;\n\n"
)


def test_session_get_entity_class_emits_dao_edge(tmp_path: Path):
    body = (
        "    private SessionFactory sessionFactory;\n\n"
        "    public UserMaster find(int id) {\n"
        "        Session session = sessionFactory.getCurrentSession();\n"
        "        return (UserMaster) session.get(UserMaster.class, id);\n"
        "    }\n"
    )
    edges = _extract(tmp_path, _SESSION_IMPORTS, body)
    dao_edges = _edges_between(edges, "dao")
    assert dao_edges, f"expected a dao edge from ApprovalDaoImpl to UserMaster, got: {edges}"


def test_session_load_entity_class_emits_dao_edge(tmp_path: Path):
    body = (
        "    private SessionFactory sessionFactory;\n\n"
        "    public UserMaster find(int id) {\n"
        "        Session session = sessionFactory.getCurrentSession();\n"
        "        return (UserMaster) session.load(UserMaster.class, id);\n"
        "    }\n"
    )
    edges = _extract(tmp_path, _SESSION_IMPORTS, body)
    dao_edges = _edges_between(edges, "dao")
    assert dao_edges, f"expected a dao edge from ApprovalDaoImpl to UserMaster, got: {edges}"


def test_session_create_query_hql_emits_queries_edge(tmp_path: Path):
    body = (
        "    private SessionFactory sessionFactory;\n\n"
        "    public java.util.List<UserMaster> findAll() {\n"
        "        Session session = sessionFactory.getCurrentSession();\n"
        '        return session.createQuery("FROM UserMaster u WHERE u.active = true").list();\n'
        "    }\n"
    )
    edges = _extract(tmp_path, _SESSION_IMPORTS, body)
    query_edges = _edges_between(edges, "queries")
    assert query_edges, f"expected a queries edge from ApprovalDaoImpl to UserMaster, got: {edges}"


def test_session_create_query_join_syntax_still_resolves(tmp_path: Path):
    """HQL entity extraction reuses jpa.py's exact regex shape - JOIN must resolve too,
    not just the leading FROM clause."""
    body = (
        "    private SessionFactory sessionFactory;\n\n"
        "    public void run() {\n"
        "        Session session = sessionFactory.getCurrentSession();\n"
        '        session.createQuery("SELECT a FROM Approval a JOIN UserMaster u ON a.userId = u.id").list();\n'
        "    }\n"
    )
    edges = _extract(tmp_path, _SESSION_IMPORTS, body)
    query_edges = _edges_between(edges, "queries")
    assert query_edges, f"expected a queries edge via JOIN clause, got: {edges}"


def test_list_get_does_not_false_positive_as_dao_edge(tmp_path: Path):
    """Critical false-positive guard: list.get(0) must never be misread as a Hibernate
    session.get(Entity.class, id) call just because the method name matches - detection
    is gated on the receiver actually resolving to type "Session", not on method name."""
    body = (
        "    public UserMaster findFirst() {\n"
        "        java.util.List<UserMaster> list = new java.util.ArrayList<>();\n"
        "        return list.get(0);\n"
        "    }\n"
    )
    edges = _extract(tmp_path, "", body)
    dao_edges = _edges_between(edges, "dao")
    assert not dao_edges, f"list.get(0) must never produce a dao edge, got: {edges}"


def test_optional_get_does_not_false_positive_as_dao_edge(tmp_path: Path):
    body = (
        "    public UserMaster find() {\n"
        "        java.util.Optional<UserMaster> opt = java.util.Optional.empty();\n"
        "        return opt.get();\n"
        "    }\n"
    )
    edges = _extract(tmp_path, "", body)
    dao_edges = _edges_between(edges, "dao")
    assert not dao_edges, f"Optional.get() must never produce a dao edge, got: {edges}"


def test_non_class_reference_argument_does_not_emit_dao_edge(tmp_path: Path):
    """session.get(someVariable, id) - not a literal Entity.class argument - must not
    emit an edge; there's nothing here identifying which entity type is being fetched
    without deeper (unsupported) resolution."""
    body = (
        "    private SessionFactory sessionFactory;\n\n"
        "    public Object find(String entityName, int id) {\n"
        "        Session session = sessionFactory.getCurrentSession();\n"
        "        return session.get(entityName, id);\n"
        "    }\n"
    )
    edges = _extract(tmp_path, _SESSION_IMPORTS, body)
    dao_edges = _edges_between(edges, "dao")
    assert not dao_edges, f"non-class-literal argument must not emit a dao edge, got: {edges}"
