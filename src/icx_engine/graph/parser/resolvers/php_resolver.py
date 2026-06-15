"""
PHP language semantic edge resolver for ICX graph.

Edge types:
  php_use      (0.90): use import -> file declaring the matching namespace+class (FQCN)
  php_extends  (0.80): class/interface/trait extends/implements a type declared in the project
  php_calls    (0.75): intra-namespace method call

Activation: any .php file in the project.
"""
import re
from pathlib import Path
from collections import defaultdict

from icx_engine.graph.parser.resolvers._common import make_edge

_PHP_NAMESPACE = re.compile(r'^\s*namespace\s+([\w\\]+)\s*;', re.MULTILINE)
_PHP_USE = re.compile(r'^\s*use\s+([\w\\]+)(?:\s+as\s+\w+)?\s*;', re.MULTILINE)
_PHP_TYPE_DECL = re.compile(r'\b(?:class|interface|trait)\s+(\w+)', re.MULTILINE)
_PHP_CLASS_HERITAGE = re.compile(
    r'\b(?:class|interface)\s+(\w+)(?:\s+extends\s+([\w\\,\s]+?))?(?:\s+implements\s+([\w\\,\s]+?))?\s*\{',
    re.MULTILINE,
)
_PHP_METHOD_DECL = re.compile(
    r'^\s*(?:public|private|protected|static|abstract|final|\s)*function\s+(\w+)\s*\(',
    re.MULTILINE,
)


def resolve_php(files: list, project_path, extraction: dict) -> list:
    root = Path(str(project_path))
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    php_files = [f for f in files if str(f).endswith(".php")]
    if not php_files:
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
    for f in php_files:
        key = str(f).replace("\\", "/")
        try:
            contents[key] = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    # file -> namespace
    file_namespace: dict[str, str] = {}
    for php_file, content in contents.items():
        m = _PHP_NAMESPACE.search(content)
        if m:
            file_namespace[php_file] = m.group(1)

    # FQCN ("Namespace\ClassName") -> declaring file
    fqcn_files: dict[str, str] = {}
    # short type name -> declaring file (for inheritance resolution)
    type_files: dict[str, str] = {}
    for php_file, content in contents.items():
        ns = file_namespace.get(php_file, "")
        for m in _PHP_TYPE_DECL.finditer(content):
            fqcn = f"{ns}\\{m.group(1)}" if ns else m.group(1)
            fqcn_files.setdefault(fqcn, php_file)
            type_files.setdefault(m.group(1), php_file)

    # 1. use import edges
    for php_file, content in contents.items():
        for m in _PHP_USE.finditer(content):
            fqcn = m.group(1).lstrip("\\")
            target = fqcn_files.get(fqcn)
            if not target or target == php_file:
                continue
            for sn in node_by_file.get(php_file, []):
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(sn["id"], tn["id"], php_file, target, "php_use", 0.90))
                    break
                break

    # 2. inheritance / interface implementation edges
    for php_file, content in contents.items():
        for m in _PHP_CLASS_HERITAGE.finditer(content):
            type_name = m.group(1)
            bases_raw = " ".join(g for g in (m.group(2), m.group(3)) if g)
            if not bases_raw:
                continue
            for base in bases_raw.split(","):
                base = base.strip().lstrip("\\").split("\\")[-1]
                if not base:
                    continue
                target = type_files.get(base)
                if not target or target == php_file:
                    continue
                for sn in node_by_file.get(php_file, []):
                    if sn.get("name") != type_name:
                        continue
                    for tn in node_by_file.get(target, []):
                        if tn.get("name") != base:
                            continue
                        edges.append(_edge(sn["id"], tn["id"], php_file, target, "php_extends", 0.80))
                        break

    # 3. intra-namespace method calls
    for php_file, content in contents.items():
        ns = file_namespace.get(php_file)
        if ns is None:
            continue
        ns_methods: dict[str, str] = {}
        for other in contents:
            if other == php_file or file_namespace.get(other) != ns:
                continue
            for m in _PHP_METHOD_DECL.finditer(contents.get(other, "")):
                ns_methods[m.group(1)] = other
        for mname, tgt_file in ns_methods.items():
            if re.search(rf'\b{re.escape(mname)}\s*\(', content):
                for sn in node_by_file.get(php_file, []):
                    for tn in node_by_file.get(tgt_file, []):
                        edges.append(_edge(sn["id"], tn["id"], php_file, tgt_file, "php_calls", 0.75))
                        break
                    break

    return edges


def _edge(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return make_edge(src_id, tgt_id, src_file, tgt_file, etype, confidence, resolver="php")
