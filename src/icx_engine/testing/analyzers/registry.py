"""Select the right analyzer prompt for a target and serve its text.

Design mirrors `rules.py`: prompts ship as bundled assets and are seeded once into
~/.icx/testing_analyzers/ where the user may edit them; the user copy wins, the bundled copy is the
fallback. Selection is keyed to the framework the graph already detects (see `detect_framework`),
then language, then file extension - so the caller never has to name a prompt.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

_ASSETS_DIR = Path(__file__).parent / "assets"

# Family -> the runner surface that executes the analyzer's output.
#   ui      -> Playwright/Stagehand (selectors, modals, toasts, inline errors) - incl server-rendered JSP
#   backend -> HTTP API runner (endpoint schema -> schemathesis + hurl)
#   cpp     -> C/C++ unit runner (GoogleTest/Catch2)
#   sql     -> DB routine runner (utPLSQL/tSQLt/pgTAP)
#   grpc    -> gRPC runner (endpoint-shaped census, but NOT HTTP - own runner, no openapi materialize)
#   iac     -> IaC/policy runner (Terraform; own testableUnits schema)
FAMILIES = ("ui", "backend", "cpp", "sql", "grpc", "iac")


@dataclass(frozen=True)
class AnalyzerSpec:
    id: str                 # stable key, e.g. "react", "java", "cpp", "sql"
    family: str             # one of FAMILIES
    prompt_file: str        # basename under assets/ (and ~/.icx/testing_analyzers/)
    label: str              # human description


# One row per analyzer prompt in the suite. `id` values align with the graph's framework/language
# keys where possible so `detect_framework` can map straight through.
_SPECS: tuple[AnalyzerSpec, ...] = (
    # -- UI (Playwright) -----------------------------------------------------
    AnalyzerSpec("react",   "ui",      "JSX_SCREEN_ANALYZER_PROMPT.md", "React / JSX / TSX screens"),
    AnalyzerSpec("angular", "ui",      "ANALYZER_PROMPT_ANGULAR.md",       "Angular (Material/ng-bootstrap/PrimeNG)"),
    AnalyzerSpec("vue",     "ui",      "ANALYZER_PROMPT_VUE.md",           "Vue 2/3, Nuxt"),
    AnalyzerSpec("svelte",  "ui",      "ANALYZER_PROMPT_SVELTE.md",        "Svelte / SvelteKit"),
    # -- Backend / API -------------------------------------------------------
    AnalyzerSpec("python",  "backend", "ANALYZER_PROMPT_PYTHON.md",        "FastAPI/Flask/Django/DRF/Celery"),
    AnalyzerSpec("java",    "backend", "ANALYZER_PROMPT_JAVA_SPRINGBOOT.md", "Spring Boot / plain Java"),
    AnalyzerSpec("kotlin",  "backend", "ANALYZER_PROMPT_KOTLIN.md",        "Ktor / Spring Boot (Kotlin)"),
    AnalyzerSpec("csharp",  "backend", "ANALYZER_PROMPT_CSHARP_DOTNET.md", "ASP.NET Core / Minimal APIs"),
    AnalyzerSpec("node",    "backend", "ANALYZER_PROMPT_NODEJS_TYPESCRIPT.md", "Express/Fastify/NestJS/Koa"),
    AnalyzerSpec("php",     "backend", "ANALYZER_PROMPT_PHP.md",           "Laravel/Symfony/Slim"),
    AnalyzerSpec("ruby",    "backend", "ANALYZER_PROMPT_RUBY_RAILS.md",    "Rails API / Sinatra / Grape"),
    AnalyzerSpec("go",      "backend", "ANALYZER_PROMPT_GO.md",            "net/http/gin/echo/chi/fiber"),
    AnalyzerSpec("rust",    "backend", "ANALYZER_PROMPT_RUST.md",          "axum/actix-web/rocket/warp"),
    AnalyzerSpec("scala",   "backend", "ANALYZER_PROMPT_SCALA.md",         "Play/Akka HTTP/http4s/Tapir"),
    AnalyzerSpec("elixir",  "backend", "ANALYZER_PROMPT_ELIXIR.md",        "Phoenix/Plug/Ecto"),
    AnalyzerSpec("graphql", "backend", "ANALYZER_PROMPT_GRAPHQL.md",       "GraphQL SDL + resolvers (any language)"),
    # -- UI (server-rendered Java) ------------------------------------------
    AnalyzerSpec("jsp",     "ui",      "ANALYZER_PROMPT_JSP.md",           "JSP/JSF/Struts/Spring-MVC server-rendered UI"),
    # -- Systems & Data ------------------------------------------------------
    AnalyzerSpec("cpp",     "cpp",     "ANALYZER_PROMPT_C_CPP.md",         "C/C++ libraries, modules, CLIs"),
    AnalyzerSpec("sql",     "sql",     "ANALYZER_PROMPT_SQL_STORED_PROCEDURES.md", "PL/SQL, T-SQL, MySQL, PL/pgSQL"),
    AnalyzerSpec("grpc",    "grpc",    "ANALYZER_PROMPT_GRPC.md",          "gRPC services (.proto + impl)"),
    AnalyzerSpec("terraform", "iac",   "ANALYZER_PROMPT_TERRAFORM.md",     "Terraform / HCL IaC"),
)

_BY_ID = {s.id: s for s in _SPECS}

# Graph/framework name (as our resolvers report it) -> analyzer id. Covers the aliases the graph and
# classify.py emit. Anything not here falls back to the language map, then the extension map.
_FRAMEWORK_ALIASES = {
    "react": "react", "nextjs": "react", "next": "react", "remix": "react", "react-native": "react",
    "jsx": "react", "tsx": "react",
    "angular": "angular",
    "vue": "vue", "nuxt": "vue", "vue-options": "vue",
    "svelte": "svelte", "sveltekit": "svelte",
    "fastapi": "python", "flask": "python", "django": "python", "drf": "python", "celery": "python",
    "spring": "java", "springboot": "java", "spring-boot": "java", "jaxrs": "java", "jpa": "java",
    "kotlin-spring": "kotlin", "ktor": "kotlin",
    "aspnet": "csharp", "aspnetcore": "csharp", "dotnet": "csharp", "minimal-api": "csharp",
    "express": "node", "fastify": "node", "nestjs": "node", "nest": "node", "koa": "node",
    "laravel": "php", "symfony": "php", "slim": "php",
    "rails": "ruby", "sinatra": "ruby", "grape": "ruby",
    "gin": "go", "echo": "go", "chi": "go", "fiber": "go", "mux": "go", "nethttp": "go",
    "axum": "rust", "actix": "rust", "actix-web": "rust", "rocket": "rust", "warp": "rust",
    "play": "scala", "akka": "scala", "akka-http": "scala", "http4s": "scala", "tapir": "scala", "pekko": "scala",
    "phoenix": "elixir", "plug": "elixir", "ecto": "elixir", "absinthe": "graphql",
    "graphql": "graphql", "apollo": "graphql", "strawberry": "graphql", "graphene": "graphql", "gqlgen": "graphql",
    "jsp": "jsp", "jsf": "jsp", "struts": "jsp", "facelets": "jsp", "spring-mvc": "jsp",
    "grpc": "grpc", "protobuf": "grpc", "proto": "grpc",
    "terraform": "terraform", "tf": "terraform", "hcl": "terraform", "opentofu": "terraform",
}

# Bare language -> analyzer id (used when no specific framework is detected).
_LANGUAGE_MAP = {
    "javascript": "react", "typescript": "react",   # UI-first for JS/TS; overridden by framework
    "python": "python", "java": "java", "kotlin": "kotlin", "csharp": "csharp", "c#": "csharp",
    "php": "php", "ruby": "ruby", "go": "go", "golang": "go", "rust": "rust",
    "c": "cpp", "cpp": "cpp", "c++": "cpp", "cxx": "cpp",
    "sql": "sql", "plsql": "sql", "tsql": "sql", "pgsql": "sql",
    "scala": "scala", "elixir": "elixir", "hcl": "terraform", "terraform": "terraform",
}

# File extension -> analyzer id (last-resort when neither framework nor language is known).
_EXT_MAP = {
    ".jsx": "react", ".tsx": "react",
    ".vue": "vue", ".svelte": "svelte",
    ".py": "python", ".java": "java", ".kt": "kotlin", ".kts": "kotlin",
    ".cs": "csharp", ".php": "php", ".rb": "ruby", ".go": "go", ".rs": "rust",
    ".c": "cpp", ".h": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".sql": "sql", ".pks": "sql", ".pkb": "sql", ".prc": "sql",
    ".scala": "scala", ".sc": "scala", ".ex": "elixir", ".exs": "elixir",
    ".jsp": "jsp", ".jspx": "jsp", ".xhtml": "jsp",
    ".proto": "grpc", ".tf": "terraform", ".tfvars": "terraform", ".graphql": "graphql", ".gql": "graphql",
    # .ts/.js intentionally omitted here: they need framework/graph context (UI vs backend), so a
    # bare .ts file with no detected framework resolves via _LANGUAGE_MAP (react) only as a last hint.
    ".ts": "node", ".js": "node",
}


def analyzers_dir() -> Path:
    return Path.home() / ".icx" / "testing_analyzers"


def ensure_seeded() -> None:
    """Copy any missing bundled prompt into ~/.icx/testing_analyzers/. Never overwrites user edits."""
    d = analyzers_dir()
    d.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
    if not _ASSETS_DIR.exists():
        return
    for src in _ASSETS_DIR.glob("*.md"):
        dst = d / src.name
        if not dst.exists():
            try:
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass


def prompt_text(spec: AnalyzerSpec) -> str:
    """The analyzer prompt text - user copy first, bundled asset as fallback."""
    ensure_seeded()
    user = analyzers_dir() / spec.prompt_file
    if user.exists():
        try:
            return user.read_text(encoding="utf-8")
        except OSError:
            pass
    bundled = _ASSETS_DIR / spec.prompt_file
    if bundled.exists():
        try:
            return bundled.read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


def _spec_for_id(analyzer_id: str | None) -> AnalyzerSpec | None:
    return _BY_ID.get(analyzer_id) if analyzer_id else None


def detect_framework(file_paths: list[str]) -> str | None:
    """Best-effort framework/language key for the seed files, graph-first.

    1) Ask the graph for a detected framework on any seed file (authoritative).
    2) Fall back to file extension.
    Returns a raw key (framework name, language, or None) - `select_analyzer` normalizes it.
    """
    try:
        from icx_engine.graph import storage
        from icx_engine.graph.query import GraphQuerier
        for p in file_paths:
            try:
                info = storage.lookup_for_file(Path(p))
                if info is None:
                    continue
                gpath = storage.graph_path(info.project_id)
                if not gpath.exists():
                    continue
                q = GraphQuerier(gpath)
                fw = getattr(q, "detect_framework", None)
                if callable(fw):
                    val = fw(str(p))
                    if val:
                        return str(val).lower()
            except Exception:
                continue
    except Exception:
        pass
    # extension fallback
    for p in file_paths:
        ext = Path(p).suffix.lower()
        if ext in _EXT_MAP:
            return _EXT_MAP[ext]
    return None


def select_analyzer(
    framework: str | None = None,
    language: str | None = None,
    file_paths: list[str] | None = None,
) -> AnalyzerSpec | None:
    """Resolve the best analyzer prompt. Precedence: explicit framework alias -> language -> the
    framework detected from file_paths (graph) -> file extension. Returns None when nothing matches
    (caller then degrades to free authoring)."""
    fw = (framework or "").strip().lower()
    if fw in _FRAMEWORK_ALIASES:
        return _spec_for_id(_FRAMEWORK_ALIASES[fw])
    if fw in _BY_ID:                      # already an analyzer id
        return _BY_ID[fw]

    lang = (language or "").strip().lower()
    if lang in _LANGUAGE_MAP:
        return _spec_for_id(_LANGUAGE_MAP[lang])

    if file_paths:
        detected = detect_framework(file_paths)
        if detected:
            if detected in _FRAMEWORK_ALIASES:
                return _spec_for_id(_FRAMEWORK_ALIASES[detected])
            if detected in _BY_ID:
                return _BY_ID[detected]
            if detected in _LANGUAGE_MAP:
                return _spec_for_id(_LANGUAGE_MAP[detected])
        for p in file_paths:
            ext = Path(p).suffix.lower()
            if ext in _EXT_MAP:
                return _spec_for_id(_EXT_MAP[ext])
    return None


def list_analyzers() -> list[AnalyzerSpec]:
    return list(_SPECS)
