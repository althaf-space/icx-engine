"""Hibernate / JPA relationship resolver.

Detects:
  * @Entity / @MappedSuperclass classes
  * Relationship fields (@OneToOne / @OneToMany / @ManyToOne / @ManyToMany)
    -> relation edges between entity classes
  * @Inheritance -> inherits edges to parent entity classes
  * Spring Data Repository<T, ID> declarations -> dao edges from the
    repository class to the entity class
  * @Query JPQL strings -> queries edges to referenced entity classes
  * @NamedQuery JPQL strings -> queries edges to referenced entity classes
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    FRAMEWORK_RESOLVED, annotate_edge,
)

_log = logging.getLogger(__name__)

_RELATIONSHIP_ANNOTATIONS: frozenset[str] = frozenset({
    "OneToOne", "OneToMany", "ManyToOne", "ManyToMany",
})

_REPOSITORY_PARENT_NAMES: frozenset[str] = frozenset({
    "JpaRepository", "CrudRepository", "PagingAndSortingRepository",
    "ReactiveCrudRepository", "ReactiveSortingRepository",
    "MongoRepository", "ReactiveMongoRepository", "Repository",
})


def extract_jpa_edges(
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
            name = getattr(type_decl, "name", None)
            if name:
                fqn = f"{package}.{name}" if package else name
                fqn_to_file.setdefault(fqn, rel)
        parsed.append((jf, rel, tree))

    # Supplement with regex scan for files javalang timed out on
    from . import _java_fqn_map as _jfm
    for _fqn, _rel in _jfm.build_fqn_map(java_files, project_root).items():
        fqn_to_file.setdefault(_fqn, _rel)

    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for jf, rel, tree in parsed:
        file_node_id = node_index["by_file"].get(rel)
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }
        package = tree.package.name if tree.package else ""
        import_map = _build_import_map(tree, package, fqn_to_file)

        for type_decl in (tree.types or []):
            type_name = getattr(type_decl, "name", "") or ""
            class_node_id = local_symbols.get(type_name.lower()) or file_node_id
            if not class_node_id:
                continue

            anns = _annotation_names(type_decl)
            is_entity = bool(anns & {"Entity", "MappedSuperclass", "Embeddable"})

            if is_entity:
                for field in (getattr(type_decl, "fields", None) or []):
                    field_anns = _annotation_names(field)
                    if not (field_anns & _RELATIONSHIP_ANNOTATIONS):
                        continue
                    target_type = _inner_generic_type(field.type) or field.type
                    _emit(
                        target_type, "has_relation", import_map, fqn_to_file,
                        node_index, seen, edges,
                        src_id=class_node_id, rel=rel,
                        position=getattr(field, "position", None),
                    )

                # @Inheritance: emit inherits edge to parent entity.
                inh_parents = list(getattr(type_decl, "extends", None) or [])
                if not isinstance(inh_parents, list):
                    inh_parents = [inh_parents]
                for parent in inh_parents:
                    _emit(
                        parent, "inherits", import_map, fqn_to_file,
                        node_index, seen, edges,
                        src_id=class_node_id, rel=rel,
                        position=getattr(type_decl, "position", None),
                    )

            # Repository<Entity, ID>: any interface extending a Spring Data
            # repository where the first type arg resolves to an entity in
            # the project.
            parents = list(getattr(type_decl, "extends", None) or [])
            if not isinstance(parents, list):
                parents = [parents]
            for parent in parents:
                parent_name = getattr(parent, "name", None)
                if parent_name not in _REPOSITORY_PARENT_NAMES:
                    continue
                args = getattr(parent, "arguments", None) or []
                if not args:
                    continue
                first = args[0]
                first_type = getattr(first, "type", None)
                if first_type is None:
                    continue
                _emit(
                    first_type, "dao", import_map, fqn_to_file,
                    node_index, seen, edges,
                    src_id=class_node_id, rel=rel,
                    position=getattr(type_decl, "position", None),
                )

    # @Query JPQL -> entity reference edges.
    # Build entity simple name -> node_id map for JPQL resolution.
    entity_names: dict[str, str] = {}
    for jf, rel, tree in parsed:
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }
        for type_decl in (tree.types or []):
            anns = _annotation_names(type_decl)
            if anns & {"Entity", "MappedSuperclass"}:
                name = getattr(type_decl, "name", None)
                if name:
                    nid = local_symbols.get(name.lower()) or node_index["by_file"].get(rel)
                    if nid:
                        entity_names[name] = nid

    if entity_names:
        _jpql_entity_re = re.compile(
            r"\b(?:FROM|JOIN|UPDATE|DELETE\s+FROM)\s+([A-Z][A-Za-z0-9_]*)",
            re.IGNORECASE,
        )
        for jf, rel, tree in parsed:
            local_symbols = {
                sym: nid for (path, sym), nid in node_index["by_symbol"].items()
                if path == rel
            }
            for type_decl in (tree.types or []):
                # @Query on repository methods.
                for method in (getattr(type_decl, "methods", None) or []):
                    method_anns = _annotation_names(method)
                    if "Query" not in method_anns:
                        continue
                    query_str = _extract_query_value(method)
                    if not query_str:
                        continue
                    method_node_id = local_symbols.get(method.name.lower())
                    if not method_node_id:
                        continue
                    for m in _jpql_entity_re.finditer(query_str):
                        entity_name = m.group(1)
                        target_id = entity_names.get(entity_name)
                        if not target_id or target_id == method_node_id:
                            continue
                        key = (method_node_id, target_id, "queries")
                        if key in seen:
                            continue
                        seen.add(key)
                        edge = {
                            "relation": "queries",
                            "source": method_node_id,
                            "target": target_id,
                            "source_file": rel,
                            "source_location": _position_str(method),
                            "weight": 0.8,
                        }
                        annotate_edge(edge, FRAMEWORK_RESOLVED, "jpa_jpql")
                        edges.append(edge)

                # @NamedQuery on entity classes -> parse JPQL for entity refs.
                type_name = getattr(type_decl, "name", "") or ""
                class_node_id = local_symbols.get(type_name.lower()) or node_index["by_file"].get(rel)
                if not class_node_id:
                    continue
                for ann in (getattr(type_decl, "annotations", None) or []):
                    ann_name = getattr(ann, "name", None)
                    if ann_name not in ("NamedQuery", "NamedQueries"):
                        continue
                    queries = _extract_named_query_values(ann)
                    for q in queries:
                        for m in _jpql_entity_re.finditer(q):
                            entity_name = m.group(1)
                            target_id = entity_names.get(entity_name)
                            if not target_id or target_id == class_node_id:
                                continue
                            key = (class_node_id, target_id, "queries")
                            if key in seen:
                                continue
                            seen.add(key)
                            edge = {
                                "relation": "queries",
                                "source": class_node_id,
                                "target": target_id,
                                "source_file": rel,
                                "source_location": _position_str(type_decl),
                                "weight": 0.8,
                            }
                            annotate_edge(edge, FRAMEWORK_RESOLVED, "jpa_named_query")
                            edges.append(edge)

    return edges


def _emit(
    type_node, relation, import_map, fqn_to_file, node_index,
    seen, edges, *, src_id, rel, position,
) -> None:
    type_name = _extract_type_name(type_node)
    if not type_name:
        return
    fqn = import_map.get(type_name)
    if not fqn:
        return
    target_file = fqn_to_file.get(fqn)
    if not target_file:
        return
    target_id = (
        node_index["by_symbol"].get((target_file, type_name.lower()))
        or node_index["by_file"].get(target_file)
    )
    if not target_id or target_id == src_id:
        return
    key = (src_id, target_id, relation)
    if key in seen:
        return
    seen.add(key)
    line = position.line if position is not None else None
    edge = {
        "relation": relation,
        "source": src_id,
        "target": target_id,
        "source_file": rel,
        "source_location": f"L{line}" if line else "",
        "weight": 1.0,
    }
    annotate_edge(edge, FRAMEWORK_RESOLVED, "jpa_resolver")
    edges.append(edge)


def _annotation_names(node) -> set[str]:
    out: set[str] = set()
    for ann in (getattr(node, "annotations", None) or []):
        name = getattr(ann, "name", None)
        if name:
            out.add(name)
    return out


def _extract_type_name(type_node) -> str | None:
    name = getattr(type_node, "name", None)
    if name:
        return name
    sub_type = getattr(type_node, "sub_type", None)
    if sub_type is not None:
        return _extract_type_name(sub_type)
    return None


def _inner_generic_type(type_node):
    """For `List<User>` / `Set<Post>` return the inner Type."""
    args = getattr(type_node, "arguments", None)
    if not args:
        return None
    inner = args[0]
    inner_type = getattr(inner, "type", None)
    return inner_type


def _position_str(node) -> str:
    pos = getattr(node, "position", None)
    if pos is None or getattr(pos, "line", None) is None:
        return ""
    return f"L{pos.line}"


def _extract_query_value(method) -> str | None:
    """Extract the string value from @Query("...") annotation."""
    for ann in (getattr(method, "annotations", None) or []):
        if getattr(ann, "name", None) != "Query":
            continue
        element = getattr(ann, "element", None)
        if element is not None:
            if isinstance(element, list):
                for e in element:
                    val = getattr(e, "value", None)
                    if isinstance(val, str):
                        return val.strip('"')
            elif hasattr(element, "value"):
                val = element.value
                if isinstance(val, str):
                    return val.strip('"')
            elif isinstance(element, str):
                return element.strip('"')
    return None


def _extract_named_query_values(ann) -> list[str]:
    """Extract JPQL strings from @NamedQuery or @NamedQueries annotation."""
    results: list[str] = []
    ann_name = getattr(ann, "name", None)

    if ann_name == "NamedQuery":
        val = _get_annotation_member(ann, "query")
        if val:
            results.append(val)
    elif ann_name == "NamedQueries":
        # @NamedQueries({ @NamedQuery(...), @NamedQuery(...) })
        element = getattr(ann, "element", None)
        if element is not None:
            items = element if isinstance(element, list) else [element]
            for item in items:
                # Each item may be an Annotation node for @NamedQuery
                if getattr(item, "name", None) == "NamedQuery":
                    val = _get_annotation_member(item, "query")
                    if val:
                        results.append(val)
                # Or it may be an ElementValuePair wrapping annotations
                inner = getattr(item, "value", None)
                if inner is not None:
                    inner_list = inner if isinstance(inner, list) else [inner]
                    for sub in inner_list:
                        if getattr(sub, "name", None) == "NamedQuery":
                            val = _get_annotation_member(sub, "query")
                            if val:
                                results.append(val)
    return results


def _get_annotation_member(ann, member_name: str) -> str | None:
    """Get a string member value from an annotation by member name."""
    element = getattr(ann, "element", None)
    if element is None:
        return None
    if isinstance(element, list):
        for e in element:
            name = getattr(e, "name", None)
            if name == member_name:
                val = getattr(e, "value", None)
                if isinstance(val, str):
                    return val.strip('"')
                # Might be a Literal node
                literal_val = getattr(val, "value", None)
                if isinstance(literal_val, str):
                    return literal_val.strip('"')
    elif hasattr(element, "name") and getattr(element, "name", None) == member_name:
        val = getattr(element, "value", None)
        if isinstance(val, str):
            return val.strip('"')
        literal_val = getattr(val, "value", None)
        if isinstance(literal_val, str):
            return literal_val.strip('"')
    return None


def _build_import_map(tree, package: str, fqn_to_file: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for imp in (tree.imports or []):
        path = imp.path
        if imp.wildcard:
            continue
        simple = path.rsplit(".", 1)[-1] if "." in path else path
        if simple:
            out[simple] = path
    if package:
        for fqn in fqn_to_file:
            if fqn.startswith(package + "."):
                simple = fqn[len(package) + 1 :]
                if "." not in simple:
                    out.setdefault(simple, fqn)
    return out


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
