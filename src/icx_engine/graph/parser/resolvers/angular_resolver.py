"""
Angular framework semantic edge resolver for ICX graph.

Edge types:
  angular_declares (0.85): @NgModule `declarations` array -> declared component class
  angular_imports  (0.85): @NgModule `imports` array -> imported module class declared in the project
  angular_di       (0.75): constructor dependency injection -> injected service/class declared in the project
  angular_template (0.90): @Component `templateUrl` -> external template file
  angular_selector (0.75): template custom-element usage -> component class declaring that selector

Activation: any .ts file in the project (excluding .spec.ts and .d.ts). .html
files are scanned for selector usage and as templateUrl resolution targets.
Constructor DI (section 2) handles multiple exported classes per file, each
resolved against its own brace-delimited body. NgModule/Component lookups
(sections 1 and 3) key on the first exported class per file - multiple
@NgModule/@Component declarations in one file are still resolved correctly,
but class_files maps each class name to its declaring file regardless. Array
items that are not bare identifiers (e.g. `BrowserModule.forRoot(...)`) are
skipped. Plain ES module imports are already covered by jsts_imports/ts_lsp -
this resolver only covers Angular-specific decorator/DI/template relations.
"""
import re
import posixpath
from pathlib import Path
from collections import defaultdict

from icx_engine.graph.parser.resolvers._common import make_edge

_NG_CLASS_DECL = re.compile(r'\bexport\s+(?:abstract\s+)?class\s+(\w+)', re.MULTILINE)
_NG_NGMODULE = re.compile(r'@NgModule\(\s*\{(.*?)\}\s*\)\s*export\s+(?:abstract\s+)?class\s+(\w+)', re.DOTALL)
_NG_COMPONENT = re.compile(r'@Component\(\s*\{(.*?)\}\s*\)\s*export\s+(?:abstract\s+)?class\s+(\w+)', re.DOTALL)
_NG_CONSTRUCTOR = re.compile(r'constructor\s*\(([^)]*)\)', re.DOTALL)
_NG_PARAM_TYPE = re.compile(r':\s*(\w+)')
_NG_SELECTOR = re.compile(r'selector\s*:\s*[\'"]([\w-]+)[\'"]')
_NG_TEMPLATE_URL = re.compile(r'templateUrl\s*:\s*[\'"]([^\'"]+)[\'"]')
_HTML_TAG = re.compile(r'<([a-z][\w]*(?:-[\w]+)+)')
_IDENTIFIER = re.compile(r'^\w+$')


def _bracket_body(text: str, key: str) -> str | None:
    """Return the contents of `key: [ ... ]`, depth-aware so nested `[...]`
    (e.g. `RouterModule.forRoot([...])`) don't truncate the match early."""
    m = re.search(rf'{key}\s*:\s*\[', text)
    if not m:
        return None
    start = m.end()
    depth = 1
    i = start
    while i < len(text) and depth > 0:
        if text[i] == '[':
            depth += 1
        elif text[i] == ']':
            depth -= 1
        i += 1
    return text[start:i - 1]


def _list_items(text: str, key: str) -> list[str]:
    body = _bracket_body(text, key)
    if body is None:
        return []
    items = []
    for raw in body.split(","):
        item = raw.strip()
        if _IDENTIFIER.match(item):
            items.append(item)
    return items


def _class_body(text: str, start: int) -> str:
    """Return the brace-delimited body of the class whose declaration ends at
    `start`, depth-aware so nested `{...}` blocks don't truncate the body early."""
    brace = text.find('{', start)
    if brace == -1:
        return ""
    depth = 1
    i = brace + 1
    while i < len(text) and depth > 0:
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
        i += 1
    return text[brace + 1:i - 1]


