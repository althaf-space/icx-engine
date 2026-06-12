"""
JSP and Servlet edge resolver for ICX graph.

Edge types produced:
  jsp_forward      (0.70): Spring MVC controller/Servlet -> JSP view file
  jsp_include      (0.85): JSP <%@ include %> / <jsp:include> -> included JSP
  taglib_import    (0.90): JSP <%@ taglib %> -> tag library URI
  el_binding       (0.55): JSP EL ${bean.prop} -> Java backing bean getter
  servlet_mapping  (0.95): web.xml <servlet-class> -> Java servlet class file

Activation: any .java or .jsp/.jspx file present in the project.
All parsing is regex-based (JSP is not valid XML in general).
Runs after the Java/Kotlin LSP pass.
"""
import re
from pathlib import Path, PurePosixPath
from collections import defaultdict

_SPRING_VIEW_RETURN   = re.compile(r'return\s+"([a-zA-Z0-9_/\-]+)"', re.MULTILINE)
_SPRING_MAV           = re.compile(r'new\s+ModelAndView\s*\(\s*"([a-zA-Z0-9_/\-]+)"', re.MULTILINE)
_DISPATCHER_FORWARD   = re.compile(r'getRequestDispatcher\s*\(\s*"([^"]+\.jsp)"', re.MULTILINE)
_JSP_INCLUDE_DIR      = re.compile(r'<%@\s*include\s+file\s*=\s*"([^"]+)"', re.MULTILINE)
_JSP_INCLUDE_ACT      = re.compile(r'<jsp:include\s+page\s*=\s*"([^"]+)"', re.MULTILINE)
_TAGLIB_DIR           = re.compile(r'<%@\s*taglib\s+[^%]*uri\s*=\s*"([^"]+)"', re.MULTILINE)
_EL_EXPR              = re.compile(r'\$\{([a-zA-Z_]\w*)\.([a-zA-Z_]\w*)', re.MULTILINE)
_WEBXML_SERVLET_CLASS = re.compile(r'<servlet-class>\s*([a-zA-Z0-9_.]+)\s*</servlet-class>', re.MULTILINE)


