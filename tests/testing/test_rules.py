import pytest

from icx_engine.testing import rules as _rules
from icx_engine.testing import nodes
from icx_engine.testing.state import make_initial_state


# rules_dir is redirected to a temp path by the autouse fixture in conftest.py.

def test_ensure_seeded_copies_defaults():
    _rules.ensure_seeded()
    d = _rules.rules_dir()
    names = {f.name for f in d.glob("*.md")}
    assert {"_common.md", "2b.md", "compat_scan.md", "compat_check.md",
            "expand_scan.md"} <= names


def test_ensure_seeded_never_overwrites_user_edits():
    _rules.ensure_seeded()
    f = _rules.rules_dir() / "compat_scan.md"
    f.write_text("MY CUSTOM RULE", encoding="utf-8")
    _rules.ensure_seeded()  # second call must not clobber
    assert f.read_text(encoding="utf-8") == "MY CUSTOM RULE"


def test_refresh_stale_seeds_missing_files():
    outcome = _rules.refresh_stale()
    assert "compat_scan.md" in outcome["seeded"]
    assert (_rules.rules_dir() / "compat_scan.md").exists()


def test_refresh_stale_leaves_edited_file_alone():
    _rules.ensure_seeded()
    f = _rules.rules_dir() / "compat_scan.md"
    f.write_text("MY CUSTOM RULE", encoding="utf-8")   # edited after seeding, no re-marking
    outcome = _rules.refresh_stale()
    assert "compat_scan.md" in outcome["skipped"]
    assert f.read_text(encoding="utf-8") == "MY CUSTOM RULE"


def test_refresh_stale_reports_up_to_date_when_unchanged():
    _rules.ensure_seeded()
    outcome = _rules.refresh_stale()
    assert "compat_scan.md" in outcome["up_to_date"]
    assert "compat_scan.md" not in outcome["refreshed"]


def test_refresh_stale_picks_up_a_bundled_update_on_untouched_file():
    # simulate: user has an untouched, pristine-tracked copy from an OLDER bundled version;
    # the bundled default then changes (a real rule fix ships) - refresh_stale must pick it up.
    real_default_text = (_rules._DEFAULTS_DIR / "compat_scan.md").read_text(encoding="utf-8")

    # emulate the "older bundled content" by writing it as what the user has (still pristine-marked
    # to itself), so refresh_stale sees: current == old marker, but bundled has since moved on.
    old_content = "# an older bundled compat_scan.md\nold rule text\n"
    f = _rules.rules_dir() / "compat_scan.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(old_content, encoding="utf-8")
    _rules._write_pristine_marker("compat_scan.md", _rules._hash_text(old_content))

    outcome = _rules.refresh_stale()
    assert "compat_scan.md" in outcome["refreshed"]
    assert f.read_text(encoding="utf-8") == real_default_text


def test_refresh_stale_leaves_untracked_existing_file_alone():
    # a file present with no pristine marker at all (e.g. an install from before this mechanism
    # existed) must be conservatively treated as customized, not silently overwritten.
    f = _rules.rules_dir()
    f.mkdir(parents=True, exist_ok=True)
    (f / "compat_scan.md").write_text("some pre-existing content, no marker", encoding="utf-8")
    outcome = _rules.refresh_stale()
    assert "compat_scan.md" in outcome["skipped"]
    assert (f / "compat_scan.md").read_text(encoding="utf-8") == "some pre-existing content, no marker"


def test_load_gate_rules_includes_common_and_specific():
    text = _rules.load_gate_rules("compat_scan")
    assert "COMMON" in text                       # from _common.md
    assert "testability assessment" in text       # from compat_scan.md


def test_compat_scan_rules_forbid_shallow_undefined_check():
    # REGRESSION: the shallow-undefined-check fix was added to the hardcoded _COMPAT_MANDATE in
    # nodes.py but NOT to this durable, user-editable rulebook file - the one the RULEBOOK RULE
    # tells the agent is binding. Both must carry it.
    text = _rules.load_gate_rules("compat_scan")
    assert "index.html" in text and "script src" in text
    assert "grep for it" in text.lower()


def test_load_gate_rules_falls_back_to_bundled_when_deleted():
    _rules.ensure_seeded()
    (_rules.rules_dir() / "2b.md").unlink()
    text = _rules.load_gate_rules("2b")
    assert "REQUIRED_SECTIONS" in text or "test spec" in text  # bundled default served


def _complete_2b_spec():
    """A spec satisfying every 2b marker: non-empty REQUIRED_SECTIONS, present-but-empty
    REQUIRED_PRESENT, and one functionality/field carrying all nested required keys."""
    obj = {k: ["x"] for k in _rules.required_sections("2b")}
    for k in _rules.required_present("2b"):
        obj[k] = []                       # present, legitimately empty
    fld = {k: "x" for k in _rules.required_per_field("2b")}
    fn = {k: "x" for k in _rules.required_per_functionality("2b")}
    fn["fields"] = [fld]
    obj["functionalities"] = [fn]         # non-empty, fully-keyed
    return obj


