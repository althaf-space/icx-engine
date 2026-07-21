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
    # goto|fill|fillunique|smartfill|select|multiselect|check|uncheck|click|dblclick|hover|press|
    # upload|draganddrop|scroll|type|setvalue|pickoption|waitfor|waithidden|assert|assertjs|assertgone|
    # a11y|perf|route|unroute|offline|download|confirmdialog|screenshot
    action: str = ""
    target: str = ""            # selector (URL for goto; JS expr for assertjs; source selector for draganddrop)
    value: str = ""             # text / option label(s) / key / file path / dest selector / expected text
    description: str = ""        # human-readable intent
    soft: bool = False           # a step that SKIPS (not fails) when it cannot run - graceful checks
                                 # (constraint probes on gated fields, dashboard data that may be empty)


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
            steps=[UiStep(action=str(s.get("action", "")), target=str(s.get("target", "")),
                          value=str(s.get("value", "")), description=str(s.get("description", "")),
                          soft=bool(s.get("soft", False)))
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


def flow_path(key: str) -> str:
    """Absolute path to the cached flow file for a key (used by the executor for replay)."""
    return str(_flow_path(key))


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

@dataclass
class _StagehandUi:
    lang: str = "ui"
    name: str = "stagehand"
    category: str = "ui"
    requires: str = "stagehand"   # ICX-owned Stagehand+Playwright tooling

    def detect(self, repo: Path) -> bool:
        # UI testing is URL-driven and repo-AGNOSTIC: ICX brings its own Playwright/Stagehand
        # (installed under ~/.icx/testing via ensure_runner) and drives a browser against the
        # running app at the confirmed URL. It never needs the user's repo to contain Playwright
        # or a frontend framework - so whenever the UI layer is requested, this runner is available.
        # The real gates are: the ICX tooling being installed (ensure_runner) and a confirmed URL.
        return True

    def build_command(self, repo: Path, runtime_path: str | None,
                      mode: str = "replay", report: str | None = None) -> RunSpec:
        report = report or str(repo / ".icx-ui-junit.xml")
        node = runtime_path or "node"
        # Packaged ICX harness runs Stagehand in REPLAY mode against a cached flow and emits JUnit XML
        # via Playwright. Stagehand+Playwright are installed by the runner-install manager; the
        # harness .mjs itself ships with ICX. The executor injects --flow <cache> and --url <target>.
        from icx_engine.testing.runners.install import (
            installed_path, browsers_dir, harness_path, runtime_harness_path,
        )
        # Run the harness FROM the install dir (next to node_modules) so ESM `import "playwright"`
        # resolves - NODE_PATH does not work for ESM imports.
        harness = runtime_harness_path("icx-replay.mjs", harness_path())
        env = {}
        try:
            sh = installed_path("stagehand")
            if sh:
                env["NODE_PATH"] = str(Path(sh) / "node_modules")
                # Point Playwright at the Chromium ICX downloaded into the pinned home.
                env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir(Path(sh)))
        except Exception:
            pass
        run_mode = "verify" if mode == "verify" else "replay"
        return RunSpec(
            command=[node, harness, "--mode", run_mode, "--junit", report],
            cwd=str(repo), report_path=report, env=env,
            note=("AI authors the flow once (human-gated) then deterministic cached replay - no LLM "
                  "on rerun; a stale selector fails loud. Executor appends --flow <cache>, --url "
                  "<target>, and --storage-state <session> when an authenticated session exists. "
                  "mode=verify probes every selector against the live DOM (heal report, no scoring)."),
        )


register_runner(_StagehandUi())
