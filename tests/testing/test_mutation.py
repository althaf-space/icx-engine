"""Tests for the mutation-testing filter (rejects AI-draft tests that verify nothing)."""
from pathlib import Path

from icx_engine.testing.mutation import (
    select_mutation_tool, build_mutation_command,
    parse_mutmut, parse_stryker, parse_pit,
    evaluate_mutation, MutationResult, DEFAULT_MIN_MUTATION_SCORE,
)


def test_tool_selection():
    assert select_mutation_tool("python") == "mutmut"
    assert select_mutation_tool("js-ts") == "stryker"
    assert select_mutation_tool("java") == "pit"
    assert select_mutation_tool("kotlin") == "pit"
    assert select_mutation_tool("php") == "infection"
    assert select_mutation_tool("cobol") is None


def test_build_command_python(tmp_path):
    spec = build_mutation_command("python", tmp_path, None, target="src/foo.py")
    assert spec is not None
    assert spec.command[:2] == ["mutmut", "run"]
    assert "src/foo.py" in spec.command


def test_build_command_pit_sets_java_home(tmp_path):
    spec = build_mutation_command("java", tmp_path, "/opt/jdk17")
    assert spec.env.get("JAVA_HOME") == "/opt/jdk17"
    assert spec.report_path.endswith("mutations.xml")


def test_build_command_unsupported():
    assert build_mutation_command("cobol", "/x", None) is None


def test_build_command_stryker_report_path_is_pathlib_joined(tmp_path):
    spec = build_mutation_command("javascript", tmp_path, None)
    assert spec is not None
    assert Path(spec.report_path) == tmp_path / "reports" / "mutation" / "mutation-report.json"


def test_build_command_infection_report_path_is_pathlib_joined(tmp_path):
    spec = build_mutation_command("php", tmp_path, None)
    assert spec is not None
    assert Path(spec.report_path) == tmp_path / "infection.json"


def test_parse_mutmut():
    r = parse_mutmut("killed: 8, survived: 2")
    assert r.killed == 8 and r.survived == 2 and r.total == 10
    assert r.score == 0.8 and r.meaningful is True


def test_parse_stryker_json():
    data = '''{"files": {"a.ts": {"mutants": [
        {"status": "Killed"}, {"status": "Killed"}, {"status": "Survived"}]}}}'''
    r = parse_stryker(data)
    assert r.killed == 2 and r.survived == 1 and r.total == 3


def test_parse_pit_xml():
    xml = ('<mutations>'
           '<mutation detected="true" status="KILLED"/>'
           '<mutation detected="false" status="SURVIVED"/>'
           '<mutation detected="false" status="NO_COVERAGE"/>'
           '</mutations>')
    r = parse_pit(xml)
    assert r.killed == 1 and r.survived == 2 and r.total == 3


def test_parse_malformed_is_empty():
    assert parse_stryker("not json").total == 0
    assert parse_pit("<bad").total == 0


def test_evaluate_rejects_zero_killed():
    r = MutationResult(tool="mutmut", total=5, killed=0, survived=5)
    passed, reason = evaluate_mutation(r)
    assert passed is False and "verifies nothing" in reason


def test_evaluate_rejects_below_min_score():
    r = MutationResult(tool="mutmut", total=10, killed=3, survived=7)  # 0.3 < 0.6
    passed, reason = evaluate_mutation(r)
    assert passed is False and "below minimum" in reason


def test_evaluate_passes_above_min_score():
    r = MutationResult(tool="mutmut", total=10, killed=8, survived=2)  # 0.8
    passed, reason = evaluate_mutation(r)
    assert passed is True and "0.8" in reason


def test_evaluate_no_mutants():
    passed, reason = evaluate_mutation(MutationResult(tool="pit", total=0))
    assert passed is False and "no mutants" in reason


def test_evaluate_custom_min_score():
    r = MutationResult(tool="mutmut", total=10, killed=3, survived=7)  # 0.3
    passed, _ = evaluate_mutation(r, min_score=0.2)
    assert passed is True
