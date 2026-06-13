"""
Ruby on Rails convention-based edge resolver for ICX graph.

Rails uses convention-over-configuration. Connections exist by naming
convention, not explicit imports.

Edge types:
  rails_view              (0.85): controller -> app/views/<resource>/*.erb
  rails_model_controller  (0.80): model -> plural controller
  rails_route             (0.90): config/routes.rb -> controller file
  rails_ar_usage          (0.70): controller uses Model.find/where/new/etc
  rails_concern           (0.80): file with include ModuleName -> concern file
  rails_service           (0.75): caller with ServiceName.new/.call -> service file

Activation: app/controllers/ directory present in the project.
"""
import re
from pathlib import Path
from collections import defaultdict

from icx_engine.graph.parser.resolvers._common import make_edge

_ROUTE_RESOURCES    = re.compile(r'resources?\s+:(\w+)', re.MULTILINE)
_ROUTE_GET_TO       = re.compile(r"get\s+['\"][^'\"]+['\"],\s*to:\s*['\"](\w+)#", re.MULTILINE)
_ROUTE_POST_TO      = re.compile(r"post\s+['\"][^'\"]+['\"],\s*to:\s*['\"](\w+)#", re.MULTILINE)
_INCLUDE_CONCERN    = re.compile(r'\binclude\s+([A-Z]\w+)', re.MULTILINE)
_AR_USAGE           = re.compile(r'\b([A-Z][a-zA-Z]+)\.(find|where|new|create|all|first)\b', re.MULTILINE)
_SERVICE_CALL       = re.compile(r'\b([A-Z][a-zA-Z]+Service)\.(?:new|call)\b', re.MULTILINE)


def resolve_rails(files: list, project_path, extraction: dict) -> list:
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    # Only activate for Rails projects
    file_strs = [str(f).replace("\\", "/") for f in files]
    if not any("app/controllers" in f for f in file_strs):
        return []

    rb_files = [f for f in file_strs if f.endswith((".rb", ".erb"))]
    if not rb_files:
        return []

    edges = []
    proj_posix = Path(str(project_path)).as_posix()
    node_by_file: dict[str, list] = defaultdict(list)
    for n in nodes:
        sf = (n.get("source_file") or n.get("file") or "").replace("\\", "/")
        if not sf:
            continue
        node_by_file[sf].append(n)
        # Also index by absolute path so lookups work when file_strs are absolute
        if not (sf.startswith("/") or (len(sf) > 1 and sf[1] == ":")):
            node_by_file[f"{proj_posix}/{sf}"].append(n)

    controllers = [f for f in file_strs if "app/controllers/" in f and f.endswith("_controller.rb")]
    models      = [f for f in file_strs if "app/models/" in f and f.endswith(".rb") and "concerns" not in f]
    views_dir   = [f for f in file_strs if "app/views/" in f]
    concerns    = [f for f in file_strs if "concerns/" in f and f.endswith(".rb")]
    services    = [f for f in file_strs if "app/services/" in f and f.endswith(".rb")]
    routes_file = next((f for f in file_strs if f.endswith("config/routes.rb")), None)

    contents: dict[str, str] = {}
    for f in rb_files + ([routes_file] if routes_file else []):
        if f:
            try:
                contents[f] = Path(f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

    # 1. Controller -> views
    for ctrl in controllers:
        resource = Path(ctrl).stem.replace("_controller", "")
        ctrl_nodes = node_by_file.get(ctrl, [])
        if not ctrl_nodes:
            continue
        for view in views_dir:
            if f"/views/{resource}/" in view:
                for vn in node_by_file.get(view, []):
                    edges.append(_edge(ctrl_nodes[0]["id"], vn["id"],
                                       ctrl, view, "rails_view", 0.85))

    # 2. Model -> controller (by pluralisation convention)
    for model_file in models:
        model_name = Path(model_file).stem
        plural = model_name + "s"
        model_nodes = node_by_file.get(model_file, [])
        if not model_nodes:
            continue
        for ctrl in controllers:
            ctrl_stem = Path(ctrl).stem.replace("_controller", "")
            if ctrl_stem in (plural, model_name):
                for cn in node_by_file.get(ctrl, []):
                    edges.append(_edge(model_nodes[0]["id"], cn["id"],
                                       model_file, ctrl, "rails_model_controller", 0.80))

    # 3. Routes -> controllers
    if routes_file and routes_file in contents:
        routes_content = contents[routes_file]
        route_nodes = node_by_file.get(routes_file, [])
        named_resources: set[str] = set(_ROUTE_RESOURCES.findall(routes_content))
        named_resources.update(_ROUTE_GET_TO.findall(routes_content))
        named_resources.update(_ROUTE_POST_TO.findall(routes_content))
        for resource in named_resources:
            for ctrl in controllers:
                ctrl_stem = Path(ctrl).stem.replace("_controller", "")
                if ctrl_stem in (resource, resource + "s"):
                    if route_nodes:
                        for cn in node_by_file.get(ctrl, []):
                            edges.append(_edge(route_nodes[0]["id"], cn["id"],
                                               routes_file, ctrl, "rails_route", 0.90))

    # 4. Controller -> ActiveRecord model usage
    for ctrl in controllers:
        content = contents.get(ctrl, "")
        ctrl_nodes = node_by_file.get(ctrl, [])
        if not ctrl_nodes:
            continue
        for model_class, _ in _AR_USAGE.findall(content):
            for model_file in models:
                if Path(model_file).stem == _snake(model_class):
                    for mn in node_by_file.get(model_file, []):
                        edges.append(_edge(ctrl_nodes[0]["id"], mn["id"],
                                           ctrl, model_file, "rails_ar_usage", 0.70))

    # 5. Concern includes
    concern_by_name: dict[str, str] = {_pascal(Path(c).stem): c for c in concerns}
    for rb_file, content in contents.items():
        if rb_file in concerns:
            continue
        src_nodes = node_by_file.get(rb_file, [])
        if not src_nodes:
            continue
        for module_name in _INCLUDE_CONCERN.findall(content):
            target = concern_by_name.get(module_name)
            if target:
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(src_nodes[0]["id"], tn["id"],
                                       rb_file, target, "rails_concern", 0.80))

    # 6. Service calls
    service_by_name: dict[str, str] = {_pascal(Path(s).stem): s for s in services}
    for rb_file, content in contents.items():
        src_nodes = node_by_file.get(rb_file, [])
        if not src_nodes:
            continue
        for service_class in _SERVICE_CALL.findall(content):
            target = service_by_name.get(service_class)
            if target:
                for tn in node_by_file.get(target, []):
                    edges.append(_edge(src_nodes[0]["id"], tn["id"],
                                       rb_file, target, "rails_service", 0.75))

    return edges


def _snake(pascal: str) -> str:
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', pascal)
    return re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s).lower()


def _pascal(snake: str) -> str:
    return "".join(w.capitalize() for w in snake.split("_"))


def _edge(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return make_edge(src_id, tgt_id, src_file, tgt_file, etype, confidence, resolver="rails")
