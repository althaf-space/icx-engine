import icx_engine.testing.benchmark.runner as R
from icx_engine.testing.runners.junit import parse_junit_xml


async def test_run_benchmark_uses_pipeline_and_scores(monkeypatch, tmp_path):
    census = {"functionalities": [
        {"functionality": "Create User", "fields": [{"label": "first Name"}]},
        {"functionality": "Search"}]}

    async def _disc(repo, url, **kw):
        return census

    async def _replay(repo, flow_path, **kw):
        return parse_junit_xml('<testsuite><testcase name="RENDER: x"/>'
                               '<testcase name="ACCESSIBILITY: audit"><failure>no alt</failure></testcase></testsuite>')

    monkeypatch.setattr(R, "run_ui_discovery", _disc)
    monkeypatch.setattr(R, "run_ui_replay", _replay)
    monkeypatch.setattr(R, "census_to_flow", lambda *a, **k: [{"action": "goto", "target": "u"}])

    metrics = await R.run_benchmark(repeats=2)
    assert metrics, "expected at least the demo app"
    m = metrics[0]
    assert m.authoring_actions == 0
    assert m.coverage.recall > 0
    assert m.real_findings == 1          # the a11y failure counts as a real finding


async def test_run_benchmark_skips_app_when_discovery_none(monkeypatch):
    async def _none(repo, url, **kw):
        return None
    monkeypatch.setattr(R, "run_ui_discovery", _none)
    metrics = await R.run_benchmark(repeats=1)
    assert metrics == []                 # nothing discovered -> app skipped, no raise
