"""Cross-service REST linker.

At build time, matches HTTP client calls against REST route definitions (Spring/
FastAPI) two ways:

- Same project: a frontend calling its own backend in the same monorepo. Returned as
  real graph edges (relation="calls_api") for the caller (builder.py) to merge into
  extraction["edges"] - these ARE part of graph.json, traversable by graph_call_chain/
  graph_impact like any other edge.
- Peer registered projects: scans ~/.icx/graphs/registry.json for OTHER registered ICX
  projects, extracts their routes, matches against this project's calls. These are
  cross-graph by nature (node ids aren't shared across separate graph.json files) and
  are written to cross_links.json in this project's graph directory instead - read by
  the `graph_cross_links` MCP tool, not merged into graph.json.

Detection is deliberately NOT tied to one project's specific conventions:
- Frontend calls: named patterns (axios/fetch/requests/RestTemplate/.uri()) plus a
  GENERIC shape-based fallback (`_GENERIC_PATH_ARG_RE`) that matches any path-looking
  quoted literal passed as a call argument, regardless of which function is being
  called - so a project's own custom HTTP wrapper (e.g. `sendRequest(data,
  "/getCampaignChannel")`) is caught without hardcoding that project's function names.
  Precision comes from the second stage (a candidate only becomes an edge if it
  actually matches a real extracted backend route), not from narrowing the first stage.
- Backend routes: a Spring mapping annotation's path may be a string literal
  (`@GetMapping("/x")`) or a reference to a project-wide constant
  (`@GetMapping(ServicePaths.GET_X)`, common in enterprise codebases that centralize
  route strings). `_build_java_const_map` resolves the latter by scanning every .java
  file's `public static final String` declarations project-wide, keyed by declaring
  type name - not specific to any one constants-class name. An unresolvable reference
  is silently skipped, never guessed. `value=` and `path=` are both recognized (Spring
  treats them as aliases). The class-level `@RequestMapping` occurrence is excluded from
  the method-level scan by exact source position (both regexes match the same
  `@RequestMapping` token; without this a class-level mapping was double-counted as a
  spurious method route).
- FastAPI routes: `APIRouter(prefix="/x")` is FastAPI's own documented multi-router
  structure (not a project-specific convention) - `_FASTAPI_ROUTER_PREFIX_RE` resolves
  the prefix for whatever variable name the router was assigned to (not hardcoded to
  literally "router"), so `@orders_router.get("/y")` correctly resolves to "/x/y"
  instead of losing the prefix entirely.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_log = logging.getLogger(__name__)

_HTTP_CLIENT_PATTERNS: list[tuple] = [
    (re.compile(
        r"""axios\s*\.\s*(get|post|put|patch|delete|head)\s*\(\s*[`'"]([^`'"]+)[`'"]""",
        re.IGNORECASE,
    ), 1, 2),
    (re.compile(
        r"""\bfetch\s*\(\s*[`'"]([^`'"]+)[`'"]""",
        re.IGNORECASE,
    ), None, 1),
    (re.compile(
        r"""\brequests\s*\.\s*(get|post|put|patch|delete|head)\s*\(\s*['"]([^'"]+)['"]""",
        re.IGNORECASE,
    ), 1, 2),
    (re.compile(
        r"""\brestTemplate\s*\.\s*(?:getForObject|postForObject|getForEntity|postForEntity|exchange|patchForObject)\s*\(\s*['"]([^'"]+)['"]""",
        re.IGNORECASE,
    ), None, 1),
    (re.compile(
        r"""\.uri\s*\(\s*['"]([^'"]+)['"]""",
        re.IGNORECASE,
    ), None, 1),
]

