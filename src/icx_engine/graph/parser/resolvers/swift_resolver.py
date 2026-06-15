"""
Swift language semantic edge resolver for ICX graph.

Edge types:
  swift_import   (0.85): `import Foo` -> nodes in a project directory named `Foo`
                          (heuristic mapping for local Swift Package/Xcode targets)
  swift_conforms (0.80): `class X: ProtocolY` -> file declaring `protocol ProtocolY`
  swift_calls    (0.75): intra-directory function call

Activation: any .swift file in the project.
"""
import re
from pathlib import Path
from collections import defaultdict

from icx_engine.graph.parser.resolvers._common import make_edge

_SWIFT_IMPORT = re.compile(r'^\s*import\s+(\w+)', re.MULTILINE)
_SWIFT_TYPE_DECL = re.compile(
    r'^\s*(?:public\s+|private\s+|internal\s+|fileprivate\s+|open\s+|final\s+)*'
    r'(?:class|struct|enum)\s+(\w+)(?:\s*:\s*([\w,\s]+?))?\s*\{',
    re.MULTILINE,
)
_SWIFT_PROTOCOL_DECL = re.compile(
    r'^\s*(?:public\s+|private\s+|internal\s+|open\s+)*protocol\s+(\w+)', re.MULTILINE,
)
_SWIFT_FUNC_DECL = re.compile(
    r'^\s*(?:public\s+|private\s+|internal\s+|fileprivate\s+|static\s+|override\s+|final\s+|class\s+)*func\s+(\w+)',
    re.MULTILINE,
)


def resolve_swift(files: list, project_path, extraction: dict) -> list:
    root = Path(str(project_path))
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    swift_files = [f for f in files if str(f).lower().endswith(".swift")]
    if not swift_files:
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
    for f in swift_files:
        key = str(f).replace("\\", "/")
        try:
            contents[key] = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    if not contents:
        return []

    # directory name -> files within it (for `import <DirName>` heuristic)
    dir_files: dict[str, list[str]] = defaultdict(list)
    for swift_file in contents:
        dir_files[Path(swift_file).parent.name].append(swift_file)

    # protocol name -> declaring file
    protocol_files: dict[str, str] = {}
    for swift_file, content in contents.items():
        for m in _SWIFT_PROTOCOL_DECL.finditer(content):
            protocol_files.setdefault(m.group(1), swift_file)

    # 1. import edges (module name -> matching project directory)
    for swift_file, content in contents.items():
        own_dir = Path(swift_file).parent.name
        for m in _SWIFT_IMPORT.finditer(content):
            module_name = m.group(1)
            if module_name == own_dir:
                continue
            for target in dir_files.get(module_name, []):
                if target == swift_file:
                    continue
                for sn in node_by_file.get(swift_file, []):
                    for tn in node_by_file.get(target, []):
                        edges.append(_edge(sn["id"], tn["id"], swift_file, target, "swift_import", 0.85))
                        break
                    break
                break

    # 2. protocol conformance edges
    for swift_file, content in contents.items():
        for m in _SWIFT_TYPE_DECL.finditer(content):
            type_name, conforms_str = m.group(1), m.group(2)
            if not conforms_str:
                continue
            for proto_name in (p.strip().split("<")[0] for p in conforms_str.split(",")):
                proto_target = protocol_files.get(proto_name)
                if not proto_target or proto_target == swift_file:
                    continue
                for sn in node_by_file.get(swift_file, []):
                    if sn.get("name") != type_name:
                        continue
                    for tn in node_by_file.get(proto_target, []):
                        if tn.get("name") != proto_name:
                            continue
                        edges.append(_edge(sn["id"], tn["id"], swift_file, proto_target, "swift_conforms", 0.80))
                        break

    # 3. intra-directory function calls
    for swift_file, content in contents.items():
        dir_ = Path(swift_file).parent.as_posix()
        dir_fns: dict[str, str] = {}
        for other in contents:
            if other == swift_file or Path(other).parent.as_posix() != dir_:
                continue
            for m in _SWIFT_FUNC_DECL.finditer(contents.get(other, "")):
                dir_fns[m.group(1)] = other
        for fname, tgt_file in dir_fns.items():
            if re.search(rf'\b{re.escape(fname)}\s*\(', content):
                for sn in node_by_file.get(swift_file, []):
                    for tn in node_by_file.get(tgt_file, []):
                        edges.append(_edge(sn["id"], tn["id"], swift_file, tgt_file, "swift_calls", 0.75))
                        break
                    break

    return edges


def _edge(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return make_edge(src_id, tgt_id, src_file, tgt_file, etype, confidence, resolver="swift")
