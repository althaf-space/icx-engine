"""UI-layer runner: Stagehand (AI director) + Playwright (deterministic judge).

The determinism model (the core guarantee):
  - AUTHOR once (human-gated): the AI explores the live UI and resolves each action to a concrete
    selector. The resolved steps are CACHED to ~/.icx/testing/ui-cache/<key>.json.
  - REPLAY every run after: the cached steps are replayed with NO LLM call - reproducible, never
    silently wrong. If a cached selector no longer resolves, the harness fails LOUD -> re-author.

This module provides the testable core: UI detection, the flow-cache model (author/replay), and the
command that runs the ICX Node harness (Stagehand replay -> Playwright -> JUnit XML). The AI
exploration and browser execution live in the Node harness (installed with the UI runner tooling
under ~/.icx/testing/), invoked by the executor - not here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from icx_engine.testing.runners.base import RunSpec, register_runner


# -- Flow model (cached, deterministic replay) ---------------------------------

@dataclass
class UiStep:
    action: str                 # goto | click | fill | assert
    target: str = ""            # resolved selector (or URL for goto)
    value: str = ""             # e.g. text to type, or expected text for assert
    description: str = ""        # human-readable intent


@dataclass
class UiFlow:
    name: str
    url: str = ""
    steps: list[UiStep] = field(default_factory=list)
    authored: bool = False      # True once AI has resolved + cached the steps

    def to_dict(self) -> dict:
        return {"name": self.name, "url": self.url, "authored": self.authored,
                "steps": [asdict(s) for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict) -> "UiFlow":
        return cls(
            name=str(d.get("name", "")),
            url=str(d.get("url", "")),
            authored=bool(d.get("authored", False)),
            steps=[UiStep(**{k: s.get(k, "") for k in ("action", "target", "value", "description")})
                   for s in (d.get("steps") or [])],
        )


def _ui_cache_dir() -> Path:
    try:
        from icx_engine.graph.storage import temp_root
        d = temp_root().parent / "testing" / "ui-cache"
    except Exception:
        d = Path.home() / ".icx" / "testing" / "ui-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _flow_path(key: str) -> Path:
    from icx_engine.graph.storage import _normalize_issue_key
    try:
        safe = _normalize_issue_key(key)
    except Exception:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)[:80] or "flow"
    return _ui_cache_dir() / f"{safe}.json"


def save_flow(key: str, flow: UiFlow) -> str:
    p = _flow_path(key)
    tmp = p.parent / f"{p.name}.tmp"
    tmp.write_text(json.dumps(flow.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(p)
    return str(p)


def load_flow(key: str) -> UiFlow | None:
    p = _flow_path(key)
    if not p.exists():
        return None
    try:
        return UiFlow.from_dict(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def plan_ui_run(flow: UiFlow | None) -> str:
    """Return 'replay' when a cached authored flow with steps exists (deterministic, no LLM), else
    'author' (AI must explore + resolve first, human-gated)."""
    if flow is not None and flow.authored and flow.steps:
        return "replay"
    return "author"


# -- Adapter -------------------------------------------------------------------

_UI_FRAMEWORK_DEPS = ("react", "react-dom", "vue", "next", "svelte", "@angular/core",
                      "@sveltejs/kit", "solid-js", "preact")


def _pkg(repo: Path) -> dict:
    try:
        return json.loads((repo / "package.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


@dataclass
class _StagehandUi:
    lang: str = "ui"
    name: str = "stagehand"
    category: str = "ui"

    def detect(self, repo: Path) -> bool:
        pkg = _pkg(repo)
        deps = {}
        deps.update(pkg.get("dependencies") or {})
        deps.update(pkg.get("devDependencies") or {})
        return any(d in deps for d in _UI_FRAMEWORK_DEPS)

    def build_command(self, repo: Path, runtime_path: str | None) -> RunSpec:
        report = str(repo / ".icx-ui-junit.xml")
        node = runtime_path or "node"
        # The ICX Node harness runs Stagehand in REPLAY mode against a cached flow and emits JUnit
        # XML via Playwright. Harness + Node + Stagehand + Playwright are installed under
        # ~/.icx/testing/ (executor phase); the executor injects --flow <cache> and --url <target>.
        harness = str(Path.home() / ".icx" / "testing" / "stagehand" / "icx-replay.mjs")
        return RunSpec(
            command=[node, harness, "--mode", "replay", "--junit", report],
            cwd=str(repo), report_path=report,
            note=("AI authors the flow once (human-gated) then deterministic cached replay - no LLM "
                  "on rerun; a stale selector fails loud. Executor appends --flow <cache> --url <target>."),
        )


register_runner(_StagehandUi())
