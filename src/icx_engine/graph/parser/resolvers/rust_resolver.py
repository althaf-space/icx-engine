"""
Rust language semantic edge resolver for ICX graph.

Edge types:
  rust_use   (0.90): use path -> file declaring the matching fn/struct/enum/trait item
  rust_impl  (0.80): `impl Trait for Type` -> trait declared in the project
  rust_calls (0.75): intra-directory ("module") function call

Activation: any .rs file in the project.
Rust modules map to the file/directory tree rather than an in-file namespace
declaration, so intra-module calls are grouped by directory (mirrors
go_resolver.py's intra-package call resolution).
"""
import re
from pathlib import Path
from collections import defaultdict

from icx_engine.graph.parser.resolvers._common import make_edge

_RUST_USE = re.compile(r'^\s*use\s+([\w:]+)\s*;', re.MULTILINE)
_RUST_FN_DECL = re.compile(r'^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)', re.MULTILINE)
_RUST_TRAIT_DECL = re.compile(r'^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+(\w+)', re.MULTILINE)
_RUST_TYPE_DECL = re.compile(r'^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum)\s+(\w+)', re.MULTILINE)
_RUST_IMPL_TRAIT = re.compile(r'^\s*impl(?:<[^>]*>)?\s+(\w+)(?:<[^>]*>)?\s+for\s+(\w+)', re.MULTILINE)


def resolve_rust(files: list, project_path, extraction: dict) -> list:
    root = Path(str(project_path))
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    rs_files = [f for f in files if str(f).endswith(".rs")]
    if not rs_files:
        return []

    edges: list[dict] = []
    node_by_file: dict[str, list] = defaultdict(list)
    _root_posix = root.as_posix()
    for n in nodes:
        sf = (n.get("source_file") or n.get("file") or "").replace("\\", "/")
        if not sf:
            continue
        node_by_file[sf].append(n)
        if not (sf.startswith("/") or (len(sf) > 1 and sf[1] == ":")):
            node_by_file[f"{_root_posix}/{sf}"].append(n)

    contents: dict[str, str] = {}
    for f in rs_files:
        key = str(f).replace("\\", "/")
        try:
            contents[key] = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    # item name (fn/struct/enum/trait) -> declaring file
    item_files: dict[str, str] = {}
    trait_files: dict[str, str] = {}
    type_files: dict[str, str] = {}
    for rs_file, content in contents.items():
        for m in _RUST_FN_DECL.finditer(content):
            item_files.setdefault(m.group(1), rs_file)
        for m in _RUST_TRAIT_DECL.finditer(content):
            item_files.setdefault(m.group(1), rs_file)
            trait_files.setdefault(m.group(1), rs_file)
        for m in _RUST_TYPE_DECL.finditer(content):
            item_files.setdefault(m.group(1), rs_file)
            type_files.setdefault(m.group(1), rs_file)

    # 1. use path edges
    for rs_file, content in contents.items():
        for m in _RUST_USE.finditer(content):
            segment = m.group(1).split("::")[-1]
            target = item_files.get(segment)
            if not target or target == rs_file:
                continue
            for sn in node_by_file.get(rs_file, []):
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(sn["id"], tn["id"], rs_file, target, "rust_use", 0.90))
                    break
                break

    # 2. trait implementation edges
    for rs_file, content in contents.items():
        for m in _RUST_IMPL_TRAIT.finditer(content):
            trait_name, type_name = m.group(1), m.group(2)
            trait_target = trait_files.get(trait_name)
            type_target = type_files.get(type_name)
            if not trait_target or not type_target or trait_target == type_target:
                continue
            for sn in node_by_file.get(type_target, []):
                if sn.get("name") != type_name:
                    continue
                for tn in node_by_file.get(trait_target, []):
                    if tn.get("name") != trait_name:
                        continue
                    edges.append(_edge(sn["id"], tn["id"], type_target, trait_target, "rust_impl", 0.80))
                    break

    # 3. intra-directory ("module") function calls
    for rs_file, content in contents.items():
        dir_ = Path(rs_file).parent.as_posix()
        dir_fns: dict[str, str] = {}
        for other in contents:
            if other == rs_file or Path(other).parent.as_posix() != dir_:
                continue
            for m in _RUST_FN_DECL.finditer(contents.get(other, "")):
                dir_fns[m.group(1)] = other
        for fname, tgt_file in dir_fns.items():
            if re.search(rf'\b{re.escape(fname)}\s*\(', content):
                for sn in node_by_file.get(rs_file, []):
                    for tn in node_by_file.get(tgt_file, []):
                        edges.append(_edge(sn["id"], tn["id"], rs_file, tgt_file, "rust_calls", 0.75))
                        break
                    break

    return edges


def _edge(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return make_edge(src_id, tgt_id, src_file, tgt_file, etype, confidence, resolver="rust")
