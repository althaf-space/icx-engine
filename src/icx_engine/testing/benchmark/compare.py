"""Competitors' PUBLISHED metrics, each with a source URL. These are marketing/published figures, not
measured by us - the scorecard labels them 'published' and cites the source. Never present as measured."""
from __future__ import annotations

COMPETITORS: list[dict] = [
    {"tool": "BrowserStack", "metric": "test-generation accuracy", "value": "91%",
     "source": "https://www.browserstack.com/low-code-automation/ai-agents"},
    {"tool": "BrowserStack", "metric": "coverage", "value": "92%",
     "source": "https://www.browserstack.com/low-code-automation/ai-agents"},
    {"tool": "BrowserStack", "metric": "authoring speed-up", "value": "90% faster",
     "source": "https://www.browserstack.com/press/browserstack-launches-suite-of-ai-agents-to-redefine-software-quality-at-scale"},
    {"tool": "BrowserStack", "metric": "build-failure reduction (self-heal)", "value": "40%",
     "source": "https://www.prnewswire.com/news-releases/browserstack-unveils-ai-powered-self-healing-agent-to-keep-builds-green-302617102.html"},
    {"tool": "Testim", "metric": "self-heal stability", "value": "smart-locator weighted scoring",
     "source": "https://www.tricentis.com/learn/self-healing-test-automation"},
    {"tool": "KaneAI", "metric": "coverage surface", "value": "web+mobile+api+db+network+a11y",
     "source": "https://www.testmuai.com/kane-ai/"},
]


def competitor_rows() -> list[dict]:
    """Return the published competitor rows (a copy, so callers cannot mutate the source list)."""
    return [dict(r) for r in COMPETITORS]
