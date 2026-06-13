"""Tests for Phase 14: CODEOWNERS ownership integration."""
import json
import tempfile
from pathlib import Path
import pytest

from icx_engine.graph.parser.ownership import (
    _parse_codeowners,
    find_owners,
    load_codeowners,
    _match_pattern,
)
from icx_engine.graph.query import GraphQuerier


class TestParseCODEOWNERS:
    def test_simple_pattern(self):
        text = "*.py @python-team\n/src/ @core-team @alice"
        rules = _parse_codeowners(text)
        assert len(rules) == 2
        assert rules[0] == ("*.py", ["@python-team"])
        assert rules[1] == ("/src/", ["@core-team", "@alice"])

    def test_comments_and_blanks_skipped(self):
        text = "# This is a comment\n\n*.py @team\n# another comment"
        rules = _parse_codeowners(text)
        assert len(rules) == 1

    def test_line_without_owners_skipped(self):
        text = "*.py\n/src/ @team"
        rules = _parse_codeowners(text)
        assert len(rules) == 1
        assert rules[0][0] == "/src/"


class TestFindOwners:
    def test_last_match_wins(self):
        rules = [
            ("*.py", ["@general"]),
            ("/src/auth/*.py", ["@security"]),
        ]
        owners = find_owners("src/auth/token.py", rules)
        assert owners == ["@security"]

    def test_no_match_returns_empty(self):
        rules = [("*.py", ["@python-team"])]
        owners = find_owners("README.md", rules)
        assert owners == []

    def test_wildcard_matches(self):
        rules = [("*.py", ["@python-team"])]
        owners = find_owners("src/models/user.py", rules)
        assert "@python-team" in owners

    def test_directory_pattern(self):
        rules = [("src/auth/", ["@security-team"])]
        owners = find_owners("src/auth/login.py", rules)
        assert "@security-team" in owners


class TestMatchPatternDirectory:
    def test_nested_dir_pattern_matches_anywhere(self):
        assert _match_pattern("src/auth/", "app/src/auth/token.py") is True

    def test_no_false_positive_on_substring(self):
        assert _match_pattern("lib/", "app/oldlib/utils.py") is False

    def test_dir_pattern_matches_at_root(self):
        assert _match_pattern("lib/", "app/lib/utils.py") is True


class TestLoadCODEOWNERS:
    def test_loads_from_project_root(self, tmp_path):
        codeowners = tmp_path / "CODEOWNERS"
        codeowners.write_text("*.py @python-team\n")
        rules = load_codeowners(str(tmp_path))
        assert len(rules) == 1

    def test_loads_from_github_dir(self, tmp_path):
        (tmp_path / ".github").mkdir()
        codeowners = tmp_path / ".github" / "CODEOWNERS"
        codeowners.write_text("*.go @go-team\n")
        rules = load_codeowners(str(tmp_path))
        assert len(rules) == 1

    def test_returns_empty_when_no_codeowners(self, tmp_path):
        rules = load_codeowners(str(tmp_path))
        assert rules == []


class TestGetOwnership:
    def _build_querier(self, nodes, links):
        graph = {"nodes": nodes, "links": links}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(graph, f)
        return GraphQuerier(Path(f.name))

    def test_returns_no_codeowners_when_missing(self, tmp_path):
        nodes = [{"id": "na", "label": "a", "source_file": "a.py"}]
        q = self._build_querier(nodes, [])
        result = q.get_ownership("a.py", str(tmp_path))
        assert result["codeowners_found"] is False
        assert result["owners"] == []

    def test_returns_owners_when_codeowners_present(self, tmp_path):
        (tmp_path / "CODEOWNERS").write_text("*.py @python-team\n")
        nodes = [{"id": "na", "label": "a", "source_file": "src/a.py"}]
        q = self._build_querier(nodes, [])
        result = q.get_ownership("src/a.py", str(tmp_path))
        assert result["codeowners_found"] is True
        assert "@python-team" in result["owners"]
