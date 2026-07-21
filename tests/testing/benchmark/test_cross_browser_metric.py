import os

import icx_engine.testing.benchmark.runner as R
import icx_engine.testing.devices.device_backend as device_backend
from icx_engine.testing.runners.junit import parse_junit_xml
from icx_engine.testing.benchmark.metrics import cross_browser_pass, RunMetrics, CoverageScore


def test_cross_browser_pass_ratio_per_target():
    good = parse_junit_xml('<testsuite><testcase name="a"/><testcase name="b"/></testsuite>')
    half = parse_junit_xml('<testsuite><testcase name="a"/><testcase name="b"><failure>x</failure></testcase></testsuite>')
    out = cross_browser_pass({"chromium": good, "firefox": half})
    assert out["chromium"] == 1.0 and out["firefox"] == 0.5


def test_cross_browser_pass_empty():
    assert cross_browser_pass({}) == {}


def test_run_metrics_has_cross_browser_field():
    m = RunMetrics(app="x", url="u", coverage=CoverageScore(1, 1, 1, 1, 1),
                   misfire_rate=0.0, flakiness=0.0, speed_seconds=1.0, authoring_actions=0,
                   total_tests=1, real_findings=0, cross_browser={"chromium": 1.0})
    assert m.cross_browser["chromium"] == 1.0


async def test_run_benchmark_populates_cross_browser_metric(monkeypatch):
    """Integration test for the ordering bug: the flow file written before the repeat loop must still
    exist when the cross-browser target loop replays it. A regression (flow removed after the repeat
    loop instead of after both loops) makes the fake replay's exists-check below fail."""
    census = {"functionalities": [{"functionality": "Search"}]}

    async def _disc(repo, url, **kw):
        return census

    async def _replay(repo, flow_path, **kw):
        assert os.path.exists(flow_path), "flow file missing - cleanup ran before the target loop"
        with open(flow_path, encoding="utf-8") as f:
            f.read()
        return parse_junit_xml('<testsuite><testcase name="a"/><testcase name="b"/></testsuite>')

    monkeypatch.setattr(R, "run_ui_discovery", _disc)
    monkeypatch.setattr(R, "run_ui_replay", _replay)
    monkeypatch.setattr(R, "census_to_flow", lambda *a, **k: [{"action": "goto", "target": "u"}])
    monkeypatch.setattr(device_backend, "installed_engines", lambda *a, **k: ["chromium", "firefox"])

    had_prev = "ICX_UI_TARGETS" in os.environ
    prev = os.environ.get("ICX_UI_TARGETS")
    os.environ["ICX_UI_TARGETS"] = "chromium,firefox"
    try:
        metrics = await R.run_benchmark(repeats=1)
    finally:
        if had_prev:
            os.environ["ICX_UI_TARGETS"] = prev
        else:
            os.environ.pop("ICX_UI_TARGETS", None)

    assert metrics, "expected at least the demo app"
    xb = metrics[0].cross_browser
    assert xb.get("chromium") == 1.0
    assert xb.get("firefox") == 1.0


async def test_run_benchmark_installs_missing_engine_via_ensure_browser(monkeypatch):
    """installed_engines() reports only chromium; firefox is missing. run_benchmark must attempt
    ensure_browser(engine, approve) for firefox and, on True, still run and report it - proving the
    install wiring, not just the pre-installed path covered above."""
    import icx_engine.testing.runners.install as install_mod

    census = {"functionalities": [{"functionality": "Search"}]}

    async def _disc(repo, url, **kw):
        return census

    async def _replay(repo, flow_path, **kw):
        return parse_junit_xml('<testsuite><testcase name="a"/></testsuite>')

    def _ensure_browser(engine, approve=None):
        assert engine == "firefox"
        return True

    monkeypatch.setattr(R, "run_ui_discovery", _disc)
    monkeypatch.setattr(R, "run_ui_replay", _replay)
    monkeypatch.setattr(R, "census_to_flow", lambda *a, **k: [{"action": "goto", "target": "u"}])
    monkeypatch.setattr(device_backend, "installed_engines", lambda *a, **k: ["chromium"])
    monkeypatch.setattr(install_mod, "ensure_browser", _ensure_browser)

    had_prev = "ICX_UI_TARGETS" in os.environ
    prev = os.environ.get("ICX_UI_TARGETS")
    os.environ["ICX_UI_TARGETS"] = "chromium,firefox"
    try:
        metrics = await R.run_benchmark(repeats=1)
    finally:
        if had_prev:
            os.environ["ICX_UI_TARGETS"] = prev
        else:
            os.environ.pop("ICX_UI_TARGETS", None)

    assert metrics, "expected at least the demo app"
    xb = metrics[0].cross_browser
    assert xb.get("firefox") == 1.0
