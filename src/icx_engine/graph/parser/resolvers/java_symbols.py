"""Java cross-file resolver backed by javalang.

Parses every .java file, builds a project-wide FQN -> file map from
package + class declarations, then resolves imports / inheritance /
method calls / field type references against AST node IDs.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    AST_DIRECT, LSP_RESOLVED, annotate_edge,
)

_log = logging.getLogger(__name__)

_RELATION_PRIORITY: dict[str, int] = {
    "imports": 4, "inherits": 3, "calls": 2, "uses": 1,
}


def extract_java_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    try:
        import javalang
    except ImportError:
        return []

    project_root = project_root.resolve()
    java_files = [Path(f).resolve() for f in files if str(f).endswith(".java")]
    if not java_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_symbol"] and not node_index["by_file"]:
        return []

    fqn_to_file: dict[str, str] = {}
    parsed: list[tuple[Path, str, object]] = []

    for jf in java_files:
        try:
            rel = jf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        from . import _java_parse_cache as _jpc
        tree = _jpc.get_tree(jf)
        if tree is None:
            continue
        package = tree.package.name if tree.package else ""
        for type_decl in (tree.types or []):
            type_name = getattr(type_decl, "name", None)
            if type_name:
                fqn = f"{package}.{type_name}" if package else type_name
                fqn_to_file.setdefault(fqn, rel)
        parsed.append((jf, rel, tree))

    # Supplement with regex scan - fills gaps for files javalang timed out on
    from . import _java_fqn_map as _jfm
    for _fqn, _rel in _jfm.build_fqn_map(java_files, project_root).items():
        fqn_to_file.setdefault(_fqn, _rel)

    best_edge: dict[tuple[str, str], dict] = {}

    for jf, rel, tree in parsed:
        file_node_id = node_index["by_file"].get(rel)
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }
        package = tree.package.name if tree.package else ""

        import_map = _build_import_map(tree, package, fqn_to_file)

        # 1) Import edges from import statements.
        for imp in (tree.imports or []):
            target_file = _resolve_import(imp.path, fqn_to_file)
            if not target_file:
                continue
            tgt_id = node_index["by_file"].get(target_file)
            if not tgt_id or not file_node_id or tgt_id == file_node_id:
                continue
            _record(best_edge, file_node_id, tgt_id, "imports", rel,
                    getattr(imp, "position", None),
                    confidence=AST_DIRECT, source="java_imports")

        # 2) Walk type declarations.
        for type_decl in (tree.types or []):
            type_name = getattr(type_decl, "name", "") or ""
            type_node_id = local_symbols.get(type_name.lower())
            if not type_node_id:
                type_node_id = file_node_id
            type_pos = getattr(type_decl, "position", None)

            # Inherits: extends/implements
            extends = getattr(type_decl, "extends", None) or []
            if not isinstance(extends, list):
                extends = [extends]
            for parent in extends:
                _emit_type_ref(
                    parent, import_map, fqn_to_file, node_index, best_edge,
                    src_id=type_node_id, rel=rel, relation="inherits",
                    position=type_pos,
                )
            for iface in (getattr(type_decl, "implements", None) or []):
                _emit_type_ref(
                    iface, import_map, fqn_to_file, node_index, best_edge,
                    src_id=type_node_id, rel=rel, relation="inherits",
                    position=type_pos,
                )

            # Generic type parameter bounds: <T extends Foo & Bar>
            for tp in (getattr(type_decl, "type_parameters", None) or []):
                for bound in (getattr(tp, "extends", None) or []):
                    _emit_type_ref(
                        bound, import_map, fqn_to_file, node_index, best_edge,
                        src_id=type_node_id, rel=rel, relation="uses",
                        position=type_pos,
                    )

            # Field type references + per-class field-name -> type-name index
            # so qualified calls like `userService.get(...)` resolve through
            # the field type back to an imported class.
            field_type_map: dict[str, str] = {}
            for field in (getattr(type_decl, "fields", None) or []):
                field_type_name = _extract_type_name(field.type)
                if field_type_name and field_type_name in import_map:
                    for declarator in (getattr(field, "declarators", None) or []):
                        dname = getattr(declarator, "name", None)
                        if dname:
                            field_type_map[dname] = field_type_name
                _emit_type_ref(
                    field.type, import_map, fqn_to_file, node_index, best_edge,
                    src_id=type_node_id, rel=rel, relation="uses",
                    position=getattr(field, "position", None),
                )

            for method in (getattr(type_decl, "methods", None) or []):
                method_node_id = local_symbols.get(method.name.lower()) or type_node_id
                method_pos = getattr(method, "position", None)
                if method.return_type is not None:
                    _emit_type_ref(
                        method.return_type, import_map, fqn_to_file, node_index, best_edge,
                        src_id=method_node_id, rel=rel, relation="uses",
                        position=method_pos,
                    )
                local_type_map = dict(field_type_map)
                for param in (method.parameters or []):
                    pname = getattr(param, "name", None)
                    ptype = _extract_type_name(param.type)
                    if pname and ptype and ptype in import_map:
                        local_type_map[pname] = ptype
                    _emit_type_ref(
                        param.type, import_map, fqn_to_file, node_index, best_edge,
                        src_id=method_node_id, rel=rel, relation="uses",
                        position=getattr(param, "position", None),
                    )
                if method.body:
                    _emit_body_refs(
                        method.body, import_map, fqn_to_file, node_index, best_edge,
                        src_id=method_node_id, rel=rel,
                        var_type_map=local_type_map,
                    )
                # Exception types in throws clause.
                for exc in (getattr(method, "throws", None) or []):
                    exc_name = _extract_type_name(exc) if not isinstance(exc, str) else exc
                    if exc_name:
                        _emit_type_ref(
                            exc_name if isinstance(exc, str) else exc,
                            import_map, fqn_to_file, node_index, best_edge,
                            src_id=method_node_id, rel=rel, relation="uses",
                            position=method_pos,
                        )

            for ctor in (getattr(type_decl, "constructors", None) or []):
                ctor_node_id = local_symbols.get(type_name.lower()) or type_node_id
                local_type_map = dict(field_type_map)
                for param in (ctor.parameters or []):
                    pname = getattr(param, "name", None)
                    ptype = _extract_type_name(param.type)
                    if pname and ptype and ptype in import_map:
                        local_type_map[pname] = ptype
                    _emit_type_ref(
                        param.type, import_map, fqn_to_file, node_index, best_edge,
                        src_id=ctor_node_id, rel=rel, relation="uses",
                        position=getattr(param, "position", None),
                    )
                if ctor.body:
                    _emit_body_refs(
                        ctor.body, import_map, fqn_to_file, node_index, best_edge,
                        src_id=ctor_node_id, rel=rel,
                        var_type_map=local_type_map,
                    )

            # Enum constants with overridden methods: walk each constant's body
            enum_body = getattr(type_decl, "body", None)
            enum_constants = getattr(enum_body, "constants", None) or []
            for econst in enum_constants:
                for emethod in (getattr(econst, "body", None) or []):
                    if not hasattr(emethod, "body") or not emethod.body:
                        continue
                    emethod_id = (
                        local_symbols.get(emethod.name.lower())
                        if hasattr(emethod, "name") else None
                    ) or type_node_id
                    _emit_body_refs(
                        emethod.body, import_map, fqn_to_file, node_index,
                        best_edge, src_id=emethod_id, rel=rel,
                        var_type_map=dict(field_type_map),
                    )

    return list(best_edge.values())


def _build_import_map(tree, package: str, fqn_to_file: dict[str, str]) -> dict[str, str]:
    """Map simple type name -> FQN, given the file's imports + same-package types."""
    out: dict[str, str] = {}
    for imp in (tree.imports or []):
        path = imp.path
        if imp.wildcard:
            # Resolve wildcard: add every type directly inside that package.
            prefix = path + "."
            for fqn in fqn_to_file:
                if fqn.startswith(prefix) and "." not in fqn[len(prefix):]:
                    out.setdefault(fqn[len(prefix):], fqn)
            continue
        simple = path.rsplit(".", 1)[-1] if "." in path else path
        if simple:
            out[simple] = path
    if package:
        pkg_prefix = package + "."
        for fqn in fqn_to_file:
            if fqn.startswith(pkg_prefix):
                simple = fqn[len(pkg_prefix):]
                if "." not in simple:
                    out.setdefault(simple, fqn)
    return out


