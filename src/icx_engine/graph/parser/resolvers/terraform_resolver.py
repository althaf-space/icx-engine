"""
Terraform / HCL infrastructure edge resolver for ICX graph.

Edge types:
  tf_module       (0.95): module call with local source -> module main.tf
  tf_var_ref      (0.80): var.name reference -> variable declaration file
  tf_data_ref     (0.85): data.type.name reference -> data block file
  tf_resource_dep (0.90): resource.type.name reference -> resource block file
  tf_output       (0.85): output block value -> resource it exposes

Activation: any .tf file present.
Uses regex (no hcl2 dependency required - optional import for edge cases).
"""
import re
from pathlib import Path
from collections import defaultdict

from icx_engine.graph.parser.resolvers._common import make_edge

_TF_RESOURCE  = re.compile(r'^resource\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE)
_TF_DATA      = re.compile(r'^data\s+"([^"]+)"\s+"([^"]+)"', re.MULTILINE)
_TF_VAR       = re.compile(r'^variable\s+"([^"]+)"', re.MULTILINE)
_TF_OUTPUT    = re.compile(r'^output\s+"([^"]+)"', re.MULTILINE)
_TF_MODULE    = re.compile(r'^module\s+"(\w+)"\s*\{([^}]*)\}', re.MULTILINE | re.DOTALL)
_TF_MOD_SRC   = re.compile(r'source\s*=\s*"([^"]+)"')
_TF_VAR_REF   = re.compile(r'\bvar\.(\w+)')
_TF_DATA_REF  = re.compile(r'\bdata\.(\w+)\.(\w+)')
_TF_RES_REF   = re.compile(r'\b(aws_\w+|google_\w+|azurerm_\w+|kubernetes_\w+)\.(\w+)\.\w+')
_TF_OUT_BLOCK = re.compile(r'^output\s+"\w+"\s*\{([^}]*)\}', re.MULTILINE | re.DOTALL)


def resolve_terraform(files: list, project_path, extraction: dict) -> list:
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    file_strs = [str(f).replace("\\", "/") for f in files]
    tf_files = [f for f in file_strs if f.endswith(".tf")]
    if not tf_files:
        return []

    edges = []
    _root_posix = Path(str(project_path)).as_posix()
    node_by_file: dict[str, list] = defaultdict(list)
    for n in nodes:
        sf = (n.get("source_file") or n.get("file") or "").replace("\\", "/")
        if not sf:
            continue
        node_by_file[sf].append(n)
        # Also index by absolute path so lookups work when file_strs keys are absolute.
        if not (sf.startswith("/") or (len(sf) > 1 and sf[1] == ":")):
            node_by_file[f"{_root_posix}/{sf}"].append(n)

    contents: dict[str, str] = {}
    for f in tf_files:
        try:
            contents[f] = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    # Build definition indexes
    var_defs: dict[str, str] = {}
    data_defs: dict[str, str] = {}
    res_defs: dict[str, str] = {}

    for tf_file, content in contents.items():
        for vname in _TF_VAR.findall(content):
            var_defs[vname] = tf_file
        for dtype, dname in _TF_DATA.findall(content):
            data_defs[f"{dtype}.{dname}"] = tf_file
        for rtype, rname in _TF_RESOURCE.findall(content):
            res_defs[f"{rtype}.{rname}"] = tf_file

    for tf_file, content in contents.items():
        src_nodes = node_by_file.get(tf_file, [])
        if not src_nodes:
            continue
        src_id = src_nodes[0]["id"]

        # 1. Module source -> local module main.tf
        for _, mod_body in _TF_MODULE.findall(content):
            m = _TF_MOD_SRC.search(mod_body)
            if m:
                source = m.group(1)
                if source.startswith(("./", "../")):
                    tf_dir = Path(tf_file).parent.as_posix()
                    mod_dir = (Path(tf_dir) / source).as_posix()
                    target = _find_tf_in_dir(mod_dir, tf_files)
                    if target:
                        for tn in node_by_file.get(target, []):
                            edges.append(_edge(src_id, tn["id"],
                                               tf_file, target, "tf_module", 0.95))

        # 2. Variable references
        for vname in set(_TF_VAR_REF.findall(content)):
            target = var_defs.get(vname)
            if target and target != tf_file:
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(src_id, tn["id"],
                                       tf_file, target, "tf_var_ref", 0.80))

        # 3. Data source references
        for dtype, dname in set(_TF_DATA_REF.findall(content)):
            target = data_defs.get(f"{dtype}.{dname}")
            if target and target != tf_file:
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(src_id, tn["id"],
                                       tf_file, target, "tf_data_ref", 0.85))

        # 4. Resource dependency references
        for rtype, rname in set(_TF_RES_REF.findall(content)):
            target = res_defs.get(f"{rtype}.{rname}")
            if target and target != tf_file:
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(src_id, tn["id"],
                                       tf_file, target, "tf_resource_dep", 0.90))

        # 5. Output -> resource
        for out_body in _TF_OUT_BLOCK.findall(content):
            for rtype, rname in _TF_RES_REF.findall(out_body):
                target = res_defs.get(f"{rtype}.{rname}")
                if target and target != tf_file:
                    for tn in node_by_file.get(target, []):
                        edges.append(_edge(src_id, tn["id"],
                                           tf_file, target, "tf_output", 0.85))

    return edges


def _find_tf_in_dir(dir_path: str, tf_files: list) -> str | None:
    dir_posix = dir_path.replace("\\", "/")
    candidate = f"{dir_posix}/main.tf"
    if candidate in tf_files:
        return candidate
    for f in tf_files:
        if f.replace("\\", "/").startswith(dir_posix + "/"):
            return f
    return None


def _edge(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return make_edge(src_id, tgt_id, src_file, tgt_file, etype, confidence, resolver="terraform")
