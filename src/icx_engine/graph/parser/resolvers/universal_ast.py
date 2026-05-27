"""Universal structural AST resolver for languages outside the deep resolver set.

Handles: Go, C#, Ruby, Rust, Swift, PHP, C, C++, Scala, Dart, Elixir.
Skips: .py, .js, .jsx, .mjs, .ts, .tsx, .kt, .kts, .java, .groovy, .vue, .svelte
       (those have dedicated deep resolvers).

Extracts definition nodes + import/inherits edges. Does NOT attempt call-target
resolution (too noisy without framework knowledge or LSP).

Confidence: 0.55 (UNIVERSAL_AST). All edges tagged resolver_tag="universal_ast".

Each language grammar is imported lazily and silently skipped if not installed.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import UNIVERSAL_AST, annotate_edge

_log = logging.getLogger(__name__)

_DEEP_RESOLVER_EXTS: frozenset[str] = frozenset({
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
    ".java", ".groovy", ".gradle", ".kt", ".kts",
    ".vue", ".svelte",
})

_EXT_TO_LANG: dict[str, tuple[str, str]] = {
    ".go":    ("tree_sitter_go",     "language"),
    ".cs":    ("tree_sitter_c_sharp","language"),
    ".rb":    ("tree_sitter_ruby",   "language"),
    ".rs":    ("tree_sitter_rust",   "language"),
    ".swift": ("tree_sitter_swift",  "language"),
    ".php":   ("tree_sitter_php",    "language_php"),
    ".c":     ("tree_sitter_c",      "language"),
    ".h":     ("tree_sitter_c",      "language"),
    ".cpp":   ("tree_sitter_cpp",    "language"),
    ".cc":    ("tree_sitter_cpp",    "language"),
    ".cxx":   ("tree_sitter_cpp",    "language"),
    ".hpp":   ("tree_sitter_cpp",    "language"),
    ".scala": ("tree_sitter_scala",  "language"),
    ".dart":  ("tree_sitter_dart",   "language"),
    ".ex":    ("tree_sitter_elixir", "language"),
    ".exs":   ("tree_sitter_elixir", "language"),
}

_LANG_CACHE: dict[str, object] = {}


def _get_language(ext: str) -> "object | None":
    if ext in _LANG_CACHE:
        return _LANG_CACHE[ext]
    spec = _EXT_TO_LANG.get(ext)
    if spec is None:
        _LANG_CACHE[ext] = None
        return None
    mod_name, attr = spec
    try:
        import importlib
        from tree_sitter import Language
        mod = importlib.import_module(mod_name)
        lang_fn = getattr(mod, attr)
        lang = Language(lang_fn())
        _LANG_CACHE[ext] = lang
        return lang
    except Exception as exc:
        _log.debug("universal_ast: %s not available (%s)", mod_name, exc)
        _LANG_CACHE[ext] = None
        return None


_IMPORT_TYPES: dict[str, frozenset[str]] = {
    ".go":    frozenset({"import_declaration", "import_spec"}),
    ".cs":    frozenset({"using_directive"}),
    ".rb":    frozenset({"call"}),
    ".rs":    frozenset({"use_declaration"}),
    ".swift": frozenset({"import_declaration"}),
    ".php":   frozenset({"namespace_use_declaration", "require_expression", "include_expression"}),
    ".c":     frozenset({"preproc_include"}),
    ".h":     frozenset({"preproc_include"}),
    ".cpp":   frozenset({"preproc_include"}),
    ".cc":    frozenset({"preproc_include"}),
    ".cxx":   frozenset({"preproc_include"}),
    ".hpp":   frozenset({"preproc_include"}),
    ".scala": frozenset({"import_declaration"}),
    ".dart":  frozenset({"import_or_export"}),
    ".ex":    frozenset({"call"}),
    ".exs":   frozenset({"call"}),
}


def _node_text(node, source: bytes) -> str:
    try:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _walk(node, visitor, depth: int = 0) -> None:
    visitor(node, depth)
    for child in node.children:
        _walk(child, visitor, depth + 1)


def _extract_import_text(node, source: bytes, ext: str) -> str | None:
    raw = _node_text(node, source).strip()
    for prefix in ("import ", "using ", "#include ", "use ", "require ", "include "):
        if raw.lower().startswith(prefix):
            raw = raw[len(prefix):]
    raw = raw.strip("'\"<>;{}")
    raw = raw.strip('"')
    if not raw or raw.startswith("//"):
        return None
    if ext in (".rb", ".ex", ".exs"):
        full = _node_text(node, source)
        if ext == ".rb" and not full.strip().startswith("require"):
            return None
        if ext in (".ex", ".exs"):
            if not any(full.strip().startswith(k) for k in ("use ", "import ", "alias ", "require ")):
                return None
    return raw.split("\n")[0].strip() or None


def _is_import_node(node, ext: str) -> bool:
    return node.type in _IMPORT_TYPES.get(ext, frozenset())


def _make_node_id(*parts: str) -> str:
    import re
    import unicodedata
    combined = "_".join(p.strip("_.") for p in parts if p)
    combined = unicodedata.normalize("NFKC", combined)
    cleaned = re.sub(r"[^\w]+", "_", combined, flags=re.UNICODE)
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned.strip("_").casefold()


def extract_universal_ast_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    """Extract structural edges for non-deep-resolver languages.

    Returns edges list. Each edge has:
      relation, source, target, source_file, source_location,
      confidence_score=0.55, confidence_source="universal_ast", resolver_tag="universal_ast"
    """
    project_root = project_root.resolve()
    edges: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    try:
        from tree_sitter import Parser
    except ImportError:
        _log.debug("universal_ast: tree-sitter not installed, skipping")
        return []

    for fpath in files:
        fpath = Path(fpath).resolve()
        ext = fpath.suffix.lower()

        if ext in _DEEP_RESOLVER_EXTS:
            continue

        lang = _get_language(ext)
        if lang is None:
            continue

        try:
            rel = fpath.relative_to(project_root).as_posix()
        except ValueError:
            continue

        try:
            source = fpath.read_bytes()
        except OSError:
            continue

        try:
            parser = Parser(lang)
            tree = parser.parse(source)
        except Exception as exc:
            _log.debug("universal_ast: parse failed %s (%s)", fpath.name, exc)
            continue

        file_stem = fpath.parent.name + "." + fpath.stem if fpath.parent.name else fpath.stem
        file_node_id = _make_node_id(rel, file_stem)

        def _visit(node, depth: int) -> None:
            if _is_import_node(node, ext):
                import_text = _extract_import_text(node, source, ext)
                if import_text:
                    target_id = _make_node_id("import", import_text)
                    key = (file_node_id, target_id, "imports")
                    if key not in seen:
                        seen.add(key)
                        edge = {
                            "relation": "imports",
                            "source": file_node_id,
                            "target": target_id,
                            "source_file": rel,
                            "source_location": f"L{node.start_point[0] + 1}",
                            "weight": 1.0,
                            "resolver_tag": "universal_ast",
                        }
                        annotate_edge(edge, UNIVERSAL_AST, "universal_ast")
                        edges.append(edge)

        _walk(tree.root_node, _visit)

    return edges
