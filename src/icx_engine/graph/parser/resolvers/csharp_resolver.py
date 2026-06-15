"""
C# language semantic edge resolver for ICX graph.

Edge types:
  csharp_using    (0.90): using directive -> file declaring the matching namespace
  csharp_extends  (0.80): class/interface/struct inherits/implements a type declared in the project
  csharp_calls    (0.75): intra-namespace method call

Activation: any .cs file in the project.
"""
import re
from pathlib import Path
from collections import defaultdict

from icx_engine.graph.parser.resolvers._common import make_edge

_CS_NAMESPACE = re.compile(r'^\s*namespace\s+([\w.]+)', re.MULTILINE)
_CS_USING = re.compile(r'^\s*using\s+(?!static\b)([\w.]+)\s*;', re.MULTILINE)
_CS_TYPE_DECL = re.compile(r'\b(?:class|interface|struct)\s+(\w+)(?:<[^>]*>)?\s*(?::\s*([^{]+))?\{', re.MULTILINE)
_CS_METHOD_DECL = re.compile(
    r'^\s*(?:public|private|protected|internal|static|virtual|override|async|sealed|partial|new|\s)+'
    r'[\w<>\[\],?]+\s+(\w+)\s*\([^)]*\)',
    re.MULTILINE,
)


def resolve_csharp(files: list, project_path, extraction: dict) -> list:
    root = Path(str(project_path))
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    cs_files = [f for f in files if str(f).endswith(".cs")]
    if not cs_files:
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
    for f in cs_files:
        key = str(f).replace("\\", "/")
        try:
            contents[key] = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    namespace_files: dict[str, list[str]] = defaultdict(list)
    for cs_file, content in contents.items():
        for m in _CS_NAMESPACE.finditer(content):
            namespace_files[m.group(1)].append(cs_file)

    # type name -> declaring file (for inheritance resolution)
    type_files: dict[str, str] = {}
    for cs_file, content in contents.items():
        for m in _CS_TYPE_DECL.finditer(content):
            type_files.setdefault(m.group(1), cs_file)

    # 1. using directive edges
    for cs_file, content in contents.items():
        for m in _CS_USING.finditer(content):
            for target in namespace_files.get(m.group(1), []):
                if target == cs_file:
                    continue
                for sn in node_by_file.get(cs_file, []):
                    for tn in node_by_file.get(target, []):
                        edges.append(_edge(sn["id"], tn["id"], cs_file, target, "csharp_using", 0.90))
                        break
                    break

    # 2. inheritance / interface implementation edges
    for cs_file, content in contents.items():
        for m in _CS_TYPE_DECL.finditer(content):
            bases = m.group(2)
            if not bases:
                continue
            type_name = m.group(1)
            for base in (b.strip() for b in bases.split(",")):
                base = base.split("<")[0].strip()
                target = type_files.get(base)
                if not target or target == cs_file:
                    continue
                for sn in node_by_file.get(cs_file, []):
                    if sn.get("name") != type_name:
                        continue
                    for tn in node_by_file.get(target, []):
                        if tn.get("name") != base:
                            continue
                        edges.append(_edge(sn["id"], tn["id"], cs_file, target, "csharp_extends", 0.80))
                        break

    # 3. intra-namespace method calls
    file_namespace: dict[str, str] = {}
    for cs_file, content in contents.items():
        m = _CS_NAMESPACE.search(content)
        if m:
            file_namespace[cs_file] = m.group(1)

    for cs_file, content in contents.items():
        ns = file_namespace.get(cs_file)
        if ns is None:
            continue
        ns_methods: dict[str, str] = {}
        for other in contents:
            if other == cs_file or file_namespace.get(other) != ns:
                continue
            for m in _CS_METHOD_DECL.finditer(contents.get(other, "")):
                ns_methods[m.group(1)] = other
        for mname, tgt_file in ns_methods.items():
            if re.search(rf'\b{re.escape(mname)}\s*\(', content):
                for sn in node_by_file.get(cs_file, []):
                    for tn in node_by_file.get(tgt_file, []):
                        edges.append(_edge(sn["id"], tn["id"], cs_file, tgt_file, "csharp_calls", 0.75))
                        break
                    break

    return edges


def _edge(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return make_edge(src_id, tgt_id, src_file, tgt_file, etype, confidence, resolver="csharp")
