"""Java inter-service/inter-class client resolver.

Detects:
  * @FeignClient interfaces -- emits "calls_service" edges to matching
    @RestController classes serving the same service/path prefix, plus
    standard type edges for method return/parameter types.
  * RestTemplate usage -- getForObject / postForObject / exchange with
    class literal arguments -> type edges to the referenced DTO class.
  * WebClient usage -- bodyToMono / bodyToFlux / body(BodyInserters.fromValue(...))
    with class literal arguments -> type edges to the referenced DTO class.
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

_FEIGN_ANNOTATION = "FeignClient"

# Substrings that any HTTP-client source must contain. Cheap per-file gate:
# a file with none of these cannot emit client edges, so skip its tree walk
# (perf only, zero edge loss).
_CLIENT_TRIGGERS = ("FeignClient", "RestTemplate", "WebClient")

_CONTROLLER_ANNOTATIONS: frozenset[str] = frozenset({
    "RestController", "Controller",
})

_MAPPING_ANNOTATIONS: frozenset[str] = frozenset({
    "RequestMapping", "GetMapping", "PostMapping",
    "PutMapping", "PatchMapping", "DeleteMapping",
})

_REST_TEMPLATE_METHODS: frozenset[str] = frozenset({
    "getForObject", "getForEntity",
    "postForObject", "postForEntity",
    "exchange", "patchForObject",
})

_WEBCLIENT_BODY_METHODS: frozenset[str] = frozenset({
    "bodyToMono", "bodyToFlux",
})

# Regex to find class literal references in source text (fallback scan).
_CLASS_LITERAL_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\s*\.class\b")

# Regex to extract service name from restTemplate/webClient URI strings.
_URI_SERVICE_RE = re.compile(r"""["']https?://([a-z0-9\-]+)""", re.IGNORECASE)


def extract_java_client_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    """Main entry point conforming to the resolver protocol."""
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

    # First pass: parse only files that use an HTTP client, build metadata.
    fqn_to_file: dict[str, str] = {}
    parsed: list[tuple[Path, str, object]] = []

    from . import _java_parse_cache as _jpc

    for jf in java_files:
        try:
            rel = jf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        # Cheap gate: only files referencing an HTTP client can emit client
        # edges. Skip the parse-tree walk for the rest - zero edge loss, since
        # fqn_to_file is rebuilt for ALL files below via the regex fqn map.
        try:
            src = jf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(trigger in src for trigger in _CLIENT_TRIGGERS):
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

    # Build the full fqn -> file map over ALL files (call targets may live in
    # gated-out files). Regex-based, also covers javalang timeouts.
    from . import _java_fqn_map as _jfm
    for _fqn, _rel in _jfm.build_fqn_map(java_files, project_root).items():
        fqn_to_file.setdefault(_fqn, _rel)

    # Build controller index: service-name -> list of (node_id, rel).
    # The "service name" for a controller is derived from its
    # @RequestMapping value at class level, or from the class name with
    # common suffixes stripped and kebab-cased.
    controller_index: dict[str, list[str]] = {}  # service_name -> [node_id]
    _build_controller_index(parsed, node_index, controller_index)

    pkg_index = _build_pkg_member_index(fqn_to_file)

    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for jf, rel, tree in parsed:
        file_node_id = node_index["by_file"].get(rel)
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }
        package = tree.package.name if tree.package else ""
        import_map = _build_import_map(tree, package, fqn_to_file, pkg_index)

        for type_decl in (tree.types or []):
            type_name = getattr(type_decl, "name", "") or ""
            class_node_id = local_symbols.get(type_name.lower()) or file_node_id
            if not class_node_id:
                continue

            anns = _annotation_names(type_decl)

            # --- Feign Client detection ---
            if _FEIGN_ANNOTATION in anns:
                service_name = _extract_feign_service_name(type_decl)
                if service_name:
                    _emit_feign_edges(
                        type_decl, service_name, class_node_id,
                        controller_index, import_map, fqn_to_file,
                        node_index, seen, edges, rel,
                    )

            # --- RestTemplate / WebClient usage in method bodies ---
            source_text = None
            for method in (getattr(type_decl, "methods", None) or []):
                method_node_id = local_symbols.get(method.name.lower()) or class_node_id
                # Try AST-based detection first.
                ast_types = _find_client_class_refs_ast(method)
                if ast_types:
                    for type_ref in ast_types:
                        _emit_type_edge(
                            type_ref, "calls_type", import_map, fqn_to_file,
                            node_index, seen, edges,
                            src_id=method_node_id, rel=rel,
                            position=getattr(method, "position", None),
                        )
                else:
                    # Fallback: regex scan on source for .class patterns in
                    # RestTemplate/WebClient invocations.
                    if source_text is None:
                        try:
                            source_text = jf.read_text(encoding="utf-8")
                        except Exception:
                            source_text = ""
                    _emit_regex_client_edges(
                        method, source_text, import_map, fqn_to_file,
                        node_index, seen, edges,
                        src_id=method_node_id, rel=rel,
                    )

    return edges


