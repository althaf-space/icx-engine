"""JAX-RS / Quarkus / Micronaut annotation resolver.

Detects:
  * JAX-RS: @Path + @GET/@POST/@PUT/@DELETE/@PATCH on classes/methods
  * CDI: @Inject constructor/field injection -> depends_on edges
  * Quarkus: @ApplicationScoped / @RequestScoped / @Singleton providers
  * Micronaut: @Controller / @Singleton / @Prototype / @Bean providers
  * Micronaut: @Get/@Post/@Put/@Delete/@Patch route annotations
  * @Scheduled (Quarkus and Micronaut variants)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    FRAMEWORK_RESOLVED, annotate_edge,
)

_log = logging.getLogger(__name__)

_PROVIDER_ANNOTATIONS: frozenset[str] = frozenset({
    # JAX-RS / CDI
    "ApplicationScoped", "RequestScoped", "SessionScoped", "Dependent",
    "Singleton",
    # Quarkus extras
    "Startup",
    # Micronaut
    "Controller", "Singleton", "Prototype", "Bean", "Factory",
    "Infrastructure",
})

_JAXRS_ROUTE_ANNOTATIONS: frozenset[str] = frozenset({
    "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS",
})

_MICRONAUT_ROUTE_ANNOTATIONS: frozenset[str] = frozenset({
    "Get", "Post", "Put", "Delete", "Patch", "Head", "Options",
    "HttpMethodMapping",
})

_ALL_ROUTE_ANNOTATIONS = _JAXRS_ROUTE_ANNOTATIONS | _MICRONAUT_ROUTE_ANNOTATIONS | frozenset({
    "Path",
})

# Every annotation jaxrs can emit an edge from: routes, providers, plus CDI
# @Inject / @Observes and @Scheduled. A source lacking ALL of these `@`-tokens
# cannot produce a jaxrs edge, so its parse-tree walk is skipped (perf only,
# zero edge loss). MUST stay complete - any annotation the walk reacts to below
# has to appear here. Built once at import time.
_JAXRS_TRIGGER_TOKENS = tuple(
    "@" + ann for ann in (
        _ALL_ROUTE_ANNOTATIONS | _PROVIDER_ANNOTATIONS
        | frozenset({"Inject", "Observes", "Scheduled"})
    )
)


def extract_jaxrs_edges(
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

    from . import _java_parse_cache as _jpc

    for jf in java_files:
        try:
            rel = jf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        # Cheap gate: only files importing a JAX-RS / Micronaut HTTP package can
        # declare routes. Skip the parse-tree walk for the rest (usually the vast
        # majority) - zero edge loss, since fqn_to_file is rebuilt for ALL files
        # below via the regex fqn map. This turns an O(all-files) walk into
        # O(jaxrs-files).
        try:
            src = jf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(trigger in src for trigger in _JAXRS_TRIGGER_TOKENS):
            continue
        tree = _jpc.get_tree(jf, src)
        if tree is None:
            continue
        package = tree.package.name if tree.package else ""
        for type_decl in (tree.types or []):
            name = getattr(type_decl, "name", None)
            if name:
                fqn = f"{package}.{name}" if package else name
                fqn_to_file.setdefault(fqn, rel)
        parsed.append((jf, rel, tree))

    # Build the full fqn -> file map over ALL files (targets may live in files
    # that were gated out above). Regex-based, also covers javalang timeouts.
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
            is_provider = bool(anns & _PROVIDER_ANNOTATIONS)
            has_path = "Path" in anns

            # Route methods: JAX-RS @GET/@POST etc, Micronaut @Get/@Post etc.
            for method in (getattr(type_decl, "methods", None) or []):
                method_anns = _annotation_names(method)
                is_route = bool(method_anns & (_JAXRS_ROUTE_ANNOTATIONS | _MICRONAUT_ROUTE_ANNOTATIONS))
                if not is_route and not (has_path and "Path" in method_anns):
                    continue
                method_node_id = local_symbols.get(method.name.lower())
                if not method_node_id or not file_node_id:
                    continue
                key = (file_node_id, method_node_id, "routes")
                if key in seen:
                    continue
                seen.add(key)
                edge = {
                    "relation": "routes",
                    "source": file_node_id,
                    "target": method_node_id,
                    "source_file": rel,
                    "source_location": _position_str(method),
                    "weight": 1.0,
                }
                annotate_edge(edge, FRAMEWORK_RESOLVED, "jaxrs_resolver")
                edges.append(edge)

            # @Scheduled (Quarkus @Scheduled or Micronaut @Scheduled).
            for method in (getattr(type_decl, "methods", None) or []):
                method_anns = _annotation_names(method)
                if "Scheduled" not in method_anns:
                    continue
                method_node_id = local_symbols.get(method.name.lower())
                if not method_node_id or not class_node_id:
                    continue
                key = (class_node_id, method_node_id, "scheduled")
                if key in seen:
                    continue
                seen.add(key)
                edge = {
                    "relation": "scheduled",
                    "source": class_node_id,
                    "target": method_node_id,
                    "source_file": rel,
                    "source_location": _position_str(method),
                    "weight": 1.0,
                }
                annotate_edge(edge, FRAMEWORK_RESOLVED, "jaxrs_resolver")
                edges.append(edge)

            # CDI @Observes: method parameter annotated with @Observes -> listens edge.
            for method in (getattr(type_decl, "methods", None) or []):
                for param in (method.parameters or []):
                    param_anns = _annotation_names(param)
                    if "Observes" not in param_anns:
                        continue
                    method_node_id = local_symbols.get(method.name.lower())
                    if not method_node_id:
                        continue
                    _emit_event_edge(
                        param.type, import_map, fqn_to_file, node_index,
                        seen, edges, src_id=method_node_id, rel=rel,
                        position=getattr(param, "position", None),
                    )
                    break  # Only first @Observes param matters

            if not is_provider and not has_path:
                continue

            # CDI @Inject: constructor injection.
            constructors = list(getattr(type_decl, "constructors", None) or [])
            inject_ctors = [
                c for c in constructors
                if _annotation_names(c) & {"Inject"}
            ] or (constructors if len(constructors) == 1 else [])

            for ctor in inject_ctors:
                for param in (ctor.parameters or []):
                    _emit_di_edge(
                        param.type, import_map, fqn_to_file, node_index,
                        seen, edges,
                        src_id=class_node_id, rel=rel,
                        position=getattr(param, "position", None),
                    )

            # Field injection: @Inject on fields.
            for field in (getattr(type_decl, "fields", None) or []):
                if not (_annotation_names(field) & {"Inject"}):
                    continue
                _emit_di_edge(
                    field.type, import_map, fqn_to_file, node_index,
                    seen, edges,
                    src_id=class_node_id, rel=rel,
                    position=getattr(field, "position", None),
                )

    return edges


def _emit_event_edge(
    type_node, import_map, fqn_to_file, node_index,
    seen, edges, *, src_id, rel, position,
) -> None:
    if type_node is None:
        return
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
    key = (src_id, target_id, "listens")
    if key in seen:
        return
    seen.add(key)
    line = position.line if position is not None else None
    edge = {
        "relation": "listens",
        "source": src_id,
        "target": target_id,
        "source_file": rel,
        "source_location": f"L{line}" if line else "",
        "weight": 1.0,
    }
    annotate_edge(edge, FRAMEWORK_RESOLVED, "cdi_observes")
    edges.append(edge)


def _emit_di_edge(
    type_node, import_map, fqn_to_file, node_index,
    seen, edges, *, src_id, rel, position,
) -> None:
    if type_node is None:
        return
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
    key = (src_id, target_id, "depends_on")
    if key in seen:
        return
    seen.add(key)
    line = position.line if position is not None else None
    edge = {
        "relation": "depends_on",
        "source": src_id,
        "target": target_id,
        "source_file": rel,
        "source_location": f"L{line}" if line else "",
        "weight": 1.0,
    }
    annotate_edge(edge, FRAMEWORK_RESOLVED, "jaxrs_resolver")
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


def _position_str(node) -> str:
    pos = getattr(node, "position", None)
    if pos is None or getattr(pos, "line", None) is None:
        return ""
    return f"L{pos.line}"


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
                simple = fqn[len(package) + 1:]
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
            rel = src_file[len(project_str) + 1:]
        elif src_file.startswith(project_str):
            rel = src_file[len(project_str):].lstrip("/")
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
