from __future__ import annotations

from icx_engine.gitlab.ci_tags import extract_tag_patterns, valid_environments, matches_any_pattern

# Real-shaped fixture, faithfully mirroring the actual .gitlab-ci.yml captured live
# from a real project (see developer.md's GitLab integration section) - two tag
# patterns (dev/qa), plus one non-regex `only:` entry (a plain branch/keyword) to
# prove those are correctly skipped, not misread as tag patterns.
_REAL_CI_YAML = r"""
stages:
  - build

variables:
  TAG: $CI_COMMIT_TAG

dev_def_env_20-buildapp-npm:
  stage: build
  only:
    - /^v\d+\.\d+\.\d+-dev-(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])(\d{3})$/
    - /^v\d+\.\d+\.\d+-dev-(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])(\d{3})-n$/
    - merge_requests

qa_def_ap_env_14-artifactpush-npm:
  stage: build
  only:
    - /^v\d+\.\d+\.\d+-qa-(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])(\d{3})$/

no_only_job:
  stage: build
  script:
    - echo hi
"""


def test_extract_tag_patterns_only_keeps_regex_literal_entries():
    patterns = extract_tag_patterns(_REAL_CI_YAML)
    assert len(patterns) == 3
    assert all(p.startswith("^v") for p in patterns)
    assert not any("merge_requests" in p for p in patterns)


def test_valid_environments_extracts_dev_and_qa_from_real_patterns():
    assert valid_environments(_REAL_CI_YAML) == {"dev", "qa"}


def test_valid_environments_empty_when_no_only_blocks_found():
    assert valid_environments("job:\n  script:\n    - echo hi\n") == set()


def test_valid_environments_empty_on_malformed_yaml():
    assert valid_environments("not: valid: yaml: [") == set()


def test_matches_any_pattern_true_for_real_dev_tag():
    assert matches_any_pattern("v0.0.1-dev-20260803001", _REAL_CI_YAML) is True


def test_matches_any_pattern_true_for_real_qa_tag():
    assert matches_any_pattern("v0.0.1-qa-20260803001", _REAL_CI_YAML) is True


def test_matches_any_pattern_false_for_wrong_case_environment():
    """The exact reported bug: 'DEV' (uppercase) does not match the lowercase-only
    pattern, even though it looks superficially like a valid tag name."""
    assert matches_any_pattern("v0.0.1-DEV-20260803001", _REAL_CI_YAML) is False


def test_matches_any_pattern_false_for_malformed_date_suffix():
    assert matches_any_pattern("v0.0.1-qa-not-a-real-date", _REAL_CI_YAML) is False


def test_matches_any_pattern_false_when_no_patterns_exist_at_all():
    assert matches_any_pattern("v0.0.1-dev-20260803001", "job:\n  script:\n    - echo hi\n") is False


def test_extract_tag_patterns_returns_empty_list_for_non_dict_yaml():
    assert extract_tag_patterns("- just\n- a\n- list\n") == []


def test_extract_tag_patterns_ignores_jobs_with_non_list_only():
    yaml_text = "job:\n  only:\n    refs:\n      - main\n"
    assert extract_tag_patterns(yaml_text) == []