# ---------------------------------------------------------------------------
# Feign helpers
# ---------------------------------------------------------------------------

def _extract_feign_service_name(type_decl) -> str | None:
    """Extract the service name from @FeignClient annotation."""
    for ann in (getattr(type_decl, "annotations", None) or []):
        if getattr(ann, "name", None) != _FEIGN_ANNOTATION:
            continue
        element = getattr(ann, "element", None)
        if element is None:
            continue
        # Single-value annotation: @FeignClient("service-name")
        if isinstance(element, str):
            return element.strip('"').strip("'")
        if hasattr(element, "value"):
            val = getattr(element, "value", None)
            if isinstance(val, str):
                return val.strip('"').strip("'")
        # Multi-value: @FeignClient(name="x", value="y", url="...")
        if isinstance(element, list):
            for pair in element:
                pair_name = getattr(pair, "name", None)
                if pair_name in ("value", "name"):
                    val = getattr(pair, "value", None)
                    if isinstance(val, str):
                        return val.strip('"').strip("'")
                    val_node = getattr(val, "value", None) if val else None
                    if isinstance(val_node, str):
                        return val_node.strip('"').strip("'")
    return None


def _emit_feign_edges(
    type_decl, service_name: str, feign_node_id: str,
    controller_index: dict[str, list[str]],
    import_map: dict[str, str], fqn_to_file: dict[str, str],
    node_index: dict, seen: set, edges: list[dict], rel: str,
) -> None:
    """Emit calls_service edges from Feign interface to matching controllers."""
    # Try to match service name to controllers.
    normalized = _normalize_service_name(service_name)
    target_ids = controller_index.get(normalized, [])
    for target_id in target_ids:
        if target_id == feign_node_id:
            continue
        key = (feign_node_id, target_id, "calls_service")
        if key in seen:
            continue
        seen.add(key)
        edge = {
            "relation": "calls_service",
            "source": feign_node_id,
            "target": target_id,
            "source_file": rel,
            "source_location": _position_str(type_decl),
            "weight": 1.0,
            "metadata": {"service_name": service_name},
        }
        annotate_edge(edge, FRAMEWORK_RESOLVED, "java_clients_feign")
        edges.append(edge)

    # Emit type edges for method return types and parameter types.
    for method in (getattr(type_decl, "methods", None) or []):
        # Return type.
        ret_type = getattr(method, "return_type", None)
        if ret_type:
            _emit_type_edge_from_node(
                ret_type, import_map, fqn_to_file, node_index,
                seen, edges, src_id=feign_node_id, rel=rel,
                position=getattr(method, "position", None),
            )
        # Parameter types.
        for param in (getattr(method, "parameters", None) or []):
            param_type = getattr(param, "type", None)
            if param_type:
                _emit_type_edge_from_node(
                    param_type, import_map, fqn_to_file, node_index,
                    seen, edges, src_id=feign_node_id, rel=rel,
                    position=getattr(method, "position", None),
                )


# ---------------------------------------------------------------------------
# RestTemplate / WebClient AST detection
# ---------------------------------------------------------------------------

def _find_client_class_refs_ast(method) -> list[str]:
    """Walk the method body AST looking for class literals used with
    RestTemplate/WebClient method calls.

    Returns a list of simple type names (e.g. ["UserDto", "OrderResponse"]).
    """
    refs: list[str] = []
    body = getattr(method, "body", None)
    if not body:
        return refs
    _walk_for_class_refs(body, refs)
    return refs


def _walk_for_class_refs(node, refs: list[str]) -> None:
    """Recursively walk AST nodes looking for MethodInvocation patterns
    that indicate RestTemplate/WebClient calls with class literal args."""
    if node is None:
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _walk_for_class_refs(item, refs)
        return

    # Check for MethodInvocation nodes.
    node_type = type(node).__name__
    if node_type == "MethodInvocation":
        method_name = getattr(node, "member", None)
        # RestTemplate methods with class literal argument.
        if method_name in _REST_TEMPLATE_METHODS:
            _extract_class_args(node, refs)
        # WebClient bodyToMono/bodyToFlux.
        elif method_name in _WEBCLIENT_BODY_METHODS:
            _extract_class_args(node, refs)

    # Recurse into child attributes.
    if hasattr(node, "attrs"):
        for attr_name in node.attrs:
            child = getattr(node, attr_name, None)
            if child is not None:
                _walk_for_class_refs(child, refs)
    elif hasattr(node, "children"):
        children = getattr(node, "children", None)
        if children:
            for child in children:
                if child is not None:
                    _walk_for_class_refs(child, refs)