def _resolve_import(path: str, fqn_to_file: dict[str, str]) -> str | None:
    """Match `com.example.blog.entity.User` to a project file."""
    if not path:
        return None
    if path in fqn_to_file:
        return fqn_to_file[path]
    parent = path.rsplit(".", 1)[0]
    if parent in fqn_to_file:
        return fqn_to_file[parent]
    return None


def _emit_type_ref(
    type_node, import_map, fqn_to_file, node_index, best_edge,
    *, src_id, rel, relation, position,
) -> None:
    if type_node is None or not src_id:
        return
    type_name = _extract_type_name(type_node)
    if type_name:
        fqn = import_map.get(type_name)
        if fqn:
            target_file = fqn_to_file.get(fqn)
            if target_file:
                target_symbol_id = node_index["by_symbol"].get((target_file, type_name.lower()))
                target_file_id = node_index["by_file"].get(target_file)
                tgt_id = target_symbol_id or target_file_id
                if tgt_id and tgt_id != src_id:
                    confidence = AST_DIRECT if relation == "imports" else LSP_RESOLVED
                    _record(best_edge, src_id, tgt_id, relation, rel, position,
                            confidence=confidence, source="java_symbols")
    # Recurse into generic type arguments: List<UserDto>, ResponseEntity<Entity>,
    # Map<String, UserDto>, Page<User>, Optional<Dto>, etc.
    for arg in (getattr(type_node, "arguments", None) or []):
        inner = getattr(arg, "type", None)
        if inner is not None:
            _emit_type_ref(inner, import_map, fqn_to_file, node_index, best_edge,
                           src_id=src_id, rel=rel, relation="uses", position=position)


