"""Parses tag-trigger regex patterns out of a real `.gitlab-ci.yml` - so a proposed
tag name (and its "environment" token, e.g. dev/qa) can be validated against what the
project's own CI pipeline actually matches on, instead of the agent inventing an
environment list from guesswork.

Verified live against a real captured .gitlab-ci.yml (see developer.md's GitLab
integration section): jobs use an `only:` list of Ruby-style regex-literal strings
(`/^v\\d+\\.\\d+\\.\\d+-dev-.../`), each with a fixed literal segment (e.g. `dev`, `qa`)
marking the environment. `rules:`-style CI_COMMIT_TAG matching was never observed live
and is NOT parsed here - only `only:` lists, matching what was actually captured.

Pure module - no I/O, no network. The caller (gitlab/service.py) fetches the file
content live via GitLabClient.get_repository_file() and passes the text in.
"""
from __future__ import annotations

import re

import yaml

# A literal (non-regex-metachar) hyphen-delimited segment - used to extract the
# "environment" token (e.g. "dev", "qa") embedded inside an otherwise-regex pattern.
# Heuristic, not a full CI YAML spec parser - matches the ONE pattern shape actually
# observed live (a fixed literal word between version/date regex groups).
_METACHARS = set(r"\^$.|?*+()[]{}")


def _is_literal_segment(segment: str) -> bool:
    return bool(segment) and not any(ch in _METACHARS for ch in segment)


def extract_tag_patterns(ci_yaml_text: str) -> list[str]:
    """Return every `only:`-list entry across all jobs that looks like a Ruby-style
    regex literal (`/.../`), with the wrapping slashes stripped so it's usable
    directly as a Python regex. Non-regex-looking `only:` entries (branch names,
    `merge_requests`, etc.) are skipped - they're not tag patterns."""
    try:
        doc = yaml.safe_load(ci_yaml_text)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []

    patterns: list[str] = []
    for job in doc.values():
        if not isinstance(job, dict):
            continue
        only = job.get("only")
        if not isinstance(only, list):
            continue
        for entry in only:
            if isinstance(entry, str) and entry.startswith("/") and entry.endswith("/") and len(entry) > 2:
                patterns.append(entry[1:-1])
    return patterns


def valid_environments(ci_yaml_text: str) -> set[str]:
    """Best-effort extraction of the literal environment tokens (e.g. {"dev", "qa"})
    embedded in the project's own tag-trigger patterns. Heuristic: split each pattern
    on '-', keep segments with no regex metacharacters. Returns an empty set if
    nothing recognizable is found - callers must treat that as "could not determine
    the real list", never as "no environments are valid"."""
    envs: set[str] = set()
    for pattern in extract_tag_patterns(ci_yaml_text):
        for segment in pattern.split("-"):
            if _is_literal_segment(segment) and segment.isalpha():
                envs.add(segment.lower())
    return envs


def matches_any_pattern(tag_name: str, ci_yaml_text: str) -> bool:
    """True if `tag_name` matches at least one of the project's own tag-trigger
    regex patterns - i.e. creating this tag would actually trigger a CI pipeline.
    False (including when no patterns were found at all) means creating it would be
    a silent no-op - callers should treat False as a reason to refuse or require an
    explicit override, never as proof the tag name itself is malformed."""
    for pattern in extract_tag_patterns(ci_yaml_text):
        try:
            if re.match(pattern, tag_name):
                return True
        except re.error:
            continue
    return False
