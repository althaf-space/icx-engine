"""Browser-engine + device-profile targets for running a UI flow across engines and form factors.

A Target is an (engine, device) pair. parse_targets turns a user string
("chromium,firefox,webkit:Pixel 7") into Targets. installed_engines reports which engines' Playwright
browser binary is actually present in the pinned browsers dir, so the runner never tries an engine that
was not installed. Pure - no launching here; the harness does the launch per Target."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_ENGINES = ("chromium", "firefox", "webkit")


@dataclass(frozen=True)
class Target:
    engine: str
    device: str = ""

    def label(self) -> str:
        return self.engine if not self.device else f"{self.engine}:{self.device}"


def parse_targets(spec: str) -> list[Target]:
    """Parse 'engine[:device], ...' into Targets, dropping blanks and unknown engines."""
    out: list[Target] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        engine, _, device = part.partition(":")
        engine = engine.strip().lower()
        device = device.strip()
        if engine not in _ENGINES:
            continue
        out.append(Target(engine, device))
    return out


def installed_engines(browsers_root: Path | None = None) -> list[str]:
    """The engines whose Playwright browser binary dir is present under browsers_root. Playwright names
    those dirs '<engine>-<build>' (e.g. chromium-1140). Returns [] when the root is missing."""
    if browsers_root is None:
        try:
            from icx_engine.testing.runners.install import installed_path, browsers_dir
            sh = installed_path("stagehand")
            browsers_root = browsers_dir(Path(sh)) if sh else None
        except Exception:
            browsers_root = None
    if not isinstance(browsers_root, Path) or not browsers_root.exists():
        return []
    found = []
    for eng in _ENGINES:
        if any(browsers_root.glob(f"{eng}-*")):
            found.append(eng)
    return found


def default_targets() -> list[Target]:
    """The safe default when the user sets no targets: chromium desktop (current behavior)."""
    return [Target("chromium")]
