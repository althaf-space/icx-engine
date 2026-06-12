"""Role tagging for source files. Public API: role_tag(filepath) -> str."""
from __future__ import annotations

from pathlib import Path

_SUFFIX_TAGS: tuple[tuple[str, str], ...] = (
    ("restcontroller", "[controller]"),
    ("controller",     "[controller]"),
    ("handler",        "[controller]"),
    ("views",          "[controller]"),
    ("serviceimpl",    "[service]"),
    ("service",        "[service]"),
    ("usecase",        "[service]"),
    ("interactor",     "[service]"),
    ("task",           "[service]"),
    ("channel",        "[service]"),
    ("consumers",      "[service]"),
    ("consumer",       "[service]"),
    ("bloc",           "[service]"),
    ("cubit",          "[service]"),
    ("provider",       "[service]"),
    ("coordinator",    "[service]"),
    ("presenter",      "[service]"),
    ("delegate",       "[service]"),
    ("repositoryimpl", "[dao]"),
    ("repository",     "[dao]"),
    ("daoimpl",        "[dao]"),
    ("dao",            "[dao]"),
    ("repo",           "[dao]"),
    ("serializer",     "[model]"),
    ("serializers",    "[model]"),
    ("schema",         "[model]"),
    ("schemas",        "[model]"),
    ("entity",         "[model]"),
    ("entities",       "[model]"),
    ("models",         "[model]"),
    ("model",          "[model]"),
    ("dto",            "[model]"),
    ("to",             "[model]"),
    ("bean",           "[model]"),
    ("configuration",  "[config]"),
    ("settings",       "[config]"),
    ("configs",        "[config]"),
    ("config",         "[config]"),
    ("widget",         "[component]"),
    ("screen",         "[container]"),
    ("page",           "[container]"),
    ("middleware",     "[middleware]"),
    ("filter",         "[middleware]"),
    ("interceptor",    "[middleware]"),
    ("plug",           "[middleware]"),
    ("decoder",        "[util]"),
    ("encoder",        "[util]"),
    ("helpers",        "[util]"),
    ("helper",         "[util]"),
    ("utils",          "[util]"),
    ("util",           "[util]"),
    ("urls",           "[route]"),
    ("url",            "[route]"),
    ("router",         "[route]"),
    ("routes",         "[route]"),
    ("route",          "[route]"),
    ("tests",          "[test]"),
    ("test",           "[test]"),
    ("spec",           "[test]"),
    ("exceptions",     "[exception]"),
    ("exception",      "[exception]"),
    ("error",          "[exception]"),
    ("errors",         "[exception]"),
)

_DIR_TAGS: tuple[tuple[str, str], ...] = (
    ("/models/",      "[model]"),
    ("/controllers/", "[controller]"),
    ("/schemas/",     "[model]"),
    ("/serializers/", "[model]"),
    ("/middleware/",  "[middleware]"),
    ("/policies/",    "[middleware]"),
    ("/composables/", "[hook]"),
    ("/widgets/",     "[component]"),
    ("/screens/",     "[container]"),
    ("/pages/",       "[container]"),
    ("/views/",       "[container]"),
    ("/app/models/",      "[model]"),
    ("/app/controllers/", "[controller]"),
    ("/app/services/",    "[service]"),
    ("/app/jobs/",        "[service]"),
    ("/app/mailers/",     "[service]"),
)


def role_tag(filepath: str) -> str:
    """Return a role tag like '[controller]' based on filename/path patterns, or '' if none.

    Three layers:
    1. JS/JSX/TS/TSX/Vue/Svelte framework path conventions.
    2. Universal stem-suffix detection across all languages.
    3. Directory convention detection for languages without stem role hints.
    """
    fp = Path(filepath).as_posix()
    stem = Path(fp).stem
    sl = stem.lower()
    fpl = fp.lower()

    if fp.endswith((".js", ".jsx", ".mjs", ".ts", ".tsx", ".vue", ".svelte")):
        if "/hooks/" in fpl or "/composables/" in fpl or (
            stem.startswith("use") and len(stem) > 3 and stem[3].isupper()
        ):
            return "[hook]"
        if "redux/actions" in fpl or "/actions/" in fpl:
            return "[action]"
        if "redux/reducers" in fpl or "/reducers/" in fpl:
            return "[reducer]"
        if "/store/" in fpl or "redux/store" in fpl:
            return "[store]"
        if "routeconfig/" in fpl or "/routes/" in fpl:
            return "[route]"
        if "/util/" in fpl or "/utils/" in fpl or "/helpers/" in fpl:
            return "[util]"
        if "modal" in sl:
            return "[modal]"
        if "context" in sl:
            return "[context]"
        if "containers/" in fpl:
            return "[container]"
        if "components/" in fpl:
            return "[component]"
        if "appconfig/" in fpl or "/config/" in fpl:
            return "[config]"

    for suffix, tag in _SUFFIX_TAGS:
        if sl.endswith(suffix):
            return tag

    for dir_pattern, tag in _DIR_TAGS:
        bare = dir_pattern.lstrip("/")
        if dir_pattern in fpl or fpl.startswith(bare):
            return tag

    # JSP/template views (extension-based, catches files outside /views/ dirs)
    if fp.endswith((".jsp", ".jspx")):
        return "[view]"
    if fp.endswith(".erb"):
        return "[view]"

    # Go test files
    if fp.endswith("_test.go"):
        return "[test]"

    # Protocol buffer / gRPC contracts
    if fp.endswith(".proto"):
        return "[contract]"

    # Terraform infrastructure files
    if fp.endswith((".tf", ".tfvars")):
        return "[infrastructure]"

    return ""
