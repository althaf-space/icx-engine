from icx_engine.testing import screen_cache as sc


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_seed_key_is_order_independent():
    assert sc.seed_key(["a.jsx", "b.jsx"]) == sc.seed_key(["b.jsx", "a.jsx"])


def test_save_and_load_round_trip(tmp_path):
    store = tmp_path / "screens.json"
    f1 = _write(tmp_path, "Users.jsx", "export default function Users(){}")
    seeds = [f1]
    sc.save_screen("proj1", seeds, test_type="agent", url="http://x/users",
                   all_candidates=[f1, str(tmp_path / "Other.jsx")], confirmed_files=[f1],
                   screen_model={"functionalities": [{"functionality": "Create"}]},
                   census_coverage=1.0, analyzer_id="react", analyzer_family="ui",
                   compat_resolution={f1: "manual"}, store=store)
    entry = sc.load_screen("proj1", seeds, store=store)
    assert entry is not None
    assert entry.confirmed_files == [f1]
    assert entry.screen_model == {"functionalities": [{"functionality": "Create"}]}
    assert entry.census_coverage == 1.0
    assert entry.analyzer_id == "react"
    assert entry.compat_resolution == {f1: "manual"}
    assert f1 in entry.file_hashes


def test_load_screen_none_when_not_cached(tmp_path):
    store = tmp_path / "screens.json"
    assert sc.load_screen("proj1", ["a.jsx"], store=store) is None


def test_load_screen_none_on_different_seeds(tmp_path):
    store = tmp_path / "screens.json"
    f1 = _write(tmp_path, "Users.jsx", "x")
    sc.save_screen("proj1", [f1], test_type="agent", url=None, all_candidates=[f1],
                   confirmed_files=[f1], screen_model={}, census_coverage=1.0,
                   analyzer_id=None, analyzer_family=None, store=store)
    assert sc.load_screen("proj1", ["Other.jsx"], store=store) is None


def test_clear_screen_removes_entry(tmp_path):
    store = tmp_path / "screens.json"
    f1 = _write(tmp_path, "Users.jsx", "x")
    sc.save_screen("proj1", [f1], test_type="agent", url=None, all_candidates=[f1],
                   confirmed_files=[f1], screen_model={}, census_coverage=1.0,
                   analyzer_id=None, analyzer_family=None, store=store)
    sc.clear_screen("proj1", [f1], store=store)
    assert sc.load_screen("proj1", [f1], store=store) is None


def test_freshness_true_when_files_unchanged(tmp_path):
    store = tmp_path / "screens.json"
    f1 = _write(tmp_path, "Users.jsx", "unchanged content")
    sc.save_screen("proj1", [f1], test_type="agent", url=None, all_candidates=[f1],
                   confirmed_files=[f1], screen_model={}, census_coverage=1.0,
                   analyzer_id=None, analyzer_family=None, store=store)
    entry = sc.load_screen("proj1", [f1], store=store)
    fresh, changed = sc.freshness(entry)
    assert fresh is True and changed == []


def test_freshness_false_when_a_cached_file_changes(tmp_path):
    store = tmp_path / "screens.json"
    f1 = _write(tmp_path, "Users.jsx", "original content")
    sc.save_screen("proj1", [f1], test_type="agent", url=None, all_candidates=[f1],
                   confirmed_files=[f1], screen_model={}, census_coverage=1.0,
                   analyzer_id=None, analyzer_family=None, store=store)
    entry = sc.load_screen("proj1", [f1], store=store)
    # simulate the file being edited after caching
    from pathlib import Path
    Path(f1).write_text("edited content", encoding="utf-8")
    fresh, changed = sc.freshness(entry)
    assert fresh is False and changed == [f1]


def test_freshness_false_when_a_cached_file_is_deleted(tmp_path):
    store = tmp_path / "screens.json"
    f1 = _write(tmp_path, "Users.jsx", "content")
    sc.save_screen("proj1", [f1], test_type="agent", url=None, all_candidates=[f1],
                   confirmed_files=[f1], screen_model={}, census_coverage=1.0,
                   analyzer_id=None, analyzer_family=None, store=store)
    entry = sc.load_screen("proj1", [f1], store=store)
    from pathlib import Path
    Path(f1).unlink()
    fresh, changed = sc.freshness(entry)
    assert fresh is False and changed == [f1]


def test_all_candidates_preserved_separately_from_confirmed(tmp_path):
    # a deliberately-excluded file must stay recorded in all_candidates so a future re-discovery
    # of the SAME file is never mistaken for a "new file appeared".
    store = tmp_path / "screens.json"
    f1 = _write(tmp_path, "Users.jsx", "x")
    excluded = str(tmp_path / "Reports.jsx")
    sc.save_screen("proj1", [f1], test_type="agent", url=None,
                   all_candidates=[f1, excluded], confirmed_files=[f1],
                   screen_model={}, census_coverage=1.0, analyzer_id=None, analyzer_family=None,
                   store=store)
    entry = sc.load_screen("proj1", [f1], store=store)
    assert excluded in entry.all_candidates
    assert excluded not in entry.confirmed_files
