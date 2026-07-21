import icx_engine.cli as cli


def test_benchmark_command_registered_and_runs(cli_runner, monkeypatch, tmp_path):
    from icx_engine.testing.benchmark.metrics import RunMetrics, CoverageScore

    async def _fake_run(**kw):
        return [RunMetrics("magik_ui", "http://x", CoverageScore(1.0, 0.9, 9, 10, 9),
                           0.0, 0.0, 5.5, 0, 25, 1)]
    monkeypatch.setattr("icx_engine.testing.benchmark.runner.run_benchmark", _fake_run)

    out = tmp_path / "score.html"
    result = cli_runner.invoke(cli.app, ["test", "benchmark", "--repeats", "1", "--out", str(out)])
    assert result.exit_code == 0
    assert out.exists()
    assert "magik_ui" in result.stdout