def resolve_jsp(files: list, project_path, extraction: dict) -> list:
    root = Path(str(project_path))
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else extraction
    root_posix = root.as_posix().rstrip("/") + "/"

    # Normalize all file paths to forward slashes for consistent comparison
    file_strs = [str(f).replace("\\", "/") for f in files]
    jsp_files  = {f for f in file_strs if f.endswith((".jsp", ".jspx", ".xhtml"))}
    java_files = {f for f in file_strs if f.endswith((".java", ".kt"))}
    if not jsp_files and not java_files:
        return []

    # Build node lookup that handles both absolute and relative source_file values.
    # Nodes from the AST extraction may use relative paths; file_strs are absolute.
    # We index by both so lookups succeed regardless of which form is present.
    node_by_file: dict[str, list] = defaultdict(list)
    for n in nodes:
        sf = (n.get("source_file") or n.get("file") or "").replace("\\", "/")
        if not sf:
            continue
        node_by_file[sf].append(n)
        # If relative, also store as absolute so absolute file_strs can find it
        if not (sf.startswith("/") or (len(sf) > 1 and sf[1] == ":")):
            node_by_file[root_posix + sf].append(n)
        else:
            # If absolute, also store as relative so relative keys can find it
            if sf.startswith(root_posix):
                rel = sf[len(root_posix):]
                node_by_file[rel].append(n)

    edges = []

    # 1. Spring MVC / Servlet -> JSP forward
    for java_file in java_files:
        try:
            content = Path(java_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        view_names: set[str] = set()
        view_names.update(_SPRING_VIEW_RETURN.findall(content))
        view_names.update(_SPRING_MAV.findall(content))
        view_names.update(_DISPATCHER_FORWARD.findall(content))
        for view_name in view_names:
            target = _resolve_view_to_jsp(view_name, jsp_files)
            if target:
                for sn in node_by_file.get(java_file, []):
                    for tn in node_by_file.get(target, []):
                        edges.append(_edge(sn["id"], tn["id"], java_file, target, "jsp_forward", 0.70))

    # 2. JSP includes + taglibs + EL expressions
    for jsp_file in jsp_files:
        try:
            content = Path(jsp_file).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        src_nodes = node_by_file.get(jsp_file, [])

        for inc in set(_JSP_INCLUDE_DIR.findall(content)) | set(_JSP_INCLUDE_ACT.findall(content)):
            resolved = _resolve_relative_jsp(inc, jsp_file, jsp_files)
            if resolved and src_nodes:
                tgt_nodes = node_by_file.get(resolved, [])
                if tgt_nodes:
                    edges.append(_edge(src_nodes[0]["id"], tgt_nodes[0]["id"],
                                       jsp_file, resolved, "jsp_include", 0.85))

        for uri in _TAGLIB_DIR.findall(content):
            if src_nodes:
                edges.append({
                    "source": src_nodes[0]["id"], "target": f"__taglib__{uri}",
                    "source_file": jsp_file, "target_file": uri,
                    "relation": "taglib_import", "type": "taglib_import", "confidence": 0.90,
                    "resolver": "jsp", "fix_confidence_delta": 0.0, "resolution_weight": 0.0,
                })

        for bean_name, prop_name in _EL_EXPR.findall(content):
            getter = f"get{prop_name[0].upper()}{prop_name[1:]}"
            for java_file in java_files:
                if Path(java_file).stem.lower() == bean_name.lower():
                    for sn in src_nodes:
                        for tn in node_by_file.get(java_file, []):
                            if tn.get("name", "").lower() in (getter.lower(), prop_name.lower()):
                                edges.append(_edge(sn["id"], tn["id"],
                                                   jsp_file, java_file, "el_binding", 0.55))
                                break

    # 3. web.xml servlet class declarations
    for f in file_strs:
        if Path(f).name != "web.xml":
            continue
        try:
            content = Path(f).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for fqcn in _WEBXML_SERVLET_CLASS.findall(content):
            class_name = fqcn.split(".")[-1]
            for java_file in java_files:
                if Path(java_file).stem == class_name:
                    for sn in node_by_file.get(f, []):
                        for tn in node_by_file.get(java_file, []):
                            edges.append(_edge(sn["id"], tn["id"],
                                               f, java_file, "servlet_mapping", 0.95))

    return edges


def _resolve_view_to_jsp(view_name: str, jsp_files: set) -> str | None:
    if view_name.endswith((".jsp", ".jspx")):
        return view_name if view_name in jsp_files else None
    prefixes = [
        "WEB-INF/views/", "WEB-INF/jsp/", "WEB-INF/",
        "templates/", "views/",
        "src/main/webapp/WEB-INF/views/", "src/main/webapp/WEB-INF/jsp/",
    ]
    for prefix in prefixes:
        for suffix in (".jsp", ".jspx", ".html"):
            candidate = f"{prefix}{view_name}{suffix}"
            if candidate in jsp_files:
                return candidate
    for jsp in jsp_files:
        if jsp.endswith(f"{view_name}.jsp") or jsp.endswith(f"{view_name}.jspx"):
            return jsp
    return None


def _resolve_relative_jsp(inc_path: str, source_jsp: str, jsp_files: set) -> str | None:
    base = PurePosixPath(source_jsp.replace("\\", "/")).parent
    resolved = str(base / inc_path.lstrip("/"))
    if resolved in jsp_files:
        return resolved
    inc_name = inc_path.lstrip("/")
    for jsp in jsp_files:
        if jsp.endswith(inc_name):
            return jsp
    return None


def _edge(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return {
        "source": src_id, "target": tgt_id,
        "source_file": src_file, "target_file": tgt_file,
        "relation": etype, "type": etype, "confidence": confidence,
        "resolver": "jsp", "fix_confidence_delta": 0.0, "resolution_weight": 0.0,
    }
