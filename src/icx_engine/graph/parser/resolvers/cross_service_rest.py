"""Cross-service REST linker.

At build time, scans ~/.icx/graphs/registry.json for other registered projects,
extracts their REST routes, matches against HTTP client calls in the current
project, and writes cross_links.json to the current project's graph directory.

Does NOT modify graph.json or extraction. The companion cross_links.json is
read by the `graph_cross_links` MCP tool.
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

_SPRING_CLASS_MAPPING_RE = re.compile(
    r'@(?:RequestMapping)\s*\(\s*(?:value\s*=\s*)?["\']([^"\']+)["\']',
    re.MULTILINE,
)
_SPRING_METHOD_MAPPING_RE = re.compile(
    r'@(?:GetMapping|PostMapping|PutMapping|PatchMapping|DeleteMapping|RequestMapping)\s*'
    r'(?:\(\s*(?:value\s*=\s*)?["\']([^"\']*)["\'])?',
    re.MULTILINE,
)
_FASTAPI_ROUTE_RE = re.compile(
    r"""@(?:app|router)\s*\.\s*(?:get|post|put|patch|delete|api_route)\s*\(\s*['"]([^'"]+)['"]""",
    re.IGNORECASE | re.MULTILINE,
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

    return results


def extract_rest_routes(
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

        if f.suffix == ".java":
            class_prefix = ""
            cm = _SPRING_CLASS_MAPPING_RE.search(code)
            if cm:
                class_prefix = cm.group(1).rstrip('/')

            for m in _SPRING_METHOD_MAPPING_RE.finditer(code):
                suffix = m.group(1) or ""
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

        elif f.suffix == ".py":
            for m in _FASTAPI_ROUTE_RE.finditer(code):
                norm = normalize_url(m.group(1))
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
) -> None:
    """Match HTTP calls in current project to REST routes in peer registered projects.

    Writes cross_links.json to out_dir. Never raises; all errors are debug-logged.
    graphs_root defaults to ~/.icx/graphs. Override for testing.
    """
    from icx_engine.graph.storage import derive_project_id, _graphs_root as _default_root

    project_root = Path(project_root).resolve()
    out_dir = Path(out_dir)

    if graphs_root is None:
        graphs_root = _default_root()

    registry_path = graphs_root / "registry.json"
    if not registry_path.exists():
        return

    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception:
        return

    try:
        current_project_id = derive_project_id(project_root)
    except Exception:
        return

    peer_entries = [
        e for e in registry
        if isinstance(e, dict) and e.get("project_id") != current_project_id
    ]
    if not peer_entries:
        return

    files_list = list(files)
    all_calls = extract_http_calls(files_list, project_root)
    if not all_calls:
        return

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

    if not all_matches:
        return

    cross_links = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_project": current_project_id,
        "links": all_matches,
    }
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "cross_links.json").write_text(
            json.dumps(cross_links, indent=2), encoding="utf-8"
        )
        _log.debug(
            "cross_service_rest: wrote %d links to %s",
            len(all_matches),
            out_dir / "cross_links.json",
        )
    except Exception as exc:
        _log.debug("cross_service_rest: failed to write cross_links.json (%s)", exc)
