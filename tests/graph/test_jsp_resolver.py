"""Tests for Phase 3: JSP + Servlet resolver."""
import pytest
from pathlib import Path
from icx_engine.graph.parser.resolvers.jsp_resolver import (
    resolve_jsp,
    _resolve_view_to_jsp,
    _resolve_relative_jsp,
)


def _make_node(node_id, source_file):
    return {"id": node_id, "source_file": source_file, "name": node_id, "file": source_file}


class TestResolveViewToJsp:
    def test_spring_view_name_resolves_to_webinf_views(self):
        jsp_files = {"WEB-INF/views/userList.jsp"}
        result = _resolve_view_to_jsp("userList", jsp_files)
        assert result == "WEB-INF/views/userList.jsp"

    def test_direct_jsp_path_returned_if_exists(self):
        jsp_files = {"views/home.jsp"}
        result = _resolve_view_to_jsp("home", jsp_files)
        assert result == "views/home.jsp"

    def test_nonexistent_view_returns_none(self):
        result = _resolve_view_to_jsp("nonexistent", set())
        assert result is None


class TestResolveRelativeJsp:
    def test_sibling_jsp_resolved(self):
        jsp_files = {"WEB-INF/views/header.jsp", "WEB-INF/views/index.jsp"}
        result = _resolve_relative_jsp("header.jsp", "WEB-INF/views/index.jsp", jsp_files)
        assert result == "WEB-INF/views/header.jsp"

    def test_nonexistent_returns_none(self):
        result = _resolve_relative_jsp("missing.jsp", "views/page.jsp", set())
        assert result is None


class TestResolveJsp:
    def test_no_java_or_jsp_files_returns_empty(self):
        result = resolve_jsp([], "/tmp", {"nodes": []})
        assert result == []

    def test_spring_return_view_creates_jsp_forward(self, tmp_path):
        # Create controller
        ctrl = tmp_path / "UserController.java"
        ctrl.write_text('return "userList";')
        # Create JSP
        (tmp_path / "WEB-INF").mkdir()
        (tmp_path / "WEB-INF" / "views").mkdir()
        jsp = tmp_path / "WEB-INF" / "views" / "userList.jsp"
        jsp.write_text("<html></html>")

        ctrl_rel = str(ctrl.relative_to(tmp_path).as_posix())
        jsp_rel = str(jsp.relative_to(tmp_path).as_posix())

        nodes = [
            _make_node("ctrl_node", ctrl_rel),
            _make_node("jsp_node", jsp_rel),
        ]
        files = [ctrl, jsp]
        edges = resolve_jsp(files, tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "jsp_forward" in types

    def test_jsp_include_directive_creates_edge(self, tmp_path):
        (tmp_path / "views").mkdir()
        index_jsp = tmp_path / "views" / "index.jsp"
        header_jsp = tmp_path / "views" / "header.jsp"
        index_jsp.write_text('<%@ include file="header.jsp" %>')
        header_jsp.write_text("<header></header>")

        idx_rel = str(index_jsp.relative_to(tmp_path).as_posix())
        hdr_rel = str(header_jsp.relative_to(tmp_path).as_posix())
        nodes = [_make_node("idx_node", idx_rel), _make_node("hdr_node", hdr_rel)]
        edges = resolve_jsp([index_jsp, header_jsp], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "jsp_include" in types
        include_edges = [e for e in edges if e["type"] == "jsp_include"]
        assert include_edges[0]["confidence"] == 0.85

    def test_taglib_creates_taglib_import(self, tmp_path):
        jsp = tmp_path / "page.jsp"
        jsp.write_text('<%@ taglib uri="http://java.sun.com/jsp/jstl/core" prefix="c" %>')
        rel = str(jsp.relative_to(tmp_path).as_posix())
        nodes = [_make_node("page_node", rel)]
        edges = resolve_jsp([jsp], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "taglib_import" in types
        tl = [e for e in edges if e["type"] == "taglib_import"][0]
        assert tl["confidence"] == 0.90

    def test_webxml_servlet_class_creates_servlet_mapping(self, tmp_path):
        web_xml = tmp_path / "web.xml"
        web_xml.write_text("<servlet><servlet-class>com.example.MyServlet</servlet-class></servlet>")
        servlet = tmp_path / "MyServlet.java"
        servlet.write_text("public class MyServlet extends HttpServlet {}")

        webxml_rel = str(web_xml.relative_to(tmp_path).as_posix())
        servlet_rel = str(servlet.relative_to(tmp_path).as_posix())
        nodes = [_make_node("xml_node", webxml_rel), _make_node("svc_node", servlet_rel)]
        edges = resolve_jsp([web_xml, servlet], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "servlet_mapping" in types
        sm = [e for e in edges if e["type"] == "servlet_mapping"][0]
        assert sm["confidence"] == 0.95

    def test_el_expression_creates_el_binding(self, tmp_path):
        jsp = tmp_path / "profile.jsp"
        jsp.write_text("${user.name} and ${user.email}")
        java = tmp_path / "User.java"
        java.write_text("public String getName() { return name; }")

        jsp_rel = str(jsp.relative_to(tmp_path).as_posix())
        java_rel = str(java.relative_to(tmp_path).as_posix())
        nodes = [
            {"id": "jsp_node", "source_file": jsp_rel, "file": jsp_rel, "name": "profile"},
            {"id": "java_node", "source_file": java_rel, "file": java_rel, "name": "getName"},
        ]
        edges = resolve_jsp([jsp, java], tmp_path, {"nodes": nodes})
        types = [e["type"] for e in edges]
        assert "el_binding" in types
        el = [e for e in edges if e["type"] == "el_binding"][0]
        assert el["confidence"] == 0.55


class TestJspEdgeSchema:
    def test_all_edges_have_relation_field(self, tmp_path):
        a = tmp_path / "page.jsp"
        b = tmp_path / "header.jsp"
        a.write_text('<jsp:include page="header.jsp"/>')
        b.write_text("<h1>Header</h1>")
        a_rel = str(a.relative_to(tmp_path).as_posix())
        b_rel = str(b.relative_to(tmp_path).as_posix())
        nodes = [
            {"id": "page_n", "source_file": a_rel, "file": a_rel, "name": "page"},
            {"id": "header_n", "source_file": b_rel, "file": b_rel, "name": "header"},
        ]
        edges = resolve_jsp([a, b], tmp_path, {"nodes": nodes})
        assert edges
        assert all("relation" in e for e in edges)
        assert all(e["relation"] == e["type"] for e in edges)
