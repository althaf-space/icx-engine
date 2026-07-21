from icx_engine.testing.benchmark.compare import competitor_rows


def test_competitor_rows_carry_source_and_tool():
    rows = competitor_rows()
    assert rows, "expected published competitor metrics"
    for r in rows:
        assert r["tool"] and r["metric"] and r["source"].startswith("http")
    tools = {r["tool"] for r in rows}
    assert {"BrowserStack", "Testim", "KaneAI"} <= tools
