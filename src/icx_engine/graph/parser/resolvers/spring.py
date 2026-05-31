"""Spring annotation resolver.

Detects:
  * @Controller / @RestController / @Service / @Component / @Repository /
    @Configuration / @ConfigurationProperties providers
  * Constructor, field, and setter injection (@Autowired, @Inject, or
    single-ctor in provider classes) -> depends_on edges
  * @Bean factory methods in @Configuration -> provides edges
  * @RequestMapping / @GetMapping / @PostMapping / @PutMapping /
    @PatchMapping / @DeleteMapping -> routes edges
  * @EventListener methods -> listens edges to event type
  * @Scheduled methods -> scheduled edges (entry points)
  * @Aspect with @Around/@Before/@After -> advises edges
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

_PROVIDER_ANNOTATIONS: frozenset[str] = frozenset({
    "Controller", "RestController", "Service", "Component",
    "Repository", "Configuration", "ConfigurationProperties",
    "ControllerAdvice", "RestControllerAdvice",
})

_ROUTE_ANNOTATIONS: frozenset[str] = frozenset({
    "RequestMapping", "GetMapping", "PostMapping",
    "PutMapping", "PatchMapping", "DeleteMapping",
})

_BEAN_ANNOTATION = "Bean"

_ASPECT_ANNOTATIONS: frozenset[str] = frozenset({
    "Around", "Before", "After", "AfterReturning", "AfterThrowing",
})


def extract_spring_edges(
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
            is_provider = bool(anns & _PROVIDER_ANNOTATIONS)

            # Routes on class methods (any method-level mapping makes the
            # method a route, regardless of class-level annotation).
            for method in (getattr(type_decl, "methods", None) or []):
                method_anns = _annotation_names(method)
                if not (method_anns & _ROUTE_ANNOTATIONS):
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
                annotate_edge(edge, FRAMEWORK_RESOLVED, "spring_resolver")
                edges.append(edge)

            # @EventListener -> listens edge to event parameter type.
            for method in (getattr(type_decl, "methods", None) or []):
                method_anns = _annotation_names(method)
                if "EventListener" in method_anns:
                    method_node_id = local_symbols.get(method.name.lower())
                    if not method_node_id:
                        continue
                    for param in (method.parameters or [])[:1]:
                        _emit_event_edge(
                            param.type, import_map, fqn_to_file, node_index,
                            seen, edges, src_id=method_node_id, rel=rel,
                            position=getattr(param, "position", None),
                        )

            # @Scheduled -> scheduled edge (class -> method entry point).
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
                annotate_edge(edge, FRAMEWORK_RESOLVED, "spring_resolver")
                edges.append(edge)

            # @Bean factory methods in @Configuration classes.
            if "Configuration" in anns or "ConfigurationProperties" in anns:
                for method in (getattr(type_decl, "methods", None) or []):
                    method_anns = _annotation_names(method)
                    if _BEAN_ANNOTATION not in method_anns:
                        continue
                    # Return type = the bean type being provided.
                    ret_type = getattr(method, "return_type", None)
                    if ret_type:
                        _emit_provides_edge(
                            ret_type, import_map, fqn_to_file, node_index,
                            seen, edges, src_id=class_node_id, rel=rel,
                            position=getattr(method, "position", None),
                        )
                    # Parameters of @Bean methods are injected dependencies.
                    for param in (method.parameters or []):
                        _emit_di_edge(
                            param.type, import_map, fqn_to_file, node_index,
                            seen, edges, src_id=class_node_id, rel=rel,
                            position=getattr(param, "position", None),
                        )

            # @Import: explicit config class import.
            for ann in (getattr(type_decl, "annotations", None) or []):
                if getattr(ann, "name", None) != "Import":
                    continue
                element = getattr(ann, "element", None)
                if element is None:
                    continue
                # @Import({Config1.class, Config2.class}) or @Import(Config.class)
                import_refs = _extract_class_refs_from_annotation(element)
                for ref_name in import_refs:
                    _emit_di_edge_by_name(
                        ref_name, import_map, fqn_to_file, node_index,
                        seen, edges, src_id=class_node_id, rel=rel,
                    )

            if not is_provider:
                continue

            # Constructor injection: @Autowired (any constructor), or the
            # sole constructor when only one is present (Spring 4.3+).
            constructors = list(getattr(type_decl, "constructors", None) or [])
            inject_ctors = [
                c for c in constructors
                if _annotation_names(c) & {"Autowired", "Inject"}
            ] or (constructors if len(constructors) == 1 else [])

            for ctor in inject_ctors:
                for param in (ctor.parameters or []):
                    _emit_di_edge(
                        param.type, import_map, fqn_to_file, node_index,
                        seen, edges,
                        src_id=class_node_id, rel=rel,
                        position=getattr(param, "position", None),
                    )

            # @RequiredArgsConstructor / @AllArgsConstructor: Lombok generates
            # the constructor from fields, so no explicit ctor appears in source.
            # Only apply when no explicit constructors exist (Lombok code path).
            _lombok_ctor = anns & {"RequiredArgsConstructor", "AllArgsConstructor"}
            if _lombok_ctor and not constructors:
                for field in (getattr(type_decl, "fields", None) or []):
                    field_mods = set(getattr(field, "modifiers", None) or [])
                    if "static" in field_mods:
                        continue
                    if "RequiredArgsConstructor" in anns and (
                        "private" not in field_mods or "final" not in field_mods
                    ):
                        continue
                    _emit_di_edge(
                        field.type, import_map, fqn_to_file, node_index,
                        seen, edges,
                        src_id=class_node_id, rel=rel,
                        position=getattr(field, "position", None),
                    )

            # Field injection: @Autowired/@Inject/@Resource-annotated fields.
            for field in (getattr(type_decl, "fields", None) or []):
                if not (_annotation_names(field) & {"Autowired", "Inject", "Resource"}):
                    continue
                _emit_di_edge(
                    field.type, import_map, fqn_to_file, node_index,
                    seen, edges,
                    src_id=class_node_id, rel=rel,
                    position=getattr(field, "position", None),
                )

            # Setter injection: @Autowired on non-constructor methods.
            for method in (getattr(type_decl, "methods", None) or []):
                method_anns = _annotation_names(method)
                if not (method_anns & {"Autowired", "Inject"}):
                    continue
                for param in (method.parameters or []):
                    _emit_di_edge(
                        param.type, import_map, fqn_to_file, node_index,
                        seen, edges,
                        src_id=class_node_id, rel=rel,
                        position=getattr(param, "position", None),
                    )

    # ApplicationEventPublisher.publishEvent() detection.
    _extract_publish_event_edges(
        java_files, parsed, fqn_to_file, node_index, seen, edges,
    )

    # AOP @Aspect classes: detect advise targets from pointcut expressions.
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
            if "Aspect" not in anns:
                continue
            for method in (getattr(type_decl, "methods", None) or []):
                method_anns = _annotation_names(method)
                if not (method_anns & _ASPECT_ANNOTATIONS):
                    continue
                method_node_id = local_symbols.get(method.name.lower())
                if not method_node_id:
                    continue
                targets = _parse_pointcut_targets(method, import_map, fqn_to_file)
                for target_file in targets:
                    tgt_id = node_index["by_file"].get(target_file)
                    if not tgt_id or tgt_id == method_node_id:
                        continue
                    key = (method_node_id, tgt_id, "advises")
                    if key in seen:
                        continue
                    seen.add(key)
                    edge = {
                        "relation": "advises",
                        "source": method_node_id,
                        "target": tgt_id,
                        "source_file": rel,
                        "source_location": _position_str(method),
                        "weight": 0.8,
                    }
                    annotate_edge(edge, FRAMEWORK_RESOLVED, "spring_aop")
                    edges.append(edge)

    return edges


def _extract_class_refs_from_annotation(element) -> list[str]:
    """Extract class names from annotation elements like @Import({A.class, B.class})."""
    refs: list[str] = []
    if isinstance(element, list):
        for e in element:
            val = getattr(e, "value", None)
            if val and isinstance(val, str) and val.endswith(".class"):
                refs.append(val[:-6])
            member = getattr(e, "member", None)
            if member and isinstance(member, str) and member.endswith(".class"):
                refs.append(member[:-6])
    elif hasattr(element, "value"):
        val = element.value
        if isinstance(val, str) and val.endswith(".class"):
            refs.append(val[:-6])
    return refs


def _emit_di_edge_by_name(
    type_name, import_map, fqn_to_file, node_index,
    seen, edges, *, src_id, rel,
) -> None:
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
    edge = {
        "relation": "depends_on",
        "source": src_id,
        "target": target_id,
        "source_file": rel,
        "source_location": "",
        "weight": 1.0,
    }
    annotate_edge(edge, FRAMEWORK_RESOLVED, "spring_import")
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
    annotate_edge(edge, FRAMEWORK_RESOLVED, "spring_resolver")
    edges.append(edge)


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
    annotate_edge(edge, FRAMEWORK_RESOLVED, "spring_resolver")
    edges.append(edge)


def _emit_provides_edge(
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
    key = (src_id, target_id, "provides")
    if key in seen:
        return
    seen.add(key)
    line = position.line if position is not None else None
    edge = {
        "relation": "provides",
        "source": src_id,
        "target": target_id,
        "source_file": rel,
        "source_location": f"L{line}" if line else "",
        "weight": 1.0,
    }
    annotate_edge(edge, FRAMEWORK_RESOLVED, "spring_bean")
    edges.append(edge)


def _parse_pointcut_targets(method, import_map, fqn_to_file) -> list[str]:
    """Extract target class files from AOP pointcut annotations.
    Parses: execution(* com.example.service.UserService.*(..))"""
    targets: list[str] = []
    for ann in (getattr(method, "annotations", None) or []):
        name = getattr(ann, "name", None)
        if name not in ("Around", "Before", "After", "AfterReturning", "AfterThrowing"):
            continue
        element = getattr(ann, "element", None)
        if element is None:
            continue
        # Extract string value from annotation
        val = None
        if isinstance(element, list):
            for e in element:
                v = getattr(e, "value", None)
                if isinstance(v, str):
                    val = v
                    break
        elif hasattr(element, "value"):
            val = element.value if isinstance(element.value, str) else None
        elif isinstance(element, str):
            val = element
        if not val:
            continue
        val = val.strip('"')
        # Parse execution pointcut: execution(* com.example.ClassName.*(..))
        m = re.search(r"execution\s*\(\s*\S+\s+([A-Za-z_][\w.]*)\.[^.]+\(", val)
        if m:
            fqn = m.group(1)
            target_file = fqn_to_file.get(fqn)
            if target_file:
                targets.append(target_file)
            else:
                # Try matching by simple name from import map
                simple = fqn.rsplit(".", 1)[-1] if "." in fqn else fqn
                resolved_fqn = import_map.get(simple)
                if resolved_fqn:
                    tf = fqn_to_file.get(resolved_fqn)
                    if tf:
                        targets.append(tf)
    return targets


def _extract_publish_event_edges(
    java_files: list,
    parsed: list,
    fqn_to_file: dict,
    node_index: dict,
    seen: set,
    edges: list,
) -> None:
    """Detect applicationEventPublisher.publishEvent(new EventType(...)) calls.

    Regex-based pass on raw source to find publishEvent() invocations.
    Emits 'calls' edge from the containing class to the event class type.
    """
    _PUBLISH_RE = re.compile(
        r"""publishEvent\s*\(\s*new\s+([A-Za-z_][\w]*)\s*\(""",
        re.MULTILINE,
    )

    for jf, rel, tree in parsed:
        try:
            code = jf.read_text(encoding="utf-8")
        except OSError:
            continue

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

            for m in _PUBLISH_RE.finditer(code):
                event_name = m.group(1)
                fqn = import_map.get(event_name)
                if not fqn:
                    continue
                target_file = fqn_to_file.get(fqn)
                if not target_file:
                    continue
                target_id = (
                    node_index["by_symbol"].get((target_file, event_name.lower()))
                    or node_index["by_file"].get(target_file)
                )
                if not target_id or target_id == class_node_id:
                    continue
                key = (class_node_id, target_id, "calls")
                if key in seen:
                    continue
                seen.add(key)
                lineno = code[: m.start()].count("\n") + 1
                edge = {
                    "relation": "calls",
                    "source": class_node_id,
                    "target": target_id,
                    "source_file": rel,
                    "source_location": f"L{lineno}",
                    "weight": 0.9,
                }
                annotate_edge(edge, FRAMEWORK_RESOLVED, "spring_publisher")
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