def resolve_angular(files: list, project_path, extraction: dict) -> list:
    root = Path(str(project_path))
    nodes = extraction.get("nodes", []) if isinstance(extraction, dict) else []
    ts_files = [
        f for f in files
        if str(f).endswith(".ts") and not str(f).endswith((".spec.ts", ".d.ts"))
    ]
    html_files = [f for f in files if str(f).endswith(".html")]
    if not ts_files:
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

    def _read_all(file_list: list) -> dict[str, str]:
        out: dict[str, str] = {}
        for f in file_list:
            key = str(f).replace("\\", "/")
            try:
                out[key] = Path(f).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        return out

    contents = _read_all(ts_files)
    html_contents = _read_all(html_files)

    # class name -> declaring file
    class_files: dict[str, str] = {}
    for ts_file, content in contents.items():
        for m in _NG_CLASS_DECL.finditer(content):
            class_files.setdefault(m.group(1), ts_file)

    # 1. NgModule declarations / imports edges
    for ts_file, content in contents.items():
        for m in _NG_NGMODULE.finditer(content):
            body, module_class = m.group(1), m.group(2)
            module_target = class_files.get(module_class)
            if not module_target:
                continue
            for item in _list_items(body, "declarations"):
                target = class_files.get(item)
                if not target or target == module_target:
                    continue
                for sn in node_by_file.get(module_target, []):
                    if sn.get("name") != module_class:
                        continue
                    for tn in node_by_file.get(target, []):
                        if tn.get("name") != item:
                            continue
                        edges.append(_edge(sn["id"], tn["id"], module_target, target, "angular_declares", 0.85))
                        break
            for item in _list_items(body, "imports"):
                target = class_files.get(item)
                if not target or target == module_target:
                    continue
                for sn in node_by_file.get(module_target, []):
                    if sn.get("name") != module_class:
                        continue
                    for tn in node_by_file.get(target, []):
                        if tn.get("name") != item:
                            continue
                        edges.append(_edge(sn["id"], tn["id"], module_target, target, "angular_imports", 0.85))
                        break

    # 2. constructor dependency-injection edges
    for ts_file, content in contents.items():
        for class_m in _NG_CLASS_DECL.finditer(content):
            class_name = class_m.group(1)
            body = _class_body(content, class_m.end())
            ctor_m = _NG_CONSTRUCTOR.search(body)
            if not ctor_m:
                continue
            for pm in _NG_PARAM_TYPE.finditer(ctor_m.group(1)):
                type_name = pm.group(1)
                target = class_files.get(type_name)
                if not target or target == ts_file:
                    continue
                for sn in node_by_file.get(ts_file, []):
                    if sn.get("name") != class_name:
                        continue
                    for tn in node_by_file.get(target, []):
                        if tn.get("name") != type_name:
                            continue
                        edges.append(_edge(sn["id"], tn["id"], ts_file, target, "angular_di", 0.75))
                        break

    # 3. @Component templateUrl edges + selector index
    selector_map: dict[str, tuple[str, str]] = {}
    for ts_file, content in contents.items():
        for m in _NG_COMPONENT.finditer(content):
            body, component_class = m.group(1), m.group(2)
            sel_m = _NG_SELECTOR.search(body)
            if sel_m:
                selector_map[sel_m.group(1)] = (component_class, ts_file)
            tpl_m = _NG_TEMPLATE_URL.search(body)
            if not tpl_m:
                continue
            target_html = posixpath.normpath(posixpath.join(posixpath.dirname(ts_file), tpl_m.group(1)))
            if target_html not in html_contents:
                continue
            for sn in node_by_file.get(ts_file, []):
                if sn.get("name") != component_class:
                    continue
                for tn in node_by_file.get(target_html, []):
                    edges.append(_edge(sn["id"], tn["id"], ts_file, target_html, "angular_template", 0.90))
                    break

    # 4. template selector-usage edges
    for html_file, content in html_contents.items():
        for tm in _HTML_TAG.finditer(content):
            tag = tm.group(1)
            if tag not in selector_map:
                continue
            component_class, ts_file = selector_map[tag]
            for sn in node_by_file.get(html_file, []):
                for tn in node_by_file.get(ts_file, []):
                    if tn.get("name") != component_class:
                        continue
                    edges.append(_edge(sn["id"], tn["id"], html_file, ts_file, "angular_selector", 0.75))
                    break
                break

    return edges


def _edge(src_id, tgt_id, src_file, tgt_file, etype, confidence) -> dict:
    return make_edge(src_id, tgt_id, src_file, tgt_file, etype, confidence, resolver="angular")
