from pathlib import Path

from icx_engine.graph.parser.icxignore import IcxIgnore, _parse_patterns


class TestNegationParsing:
    def test_parse_patterns_keeps_negation_lines(self):
        text = "vendor/\n!vendor/internal/\n# comment\n*.log\n!important.log\n"
        patterns = _parse_patterns(text)
        assert patterns == ["vendor/", "!vendor/internal/", "*.log", "!important.log"]


class TestNegationMatching:
    def test_negated_subdirectory_is_not_excluded(self, tmp_path):
        ignore = IcxIgnore(["vendor/", "!vendor/internal/"], tmp_path)
        excluded_dir = tmp_path / "vendor" / "thirdparty"
        reincluded_dir = tmp_path / "vendor" / "internal"
        assert ignore.matches(excluded_dir, is_dir=True) is True
        assert ignore.matches(reincluded_dir, is_dir=True) is False

    def test_negated_subdirectory_file_is_not_excluded(self, tmp_path):
        ignore = IcxIgnore(["vendor/", "!vendor/internal/"], tmp_path)
        excluded_file = tmp_path / "vendor" / "thirdparty" / "lib.go"
        reincluded_file = tmp_path / "vendor" / "internal" / "tool.go"
        assert ignore.matches(excluded_file) is True
        assert ignore.matches(reincluded_file) is False

    def test_negated_single_file(self, tmp_path):
        ignore = IcxIgnore(["*.log", "!important.log"], tmp_path)
        assert ignore.matches(tmp_path / "debug.log") is True
        assert ignore.matches(tmp_path / "important.log") is False

    def test_last_matching_pattern_wins(self, tmp_path):
        """A later un-negated pattern re-excludes a path a negation re-included."""
        ignore = IcxIgnore(["*.log", "!important.log", "important.log"], tmp_path)
        assert ignore.matches(tmp_path / "important.log") is True

    def test_no_negation_unaffected(self, tmp_path):
        ignore = IcxIgnore(["node_modules/", "*.pyc"], tmp_path)
        assert ignore.matches(tmp_path / "node_modules", is_dir=True) is True
        assert ignore.matches(tmp_path / "foo.pyc") is True
        assert ignore.matches(tmp_path / "foo.py") is False
