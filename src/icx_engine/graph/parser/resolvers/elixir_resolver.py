"""
Elixir language semantic edge resolver for ICX graph.

Edge types:
  elixir_alias (0.90): `alias My.App.Foo` / `import My.App.Foo` -> file with
                        `defmodule My.App.Foo`
  elixir_use   (0.80): `use My.App.Behaviour` -> file declaring that module
  elixir_calls (0.75): `Module.function(...)` -> module's declaring file,
                        resolved through the file's alias table

Activation: any .ex/.exs file in the project.
"""
import re
from pathlib import Path
from collections import defaultdict

from icx_engine.graph.parser.resolvers._common import make_edge

_EX_DEFMODULE = re.compile(r'^\s*defmodule\s+([\w.]+)\s+do', re.MULTILINE)
_EX_ALIAS_IMPORT = re.compile(r'^\s*(alias|import)\s+([\w.]+)(?:,\s*as:\s*(\w+))?', re.MULTILINE)
_EX_USE = re.compile(r'^\s*use\s+([\w.]+)', re.MULTILINE)
_EX_CALL = re.compile(r'\b([A-Z][\w.]*)\.(\w+)\s*\(')


def resolve_elixir(files: list, project_path, extraction: dict) -> list:
    root = Path(str(project_path))
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    ex_files = [f for f in files if str(f).lower().endswith((".ex", ".exs"))]
    if not ex_files:
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
    for f in ex_files:
        key = str(f).replace("\\", "/")
        try:
            contents[key] = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    if not contents:
        return []

    # fully-qualified module name -> declaring file
    module_files: dict[str, str] = {}
    for ex_file, content in contents.items():
        for m in _EX_DEFMODULE.finditer(content):
            module_files.setdefault(m.group(1), ex_file)

    # 1. alias/import edges + per-file alias map (last segment / `as:` name -> full module)
    file_alias_map: dict[str, dict[str, str]] = defaultdict(dict)
    for ex_file, content in contents.items():
        for m in _EX_ALIAS_IMPORT.finditer(content):
            full_name, alias_name = m.group(2), m.group(3)
            short_name = alias_name or full_name.split(".")[-1]
            file_alias_map[ex_file][short_name] = full_name
            target = module_files.get(full_name)
            if not target or target == ex_file:
                continue
            for sn in node_by_file.get(ex_file, []):
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(sn["id"], tn["id"], ex_file, target, "elixir_alias", 0.90))
                    break
                break

    # 2. use edges
    for ex_file, content in contents.items():
        for m in _EX_USE.finditer(content):
            full_name = m.group(1)
            target = module_files.get(full_name)
            if not target or target == ex_file:
                continue
            for sn in node_by_file.get(ex_file, []):
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(sn["id"], tn["id"], ex_file, target, "elixir_use", 0.80))
                    break
                break

    # 3. Module.function(...) call edges, resolved through the alias map
    for ex_file, content in contents.items():
        alias_map = file_alias_map.get(ex_file, {})
        for m in _EX_CALL.finditer(content):
            ref = m.group(1)
            full_name = alias_map.get(ref, ref)
            target = module_files.get(full_name)
            if not target or target == ex_file:
                continue
            for sn in node_by_file.get(ex_file, []):
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(sn["id"], tn["id"], ex_file, target, "elixir_calls", 0.75))
                    break
                break

    return edges


def _edge(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return make_edge(src_id, tgt_id, src_file, tgt_file, etype, confidence, resolver="elixir")
