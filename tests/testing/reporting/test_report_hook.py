import icx_engine.testing.nodes as nodes


async def test_node_local_run_writes_report(monkeypatch, tmp_path):
    monkeypatch.setenv("ICX_TEST_REPORTS_DIR", str(tmp_path))
    import icx_engine.testing.local_executor as _le

    async def _fake(repo, test_type, target_url=None, **kw):
        return {"ok": True, "test_type": test_type, "summary": {"total": 2, "passed": 2, "failures": 0, "skipped": 0},
                "reports": [], "cases": [("t1", "passed", 1.0), ("t2", "passed", 1.0)]}
    monkeypatch.setattr(_le, "run_local_verification", _fake)

    await nodes.node_local_run({"test_type": "unit", "file_paths": ["a.py"], "project": "P"})
    reports = list(tmp_path.glob("*.html"))
    assert any(p.name != "index.html" for p in reports)     # a session report was written
    assert (tmp_path / "index.html").exists()
