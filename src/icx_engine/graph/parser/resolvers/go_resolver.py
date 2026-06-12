"""
Go language semantic edge resolver for ICX graph.

Edge types:
  go_import      (0.90): import path -> package file
  go_implements  (0.75): struct satisfies interface implicitly (method set superset)
  go_calls       (0.85): intra-package function call

Activation: any .go file in the project.
Go interfaces are satisfied implicitly - no 'implements' keyword.
"""
import re
from pathlib import Path
from collections import defaultdict

_GO_IMPORT_SINGLE  = re.compile(r'import\s+"([^"]+)"')
_GO_IMPORT_GROUP   = re.compile(r'import\s*\((.*?)\)', re.DOTALL)
_GO_IMPORT_LINE    = re.compile(r'(?:(\w+)\s+)?"([^"]+)"')
_GO_FUNC_DECL      = re.compile(r'^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(', re.MULTILINE)
_GO_INTERFACE_DECL = re.compile(r'type\s+(\w+)\s+interface\s*\{([^}]*)\}', re.DOTALL)
_GO_METHOD_DECL    = re.compile(r'func\s+\(([^)]+)\)\s+(\w+)\s*\(', re.MULTILINE)
_GO_IFACE_METHOD   = re.compile(r'^\s+(\w+)\s*\(', re.MULTILINE)


def resolve_go(files: list, project_path, extraction: dict) -> list:
    root = Path(str(project_path))
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    go_files = [
        f for f in files
        if str(f).endswith(".go") and not str(f).endswith("_test.go")
    ]
    if not go_files:
        return []

    edges = []
    node_by_file: dict[str, list] = defaultdict(list)
    _root_posix = root.as_posix()
    for n in nodes:
        sf = (n.get("source_file") or n.get("file") or "").replace("\\", "/")
        if not sf:
            continue
        node_by_file[sf].append(n)
        # Also index by absolute path so lookups work when contents keys are absolute.
        if not (sf.startswith("/") or (len(sf) > 1 and sf[1] == ":")):
            node_by_file[f"{_root_posix}/{sf}"].append(n)

    contents: dict[str, str] = {}
    for f in go_files:
        key = str(f).replace("\\", "/")
        try:
            contents[key] = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    module_name = _read_module_name(root)

    # directory -> representative file
    dir_to_file: dict[str, str] = {}
    for f_key in contents:
        d = str(Path(f_key).parent).replace("\\", "/")
        dir_to_file.setdefault(d, f_key)

    # 1. Import edges
    for go_file, content in contents.items():
        for imp_path in _extract_imports(content):
            target = _resolve_import(imp_path, module_name, dir_to_file, go_file)
            if target:
                for sn in node_by_file.get(go_file, []):
                    for tn in node_by_file.get(target, []):
                        edges.append(_edge(sn["id"], tn["id"],
                                           go_file, target, "go_import", 0.90))
                        break
                    break

    # 2. Implicit interface implementation
    interfaces = _extract_interfaces(contents)
    struct_methods = _extract_struct_methods(contents)

    for iface_file, iface_name, iface_methods in interfaces:
        for struct_file, struct_name, struct_ms in struct_methods:
            if iface_methods and iface_methods.issubset(struct_ms):
                iface_nodes  = [n for n in node_by_file.get(iface_file, [])
                                 if n.get("name") == iface_name]
                struct_nodes = [n for n in node_by_file.get(struct_file, [])
                                 if n.get("name") == struct_name]
                if iface_nodes and struct_nodes:
                    edges.append(_edge(struct_nodes[0]["id"], iface_nodes[0]["id"],
                                       struct_file, iface_file, "go_implements", 0.75))

    # 3. Intra-package function calls
    for go_file, content in contents.items():
        pkg_dir = Path(go_file).parent.as_posix()
        pkg_funcs: dict[str, str] = {}
        for other in contents:
            if Path(other).parent.as_posix() == pkg_dir and other != go_file:
                for m in _GO_FUNC_DECL.finditer(contents.get(other, "")):
                    pkg_funcs[m.group(1)] = other
        for fname, tgt_file in pkg_funcs.items():
            if re.search(rf'\b{re.escape(fname)}\s*\(', content):
                for sn in node_by_file.get(go_file, []):
                    for tn in node_by_file.get(tgt_file, []):
                        edges.append(_edge(sn["id"], tn["id"],
                                           go_file, tgt_file, "go_calls", 0.85))
                        break
                    break

    return edges


def _read_module_name(root: Path) -> str:
    try:
        text = (root / "go.mod").read_text(encoding="utf-8")
        m = re.search(r'^module\s+(\S+)', text, re.MULTILINE)
        return m.group(1) if m else ""
    except OSError:
        return ""


def _extract_imports(content: str) -> list:
    imports = list(_GO_IMPORT_SINGLE.findall(content))
    for group in _GO_IMPORT_GROUP.findall(content):
        for _, path in _GO_IMPORT_LINE.findall(group):
            imports.append(path)
    return imports


def _resolve_import(imp_path: str, module_name: str, dir_to_file: dict, current_file: str) -> str | None:
    if module_name and imp_path.startswith(module_name + "/"):
        rel_dir = imp_path[len(module_name) + 1:]
    else:
        rel_dir = imp_path.split("/")[-1]
    for d, f in dir_to_file.items():
        if d.replace("\\", "/").endswith(rel_dir) and f != current_file:
            return f
    return None


def _extract_interfaces(contents: dict) -> list:
    result = []
    for go_file, content in contents.items():
        for m in _GO_INTERFACE_DECL.finditer(content):
            methods = set(_GO_IFACE_METHOD.findall(m.group(2)))
            methods.discard("")
            if methods:
                result.append((go_file, m.group(1), methods))
    return result


def _extract_struct_methods(contents: dict) -> list:
    struct_ms: dict[tuple, set] = defaultdict(set)
    for go_file, content in contents.items():
        for m in _GO_METHOD_DECL.finditer(content):
            recv = m.group(1).strip()
            method_name = m.group(2)
            # Receiver: "varname TypeName" or "varname *TypeName"
            # We want the type name (second token), not the variable name.
            parts = recv.split()
            type_part = parts[-1].lstrip("*") if parts else ""
            if type_part and re.match(r'^\w+$', type_part):
                struct_ms[(go_file, type_part)].add(method_name)
    return [(f, n, ms) for (f, n), ms in struct_ms.items()]


def _edge(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return {
        "source": src_id, "target": tgt_id,
        "source_file": src_file, "target_file": tgt_file,
        "relation": etype, "type": etype, "confidence": confidence,
        "resolver": "go", "fix_confidence_delta": 0.0, "resolution_weight": 0.0,
    }
