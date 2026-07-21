import json
import os
from pathlib import Path
from icx_engine.testing.benchmark.metrics import self_heal_rate, RunMetrics, CoverageScore
from icx_engine.testing.benchmark.heal_probe import read_heals, recovered_count
import icx_engine.testing.benchmark.runner as R
from icx_engine.testing.benchmark.corpus import BenchmarkApp
from icx_engine.testing.runners.junit import parse_junit_xml


def test_self_heal_rate():
    assert self_heal_rate(4, 3) == {"injected": 4, "recovered": 3, "rate": 0.75}
    assert self_heal_rate(0, 0) == {"injected": 0, "recovered": 0, "rate": 0.0}


def test_read_heals_and_recovered(tmp_path):
    flow = tmp_path / "flow.json"
    (tmp_path / "flow.heals.json").write_text(
        json.dumps([{"old": "#save-btn", "new": "#submit", "score": 0.9},
                    {"old": "#name", "new": "#name2", "score": 0.8}]), encoding="utf-8")
    heals = read_heals(str(flow))
    assert len(heals) == 2
    # 2 selectors were mutated; both were healed -> recovered 2
    assert recovered_count(heals, ["#save-btn", "#name"]) == 2
    # a mutation not healed -> not counted
    assert recovered_count(heals, ["#save-btn", "#never"]) == 1


def test_run_metrics_has_self_heal_field():
    m = RunMetrics(app="x", url="u", coverage=CoverageScore(1, 1, 1, 1, 1),
                   misfire_rate=0.0, flakiness=0.0, speed_seconds=1.0, authoring_actions=0,
                   total_tests=1, real_findings=0, self_heal={"injected": 2, "recovered": 2, "rate": 1.0})
    assert m.self_heal["rate"] == 1.0


async def test_run_benchmark_populates_self_heal_metric(monkeypatch):
    """Integration test for the metric-wiring fix: ground truth's `mutations` list must drive one
    extra probe replay with ICX_UI_MUTATE set, and the resulting <flow>.heals.json must be scored
    into RunMetrics.self_heal - proving the wiring, not just the pure scoring functions above."""
    census = {"functionalities": [{"functionality": "Search"}]}

    async def _disc(repo, url, **kw):
        return census

    async def _replay(repo, flow_path, **kw):
        if os.environ.get("ICX_UI_MUTATE"):
            # simulate the harness healing the mutated selector during this probe pass. old must match
            # the corpus fixture's ground_truth "mutations" entry (magik_ui/users.json: "#createBtn").
            stem = os.path.splitext(flow_path)[0]
            heals = [{"old": "#createBtn", "new": "#createBtn-fresh", "score": 0.9}]
            with open(stem + ".heals.json", "w", encoding="utf-8") as f:
                json.dump(heals, f)
            return None
        return parse_junit_xml('<testsuite><testcase name="a"/></testsuite>')

    monkeypatch.setattr(R, "run_ui_discovery", _disc)
    monkeypatch.setattr(R, "run_ui_replay", _replay)
    monkeypatch.setattr(R, "census_to_flow", lambda *a, **k: [{"action": "goto", "target": "u"}])

    metrics = await R.run_benchmark(repeats=1)
    assert metrics, "expected at least the demo app"
    sh = metrics[0].self_heal
    assert sh.get("injected", 0) > 0
    assert sh.get("recovered", 0) > 0


async def test_run_benchmark_leaves_self_heal_empty_without_mutations(monkeypatch):
    """No `mutations` in ground truth (the default/unchanged path) -> self_heal stays {}, never
    fabricated."""
    census = {"functionalities": [{"functionality": "Search"}]}

    async def _disc(repo, url, **kw):
        return census

    async def _replay(repo, flow_path, **kw):
        return parse_junit_xml('<testsuite><testcase name="a"/></testsuite>')

    monkeypatch.setattr(R, "run_ui_discovery", _disc)
    monkeypatch.setattr(R, "run_ui_replay", _replay)
    monkeypatch.setattr(R, "census_to_flow", lambda *a, **k: [{"action": "goto", "target": "u"}])

    app = BenchmarkApp(name="no_mut", url="http://x", login="", ground_truth=None)
    metrics = await R.run_benchmark(apps=[app], repeats=1)
    assert metrics
    assert metrics[0].self_heal == {}
