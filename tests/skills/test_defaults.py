from __future__ import annotations

import re

from icx_engine.skills.defaults import DEFAULT_SKILLS

_SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")
_REQUIRED_TEXT_FIELDS = ("description", "title", "when_to_use", "procedure", "pitfalls", "verification")


def test_default_skill_names_are_unique():
    names = [d["name"] for d in DEFAULT_SKILLS]
    assert len(names) == len(set(names))


def test_default_skill_names_are_storage_safe():
    for definition in DEFAULT_SKILLS:
        assert _SAFE_NAME_RE.match(definition["name"]), definition["name"]


def test_default_skills_have_all_required_text_fields():
    for definition in DEFAULT_SKILLS:
        for field in _REQUIRED_TEXT_FIELDS:
            assert definition.get(field), f"{definition['name']} missing {field}"


def test_default_skills_cover_expected_catalog():
    names = {d["name"] for d in DEFAULT_SKILLS}
    expected = {
        "systematic-debugging", "test-driven-development", "plan-before-code",
        "minimal-diff-discipline", "verification-before-completion", "code-review-before-merge",
        "ui-ux-accessibility-baseline", "comprehensive-test-authoring", "sonar-quality-review",
        "ticket-context-analysis", "safe-git-workflow", "codebase-graph-navigation",
        "testing-session-driver", "memory-effective-usage",
    }
    assert names == expected
