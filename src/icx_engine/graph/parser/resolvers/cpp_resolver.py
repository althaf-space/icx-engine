"""
C/C++ language semantic edge resolver for ICX graph.

Edge types:
  cpp_include  (0.85): `#include "foo.h"` -> file matching the included basename
  cpp_inherits (0.80): `class X : public Y` -> file declaring `Y` (class/struct)
  cpp_calls    (0.75): intra-directory function call

Activation: any .cpp/.cc/.cxx/.h/.hpp/.hxx file in the project.
"""
import re
from pathlib import Path
from collections import defaultdict

from icx_engine.graph.parser.resolvers._common import make_edge

_CPP_EXTS = (".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx")

_CPP_INCLUDE = re.compile(r'^\s*#include\s*"([^"]+)"', re.MULTILINE)
_CPP_TYPE_DECL = re.compile(r'^\s*(?:class|struct)\s+(\w+)', re.MULTILINE)
_CPP_INHERITS = re.compile(
    r'^\s*class\s+(\w+)\s*:\s*((?:public|private|protected)\s+\w+(?:\s*,\s*(?:public|private|protected)\s+\w+)*)',
    re.MULTILINE,
)
_CPP_BASE_NAME = re.compile(r'(?:public|private|protected)\s+(\w+)')
_CPP_FN_DECL = re.compile(r'^\s*[\w:<>&\*]+\s+(\w+)\s*\([^;{}]*\)\s*\{', re.MULTILINE)


def resolve_cpp(files: list, project_path, extraction: dict) -> list:
    root = Path(str(project_path))
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    cpp_files = [f for f in files if str(f).lower().endswith(_CPP_EXTS)]
    if not cpp_files:
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
    for f in cpp_files:
        key = str(f).replace("\\", "/")
        try:
            contents[key] = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    if not contents:
        return []

    # basename -> declaring file (for #include resolution)
    basename_files: dict[str, str] = {}
    for cpp_file in contents:
        basename_files.setdefault(Path(cpp_file).name, cpp_file)

    # class/struct name -> declaring file
    type_files: dict[str, str] = {}
    for cpp_file, content in contents.items():
        for m in _CPP_TYPE_DECL.finditer(content):
            type_files.setdefault(m.group(1), cpp_file)

    # 1. #include edges
    for cpp_file, content in contents.items():
        for m in _CPP_INCLUDE.finditer(content):
            target = basename_files.get(Path(m.group(1)).name)
            if not target or target == cpp_file:
                continue
            for sn in node_by_file.get(cpp_file, []):
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(sn["id"], tn["id"], cpp_file, target, "cpp_include", 0.85))
                    break
                break

    # 2. base class inheritance edges
    for cpp_file, content in contents.items():
        for m in _CPP_INHERITS.finditer(content):
            derived_name = m.group(1)
            for base_m in _CPP_BASE_NAME.finditer(m.group(2)):
                base_name = base_m.group(1)
                base_target = type_files.get(base_name)
                if not base_target or base_target == cpp_file:
                    continue
                for sn in node_by_file.get(cpp_file, []):
                    if sn.get("name") != derived_name:
                        continue
                    for tn in node_by_file.get(base_target, []):
                        if tn.get("name") != base_name:
                            continue
                        edges.append(_edge(sn["id"], tn["id"], cpp_file, base_target, "cpp_inherits", 0.80))
                        break

    # 3. intra-directory function calls
    # Precompute each directory's (func_name, file) declarations once - in contents
    # iteration + finditer order - instead of re-scanning every sibling file per file.
    # Per-file dir_fns is rebuilt with the same last-write-wins (excluding self) semantics,
    # so the resolved call edges are identical.
    dir_fn_entries: dict[str, list[tuple[str, str]]] = {}
    for _f, _content in contents.items():
        _d = Path(_f).parent.as_posix()
        _lst = dir_fn_entries.setdefault(_d, [])
        for _m in _CPP_FN_DECL.finditer(_content):
            _lst.append((_m.group(1), _f))

    for cpp_file, content in contents.items():
        dir_ = Path(cpp_file).parent.as_posix()
        dir_fns: dict[str, str] = {}
        for fname, other in dir_fn_entries.get(dir_, ()):
            if other != cpp_file:
                dir_fns[fname] = other
        for fname, tgt_file in dir_fns.items():
            if re.search(rf'\b{re.escape(fname)}\s*\(', content):
                for sn in node_by_file.get(cpp_file, []):
                    for tn in node_by_file.get(tgt_file, []):
                        edges.append(_edge(sn["id"], tn["id"], cpp_file, tgt_file, "cpp_calls", 0.75))
                        break
                    break

    return edges


def _edge(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return make_edge(src_id, tgt_id, src_file, tgt_file, etype, confidence, resolver="cpp")