def _extract_type_name(type_node) -> str | None:
    name = getattr(type_node, "name", None)
    if name:
        return name
    sub_type = getattr(type_node, "sub_type", None)
    if sub_type is not None:
        return _extract_type_name(sub_type)
    return None


def _emit_body_refs(body, import_map, fqn_to_file, node_index, best_edge,
                    *, src_id, rel, var_type_map: dict[str, str] | None = None) -> None:
    """Walk a method/constructor body and emit calls + uses edges for cross-
    file types and method invocations.

    `var_type_map` maps in-scope variable / field / parameter names to their
    declared simple type name, used to resolve `userService.get(...)` style
    qualified calls back to the type's source file.

    NOTE: Anonymous class bodies (ClassCreator with a body) are handled
    implicitly -- `_iter_children` uses `node.filter(jt.Node)` which
    performs a full recursive descent through all AST descendants, so
    method calls and type references inside anonymous classes are visited.
    """
    import javalang.tree as jt

    var_type_map = var_type_map or {}

    def _resolve_qualifier_type(qualifier: str | None) -> str | None:
        if not qualifier:
            return None
        if qualifier in import_map:
            return qualifier
        return var_type_map.get(qualifier)

    def visit(node) -> None:
        if isinstance(node, jt.MethodInvocation):
            type_name = _resolve_qualifier_type(node.qualifier)
            if type_name and type_name in import_map:
                fqn = import_map[type_name]
                target_file = fqn_to_file.get(fqn)
                if target_file:
                    tgt = node_index["by_symbol"].get(
                        (target_file, node.member.lower())
                    ) or node_index["by_file"].get(target_file)
                    if tgt and tgt != src_id:
                        _record(best_edge, src_id, tgt, "calls", rel,
                                getattr(node, "position", None),
                                confidence=LSP_RESOLVED,
                                source="java_symbols")
        elif isinstance(node, jt.ClassCreator):
            type_name = _extract_type_name(node.type)
            if type_name and type_name in import_map:
                fqn = import_map[type_name]
                target_file = fqn_to_file.get(fqn)
                if target_file:
                    tgt = node_index["by_symbol"].get(
                        (target_file, type_name.lower())
                    ) or node_index["by_file"].get(target_file)
                    if tgt and tgt != src_id:
                        _record(best_edge, src_id, tgt, "calls", rel,
                                getattr(node, "position", None),
                                confidence=LSP_RESOLVED,
                                source="java_symbols")
        elif isinstance(node, jt.MemberReference):
            type_name = _resolve_qualifier_type(node.qualifier)
            if type_name and type_name in import_map:
                fqn = import_map[type_name]
                target_file = fqn_to_file.get(fqn)
                if target_file:
                    tgt = (
                        node_index["by_symbol"].get(
                            (target_file, type_name.lower())
                        )
                        or node_index["by_file"].get(target_file)
                    )
                    if tgt and tgt != src_id:
                        _record(best_edge, src_id, tgt, "uses", rel,
                                getattr(node, "position", None),
                                confidence=LSP_RESOLVED,
                                source="java_symbols")
        elif isinstance(node, jt.MethodReference):
            # Handle Type::method and variable::method (e.g. UserService::get,
            # this::validate, Post::new)
            expr = getattr(node, "expression", None)
            member = getattr(node, "method", None) or getattr(node, "member", None)
            type_name = None
            if expr is not None:
                # expression can be a ReferenceType (Type::method) or a
                # MemberReference/This (variable::method)
                type_name = _extract_type_name(expr)
                if not type_name:
                    # variable::method - qualifier holds the variable name
                    qualifier = getattr(expr, "qualifier", None) or getattr(expr, "member", None)
                    type_name = _resolve_qualifier_type(qualifier)
            if type_name and type_name in import_map:
                fqn = import_map[type_name]
                target_file = fqn_to_file.get(fqn)
                if target_file:
                    member_name = member.value if hasattr(member, "value") else member
                    tgt = None
                    if member_name and member_name != "new":
                        tgt = node_index["by_symbol"].get(
                            (target_file, str(member_name).lower())
                        )
                    if not tgt:
                        tgt = (
                            node_index["by_symbol"].get(
                                (target_file, type_name.lower())
                            )
                            or node_index["by_file"].get(target_file)
                        )
                    if tgt and tgt != src_id:
                        _record(best_edge, src_id, tgt, "calls", rel,
                                getattr(node, "position", None),
                                confidence=LSP_RESOLVED,
                                source="java_symbols")
        elif isinstance(node, jt.LocalVariableDeclaration):
            var_type_name = _extract_type_name(node.type)
            if var_type_name and var_type_name in import_map:
                for declarator in (node.declarators or []):
                    dname = getattr(declarator, "name", None)
                    if dname:
                        var_type_map[dname] = var_type_name

        for child in _iter_children(node):
            visit(child)

    for stmt in (getattr(body, "__iter__", lambda: iter([]))()
                 if not isinstance(body, list) else body):
        visit(stmt)