# Generic fallback - NOT tied to any specific function/wrapper name (axios/fetch/etc are
# a small fixed set; every codebase's own HTTP helper is different, e.g. a project-local
# `sendRequest(data, "/getCampaignChannel")`). Matches ANY quoted, path-shaped string
# literal ("/segment/segment...") passed as a call argument, regardless of which function
# is being called. This is intentionally shape-based, not name-based - it generalizes to
# whatever convention a given codebase actually uses instead of hardcoding one project's
# wrapper names. Precision comes from the SECOND stage (match_calls_to_routes only emits
# an edge when this literal actually matches a real extracted backend route) - a stray
# path-shaped string with no matching route simply produces no edge, so this pattern can
# afford to be broad here without inflating false positives downstream.
_GENERIC_PATH_ARG_RE = re.compile(
    r"""[,(]\s*[`'"](/(?!/)[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-{}:.]*)*)[`'"]""",
)

# Static asset / import extensions - excluded from the generic pattern above so a
# "/assets/logo.png"-style import string is never mistaken for an API path. A universal
# web-file-extension convention, not specific to any one codebase's naming.
_STATIC_ASSET_EXTENSIONS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".css", ".scss", ".less",
    ".woff", ".woff2", ".ttf", ".eot", ".map", ".mp4", ".mp3", ".pdf",
)

_SPRING_CLASS_MAPPING_RE = re.compile(
    r'@(?:RequestMapping)\s*\(\s*(?:(?:value|path)\s*=\s*)?'
    r'(?:["\']([^"\']+)["\']|([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+))',
    re.MULTILINE,
)
_SPRING_METHOD_MAPPING_RE = re.compile(
    r'@(?:GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping|RequestMapping)\s*'
    r'(?:\(\s*(?:(?:value|path)\s*=\s*)?'
    r'(?:["\']([^"\']*)["\']|([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+))?'
    r'\s*\))?',
    re.MULTILINE,
)
# Route path stored as a constant and referenced by name (e.g.
# `@GetMapping(MagikServicePath.GET_CAMPAIGN_CHANNEL)`) instead of inlined - a Spring-side
# equivalent of the JS const-resolution below, not tied to any one project's constants
# class name. `_SPRING_*_MAPPING_RE` above capture the dotted reference (e.g.
# "MagikServicePath.GET_CAMPAIGN_CHANNEL") in their second group when no literal is
# present; this regex finds where such constants are actually declared, anywhere in the
# project, so the reference can be resolved regardless of which file declares it.
_JAVA_CONST_DECL_RE = re.compile(
    r'(?:public|private|protected)?\s*(?:static\s+final|final\s+static)\s+String\s+'
    r'([A-Za-z_]\w*)\s*=\s*"([^"]*)"',
    re.MULTILINE,
)
_JAVA_TYPE_DECL_RE = re.compile(r'\b(?:class|interface|enum)\s+([A-Za-z_]\w*)')

# NOT hardcoded to the literal names "app"/"router" - captures whichever identifier the
# route decorator is actually attached to (group 1), since a real FastAPI project can
# name its router/app variable anything (`orders_router`, `api`, ...).
_FASTAPI_ROUTE_RE = re.compile(
    r"""@([A-Za-z_]\w*)\s*\.\s*(?:get|post|put|patch|delete|api_route)\s*\(\s*['"]([^'"]+)['"]""",
    re.IGNORECASE | re.MULTILINE,
)
# APIRouter(prefix="/orders") is FastAPI's own officially recommended multi-router
# structure, not a project-specific convention - without resolving the prefix, every
# route on that router loses its real path entirely (e.g. "/orders/{id}" comes back as
# just "/{id}"). Captures (router_var, prefix_value); {0,300} bounds the scan between the
# variable assignment and its `prefix=` kwarg so this can't runaway-match across
# unrelated code when other kwargs (tags=[...], dependencies=[...]) come first.
_FASTAPI_ROUTER_PREFIX_RE = re.compile(
    r'([A-Za-z_]\w*)\s*=\s*APIRouter\s*\([\s\S]{0,300}?prefix\s*=\s*[\'"]([^\'"]*)[\'"]',
)
_PATH_PARAM_RE = re.compile(r'\{[^}]+\}|:[a-zA-Z_]\w*')
_TEMPLATE_VAR_RE = re.compile(r'\$\{[^}]+\}|\$[A-Za-z_]\w*')

