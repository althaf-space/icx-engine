"""
Scala language semantic edge resolver for ICX graph.

Edge types:
  scala_import  (0.90): `import com.foo.Bar` (or `import com.foo.{Bar, Baz}`)
                         -> file declaring `class/object/trait Bar`
  scala_extends (0.80): `class X extends Y with Z` -> files declaring `Y`/`Z`
  scala_calls   (0.75): intra-directory function call

Activation: any .scala file in the project.
"""
import re
from pathlib import Path
from collections import defaultdict

from icx_engine.graph.parser.resolvers._common import make_edge

_SCALA_TYPE_DECL = re.compile(
    r'^\s*(?:abstract\s+|final\s+|sealed\s+|case\s+)*(?:class|object|trait)\s+(\w+)', re.MULTILINE,
)
_SCALA_EXTENDS = re.compile(
    r'^\s*(?:abstract\s+|final\s+|sealed\s+|case\s+)*(?:class|object|trait)\s+(\w+)'
    r'(?:\([^)]*\))?\s+extends\s+(\w+)((?:\s+with\s+\w+)*)',
    re.MULTILINE,
)
_SCALA_WITH_NAME = re.compile(r'with\s+(\w+)')
_SCALA_IMPORT_SIMPLE = re.compile(r'^\s*import\s+(?:[\w.]+\.)(\w+)\s*$', re.MULTILINE)
_SCALA_IMPORT_BRACES = re.compile(r'^\s*import\s+[\w.]+\.\{([^}]+)\}', re.MULTILINE)
_SCALA_FUNC_DECL = re.compile(r'^\s*(?:private\s+|protected\s+|final\s+|override\s+)*def\s+(\w+)', re.MULTILINE)


def resolve_scala(files: list, project_path, extraction: dict) -> list:
    root = Path(str(project_path))
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    scala_files = [f for f in files if str(f).lower().endswith(".scala")]
    if not scala_files:
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
    for f in scala_files:
        key = str(f).replace("\\", "/")
        try:
            contents[key] = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    if not contents:
        return []

    # type (class/object/trait) name -> declaring file
    type_files: dict[str, str] = {}
    for scala_file, content in contents.items():
        for m in _SCALA_TYPE_DECL.finditer(content):
            type_files.setdefault(m.group(1), scala_file)

    # 1. import edges
    for scala_file, content in contents.items():
        imported_names: set[str] = set()
        for m in _SCALA_IMPORT_SIMPLE.finditer(content):
            imported_names.add(m.group(1))
        for m in _SCALA_IMPORT_BRACES.finditer(content):
            for item in m.group(1).split(","):
                name = item.strip().split("=>")[0].strip()
                if name and name != "_":
                    imported_names.add(name)
        for name in imported_names:
            target = type_files.get(name)
            if not target or target == scala_file:
                continue
            for sn in node_by_file.get(scala_file, []):
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(sn["id"], tn["id"], scala_file, target, "scala_import", 0.90))
                    break
                break

    # 2. extends/with edges
    for scala_file, content in contents.items():
        for m in _SCALA_EXTENDS.finditer(content):
            derived_name = m.group(1)
            parents = [m.group(2)] + _SCALA_WITH_NAME.findall(m.group(3) or "")
            for parent_name in parents:
                parent_target = type_files.get(parent_name)
                if not parent_target or parent_target == scala_file:
                    continue
                for sn in node_by_file.get(scala_file, []):
                    if sn.get("name") != derived_name:
                        continue
                    for tn in node_by_file.get(parent_target, []):
                        if tn.get("name") != parent_name:
                            continue
                        edges.append(_edge(sn["id"], tn["id"], scala_file, parent_target, "scala_extends", 0.80))
                        break

    # 3. intra-directory function calls
    for scala_file, content in contents.items():
        dir_ = Path(scala_file).parent.as_posix()
        dir_fns: dict[str, str] = {}
        for other in contents:
            if other == scala_file or Path(other).parent.as_posix() != dir_:
                continue
            for m in _SCALA_FUNC_DECL.finditer(contents.get(other, "")):
                dir_fns[m.group(1)] = other
        for fname, tgt_file in dir_fns.items():
            if re.search(rf'\b{re.escape(fname)}\s*\(', content):
                for sn in node_by_file.get(scala_file, []):
                    for tn in node_by_file.get(tgt_file, []):
                        edges.append(_edge(sn["id"], tn["id"], scala_file, tgt_file, "scala_calls", 0.75))
                        break
                    break

    return edges


def _edge(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return make_edge(src_id, tgt_id, src_file, tgt_file, etype, confidence, resolver="scala")
