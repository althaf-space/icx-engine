from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath


_FRONTEND_EXT = {".jsx", ".tsx", ".vue", ".svelte"}
_FRONTEND_DIR = ("components", "pages", "screens", "views", "hooks", "routes", "containers")
_BACKEND_EXT = {".java", ".kt", ".go", ".cs", ".rb", ".php"}
_BACKEND_DIR = ("controller", "controllers", "service", "services", "repository",
                "repositories", "dao", "entity", "entities", "model", "models", "handler", "handlers")
_NEUTRAL_EXT = {".md", ".txt", ".json", ".yaml", ".yml", ".css", ".scss", ".html", ".xml"}

# content-signal patterns
_SELECTOR_RE = re.compile(r'data-testid|data-test\b|\bid=', re.IGNORECASE)
_JSX_RETURN_RE = re.compile(r'return\s*\(?\s*<', re.MULTILINE)
_DEFAULT_EXPORT_RE = re.compile(r'export\s+default\s+(function|class|\()')
_ROUTE_DEF_RE = re.compile(r'<Route\b|createBrowserRouter|path:\s*[\'"]')
_ENDPOINT_RE = re.compile(
    r'@(Get|Post|Put|Patch|Delete|Request)Mapping'           # spring
    r'|@(?:app|router)\.(?:get|post|put|patch|delete)'      # fastapi/express-ish
    r'|@app\.route'                                          # flask
    r'|\[Http(?:Get|Post|Put|Patch|Delete)\]',              # asp.net
    re.IGNORECASE,
)
_SCHEMA_RE = re.compile(
    r'@RequestBody'                                          # spring
    r'|:\s*[A-Z]\w+\b'                                       # python type-annotated param (Item)
    r'|FromBody'                                             # asp.net
    r'|req\.body',                                           # express
)


def _ext(path: str) -> str:
    return PurePosixPath(path.replace("\\", "/")).suffix.lower()


def _parts(path: str) -> list[str]:
    return [p.lower() for p in PurePosixPath(path.replace("\\", "/")).parts]


def _is_test_file(path: str) -> bool:
    name = PurePosixPath(path.replace("\\", "/")).name.lower()
    return ".test." in name or ".spec." in name or name.startswith("test_") or name.endswith("_test.py")


@dataclass
class FileClass:
    path: str
    layer: str = "unknown"
    role: str = ""
    artifacts: list[str] = field(default_factory=list)
    testability: dict[str, bool] = field(default_factory=dict)
    language: str | None = None
    source: str = "seed"


def _language(ext: str) -> str | None:
    return {
        ".jsx": "javascript", ".tsx": "typescript", ".ts": "typescript", ".js": "javascript",
        ".vue": "vue", ".svelte": "svelte", ".java": "java", ".kt": "kotlin",
        ".go": "go", ".cs": "csharp", ".rb": "ruby", ".php": "php", ".py": "python",
    }.get(ext)


def classify_file(path: str, content: str | None = None) -> FileClass:
    ext = _ext(path)
    parts = _parts(path)
    fc = FileClass(path=path, language=_language(ext))

    if _is_test_file(path):
        fc.layer = "unknown"
        fc.role = "test"
        fc.testability = {}
        return fc

    text = content or ""

    # ---- layer ----
    frontend_hint = ext in _FRONTEND_EXT or any(d in parts for d in _FRONTEND_DIR)
    backend_hint = ext in _BACKEND_EXT or any(d in parts for d in _BACKEND_DIR)
    if ext in (".ts", ".js") and not frontend_hint and not backend_hint:
        # neutral JS/TS: lean on content
        frontend_hint = bool(_JSX_RETURN_RE.search(text) or _ROUTE_DEF_RE.search(text))
        backend_hint = bool(_ENDPOINT_RE.search(text))
    elif ext in (".py",) and frontend_hint and not backend_hint:
        # Python with frontend-like dir name: check if content indicates backend
        if _ENDPOINT_RE.search(text):
            backend_hint = True
            frontend_hint = False

    if frontend_hint and not backend_hint:
        fc.layer = "frontend"
    elif backend_hint and not frontend_hint:
        fc.layer = "backend"
    elif frontend_hint and backend_hint:
        fc.layer = "shared"
    elif ext in _NEUTRAL_EXT:
        fc.layer = "shared"
    else:
        fc.layer = "unknown"

    # ---- content signals ----
    has_endpoint = bool(_ENDPOINT_RE.search(text))
    has_schema = bool(_SCHEMA_RE.search(text)) if has_endpoint else False
    renderable = bool(_JSX_RETURN_RE.search(text) or ext in (".vue", ".svelte"))
    has_selector = bool(_SELECTOR_RE.search(text))
    has_route = bool(_ROUTE_DEF_RE.search(text))

    fc.testability = {
        "exposes_endpoint": has_endpoint,
        "has_request_schema": has_schema,
        "renderable": renderable,
        "has_stable_selector": has_selector,
        "has_route": has_route,
    }

    # ---- artifacts + role ----
    if fc.layer == "frontend":
        if renderable or ext in _FRONTEND_EXT:
            fc.artifacts.append("component")
        if has_route:
            fc.artifacts.append("route")
        fc.role = "container" if any(d in parts for d in ("pages", "screens", "views", "containers")) else "component"
    elif fc.layer == "backend":
        if has_endpoint:
            fc.artifacts.append("endpoint")
        if any(d in parts for d in ("controller", "controllers", "handler", "handlers")):
            fc.artifacts.append("controller")
            fc.role = "controller"
        elif any(d in parts for d in ("entity", "entities", "model", "models")):
            fc.artifacts.append("entity")
            fc.role = "model"
        else:
            fc.role = "service"
    return fc
