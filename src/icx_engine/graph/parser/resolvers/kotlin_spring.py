"""Kotlin Spring annotation resolver.

Mirrors the Java spring.py and jpa.py logic for .kt files. Detects:
  * @RestController / @Service / @Repository / @Component / @Configuration /
    @ConfigurationProperties providers + primary-constructor injection
  * @RequestMapping / @GetMapping / @PostMapping / @PutMapping /
    @PatchMapping / @DeleteMapping on fun members -> routes edges
  * @EventListener methods -> listens edges to event type
  * @Scheduled methods -> scheduled edges (entry points)
  * @Entity classes + @OneToMany / @ManyToOne fields
  * interface ExampleRepo : JpaRepository<Entity, ID> -> dao edges
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

from icx_engine.graph.parser.confidence import (
    FRAMEWORK_RESOLVED, annotate_edge,
)
from icx_engine.graph.parser.resolvers.kotlin_symbols import (
    _build_node_index, _build_import_map, _KT_CLASS, _KT_PACKAGE,
    _KT_PRIMARY_CTOR_PARAM,
)

_log = logging.getLogger(__name__)

_PROVIDERS = frozenset({
    "Controller", "RestController", "Service", "Component",
    "Repository", "Configuration", "ConfigurationProperties",
})
_ROUTE_ANNS = frozenset({
    "RequestMapping", "GetMapping", "PostMapping",
    "PutMapping", "PatchMapping", "DeleteMapping",
})
_REL_ANNS = frozenset({"OneToOne", "OneToMany", "ManyToOne", "ManyToMany"})
_REPO_PARENTS = frozenset({
    "JpaRepository", "CrudRepository", "PagingAndSortingRepository",
    "ReactiveCrudRepository", "CoroutineCrudRepository",
    "MongoRepository", "ReactiveMongoRepository",
})

_KT_FUN = re.compile(
    r"""(?:@[A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?\s+)*
        fun\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(""",
    re.VERBOSE,
)
_KT_PROPERTY = re.compile(
    r"""(?:@[A-Za-z_][A-Za-z0-9_]*(?:\([^)]*\))?\s+)*
        (?:val|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_]*)""",
    re.VERBOSE,
)


def extract_kotlin_spring_edges(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> list[dict]:
    project_root = project_root.resolve()
    kt_files = [Path(f).resolve() for f in files if str(f).endswith(".kt")]
    if not kt_files:
        return []

    node_index = _build_node_index(ast_extraction.get("nodes", []), project_root)
    if not node_index["by_symbol"] and not node_index["by_file"]:
        return []

    fqn_to_file: dict[str, str] = {}
    parsed: list[tuple[Path, str, str, str]] = []

    for kf in kt_files:
        try:
            rel = kf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        try:
            source = kf.read_text(encoding="utf-8")
        except OSError:
            continue
        pkg_match = _KT_PACKAGE.search(source)
        package = pkg_match.group(1) if pkg_match else ""
        for cls_match in _KT_CLASS.finditer(source):
            cls_name = cls_match.group(1)
            fqn = f"{package}.{cls_name}" if package else cls_name
            fqn_to_file.setdefault(fqn, rel)
        parsed.append((kf, rel, source, package))

    edges: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for kf, rel, source, package in parsed:
        file_node_id = node_index["by_file"].get(rel)
        local_symbols = {
            sym: nid for (path, sym), nid in node_index["by_symbol"].items()
            if path == rel
        }
        import_map = _build_import_map(source, package, fqn_to_file)

        for cls_match in _KT_CLASS.finditer(source):
            cls_name = cls_match.group(1)
            class_node_id = local_symbols.get(cls_name.lower()) or file_node_id
            if not class_node_id:
                continue
            cls_block_start = cls_match.start()
            cls_line = source.count("\n", 0, cls_block_start) + 1

            class_anns = _extract_annotations_from_match(source[cls_match.start():cls_match.end()])
            is_provider = bool(class_anns & _PROVIDERS)
            is_entity = "Entity" in class_anns

            class_body = _extract_class_body(source, cls_block_start)

            # Routes: any fun with an HTTP-mapping annotation.
            if class_body:
                for fun_match in _KT_FUN.finditer(class_body):
                    fun_anns = _extract_annotations_from_match(
                        class_body[fun_match.start():fun_match.end()],
                    )
                    if not (fun_anns & _ROUTE_ANNS):
                        continue
                    fun_name = fun_match.group(1)
                    fun_node_id = local_symbols.get(fun_name.lower())
                    if not fun_node_id or not file_node_id:
                        continue
                    key = (file_node_id, fun_node_id, "routes")
                    if key in seen:
                        continue
                    seen.add(key)
                    edge = {
                        "relation": "routes",
                        "source": file_node_id,
                        "target": fun_node_id,
                        "source_file": rel,
                        "source_location": "",
                        "weight": 1.0,
                    }
                    annotate_edge(edge, FRAMEWORK_RESOLVED, "kotlin_spring")
                    edges.append(edge)

            # @EventListener -> listens edge to event parameter type.
            if class_body:
                for fun_match in _KT_FUN.finditer(class_body):
                    fun_anns = _extract_annotations_from_match(
                        class_body[fun_match.start():fun_match.end()],
                    )
                    if "EventListener" not in fun_anns:
                        continue
                    fun_body_start = class_body.find("(", fun_match.end() - 1)
                    if fun_body_start < 0:
                        continue
                    paren_end = class_body.find(")", fun_body_start)
                    if paren_end < 0:
                        continue
                    params_blob = class_body[fun_body_start + 1 : paren_end]
                    first_param = params_blob.split(",", 1)[0].strip()
                    type_match = re.search(r":\s*([A-Za-z_][A-Za-z0-9_]*)", first_param)
                    if type_match:
                        event_type = type_match.group(1)
                        _emit_event(
                            event_type, import_map, fqn_to_file, node_index,
                            edges, seen, src_id=class_node_id, rel=rel,
                            line=cls_line,
                        )

            # @Scheduled -> scheduled edge (class -> method entry point).
            if class_body:
                for fun_match in _KT_FUN.finditer(class_body):
                    fun_anns = _extract_annotations_from_match(
                        class_body[fun_match.start():fun_match.end()],
                    )
                    if "Scheduled" not in fun_anns:
                        continue
                    fun_name = fun_match.group(1)
                    fun_node_id = local_symbols.get(fun_name.lower())
                    if not fun_node_id or not class_node_id:
                        continue
                    key = (class_node_id, fun_node_id, "scheduled")
                    if key in seen:
                        continue
                    seen.add(key)
                    edge = {
                        "relation": "scheduled",
                        "source": class_node_id,
                        "target": fun_node_id,
                        "source_file": rel,
                        "source_location": "",
                        "weight": 1.0,
                    }
                    annotate_edge(edge, FRAMEWORK_RESOLVED, "kotlin_spring")
                    edges.append(edge)

            if is_provider:
                ctor_blob = _extract_ctor_from_class_match(source, cls_match)
                if ctor_blob:
                    for m in _KT_PRIMARY_CTOR_PARAM.finditer(ctor_blob):
                        type_name = m.group(2)
                        _emit_di(
                            type_name, import_map, fqn_to_file, node_index,
                            edges, seen, src_id=class_node_id, rel=rel,
                            line=cls_line,
                        )

            if is_entity and class_body:
                for prop in _KT_PROPERTY.finditer(class_body):
                    prop_anns = _extract_annotations_from_match(
                        class_body[prop.start():prop.end()],
                    )
                    if not (prop_anns & _REL_ANNS):
                        continue
                    type_name = prop.group(2)
                    # For collection types (List<Post>, MutableList<Post>),
                    # extract the inner generic type argument.
                    after_match = class_body[prop.end():]
                    inner = re.match(r"\s*<\s*([A-Za-z_][A-Za-z0-9_]*)", after_match)
                    if inner:
                        type_name = inner.group(1)
                    _emit_jpa(
                        type_name, import_map, fqn_to_file, node_index,
                        edges, seen, src_id=class_node_id, rel=rel,
                        line=cls_line,
                    )

            # interface FooRepo : JpaRepository<Foo, Long>
            parents_blob = cls_match.group(2) or ""
            for parent_name, generic_arg in _parse_repo_parents(parents_blob):
                if parent_name not in _REPO_PARENTS or not generic_arg:
                    continue
                _emit_dao(
                    generic_arg, import_map, fqn_to_file, node_index,
                    edges, seen, src_id=class_node_id, rel=rel,
                    line=cls_line,
                )

    return edges


def _emit_di(type_name, import_map, fqn_to_file, node_index, edges, seen,
             *, src_id, rel, line) -> None:
    fqn = import_map.get(type_name)
    if not fqn:
        return
    target_file = fqn_to_file.get(fqn)
    if not target_file:
        return
    tgt = (
        node_index["by_symbol"].get((target_file, type_name.lower()))
        or node_index["by_file"].get(target_file)
    )
    if not tgt or tgt == src_id:
        return
    key = (src_id, tgt, "depends_on")
    if key in seen:
        return
    seen.add(key)
    edge = {
        "relation": "depends_on",
        "source": src_id, "target": tgt,
        "source_file": rel, "source_location": f"L{line}" if line else "",
        "weight": 1.0,
    }
    annotate_edge(edge, FRAMEWORK_RESOLVED, "kotlin_spring")
    edges.append(edge)


def _emit_jpa(type_name, import_map, fqn_to_file, node_index, edges, seen,
              *, src_id, rel, line) -> None:
    fqn = import_map.get(type_name)
    if not fqn:
        return
    target_file = fqn_to_file.get(fqn)
    if not target_file:
        return
    tgt = (
        node_index["by_symbol"].get((target_file, type_name.lower()))
        or node_index["by_file"].get(target_file)
    )
    if not tgt or tgt == src_id:
        return
    key = (src_id, tgt, "has_relation")
    if key in seen:
        return
    seen.add(key)
    edge = {
        "relation": "has_relation",
        "source": src_id, "target": tgt,
        "source_file": rel, "source_location": f"L{line}" if line else "",
        "weight": 1.0,
    }
    annotate_edge(edge, FRAMEWORK_RESOLVED, "kotlin_jpa")
    edges.append(edge)


def _emit_dao(type_name, import_map, fqn_to_file, node_index, edges, seen,
              *, src_id, rel, line) -> None:
    fqn = import_map.get(type_name)
    if not fqn:
        return
    target_file = fqn_to_file.get(fqn)
    if not target_file:
        return
    tgt = (
        node_index["by_symbol"].get((target_file, type_name.lower()))
        or node_index["by_file"].get(target_file)
    )
    if not tgt or tgt == src_id:
        return
    key = (src_id, tgt, "dao")
    if key in seen:
        return
    seen.add(key)
    edge = {
        "relation": "dao",
        "source": src_id, "target": tgt,
        "source_file": rel, "source_location": f"L{line}" if line else "",
        "weight": 1.0,
    }
    annotate_edge(edge, FRAMEWORK_RESOLVED, "kotlin_jpa")
    edges.append(edge)


def _emit_event(type_name, import_map, fqn_to_file, node_index, edges, seen,
                *, src_id, rel, line) -> None:
    fqn = import_map.get(type_name)
    if not fqn:
        return
    target_file = fqn_to_file.get(fqn)
    if not target_file:
        return
    tgt = (
        node_index["by_symbol"].get((target_file, type_name.lower()))
        or node_index["by_file"].get(target_file)
    )
    if not tgt or tgt == src_id:
        return
    key = (src_id, tgt, "listens")
    if key in seen:
        return
    seen.add(key)
    edge = {
        "relation": "listens",
        "source": src_id, "target": tgt,
        "source_file": rel, "source_location": f"L{line}" if line else "",
        "weight": 1.0,
    }
    annotate_edge(edge, FRAMEWORK_RESOLVED, "kotlin_spring")
    edges.append(edge)


def _extract_class_body(source: str, class_start: int) -> str | None:
    open_brace = source.find("{", class_start)
    if open_brace < 0:
        return None
    depth = 0
    for i in range(open_brace, len(source)):
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return source[open_brace + 1 : i]
    return source[open_brace + 1 :]


def _extract_ctor_from_class_match(source: str, cls_match) -> str | None:
    """Extract primary constructor params from the class declaration,
    searching for '(' only after the class name to avoid matching
    annotation parentheses like @RequestMapping("/path")."""
    match_text = source[cls_match.start():cls_match.end()]
    cls_name = cls_match.group(1)
    name_pos = match_text.find(cls_name)
    if name_pos < 0:
        return None
    after_name = match_text[name_pos + len(cls_name):]
    open_paren = after_name.find("(")
    if open_paren < 0:
        return None
    abs_start = cls_match.start() + name_pos + len(cls_name) + open_paren
    depth = 0
    for i in range(abs_start, len(source)):
        ch = source[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return source[abs_start + 1:i]
    return None


def _extract_annotations_from_match(match_text: str) -> set[str]:
    """Extract all @Annotation names from the regex match text (which includes
    leading annotations consumed by the _KT_CLASS pattern)."""
    out: set[str] = set()
    for m in re.finditer(r"@([A-Za-z_][A-Za-z0-9_]*)", match_text):
        out.add(m.group(1))
    return out


def _extract_annotations_before(source: str, position: int) -> set[str]:
    """Walk backwards from `position` collecting @Annotation names until
    the previous non-blank, non-annotation token."""
    out: set[str] = set()
    chunk = source[:position]
    lines = chunk.splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        if stripped.startswith("@"):
            for m in re.finditer(r"@([A-Za-z_][A-Za-z0-9_]*)", stripped):
                out.add(m.group(1))
            continue
        break
    return out


def _parse_repo_parents(blob: str):
    """Yield (parent_name, first_generic_arg) for Kotlin parent list
    `Parent<Foo, Bar>(...), Iface`."""
    if not blob:
        return
    depth = 0
    parts: list[str] = []
    buf: list[str] = []
    for ch in blob:
        if ch == "<" or ch == "(":
            depth += 1
        elif ch == ">" or ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
            continue
        buf.append(ch)
    if buf:
        piece = "".join(buf).strip()
        if piece:
            parts.append(piece)

    for part in parts:
        head = part.strip()
        name_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", head)
        if not name_match:
            continue
        name = name_match.group(1)
        rest = head[len(name) :].strip()
        first_arg: str | None = None
        if rest.startswith("<"):
            depth = 0
            for i, ch in enumerate(rest):
                if ch == "<":
                    depth += 1
                elif ch == ">":
                    depth -= 1
                    if depth == 0:
                        args_blob = rest[1:i]
                        first = args_blob.split(",", 1)[0].strip()
                        arg_match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)", first)
                        if arg_match:
                            first_arg = arg_match.group(1)
                        break
        yield name, first_arg
