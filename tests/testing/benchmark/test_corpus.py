from icx_engine.testing.benchmark.corpus import load_corpus, load_ground_truth, BenchmarkApp


def test_corpus_lists_demo_app():
    apps = load_corpus()
    names = [a.name for a in apps]
    assert "magik_ui" in names
    app = next(a for a in apps if a.name == "magik_ui")
    assert app.url.startswith("http")


def test_load_ground_truth_reads_elements():
    app = next(a for a in load_corpus() if a.name == "magik_ui")
    gt = load_ground_truth(app)
    labels = [e["label"] for e in gt.get("elements", [])]
    assert "Create User" in labels


def test_load_ground_truth_missing_returns_empty():
    bogus = BenchmarkApp(name="x", url="http://x", login="", ground_truth=None)
    assert load_ground_truth(bogus) == {}
