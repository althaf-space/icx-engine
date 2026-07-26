"""Link preservation + 3-tier enrichment for the boost brief. Pure + deterministic - it classifies each
link and decides the enrichment TIER; it does NOT fetch (the actual pull is an instruction the agent
follows, reusing ICX's own MCP tools or the agent's own connectors). This keeps ICX from building a
connector for everything while still bringing link context into the boosted brief.

Tiers (per link):
  1. icx_tool           - the target is one ICX has a tool for (jira, sonarqube) AND it is connected ->
                          instruct the agent to call that ICX tool to pull the content.
  2. icx_connect_needed - ICX has the tool but it is not connected -> tell the user to connect it (or the
                          agent to use its own tool meanwhile).
  3. agent_fetch        - ICX has no connector (figma, confluence, github, generic web) -> instruct the
                          agent to fetch with its OWN tool/MCP and feed the content back.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# ICX has its own retrieval tools for these targets (Jira connector + SonarQube reader).
ICX_TARGETS = ("jira", "sonarqube")

# Exact-hostname-match domains (a substring check on the whole URL would wrongly match a
# lookalike, e.g. "atlassian.net.evil.com" or "evil.com/?x=github.com") - checked against
# urlparse(url).hostname only, never the full URL string.
_HOST_TARGETS = (
    ("atlassian.net", "jira"),
    ("figma.com", "figma"),
    ("github.com", "github"),
    ("githubusercontent.com", "github"),
)


def _host_matches(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith("." + domain)

_URL_RE = re.compile(r"https?://[^\s<>()\"'\]]+")

# The ICX tool to call for a connected target.
_ICX_TOOL = {
    "jira": "analyze_issue_fast (pass this ticket) to pull its full context",
    "sonarqube": "sonar_report / sonar_findings (for this project) to pull its findings",
}


def extract_links(text: str) -> list[str]:
    """Return unique http(s) URLs in text, in first-seen order. Trailing punctuation trimmed."""
    out: list[str] = []
    seen: set = set()
    for m in _URL_RE.findall(text or ""):
        url = m.rstrip(".,;:!?")
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def classify_target(url: str) -> str:
    """Classify a link's target service. Deterministic; unknown/general -> 'web'.

    Known SaaS domains (atlassian.net/figma.com/github.com/githubusercontent.com) are matched
    against the URL's HOSTNAME only (exact or a proper subdomain) - a substring check against the
    whole URL would wrongly classify a lookalike like "https://evil.com/?x=atlassian.net" or
    "https://atlassian.net.evil.com" as trusted. "jira"/"sonar"/"confluence"/"/browse/"/"/wiki/"
    stay broad substring checks BY DESIGN - those tools are commonly self-hosted at an arbitrary
    internal domain (there is no fixed hostname to anchor to), so a keyword match is the only way
    to catch them at all; that tradeoff is intentional, not the same defect.
    """
    u = (url or "").lower()
    hostname = (urlparse(u).hostname or "")
    for domain, target in _HOST_TARGETS:
        if _host_matches(hostname, domain):
            return target
    if "/browse/" in u or "jira" in u:
        return "jira"
    if "sonar" in u:
        return "sonarqube"
    if "confluence" in u or "/wiki/" in u:
        return "confluence"
    return "web"


def build_link_plan(urls: list[str], icx_connected: dict | None = None) -> list[dict]:
    """Preserve every link and attach its enrichment tier + the action the agent should take.
    icx_connected maps an ICX target ('jira'/'sonarqube') -> bool. Pure; never raises."""
    icx_connected = icx_connected or {}
    plan: list[dict] = []
    for url in urls or []:
        target = classify_target(url)
        if target in ICX_TARGETS:
            if icx_connected.get(target):
                plan.append({"url": url, "target": target, "status": "icx_tool",
                             "action": f"Call ICX {_ICX_TOOL[target]}."})
            else:
                plan.append({"url": url, "target": target, "status": "icx_connect_needed",
                             "action": (f"This links to {target}, which ICX can pull once connected - "
                                        f"connect ICX to {target}, or fetch it with your own tool "
                                        f"meanwhile.")})
        else:
            plan.append({"url": url, "target": target, "status": "agent_fetch",
                         "action": (f"ICX has no connector for {target} - if you have a {target}/web "
                                    f"tool or MCP, fetch this link and use its content.")})
    return plan
