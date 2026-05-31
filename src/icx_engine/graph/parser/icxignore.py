"""icxignore - per-project exclusion patterns for ICX graph builds.

The .icxignore file lives at ~/.icx/graphs/<project_id>/.icxignore.
ICX seeds it with defaults on first build. Edit it to add or remove patterns.

Format: one glob pattern per line (same as .gitignore).
  # lines starting with # are comments
  blank lines are ignored
  directory/ patterns (trailing slash) match only directories
  ** matches any number of path segments
"""
from __future__ import annotations

import fnmatch
from pathlib import Path


_SEED_CONTENT = """\
# .icxignore - ICX Graph Build Exclusion Patterns
#
# One glob pattern per line. Lines starting with # are comments.
# Patterns match against file paths relative to the project root.
# Directory patterns end with / and exclude the whole subtree.
# ** matches any number of path segments.
#
# ICX seeded this file from its built-in exclusion list on first build.
# Remove lines you do not want, or add your own patterns below.

# ---------------------------------------------------------------------------
# Dependency directories
# ---------------------------------------------------------------------------
node_modules/
__pycache__/
site-packages/
lib64/

# ---------------------------------------------------------------------------
# Build output
# ---------------------------------------------------------------------------
dist/
build/
target/
out/
.next/
.nuxt/
.turbo/
.angular/
.svelte-kit/
storybook-static/
dist-protected/
icx-graph-out/

# ---------------------------------------------------------------------------
# Virtual environments
# ---------------------------------------------------------------------------
venv/
.venv/
env/
.env/
*_venv/
*_env/

# ---------------------------------------------------------------------------
# IDE and tool caches
# ---------------------------------------------------------------------------
.git/
.idea/
.cache/
.parcel-cache/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.tox/
.eggs/
*.egg-info/
.terraform/
.serverless/
.worktrees/
.icx_graph/

# ---------------------------------------------------------------------------
# Lock files (generated, never architecturally meaningful)
# ---------------------------------------------------------------------------
package-lock.json
yarn.lock
pnpm-lock.yaml
Cargo.lock
poetry.lock
Gemfile.lock
composer.lock
go.sum
go.work.sum

# ---------------------------------------------------------------------------
# Coverage and test artifact directories
# ---------------------------------------------------------------------------
coverage/
lcov-report/
visual-tests/
visual-test/
__snapshots__/
snapshots/

# ---------------------------------------------------------------------------
# Compiled binaries and archives
# ---------------------------------------------------------------------------
*.jar
*.war
*.class
*.pyc
*.pyo
*.exe
*.dll
*.so
*.dylib

# ---------------------------------------------------------------------------
# Test fixture directories (sample/mock data, not real project code)
# ---------------------------------------------------------------------------
**/test/fixtures/
**/tests/fixtures/
**/spec/fixtures/
**/test/mocks/
**/tests/mocks/
**/__fixtures__/
**/__mocks__/
**/testdata/
**/test-data/
**/mock-data/

# ---------------------------------------------------------------------------
# Add your own patterns below this line.
# Examples:
#   legacy/          - exclude the legacy/ subdirectory
#   *.generated.ts   - exclude all generated TypeScript files
#   docs/            - exclude documentation directory
# ---------------------------------------------------------------------------
"""


def _parse_patterns(text: str) -> list[str]:
    patterns = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            patterns.append(stripped)
    return patterns


class IcxIgnore:
    """Compiled icxignore matcher. Immutable after construction."""

    def __init__(self, patterns: list[str], root: Path) -> None:
        self._root = root.resolve()
        # (normalized_pattern, dir_only)
        self._rules: list[tuple[str, bool]] = []
        for p in patterns:
            dir_only = p.endswith("/")
            norm = p.rstrip("/")
            if norm:
                self._rules.append((norm, dir_only))

    def matches(self, path: Path, is_dir: bool = False) -> bool:
        """Return True if path should be excluded from graph building."""
        try:
            rel = path.resolve().relative_to(self._root)
        except ValueError:
            return False

        rel_posix = rel.as_posix()

        for pattern, dir_only in self._rules:
            if dir_only and not is_dir:
                continue
            if _match(rel_posix, pattern):
                return True
            # Bare name patterns (no slash, no **) also match any component
            if "/" not in pattern and "**" not in pattern:
                for part in rel.parts:
                    if fnmatch.fnmatch(part, pattern):
                        return True
        return False

    def matches_name(self, name: str, is_dir: bool = False) -> bool:
        """Quick check: does this bare name match any simple (non-path) pattern?"""
        for pattern, dir_only in self._rules:
            if dir_only and not is_dir:
                continue
            if "/" not in pattern and "**" not in pattern:
                if fnmatch.fnmatch(name, pattern):
                    return True
        return False


def _match(rel_posix: str, pattern: str) -> bool:
    if "**" in pattern:
        norm = pattern.replace("**/", "")
        if fnmatch.fnmatch(rel_posix, norm):
            return True
        parts = rel_posix.split("/")
        for i in range(len(parts)):
            if fnmatch.fnmatch("/".join(parts[i:]), norm):
                return True
        return False
    return fnmatch.fnmatch(rel_posix, pattern)


def load(icxignore_path: Path, project_root: Path) -> IcxIgnore:
    """Load .icxignore from icxignore_path. Returns empty matcher if absent."""
    if icxignore_path.exists():
        try:
            text = icxignore_path.read_text(encoding="utf-8")
            return IcxIgnore(_parse_patterns(text), project_root)
        except Exception:
            pass
    return IcxIgnore([], project_root)


def seed(icxignore_path: Path) -> bool:
    """Write seeded .icxignore if absent. Returns True if written."""
    if icxignore_path.exists():
        return False
    try:
        icxignore_path.write_text(_SEED_CONTENT, encoding="utf-8")
        return True
    except OSError:
        return False
