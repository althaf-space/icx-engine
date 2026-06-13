"""
gRPC / Protocol Buffer edge resolver for ICX graph.

Edge types:
  proto_import        (0.95): .proto -> imported .proto
  proto_generated     (0.90): .proto -> generated stub file (naming convention)
  proto_implements    (0.80): generated service -> implementing class
  grpc_client         (0.75): file using gRPC stub -> .proto service definition

Activation: any .proto file in the project.
Cross-language: one .proto can have edges to Python, Java, Go, and TS files.
"""
import re
from pathlib import Path
from collections import defaultdict

from icx_engine.graph.parser.resolvers._common import make_edge

_PROTO_SERVICE  = re.compile(r'service\s+(\w+)\s*\{')
_PROTO_MESSAGE  = re.compile(r'message\s+(\w+)\s*\{')
_PROTO_IMPORT   = re.compile(r'^import\s+"([^"]+\.proto)"', re.MULTILINE)

_PY_SERVICER    = re.compile(r'class\s+\w+\s*\(\s*(\w+)Servicer\s*\)')
_PY_STUB_USE    = re.compile(r'(\w+)_pb2_grpc\.(\w+)Stub\s*\(')
_PY_PB2_IMPORT  = re.compile(r'import\s+(\w+)_pb2\b')

_JAVA_IMPL      = re.compile(
    r'class\s+\w+\s+(?:extends|implements)\s+\w+Grpc\.(\w+)ImplBase'
)
_GO_REGISTER    = re.compile(r'Register(\w+)Server\s*\(')


def resolve_proto(files: list, project_path, extraction: dict) -> list:
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    file_strs = [str(f).replace("\\", "/") for f in files]
    proto_files = [f for f in file_strs if f.endswith(".proto")]
    if not proto_files:
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
    for f in file_strs:
        try:
            contents[f] = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    for proto_file in proto_files:
        content = contents.get(proto_file, "")
        stem = Path(proto_file).stem          # e.g. "user_service"
        pascal = _to_pascal(stem)             # e.g. "UserService"
        services = _PROTO_SERVICE.findall(content)
        proto_nodes = node_by_file.get(proto_file, [])

        # 1. Proto import edges
        for imported in _PROTO_IMPORT.findall(content):
            target = _find_file(Path(imported).name, file_strs)
            if target and proto_nodes:
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(proto_nodes[0]["id"], tn["id"],
                                       proto_file, target, "proto_import", 0.95))

        # 2. Generated stub edges (naming convention)
        generated_names = [
            f"{stem}_pb2.py", f"{stem}_pb2_grpc.py",
            f"{pascal}Grpc.java", f"{pascal}OuterClass.java",
            f"{stem}.pb.go", f"{stem}_grpc.pb.go",
            f"{stem}_pb.js", f"{stem}_grpc_pb.js",
        ]
        for gen_name in generated_names:
            target = _find_file(gen_name, file_strs)
            if target and proto_nodes:
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(proto_nodes[0]["id"], tn["id"],
                                       proto_file, target, "proto_generated", 0.90))

        # 3. Service implementation edges
        for service_name in services:
            for f, fc in contents.items():
                if f == proto_file:
                    continue
                matched = (
                    any(b.lower() == service_name.lower() for b in _PY_SERVICER.findall(fc))
                    or any(service_name.lower() in i.lower() for i in _JAVA_IMPL.findall(fc))
                    or any(r.lower() == service_name.lower() for r in _GO_REGISTER.findall(fc))
                )
                if matched and proto_nodes:
                    for sn in node_by_file.get(f, []):
                        edges.append(_edge(sn["id"], proto_nodes[0]["id"],
                                           f, proto_file, "proto_implements", 0.80))

        # 4. gRPC client stub usage
        for f, fc in contents.items():
            if f == proto_file:
                continue
            for module_name, _ in _PY_STUB_USE.findall(fc):
                if module_name == stem and proto_nodes:
                    for sn in node_by_file.get(f, []):
                        edges.append(_edge(sn["id"], proto_nodes[0]["id"],
                                           f, proto_file, "grpc_client", 0.75))

    return edges


def _find_file(name: str, files: list) -> str | None:
    for f in files:
        if Path(f).name == name:
            return f
    return None


def _to_pascal(snake: str) -> str:
    return "".join(w.capitalize() for w in snake.split("_"))


def _edge(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return make_edge(src_id, tgt_id, src_file, tgt_file, etype, confidence, resolver="proto")
