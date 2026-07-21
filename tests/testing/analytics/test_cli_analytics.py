import icx_engine.cli as cli


def test_analytics_command_runs(cli_runner, monkeypatch, tmp_path):
    db = tmp_path / "a.db"
    # seed a run via the store, then point the CLI at it via the env override
    from icx_engine.testing.analytics.store import AnalyticsStore, RunRecord
    s = AnalyticsStore(db)
    s.record_run(RunRecord("r1", "app", 1000.0, 2, 2, 0, 0, 3.0, 0), [("t1", "passed", 1.0)])
    s.close()
    monkeypatch.setenv("ICX_ANALYTICS_DB", str(db))
    out = tmp_path / "dash.html"
    result = cli_runner.invoke(cli.app, ["test", "analytics", "--out", str(out)])
    assert result.exit_code == 0 and out.exists()