def _iter_children(node):
    import javalang.tree as jt
    if not isinstance(node, jt.Node):
        return
    for attr in node.attrs:
        val = getattr(node, attr, None)
        if val is None:
            continue
        if isinstance(val, jt.Node):
            yield val
        elif isinstance(val, (list, tuple)):
            for item in val:
                if isinstance(item, jt.Node):
                    yield item


def _record(best_edge, src_id, tgt_id, relation, rel, position, *,
            confidence, source) -> None:
    pair_key = (src_id, tgt_id)
    existing = best_edge.get(pair_key)
    new_priority = _RELATION_PRIORITY.get(relation, 0)
    if existing is not None:
        existing_priority = _RELATION_PRIORITY.get(existing.get("relation", ""), 0)
        if new_priority <= existing_priority:
            return
    line = position.line if position is not None else None
    edge = {
        "relation": relation,
        "source": src_id,
        "target": tgt_id,
        "source_file": rel,
        "source_location": f"L{line}" if line else "",
        "weight": 1.0,
    }
    annotate_edge(edge, confidence, source)
    best_edge[pair_key] = edge


def _build_node_index(nodes: list[dict], project_root: Path) -> dict[str, dict]:
    project_str = str(project_root).replace("\\", "/")
    by_file: dict[str, str] = {}
    by_symbol: dict[tuple[str, str], str] = {}

    for n in nodes:
        nid = n.get("id") or n.get("label")
        if not nid:
            continue
        src_file = (n.get("source_file") or "").replace("\\", "/").strip()
        label = (n.get("label") or "").strip()
        if not src_file:
            continue
        if src_file.startswith(project_str + "/"):
            rel = src_file[len(project_str) + 1 :]
        elif src_file.startswith(project_str):
            rel = src_file[len(project_str) :].lstrip("/")
        else:
            continue
        if label.lower().endswith(".java") or label == Path(rel).name:
            by_file.setdefault(rel, nid)
            continue
        symbol = label
        if symbol.endswith("()"):
            symbol = symbol[:-2]
        symbol = symbol.lstrip(".").lower()
        if symbol:
            by_symbol.setdefault((rel, symbol), nid)
    return {"by_file": by_file, "by_symbol": by_symbol}


def upgrade_inferred_edges(
    extraction: dict,
    project_root: "Path",
    files: "Iterable[Path]",
) -> None:
    """Promote low-confidence tree-sitter edges to EXTRACTED when target is a known project node.

    Modifies extraction["edges"] in-place.
    """
    from pathlib import Path as _Path
    java_files = [_Path(f).resolve() for f in files if str(f).endswith(".java")]
    if not java_files:
        return

    resolved_root = _Path(project_root).resolve()
    node_index = _build_node_index(extraction.get("nodes", []), resolved_root)
    file_node_ids: set = set(node_index["by_file"].values())
    if not file_node_ids:
        return

    for edge in extraction.get("edges", []):
        if not isinstance(edge, dict):
            continue
        score = edge.get("confidence_score")
        if score is None or float(score) >= 0.8:
            continue
        if edge.get("target", "") in file_node_ids:
            edge["confidence_score"] = AST_DIRECT
            edge["confidence_source"] = "java_symbols_validated"
            edge["confidence"] = "EXTRACTED"