def _extract_class_args(invocation_node, refs: list[str]) -> None:
    """From a MethodInvocation, look for arguments that are class literals
    (e.g. UserDto.class) and add the type name to refs."""
    arguments = getattr(invocation_node, "arguments", None)
    if not arguments:
        return
    for arg in arguments:
        # javalang represents Foo.class as a MemberReference with
        # member="class" and qualifier=type name, or as a ClassReference.
        arg_type = type(arg).__name__
        if arg_type == "MemberReference":
            member = getattr(arg, "member", None)
            qualifier = getattr(arg, "qualifier", None)
            if member == "class" and qualifier:
                refs.append(qualifier)
        elif arg_type == "ClassReference":
            # ClassReference has a type attribute.
            cr_type = getattr(arg, "type", None)
            if cr_type:
                name = _extract_type_name(cr_type)
                if name:
                    refs.append(name)


# ---------------------------------------------------------------------------
# Regex fallback for RestTemplate/WebClient
# ---------------------------------------------------------------------------

def _emit_regex_client_edges(
    method, source_text: str, import_map: dict[str, str],
    fqn_to_file: dict[str, str], node_index: dict,
    seen: set, edges: list[dict], *, src_id: str, rel: str,
) -> None:
    """Use regex to find .class patterns in the method's source range."""
    pos = getattr(method, "position", None)
    if not pos or not source_text:
        return
    start_line = pos.line
    # Approximate method body: scan from method start to next method or +100 lines.
    lines = source_text.split("\n")
    end_line = min(start_line + 100, len(lines))
    method_text = "\n".join(lines[start_line - 1:end_line])

    # Only process if there is a RestTemplate or WebClient reference.
    if "restTemplate" not in method_text and "webClient" not in method_text:
        return

    for m in _CLASS_LITERAL_RE.finditer(method_text):
        type_name = m.group(1)
        # Skip common non-DTO classes.
        if type_name in ("String", "Object", "Void", "Class",
                         "Integer", "Long", "Boolean", "Double", "Float",
                         "HttpEntity", "ResponseEntity", "ParameterizedTypeReference"):
            continue
        _emit_type_edge(
            type_name, "calls_type", import_map, fqn_to_file,
            node_index, seen, edges,
            src_id=src_id, rel=rel, position=pos,
        )


# ---------------------------------------------------------------------------
# Controller index (for matching @FeignClient to @RestController)
# ---------------------------------------------------------------------------

def _build_controller_index(
    parsed: list[tuple[Path, str, object]],
    node_index: dict,
    controller_index: dict[str, list[str]],
) -> None:
    """Build a mapping from normalized service names to controller node IDs."""
    for jf, rel, tree in parsed:
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }
        file_node_id = node_index["by_file"].get(rel)

        for type_decl in (tree.types or []):
            type_name = getattr(type_decl, "name", "") or ""
            anns = _annotation_names(type_decl)
            if not (anns & _CONTROLLER_ANNOTATIONS):
                continue

            class_node_id = local_symbols.get(type_name.lower()) or file_node_id
            if not class_node_id:
                continue

            # Derive service name from @RequestMapping at class level.
            class_path = _extract_mapping_path(type_decl)
            if class_path:
                # "/api/users" -> "users"; "/user-service/api" -> "user-service"
                normalized = _normalize_path_to_service(class_path)
                if normalized:
                    controller_index.setdefault(normalized, []).append(class_node_id)

            # Also derive from class name:
            # UserServiceController -> user-service
            name_based = _class_name_to_service(type_name)
            if name_based:
                controller_index.setdefault(name_based, []).append(class_node_id)


def _extract_mapping_path(type_decl) -> str | None:
    """Extract the path from @RequestMapping on a class."""
    for ann in (getattr(type_decl, "annotations", None) or []):
        ann_name = getattr(ann, "name", None)
        if ann_name not in _MAPPING_ANNOTATIONS:
            continue
        element = getattr(ann, "element", None)
        if element is None:
            continue
        if isinstance(element, str):
            return element.strip('"').strip("'")
        if hasattr(element, "value"):
            val = getattr(element, "value", None)
            if isinstance(val, str):
                return val.strip('"').strip("'")
        if isinstance(element, list):
            for pair in element:
                pair_name = getattr(pair, "name", None)
                if pair_name in ("value", "path"):
                    val = getattr(pair, "value", None)
                    if isinstance(val, str):
                        return val.strip('"').strip("'")
                    val_node = getattr(val, "value", None) if val else None
                    if isinstance(val_node, str):
                        return val_node.strip('"').strip("'")
    return None


