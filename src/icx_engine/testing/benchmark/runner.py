"""Live benchmark runner: for each corpus app, discover the census, build the deterministic flow, run
K scored replays, and score the metrics. Fully guarded - an unavailable app/tooling is skipped, never
raised, so the harness is safe to invoke in any environment."""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from icx_engine.testing.local_executor import run_ui_discovery, run_ui_replay
from icx_engine.testing.analyzers.to_flow import census_to_flow
from icx_engine.testing.benchmark.corpus import load_corpus, load_ground_truth
from icx_engine.testing.benchmark.metrics import build_run_metrics, cross_browser_pass, visual_summary, a11y_summary, dataflow_summary, RunMetrics


def _write_flow(url: str, steps: list) -> str:
    import json
    p = os.path.join(tempfile.gettempdir(), f".icx-bench-flow-{os.getpid()}-{id(steps)}.json")
    Path(p).write_text(json.dumps({"name": "bench", "url": url, "authored": True, "steps": steps}),
                       encoding="utf-8")
    return p


async def run_benchmark(apps=None, repeats: int = 2, storage_state: str | None = None, approve=None) -> list[RunMetrics]:
    """Run the benchmark across the corpus and return one RunMetrics per app that produced a census.
    `repeats` scored runs feed the flakiness metric. Apps that do not discover/replay are skipped.

    `approve`: optional `Callable[[str], bool]` forwarded to `ensure_browser` for a cross-browser
    target (ICX_UI_TARGETS) whose engine is not yet installed. None (default) leaves install
    approval to `ensure_browser`'s own env gate (ICX_AUTO_INSTALL_RUNNERS=1)."""
    corpus = apps if apps is not None else load_corpus()
    results: list[RunMetrics] = []
    repo = os.getcwd()
    for app in corpus:
        census = await run_ui_discovery(repo, app.url, storage_state=storage_state)
        if not isinstance(census, dict) or not census.get("functionalities"):
            continue
        try:
            steps = census_to_flow(census, app.url, True, "both")
            flow = _write_flow(app.url, steps)
            # single cleanup for `flow` covers BOTH the repeat loop and the cross-browser target
            # loop below - the target loop replays the same flow file and must find it intact.
            try:
                reports = []
                t0 = time.time()
                for _ in range(max(1, repeats)):
                    rep = await run_ui_replay(repo, flow, target_url=app.url, storage_state=storage_state)
                    if rep is not None:
                        reports.append(rep)
                seconds = time.time() - t0

                targets_spec = os.environ.get("ICX_UI_TARGETS", "")
                reports_by_target: dict = {}
                if targets_spec:
                    from icx_engine.testing.devices.device_backend import parse_targets, installed_engines
                    targets = parse_targets(targets_spec)
                    if targets:
                        avail = set(installed_engines())
                        for t in targets:
                            if t.engine not in avail:
                                installed = False
                                try:
                                    from icx_engine.testing.runners.install import ensure_browser
                                    installed = ensure_browser(t.engine, approve)
                                except Exception:
                                    installed = False        # never raise - an install failure just leaves the target unavailable
                                if installed:
                                    avail.add(t.engine)
                                else:
                                    reports_by_target[t.label()] = None      # still unavailable after install attempt -> omitted from metric
                                    continue
                            # mutates process env for the duration of each target replay - run_benchmark is
                            # therefore not safe to call concurrently (single-flow CLI use only).
                            prev_e, prev_d = os.environ.get("ICX_UI_ENGINE"), os.environ.get("ICX_UI_DEVICE")
                            os.environ["ICX_UI_ENGINE"] = t.engine
                            os.environ["ICX_UI_DEVICE"] = t.device
                            try:
                                reports_by_target[t.label()] = await run_ui_replay(
                                    repo, flow, target_url=app.url, storage_state=storage_state)
                            finally:
                                if prev_e is None:
                                    os.environ.pop("ICX_UI_ENGINE", None)
                                else:
                                    os.environ["ICX_UI_ENGINE"] = prev_e
                                if prev_d is None:
                                    os.environ.pop("ICX_UI_DEVICE", None)
                                else:
                                    os.environ["ICX_UI_DEVICE"] = prev_d
                xb = cross_browser_pass(reports_by_target)
                visual = visual_summary(reports[0]) if reports else {}
                a11y = a11y_summary(reports[0]) if reports else {}
                dataflow = dataflow_summary(reports[0]) if reports else {}

                gt = load_ground_truth(app)

                # SELF-HEAL PROBE: only when the app's ground truth declares `mutations` (a list of
                # selectors, or {"selector": ...} dicts) is there anything to measure - the default
                # (no mutations) leaves self_heal at {} so the scorecard section stays omitted.
                mutated_selectors: list[str] = []
                for m in (gt.get("mutations") or []) if isinstance(gt, dict) else []:
                    if isinstance(m, str) and m:
                        mutated_selectors.append(m)
                    elif isinstance(m, dict) and m.get("selector"):
                        mutated_selectors.append(str(m["selector"]))

                self_heal = None
                if mutated_selectors:
                    import json as _json
                    from icx_engine.testing.benchmark.heal_probe import read_heals, recovered_count
                    from icx_engine.testing.benchmark.metrics import self_heal_rate

                    # one extra replay with ICX_UI_MUTATE set: the harness breaks these selectors right
                    # after session-restore, so the earlier repeat runs' fingerprints (captured this same
                    # process, same tempdir flow) let the harness self-heal them on this pass.
                    prev_mutate = os.environ.get("ICX_UI_MUTATE")
                    os.environ["ICX_UI_MUTATE"] = _json.dumps(mutated_selectors)
                    try:
                        await run_ui_replay(repo, flow, target_url=app.url, storage_state=storage_state)
                    finally:
                        if prev_mutate is None:
                            os.environ.pop("ICX_UI_MUTATE", None)
                        else:
                            os.environ["ICX_UI_MUTATE"] = prev_mutate

                    heals = read_heals(flow)
                    recovered = recovered_count(heals, mutated_selectors)
                    self_heal = self_heal_rate(len(mutated_selectors), recovered)

                # authoring_actions = 0: the suite is auto-discovered with no human action.
                results.append(build_run_metrics(app.name, app.url, census, reports, seconds, 0, gt,
                                                  cross_browser=xb, self_heal=self_heal, visual=visual, a11y=a11y,
                                                  dataflow=dataflow))
            finally:
                try:
                    os.remove(flow)
                except OSError:
                    pass
                # the harness writes these sidecars beside the tempdir flow (heal fingerprints/log) -
                # clean them up alongside the flow itself so a benchmark run leaves no leak.
                stem = os.path.splitext(flow)[0]
                for suffix in (".heals.json", ".fingerprints.json"):
                    try:
                        os.remove(stem + suffix)
                    except OSError:
                        pass
                # visual-regression baselines land under <visualRoot>/<flowKey>/ (flowKey = the flow
                # file's basename, keyed by icx-replay.mjs). The flow here is an ephemeral temp file
                # unique per run, so its baseline dir would otherwise orphan forever - clean it up too.
                # Real user flows keep a stable name and are never touched by this cleanup.
                try:
                    import shutil
                    visual_root = Path(os.environ.get("ICX_VISUAL_DIR") or (Path.home() / ".icx" / "testing" / "visual"))
                    flow_key = Path(flow).stem
                    shutil.rmtree(visual_root / flow_key, ignore_errors=True)
                except Exception:
                    pass
        except Exception:
            # isolate one app's failure - a growing corpus must never abort the whole run.
            continue
    return results