# Matches simple JS/TS string constant declarations, e.g.:
#   const BASE = '/api/v1'   or   const BASE = "/api/v1"
_JS_CONST_RE = re.compile(
    r'(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*[\'"]([^\'"]*)[\'"]',
    re.MULTILINE,
)

# Matches ${VARNAME} in template literals
_TEMPLATE_SUBST_RE = re.compile(r'\$\{([A-Za-z_$][A-Za-z0-9_$]*)\}')


def _resolve_js_template(url_raw: str, const_map: dict[str, str]) -> str:
    """Substitute known JS string constants into a template literal URL."""
    def replacer(m: re.Match) -> str:
        return const_map.get(m.group(1), m.group(0))
    return _TEMPLATE_SUBST_RE.sub(replacer, url_raw)


def normalize_url(url: str) -> str:
    url = re.sub(r'^https?://[^/]+', '', url)
    url = re.sub(r'[?#].*$', '', url)
    url = _TEMPLATE_VAR_RE.sub('', url)
    url = _PATH_PARAM_RE.sub('*', url)
    url = re.sub(r'/+', '/', url)
    url = url.rstrip('/')
    if not url.startswith('/'):
        url = '/' + url
    return url or '/'


def extract_http_calls(
    files: Iterable[Path],
    project_root: Path,
) -> list[dict]:
    project_root = Path(project_root).resolve()
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for f in files:
        f = Path(f).resolve()
        try:
            rel = f.relative_to(project_root).as_posix()
            code = f.read_text(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue

        # Build a simple map of string constants for template literal resolution
        const_map: dict[str, str] = {
            m.group(1): m.group(2)
            for m in _JS_CONST_RE.finditer(code)
        }

        for pattern, method_group, url_group in _HTTP_CLIENT_PATTERNS:
            for m in pattern.finditer(code):
                url_raw = m.group(url_group)
                if not url_raw or len(url_raw) > 512:
                    continue
                # Resolve template literal variable substitutions before normalizing
                url_resolved = _resolve_js_template(url_raw, const_map)
                if re.match(r'^https?://(?!localhost)', url_resolved):
                    continue
                norm = normalize_url(url_resolved)
                if len(norm) < 2:
                    continue
                http_method = m.group(method_group).upper() if method_group else "ANY"
                key = (rel, norm)
                if key in seen:
                    continue
                seen.add(key)
                lineno = code[:m.start()].count('\n') + 1
                results.append({
                    "source_file": rel,
                    "url_raw": url_raw,
                    "url_pattern": norm,
                    "http_method": http_method,
                    "line_no": lineno,
                })

        # Generic fallback: any call-argument-shaped, path-looking literal, whatever
        # function it's passed to - catches a project's own custom HTTP wrapper
        # (e.g. sendRequest(data, "/getCampaignChannel")) without needing to name it.
        for m in _GENERIC_PATH_ARG_RE.finditer(code):
            url_raw = m.group(1)
            if not url_raw or len(url_raw) > 512:
                continue
            if url_raw.lower().endswith(_STATIC_ASSET_EXTENSIONS):
                continue
            url_resolved = _resolve_js_template(url_raw, const_map)
            norm = normalize_url(url_resolved)
            if len(norm) < 2:
                continue
            key = (rel, norm)
            if key in seen:
                continue
            seen.add(key)
            lineno = code[:m.start()].count('\n') + 1
            results.append({
                "source_file": rel,
                "url_raw": url_raw,
                "url_pattern": norm,
                "http_method": "ANY",
                "line_no": lineno,
            })

    return results


def _build_java_const_map(java_files: list[tuple[Path, str, str]]) -> dict[str, dict[str, str]]:
    """{declaring_type_simple_name: {CONST_NAME: literal_value}} across every .java file
    given. One primary type per file is a Java language convention (not project-specific),
    so the file's first class/interface/enum declaration is treated as the declaring type
    for any String constants found in it."""
    const_map: dict[str, dict[str, str]] = {}
    for _f, _rel, code in java_files:
        type_m = _JAVA_TYPE_DECL_RE.search(code)
        if not type_m:
            continue
        type_name = type_m.group(1)
        for cm in _JAVA_CONST_DECL_RE.finditer(code):
            const_map.setdefault(type_name, {})[cm.group(1)] = cm.group(2)
    return const_map


def _resolve_mapping_value(
    literal: str | None, const_ref: str | None, const_map: dict[str, dict[str, str]],
) -> str | None:
    """Resolve a Spring mapping annotation's argument: a plain literal is used as-is; a
    dotted reference (e.g. "MagikServicePath.GET_CAMPAIGN_CHANNEL") is looked up in the
    project-wide constant map built by _build_java_const_map. Returns None if a const
    reference can't be resolved (route silently skipped, same as before this fix existed -
    never fabricate a guessed path)."""
    if literal is not None:
        return literal
    if not const_ref:
        return None
    type_name, _, const_name = const_ref.rpartition(".")
    return const_map.get(type_name, {}).get(const_name)


def extract_rest_routes(
    files: Iterable[Path],
    project_root: Path,
) -> list[dict]:
    project_root = Path(project_root).resolve()
    results: list[dict] = []
    seen: set[tuple[str, str]] = set()

    files_list = list(files)
    java_entries: list[tuple[Path, str, str]] = []
    for f in files_list:
        f = Path(f).resolve()
        if f.suffix != ".java":
            continue
        try:
            rel = f.relative_to(project_root).as_posix()
            code = f.read_text(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue
        java_entries.append((f, rel, code))

    const_map = _build_java_const_map(java_entries)

    for f, rel, code in java_entries:
        class_prefix = ""
        class_mapping_start = None
        cm = _SPRING_CLASS_MAPPING_RE.search(code)
        if cm:
            class_mapping_start = cm.start()
            resolved = _resolve_mapping_value(cm.group(1), cm.group(2), const_map)
            if resolved:
                class_prefix = resolved.rstrip('/')

        for m in _SPRING_METHOD_MAPPING_RE.finditer(code):
            # _SPRING_METHOD_MAPPING_RE also matches @RequestMapping by name (needed for
            # method-level @RequestMapping(method=..., value=...) usage) - both regexes
            # anchor on the same literal "@RequestMapping" token, so the class-level
            # occurrence just consumed above would otherwise be re-counted here as if it
            # were ALSO a distinct method route (producing a garbled class_prefix+
            # class_prefix entry). Skip the exact occurrence already used as the class
            # mapping.
            if class_mapping_start is not None and m.start() == class_mapping_start:
                continue
            suffix = _resolve_mapping_value(m.group(1), m.group(2), const_map) or ""
            full = class_prefix + ("/" + suffix.lstrip('/') if suffix else "")
            norm = normalize_url(full) if full else normalize_url(class_prefix)
            if not norm or norm == "/":
                continue
            ann = m.group(0).split('(')[0].strip('@')
            method = _spring_annotation_to_method(ann)
            key = (rel, norm)
            if key in seen:
                continue
            seen.add(key)
            lineno = code[:m.start()].count('\n') + 1
            results.append({
                "source_file": rel,
                "url_pattern": norm,
                "http_method": method,
                "line_no": lineno,
            })

    for f in files_list:
        f = Path(f).resolve()
        if f.suffix != ".py":
            continue
        try:
            rel = f.relative_to(project_root).as_posix()
            code = f.read_text(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            continue

        # router_var -> prefix, e.g. `orders_router = APIRouter(prefix="/orders")` -
        # scoped per file since the router declaration and its @router.get(...) routes
        # are virtually always in the same file for this idiom.
        router_prefixes: dict[str, str] = {
            m.group(1): m.group(2) for m in _FASTAPI_ROUTER_PREFIX_RE.finditer(code)
        }

        for m in _FASTAPI_ROUTE_RE.finditer(code):
            router_var, path = m.group(1), m.group(2)
            prefix = router_prefixes.get(router_var, "")
            full = prefix.rstrip('/') + "/" + path.lstrip('/') if prefix else path
            norm = normalize_url(full)
            key = (rel, norm)
            if key in seen:
                continue
            seen.add(key)
            lineno = code[:m.start()].count('\n') + 1
            results.append({
                "source_file": rel,
                "url_pattern": norm,
                "http_method": "ANY",
                "line_no": lineno,
            })

    return results


def _spring_annotation_to_method(annotation: str) -> str:
    mapping = {
        "GetMapping": "GET",
        "PostMapping": "POST",
        "PutMapping": "PUT",
        "PatchMapping": "PATCH",
        "DeleteMapping": "DELETE",
        "RequestMapping": "ANY",
    }
    for k, v in mapping.items():
        if k in annotation:
            return v
    return "ANY"


def match_calls_to_routes(
    calls: list[dict],
    routes: list[dict],
    caller_project_id: str,
    callee_project_id: str,
) -> list[dict]:
    route_map: dict[str, list[dict]] = {}
    for r in routes:
        route_map.setdefault(r["url_pattern"], []).append(r)

    matches: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for call in calls:
        norm = call["url_pattern"]
        matched_routes = route_map.get(norm, [])
        if not matched_routes:
            for rp, rs in route_map.items():
                if norm.startswith(rp + '/') or rp.startswith(norm.rstrip('*/') + '/'):
                    matched_routes = rs
                    break
        for route in matched_routes:
            method_ok = (
                call["http_method"] == "ANY"
                or route["http_method"] == "ANY"
                or call["http_method"] == route["http_method"]
            )
            if not method_ok:
                continue
            key = (call["source_file"], route["source_file"], norm)
            if key in seen:
                continue
            seen.add(key)
            matches.append({
                "source_project": caller_project_id,
                "source_file": call["source_file"],
                "source_location": f"L{call['line_no']}",
                "http_method": call["http_method"],
                "url_pattern": norm,
                "target_project": callee_project_id,
                "target_file": route["source_file"],
                "target_location": f"L{route['line_no']}",
                "confidence": 0.85,
            })

    return matches


def run_cross_service_linking(
    files: Iterable[Path],
    project_root: Path,
    extraction: dict,
    out_dir: Path,
    graphs_root: Path | None = None,
) -> list[dict]:
    """Match HTTP calls to REST routes, both within the current project (a frontend
    calling its own backend in the same monorepo) and against peer registered ICX
    projects (separate registered graphs, e.g. a frontend repo + a backend repo).

    Peer-project matches are cross-graph by nature (node ids aren't shared across
    separate graph.json files) and are written to cross_links.json in out_dir, read by
    the graph_cross_links MCP tool - unchanged from before.

    Same-project matches ARE real edges within one graph, keyed by node id (not just
    file path), and are returned as a list[dict] in the same {relation, source, target,
    source_file, source_location, weight} shape every other resolver's edges use, ready
    for the caller (builder.py) to merge into extraction["edges"] the same way
    event_resolver's edges are merged. relation="calls_api", confidence 0.85 - matches
    the confidence already used for the (identical-mechanism) peer-project matches.

    Never raises; all errors are debug-logged. graphs_root defaults to ~/.icx/graphs.
    Override for testing.
    """
    from icx_engine.graph.storage import derive_project_id, _graphs_root as _default_root
    from icx_engine.graph.parser.confidence import annotate_edge

    project_root = Path(project_root).resolve()
    out_dir = Path(out_dir)

    try:
        current_project_id = derive_project_id(project_root)
    except Exception:
        return []

    files_list = list(files)
    all_calls = extract_http_calls(files_list, project_root)
    if not all_calls:
        return []

    # -- Same-project matching: real graph edges, returned to the caller -------------
    same_project_edges: list[dict] = []
    try:
        own_routes = extract_rest_routes(files_list, project_root)
        if own_routes:
            own_matches = match_calls_to_routes(
                all_calls, own_routes,
                caller_project_id=current_project_id,
                callee_project_id=current_project_id,
            )
            # match_calls_to_routes always returns project-root-relative source_file/
            # target_file (extract_http_calls/extract_rest_routes compute
            # f.relative_to(project_root)), but a graph node's own source_file may be
            # stored either relative or absolute (builder.py's _abs_edges() convention is
            # absolute, but this resolver doesn't control what upstream AST extraction
            # used). Index under BOTH forms - same defensive dual-keying event_resolver.py
            # already uses for the identical file-path-to-node-id problem - so the lookup
            # below is correct regardless of which form is actually present.
            root_posix = Path(project_root).resolve().as_posix()
            node_by_file: dict[str, list] = {}
            for n in (extraction or {}).get("nodes", []) or []:
                sf = (n.get("source_file") or n.get("file") or "").replace("\\", "/")
                if not sf:
                    continue
                node_by_file.setdefault(sf, []).append(n)
                is_relative = not (sf.startswith("/") or (len(sf) > 1 and sf[1] == ":"))
                if is_relative:
                    node_by_file.setdefault(f"{root_posix}/{sf}", []).append(n)
                elif sf.startswith(root_posix + "/"):
                    node_by_file.setdefault(sf[len(root_posix) + 1:], []).append(n)

            for m in own_matches:
                src_file = m["source_file"]
                tgt_file = m["target_file"]
                if src_file == tgt_file:
                    continue  # a route file calling its own route is noise, not signal
                src_nodes = node_by_file.get(src_file)
                tgt_nodes = node_by_file.get(tgt_file)
                if not src_nodes or not tgt_nodes:
                    continue
                edge = {
                    "relation": "calls_api",
                    "source": src_nodes[0]["id"],
                    "target": tgt_nodes[0]["id"],
                    "source_file": src_file,
                    "source_location": m["source_location"],
                    "weight": 1.0,
                    "http_method": m["http_method"],
                    "url_pattern": m["url_pattern"],
                }
                # 0.85: same value the pre-existing peer-project matching already uses for
                # this identical URL-pattern-match mechanism (match_calls_to_routes below).
                annotate_edge(edge, 0.85, "cross_service_rest")
                same_project_edges.append(edge)
    except Exception as exc:
        _log.debug("cross_service_rest: same-project matching failed (%s)", exc)

    # -- Cross-project matching: separate graphs, written to cross_links.json --------
    try:
        if graphs_root is None:
            graphs_root = _default_root()
        registry_path = graphs_root / "registry.json"
        if registry_path.exists():
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            peer_entries = [
                e for e in registry
                if isinstance(e, dict) and e.get("project_id") != current_project_id
            ]
            all_matches: list[dict] = []
            for peer in peer_entries:
                peer_id = peer.get("project_id", "")
                peer_path_str = peer.get("path", "")
                if not peer_id or not peer_path_str:
                    continue
                peer_path = Path(peer_path_str)
                if not peer_path.exists():
                    continue
                try:
                    peer_java = list(peer_path.rglob("*.java"))
                    peer_py = list(peer_path.rglob("*.py"))
                    peer_routes = extract_rest_routes(peer_java + peer_py, peer_path)
                except Exception:
                    continue
                if not peer_routes:
                    continue
                matches = match_calls_to_routes(
                    all_calls, peer_routes,
                    caller_project_id=current_project_id,
                    callee_project_id=peer_id,
                )
                all_matches.extend(matches)

            if all_matches:
                cross_links = {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "source_project": current_project_id,
                    "links": all_matches,
                }
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / "cross_links.json").write_text(
                    json.dumps(cross_links, indent=2), encoding="utf-8"
                )
                _log.debug(
                    "cross_service_rest: wrote %d peer-project link(s) to %s",
                    len(all_matches), out_dir / "cross_links.json",
                )
    except Exception as exc:
        _log.debug("cross_service_rest: peer-project matching failed (%s)", exc)

    return same_project_edges