def _normalize_path_to_service(path: str) -> str | None:
    """Normalize a URL path to a service name.

    "/api/users/{id}" -> "users"
    "/user-service/v1/orders" -> "user-service"
    """
    parts = [p for p in path.strip("/").split("/") if p and not p.startswith("{")]
    # Skip common prefixes like "api", "v1", "v2".
    for part in parts:
        if part.lower() in ("api", "v1", "v2", "v3", "rest", "internal"):
            continue
        return _normalize_service_name(part)
    return None


def _class_name_to_service(class_name: str) -> str | None:
    """Convert a controller class name to a normalized service name.

    UserServiceController -> user-service
    OrderController -> order
    """
    # Strip common suffixes.
    for suffix in ("Controller", "Resource", "Endpoint", "Api", "REST"):
        if class_name.endswith(suffix) and len(class_name) > len(suffix):
            class_name = class_name[:-len(suffix)]
            break

    # CamelCase to kebab-case.
    kebab = re.sub(r"([a-z0-9])([A-Z])", r"\1-\2", class_name).lower()
    return kebab if kebab else None


def _normalize_service_name(name: str) -> str:
    """Normalize a service name for matching (lowercase, strip quotes)."""
    return name.strip().strip('"').strip("'").lower().replace("_", "-")


# ---------------------------------------------------------------------------
# Edge emission helpers
# ---------------------------------------------------------------------------

def _emit_type_edge(
    type_name: str, relation: str, import_map: dict[str, str],
    fqn_to_file: dict[str, str], node_index: dict,
    seen: set, edges: list[dict], *,
    src_id: str, rel: str, position,
) -> None:
    """Emit an edge from src_id to the resolved type (by simple name)."""
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
    line = position.line if position is not None and hasattr(position, "line") else None
    edge = {
        "relation": relation,
        "source": src_id,
        "target": target_id,
        "source_file": rel,
        "source_location": f"L{line}" if line else "",
        "weight": 0.9,
    }
    annotate_edge(edge, FRAMEWORK_RESOLVED, "java_clients_rest")
    edges.append(edge)


def _emit_type_edge_from_node(
    type_node, import_map: dict[str, str],
    fqn_to_file: dict[str, str], node_index: dict,
    seen: set, edges: list[dict], *,
    src_id: str, rel: str, position,
) -> None:
    """Emit a type edge by extracting the name from a javalang type node."""
    type_name = _extract_type_name(type_node)
    if not type_name:
        return
    # Also try inner generic: List<UserDto> -> UserDto.
    inner = _inner_generic_type(type_node)
    if inner:
        inner_name = _extract_type_name(inner)
        if inner_name:
            _emit_type_edge(
                inner_name, "calls_type", import_map, fqn_to_file,
                node_index, seen, edges,
                src_id=src_id, rel=rel, position=position,
            )
    _emit_type_edge(
        type_name, "calls_type", import_map, fqn_to_file,
        node_index, seen, edges,
        src_id=src_id, rel=rel, position=position,
    )


# ---------------------------------------------------------------------------
# Utility functions (same pattern as other resolvers)
# ---------------------------------------------------------------------------

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


def _build_pkg_member_index(fqn_to_file: dict[str, str]) -> dict[str, dict[str, str]]:
    """Group FQNs by parent package: {package: {simple_name: fqn}}. Built once so
    _build_import_map does an O(1) lookup instead of scanning every FQN per file.
    Semantics mirror the original `startswith(package + '.')` / `'.' not in simple`
    direct-member condition, with first-FQN-wins over insertion order."""
    index: dict[str, dict[str, str]] = {}
    for fqn in fqn_to_file:
        dot = fqn.rfind(".")
        if dot <= 0:
            continue
        index.setdefault(fqn[:dot], {}).setdefault(fqn[dot + 1 :], fqn)
    return index


def _build_import_map(
    tree,
    package: str,
    fqn_to_file: dict[str, str],
    pkg_index: dict[str, dict[str, str]] | None = None,
) -> dict[str, str]:
    out: dict[str, str] = {}
    for imp in (tree.imports or []):
        path = imp.path
        if imp.wildcard:
            continue
        simple = path.rsplit(".", 1)[-1] if "." in path else path
        if simple:
            out[simple] = path
    if package:
        if pkg_index is not None:
            for simple, fqn in pkg_index.get(package, {}).items():
                out.setdefault(simple, fqn)
        else:
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
