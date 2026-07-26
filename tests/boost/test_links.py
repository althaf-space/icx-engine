"""Link preservation + 3-tier enrichment (pure)."""
from __future__ import annotations

from icx_engine.boost.links import extract_links, classify_target, build_link_plan


def test_extract_links_dedup_and_trim():
    text = "see https://x.atlassian.net/browse/AB-1, and https://figma.com/f/1. dup https://x.atlassian.net/browse/AB-1"
    urls = extract_links(text)
    assert urls == ["https://x.atlassian.net/browse/AB-1", "https://figma.com/f/1"]


def test_classify_targets():
    assert classify_target("https://acme.atlassian.net/browse/AB-1") == "jira"
    assert classify_target("https://sonar.acme.com/dashboard?id=x") == "sonarqube"
    assert classify_target("https://www.figma.com/file/abc") == "figma"
    assert classify_target("https://github.com/o/r/issues/3") == "github"
    assert classify_target("https://acme.atlassian.net/wiki/spaces/x") == "jira"  # atlassian wins
    assert classify_target("https://example.com/page") == "web"


def test_classify_target_rejects_lookalike_domains():
    # REGRESSION (CodeQL "incomplete URL substring sanitization"): a substring check against the
    # whole URL wrongly classified an attacker-controlled lookalike domain as a trusted SaaS target.
    # Anchored SaaS domains must be checked against the hostname only.
    assert classify_target("https://evil.com/?x=atlassian.net") != "jira"
    assert classify_target("https://atlassian.net.evil.com/phish") != "jira"
    assert classify_target("https://evil.com/figma.com") != "figma"
    assert classify_target("https://not-github.com/x") != "github"
    assert classify_target("https://github.com.evil.com/x") != "github"
    assert classify_target("https://evil.com/githubusercontent.com") != "github"


def test_classify_target_still_matches_real_subdomains():
    assert classify_target("https://raw.githubusercontent.com/o/r/main/f.py") == "github"
    assert classify_target("https://files.figma.com/f/1") == "figma"
    assert classify_target("https://my-org.atlassian.net/browse/AB-1") == "jira"


def test_tier1_icx_tool_when_connected():
    plan = build_link_plan(["https://acme.atlassian.net/browse/AB-1"], {"jira": True})
    assert plan[0]["status"] == "icx_tool"
    assert "analyze_issue_fast" in plan[0]["action"]


def test_tier2_connect_needed_when_icx_has_tool_but_not_connected():
    plan = build_link_plan(["https://sonar.acme.com/x"], {"sonarqube": False})
    assert plan[0]["status"] == "icx_connect_needed"
    assert "connect" in plan[0]["action"].lower()


def test_tier3_agent_fetch_for_figma():
    plan = build_link_plan(["https://figma.com/file/abc"], {"jira": True})
    assert plan[0]["target"] == "figma"
    assert plan[0]["status"] == "agent_fetch"
    assert "your own" in plan[0]["action"].lower() or "MCP" in plan[0]["action"]


def test_link_always_preserved():
    plan = build_link_plan(["https://example.com/x"], {})
    assert plan[0]["url"] == "https://example.com/x"
    assert plan[0]["status"] == "agent_fetch"


def test_empty_no_crash():
    assert build_link_plan([], {}) == []
    assert extract_links("") == []
