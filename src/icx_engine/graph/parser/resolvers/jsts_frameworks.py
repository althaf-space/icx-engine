"""JS/TS framework resolvers: NestJS, Angular, Express, Fastify.

All four follow the same shape: decorator or method-call patterns
that register routes or declare DI relationships. We use regex over
source text since the patterns are narrow and tree-sitter setup is
not yet wired for TS.

Emits:
  * NestJS: depends_on (constructor + @Inject() field injection),
            routes (@Get/@Post/etc on methods)
  * Angular: depends_on (constructor params with declared types in
             @Component / @Injectable / @Directive)
  * Express + Fastify: routes (app.get/post/use/all, router.METHOD,
                       fastify.get/post/register)
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    FRAMEWORK_RESOLVED, annotate_edge,
)
from icx_engine.graph.parser.resolvers.jsts_imports import (
    _load_tsconfig_paths, _resolve_spec,
)

_log = logging.getLogger(__name__)

_TS_EXTS: tuple[str, ...] = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

_NEST_ROUTE_METHODS: frozenset[str] = frozenset({
    "Get", "Post", "Put", "Patch", "Delete", "Options", "Head", "All",
    "Sse", "Websocket",
})

_NEST_PROVIDER_DECORATORS: frozenset[str] = frozenset({
    "Controller", "Injectable", "Module", "Resolver",
})

_ANGULAR_PROVIDER_DECORATORS: frozenset[str] = frozenset({
    "Component", "Injectable", "Directive", "NgModule", "Pipe",
})

_HTTP_METHODS: frozenset[str] = frozenset({
    "get", "post", "put", "patch", "delete", "head", "options", "all", "use",
})

_DECORATED_CLASS = re.compile(
    r"""(?P<decorators>(?:@[A-Za-z_][A-Za-z0-9_]*\([^)]*\)\s*)+)
        \s*export\s+(?:default\s+)?class\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)
    """,
    re.VERBOSE,
)
_CLASS_DECL = re.compile(r"export\s+(?:default\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)")
_CONSTRUCTOR = re.compile(
    r"constructor\s*\(\s*(?P<params>[^)]*)\s*\)",
    re.DOTALL,
)
_METHOD_DECORATED = re.compile(
    r"""(?P<decorators>(?:@[A-Za-z_][A-Za-z0-9_]*\s*\([^)]*\)\s*)+)
        \s*(?:async\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(""",
    re.VERBOSE,
)
_DECORATOR_NAME = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")


def extract_jsts_framework_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    ts_files = [
        Path(f).resolve()
        for f in files
        if str(f).lower().endswith(_TS_EXTS)
    ]
    if not ts_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_file"]:
        return []

    project_files = set(node_index["by_file"].keys())
    ts_paths_map = _load_tsconfig_paths(project_root)

    seen: set[tuple[str, str, str]] = set()
    edges: list[dict] = []

    for tf in ts_files:
        try:
            rel = tf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            source = tf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_node_id = node_index["by_file"].get(rel)
        if not file_node_id:
            continue
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }

        symbol_to_file = _build_symbol_to_file(
            source, rel, project_root, project_files, ts_paths_map,
        )

        _emit_nest_and_angular(
            source, rel, file_node_id, local_symbols,
            symbol_to_file, node_index, seen, edges,
        )
        _emit_express_fastify_routes(
            source, rel, file_node_id, local_symbols,
            node_index, seen, edges,
        )

    return edges


def _emit_nest_and_angular(
    source: str, rel: str, file_node_id: str, local_symbols: dict[str, str],
    symbol_to_file: dict[str, str], node_index: dict,
    seen: set, edges: list,
) -> None:
    for match in _DECORATED_CLASS.finditer(source):
        class_name = match.group("name")
        decorators_blob = match.group("decorators")
        decorator_names = set(_DECORATOR_NAME.findall(decorators_blob))
        if not (decorator_names & (_NEST_PROVIDER_DECORATORS | _ANGULAR_PROVIDER_DECORATORS)):
            continue

        class_node_id = local_symbols.get(class_name.lower()) or file_node_id
        class_start = match.end()
        class_body = _extract_class_body(source, class_start)
        if not class_body:
            continue

        ctor_match = _CONSTRUCTOR.search(class_body)
        if ctor_match:
            params_blob = ctor_match.group("params")
            for param_name, type_name in _parse_constructor_params(params_blob):
                _ = param_name
                target_file = symbol_to_file.get(type_name)
                if not target_file:
                    continue
                target_id = (
                    node_index["by_symbol"].get((target_file, type_name.lower()))
                    or node_index["by_file"].get(target_file)
                )
                if not target_id or target_id == class_node_id:
                    continue
                key = (class_node_id, target_id, "depends_on")
                if key in seen:
                    continue
                seen.add(key)
                edge = {
                    "relation": "depends_on",
                    "source": class_node_id,
                    "target": target_id,
                    "source_file": rel,
                    "source_location": "",
                    "weight": 1.0,
                }
                annotate_edge(edge, FRAMEWORK_RESOLVED, "jsts_di_resolver")
                edges.append(edge)

        if decorator_names & _NEST_PROVIDER_DECORATORS:
            # Map `import { Post as HttpPost }` back to its real Nest name
            # so `@HttpPost()` still registers as a route.
            alias_to_real = _build_decorator_alias_map(source)
            for method_match in _METHOD_DECORATED.finditer(class_body):
                method_decorators = set(
                    _DECORATOR_NAME.findall(method_match.group("decorators"))
                )
                method_real = {
                    alias_to_real.get(d, d) for d in method_decorators
                }
                if not (method_real & _NEST_ROUTE_METHODS):
                    continue
                method_name = method_match.group("name")
                if method_name == "constructor":
                    continue
                method_node_id = local_symbols.get(method_name.lower()) or class_node_id
                key = (file_node_id, method_node_id, "routes")
                if key in seen:
                    continue
                seen.add(key)
                edge = {
                    "relation": "routes",
                    "source": file_node_id,
                    "target": method_node_id,
                    "source_file": rel,
                    "source_location": "",
                    "weight": 1.0,
                }
                annotate_edge(edge, FRAMEWORK_RESOLVED, "nestjs_resolver")
                edges.append(edge)


def _emit_express_fastify_routes(
    source: str, rel: str, file_node_id: str, local_symbols: dict[str, str],
    node_index: dict, seen: set, edges: list,
) -> None:
    # Match the opening of any `<carrier>.<method>(` expression. We then
    # walk the call args manually to find the trailing handler identifier.
    pattern = re.compile(
        r"\b([A-Za-z_][A-Za-z0-9_]*)\.([a-z]+)\s*\(",
    )
    for match in pattern.finditer(source):
        method = match.group(2)
        if method not in _HTTP_METHODS:
            continue
        open_paren = match.end() - 1
        call_args = _slice_call_args(source, open_paren)
        if call_args is None:
            continue
        handler_name = _extract_handler_name(call_args)
        if not handler_name:
            continue
        handler_node_id = local_symbols.get(handler_name.lower())
        target_id = handler_node_id or file_node_id
        if target_id == file_node_id and not handler_node_id:
            continue
        key = (file_node_id, target_id, "routes")
        if key in seen:
            continue
        seen.add(key)
        line_no = source.count("\n", 0, match.start()) + 1
        edge = {
            "relation": "routes",
            "source": file_node_id,
            "target": target_id,
            "source_file": rel,
            "source_location": f"L{line_no}",
            "weight": 1.0,
            "carrier": match.group(1),
        }
        annotate_edge(edge, FRAMEWORK_RESOLVED, "express_fastify_resolver")
        edges.append(edge)


def _build_symbol_to_file(
    source: str, current_rel: str, project_root: Path,
    project_files: set[str], ts_paths_map: dict,
) -> dict[str, str]:
    """Same shape as react resolver's helper; duplicated to keep resolvers
    independent."""
    named = re.compile(
        r"""import\s*(?:type\s+)?\{\s*([^}]+)\}\s*from\s*['"]([^'"]+)['"]""",
        re.VERBOSE,
    )
    default = re.compile(
        r"""import\s+(?:type\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+from\s*['"]([^'"]+)['"]""",
        re.VERBOSE,
    )
    out: dict[str, str] = {}

    for match in named.finditer(source):
        names_blob, spec = match.group(1), match.group(2)
        target_rel = _resolve_spec(
            spec, current_rel=current_rel, project_root=project_root,
            project_files=project_files, ts_paths_map=ts_paths_map,
        )
        if not target_rel:
            continue
        for part in names_blob.split(","):
            part = part.strip()
            if not part:
                continue
            alias_match = re.match(
                r"([A-Za-z_][A-Za-z0-9_]*)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", part,
            )
            local_name = alias_match.group(2) if alias_match else part.split()[0]
            if local_name:
                out[local_name] = target_rel

    for match in default.finditer(source):
        local_name, spec = match.group(1), match.group(2)
        target_rel = _resolve_spec(
            spec, current_rel=current_rel, project_root=project_root,
            project_files=project_files, ts_paths_map=ts_paths_map,
        )
        if target_rel:
            out[local_name] = target_rel
    return out


def _build_decorator_alias_map(source: str) -> dict[str, str]:
    """Map local alias -> original imported name for named imports.

    Lets `@HttpPost()` resolve back to `Post` when the file uses
    `import { Post as HttpPost } from '@nestjs/common'`.
    """
    pattern = re.compile(
        r"""import\s*(?:type\s+)?\{\s*([^}]+)\}\s*from\s*['"][^'"]+['"]""",
        re.VERBOSE,
    )
    out: dict[str, str] = {}
    for match in pattern.finditer(source):
        for part in match.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            alias = re.match(
                r"([A-Za-z_][A-Za-z0-9_]*)\s+as\s+([A-Za-z_][A-Za-z0-9_]*)", part,
            )
            if alias:
                out[alias.group(2)] = alias.group(1)
    return out


def _parse_constructor_params(params_blob: str):
    """Yield (param_name, type_name) for TS-style typed constructor parameters."""
    if not params_blob.strip():
        return
    depth = 0
    current = []
    parts: list[str] = []
    for ch in params_blob:
        if ch in "<({[":
            depth += 1
        elif ch in ">)}]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if current:
        parts.append("".join(current).strip())

    for part in parts:
        if not part:
            continue
        cleaned = re.sub(r"@[A-Za-z_][A-Za-z0-9_]*\s*\([^)]*\)\s*", "", part)
        cleaned = re.sub(r"@[A-Za-z_][A-Za-z0-9_]*\s+", "", cleaned)
        cleaned = cleaned.strip()
        # Strip any combination of access/visibility modifiers
        # (`private readonly`, `public static`, etc.) one keyword at a time.
        while True:
            new_cleaned = re.sub(
                r"^(public|private|protected|readonly|static|override)\s+", "", cleaned,
            )
            if new_cleaned == cleaned:
                break
            cleaned = new_cleaned
        match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_]*)", cleaned)
        if match:
            yield match.group(1), match.group(2)


def _extract_class_body(source: str, start: int) -> str | None:
    """Return the substring containing the class body, scanning for the
    matching closing brace from the first `{` after `start`."""
    brace_pos = source.find("{", start)
    if brace_pos < 0:
        return None
    depth = 0
    for i in range(brace_pos, len(source)):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[brace_pos + 1 : i]
    return source[brace_pos + 1 :]


def _slice_call_args(source: str, open_paren_pos: int) -> str | None:
    """Given a position at the opening `(` of a call, return the arg-list
    substring up to the matching `)`."""
    if open_paren_pos < 0 or open_paren_pos >= len(source):
        return None
    if source[open_paren_pos] != "(":
        return None
    depth = 0
    for i in range(open_paren_pos, len(source)):
        ch = source[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return source[open_paren_pos + 1 : i]
    return None


def _extract_handler_name(call_args: str) -> str | None:
    """Return the trailing-callback handler identifier from `(path, handler)`
    or just `(handler)`; None if the handler is an inline arrow/function or
    cannot be identified."""
    if not call_args:
        return None
    parts: list[str] = []
    depth = 0
    current = []
    for ch in call_args:
        if ch in "<({[":
            depth += 1
        elif ch in ">)}]":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    if current:
        parts.append("".join(current).strip())
    if not parts:
        return None
    last = parts[-1]
    if not last:
        return None
    if "=>" in last or last.startswith("function") or last.startswith("async"):
        return None
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)", last)
    if match:
        return match.group(1)
    return None


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
        if label == Path(rel).name or any(
            label.lower().endswith(ext) for ext in _TS_EXTS
        ):
            by_file.setdefault(rel, nid)
            continue
        symbol = label
        if symbol.endswith("()"):
            symbol = symbol[:-2]
        symbol = symbol.lstrip(".").lower()
        if symbol:
            by_symbol.setdefault((rel, symbol), nid)
    return {"by_file": by_file, "by_symbol": by_symbol}