def test_required_sections_parses_marker():
    req = _rules.required_sections("2b")
    assert "functionalities" in req and "selectorAudit" in req


def test_required_present_and_nested_markers_parse():
    assert "modalFiles" in _rules.required_present("2b")
    assert "businessLogic" in _rules.required_per_functionality("2b")
    assert "interactionPattern" in _rules.required_per_field("2b")


def test_missing_sections_flags_absent_and_empty():
    spec = _complete_2b_spec()
    spec["techStack"] = []       # empty -> missing
    del spec["selectorAudit"]    # absent -> missing
    missing = _rules.missing_sections("2b", spec)
    assert "techStack" in missing and "selectorAudit" in missing


def test_missing_sections_complete_spec_is_empty():
    assert _rules.missing_sections("2b", _complete_2b_spec()) == []


def test_required_present_absent_is_flagged_empty_is_ok():
    spec = _complete_2b_spec()
    assert _rules.missing_sections("2b", spec) == []      # modalFiles=[] is fine
    del spec["modalFiles"]
    assert "modalFiles" in _rules.missing_sections("2b", spec)


def test_missing_sections_flags_nested_per_functionality():
    spec = _complete_2b_spec()
    del spec["functionalities"][0]["businessLogic"]
    missing = _rules.missing_sections("2b", spec)
    assert "functionalities[0].businessLogic" in missing


def test_missing_sections_flags_nested_per_field():
    spec = _complete_2b_spec()
    del spec["functionalities"][0]["fields"][0]["interactionPattern"]
    missing = _rules.missing_sections("2b", spec)
    assert "functionalities[0].fields[0].interactionPattern" in missing


def test_missing_sections_accepts_json_string():
    import json
    assert _rules.missing_sections("2b", json.dumps(_complete_2b_spec())) == []


def test_missing_sections_unparseable_counts_all():
    result = _rules.missing_sections("2b", "not json")
    assert "functionalities" in result and "techStack" in result


# --- gate 2b presence-enforcement loop -------------------------------------

def test_run_gate_2b_reasks_until_complete(monkeypatch):
    complete = _complete_2b_spec()
    seq = [{"json_spec": {"functionalities": [1]}}, {"json_spec": complete}]
    box = {"n": 0}
    def fake(p):
        assert p["gate"] == "2b" and p["rules"] and p["rules_path"]
        r = seq[box["n"]]; box["n"] += 1; return r
    monkeypatch.setattr(nodes, "interrupt", fake)
    resp, missing = nodes._run_gate_2b({"instruction": "x", "file_paths": ["a"]})
    assert box["n"] == 2
    assert missing == []


def test_run_gate_2b_accept_incomplete_breaks(monkeypatch):
    def fake(p):
        return {"json_spec": {"functionalities": [1]}, "accept_incomplete": True}
    monkeypatch.setattr(nodes, "interrupt", fake)
    resp, missing = nodes._run_gate_2b({"instruction": "x"})
    assert missing == []   # user knowingly accepted


def test_run_gate_2b_is_bounded_and_reports_missing(monkeypatch):
    calls = {"n": 0}
    def fake(p):
        calls["n"] += 1
        return {"json_spec": {"functionalities": [1]}}  # always incomplete
    monkeypatch.setattr(nodes, "interrupt", fake)
    resp, missing = nodes._run_gate_2b({"instruction": "x"})
    assert calls["n"] == nodes._SPEC_MAX_REASK + 1   # bounded, never hangs
    assert missing                                    # surfaced, not silently submitted


def test_run_gate_2b_second_ask_names_missing(monkeypatch):
    seen = {}
    seq = [{"json_spec": {"functionalities": [1]}}, {"json_spec": _complete_2b_spec()}]
    box = {"n": 0}
    def fake(p):
        seen[box["n"]] = p
        r = seq[box["n"]]; box["n"] += 1; return r
    monkeypatch.setattr(nodes, "interrupt", fake)
    nodes._run_gate_2b({"instruction": "x"})
    # the re-ask payload names the missing sections
    assert "missing_sections" in seen[1]
    assert "INCOMPLETE" in seen[1]["instruction"]


# --- rules are injected into gate payloads ---------------------------------

@pytest.mark.asyncio
async def test_compat_scan_payload_carries_rules(monkeypatch):
    captured = {}
    def fake(p):
        captured.update(p)
        return {"all_compatible": True, "findings": [{"path": "a.tsx", "compatible": True}]}
    monkeypatch.setattr(nodes, "interrupt", fake)
    s = make_initial_state(file_paths=["a.tsx"], test_mode="automated")
    s["test_type"] = "agent"
    await nodes.node_compat_scan(s)
    assert captured.get("rules") and "testability assessment" in captured["rules"]
    assert captured.get("rules_path", "").endswith("compat_scan.md")
