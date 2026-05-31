"""Lombok synthetic-method resolver.

Lombok generates methods at compile time. The tree-sitter AST cannot see
them, so cross-file calls like `userDto.getEmail()` would never resolve.
This module scans `.java` files for Lombok annotations and synthesizes
virtual method nodes into the extraction so downstream resolvers can
bind references to them.

Annotations handled:
  Class-level:
    @Data           -> getter + setter for every non-final field,
                       plus toString, equals, hashCode, canEqual
    @Value          -> getter for every field (immutable; no setters),
                       plus toString, equals, hashCode
    @Getter         -> getter for every field
    @Setter         -> setter for every non-final field
    @ToString       -> toString
    @EqualsAndHashCode -> equals, hashCode, canEqual
    @NoArgsConstructor -> default constructor
    @AllArgsConstructor / @RequiredArgsConstructor -> constructor (only
                       a virtual marker; tree-sitter already records
                       declared constructors)
    @Builder        -> builder() static + Builder inner type

  Field-level:
    @Getter / @Setter on a single field -> just that field's accessor

Synthetic nodes use the parser's standard label form (`.foo()`) so the
existing symbol-lookup tables pick them up without changes.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

_log = logging.getLogger(__name__)

_CLASS_GETTERS_SETTERS = {
    "Data": ("getter", "setter", "tostring", "equals", "hashcode"),
    "Value": ("getter", "tostring", "equals", "hashcode"),
    "Getter": ("getter",),
    "Setter": ("setter",),
}

_CLASS_OBJECT_METHODS = {
    "ToString": ("tostring",),
    "EqualsAndHashCode": ("equals", "hashcode"),
}

_CTOR_ANNS = frozenset({
    "NoArgsConstructor", "AllArgsConstructor", "RequiredArgsConstructor",
})


def apply_lombok_synth(
    files: Iterable[Path],
    project_root: Path,
    ast_extraction: dict,
) -> int:
    """Mutate `ast_extraction['nodes']` in place: append synthesized method
    and accessor nodes for each Lombok-annotated class field. Returns the
    number of nodes added.
    """
    try:
        import javalang
    except ImportError:
        return 0

    project_root = project_root.resolve()
    java_files = [Path(f).resolve() for f in files if str(f).endswith(".java")]
    if not java_files:
        return 0

    nodes = ast_extraction.setdefault("nodes", [])
    seen_ids: set[str] = {n.get("id") for n in nodes if n.get("id")}
    file_id_by_rel: dict[str, str] = {}
    project_str = str(project_root).replace("\\", "/")
    for n in nodes:
        label = (n.get("label") or "").strip()
        src = (n.get("source_file") or "").replace("\\", "/").strip()
        if not src:
            continue
        if src.startswith(project_str + "/"):
            rel = src[len(project_str) + 1 :]
        elif src.startswith(project_str):
            rel = src[len(project_str) :].lstrip("/")
        else:
            continue
        if label == Path(rel).name or label.endswith(".java"):
            file_id_by_rel[rel] = n.get("id", "")

    class_node_id_by: dict[tuple[str, str], str] = {}
    for n in nodes:
        nid = n.get("id")
        label = (n.get("label") or "").strip()
        src = (n.get("source_file") or "").replace("\\", "/").strip()
        if not nid or not label or label.endswith(")") or label.endswith(".java"):
            continue
        if not src:
            continue
        if src.startswith(project_str + "/"):
            rel = src[len(project_str) + 1 :]
        elif src.startswith(project_str):
            rel = src[len(project_str) :].lstrip("/")
        else:
            continue
        class_node_id_by.setdefault((rel, label.lower()), nid)

    added = 0
    for jf in java_files:
        try:
            rel = jf.relative_to(project_root).as_posix()
        except ValueError:
            continue
        from . import _java_parse_cache as _jpc
        tree = _jpc.get_tree(jf)
        if tree is None:
            continue

        for type_decl in (tree.types or []):
            type_name = getattr(type_decl, "name", "") or ""
            if not type_name:
                continue
            class_node_id = class_node_id_by.get((rel, type_name.lower()))
            file_node_id = file_id_by_rel.get(rel)
            if not class_node_id and not file_node_id:
                continue

            class_anns = _annotation_names(type_decl)
            wanted: set[str] = set()
            for ann in class_anns:
                if ann in _CLASS_GETTERS_SETTERS:
                    wanted.update(_CLASS_GETTERS_SETTERS[ann])
                if ann in _CLASS_OBJECT_METHODS:
                    wanted.update(_CLASS_OBJECT_METHODS[ann])

            fields = list(getattr(type_decl, "fields", None) or [])

            def_class_owner = class_node_id or file_node_id
            class_pos_line = getattr(getattr(type_decl, "position", None), "line", None)

            if "tostring" in wanted:
                added += _add_method(
                    nodes, seen_ids, def_class_owner, "toString",
                    str(jf), class_pos_line, rel, type_name,
                )
            if "equals" in wanted:
                added += _add_method(
                    nodes, seen_ids, def_class_owner, "equals",
                    str(jf), class_pos_line, rel, type_name,
                )
                added += _add_method(
                    nodes, seen_ids, def_class_owner, "canEqual",
                    str(jf), class_pos_line, rel, type_name,
                )
            if "hashcode" in wanted:
                added += _add_method(
                    nodes, seen_ids, def_class_owner, "hashCode",
                    str(jf), class_pos_line, rel, type_name,
                )

            if _CTOR_ANNS & class_anns:
                added += _add_method(
                    nodes, seen_ids, def_class_owner, type_name,
                    str(jf), class_pos_line, rel, type_name,
                )

            for field in fields:
                if _is_field_static(field):
                    continue
                final = _is_field_final(field)
                field_anns = _annotation_names(field)
                emit_get = "getter" in wanted or "Getter" in field_anns
                emit_set = ("setter" in wanted or "Setter" in field_anns) and not final
                for decl in (getattr(field, "declarators", None) or []):
                    fname = getattr(decl, "name", None)
                    if not fname:
                        continue
                    field_pos = getattr(field, "position", None)
                    field_line = field_pos.line if field_pos else class_pos_line
                    is_bool = _is_boolean_field(field)
                    if emit_get:
                        getter_name = _getter_name(fname, is_bool)
                        added += _add_method(
                            nodes, seen_ids, def_class_owner, getter_name,
                            str(jf), field_line, rel, type_name,
                        )
                    if emit_set:
                        setter_name = _setter_name(fname)
                        added += _add_method(
                            nodes, seen_ids, def_class_owner, setter_name,
                            str(jf), field_line, rel, type_name,
                        )

            if "Builder" in class_anns:
                added += _add_method(
                    nodes, seen_ids, def_class_owner, "builder",
                    str(jf), class_pos_line, rel, type_name,
                )

    return added


def _add_method(
    nodes: list[dict], seen_ids: set[str], parent_id: str,
    method_name: str, source_file: str, line_no: int | None,
    rel: str, type_name: str,
) -> int:
    nid = f"{_normalize_class_prefix(type_name, rel)}_{method_name.lower()}"
    if nid in seen_ids:
        return 0
    label = f".{method_name}()"
    seen_ids.add(nid)
    nodes.append({
        "id": nid,
        "label": label,
        "file_type": "code",
        "source_file": source_file,
        "source_location": f"L{line_no}" if line_no else "",
        "synthetic": "lombok",
    })
    return 1


def _normalize_class_prefix(class_name: str, rel: str) -> str:
    stem = Path(rel).stem
    return f"{stem.lower()}_{class_name.lower()}"


def _annotation_names(node) -> set[str]:
    out: set[str] = set()
    for ann in (getattr(node, "annotations", None) or []):
        name = getattr(ann, "name", None)
        if name:
            out.add(name)
    return out


def _is_field_final(field) -> bool:
    modifiers = getattr(field, "modifiers", None) or set()
    if isinstance(modifiers, (list, set, tuple)):
        return "final" in modifiers
    return False


def _is_field_static(field) -> bool:
    modifiers = getattr(field, "modifiers", None) or set()
    if isinstance(modifiers, (list, set, tuple)):
        return "static" in modifiers
    return False


def _is_boolean_field(field) -> bool:
    type_node = getattr(field, "type", None)
    if type_node is None:
        return False
    name = getattr(type_node, "name", None)
    if name in ("boolean", "Boolean"):
        return True
    return False


def _getter_name(field_name: str, is_boolean: bool) -> str:
    if is_boolean and field_name.startswith("is") and len(field_name) > 2 and field_name[2].isupper():
        return field_name
    prefix = "is" if is_boolean else "get"
    return f"{prefix}{field_name[0].upper()}{field_name[1:]}"


def _setter_name(field_name: str) -> str:
    return f"set{field_name[0].upper()}{field_name[1:]}"
