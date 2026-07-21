"""Render the benchmark scorecard: ICX measured numbers beside competitors' published figures.
Self-contained ASCII HTML - no external assets, no network."""
from __future__ import annotations

from pathlib import Path

from icx_engine.testing.benchmark.compare import competitor_rows
from icx_engine.testing.benchmark.metrics import RunMetrics


def _pct(x: float) -> str:
    return f"{round(100 * x)}%"


def _icx_table(metrics: list[RunMetrics]) -> str:
    head = ("<tr><th>App</th><th>Coverage recall</th><th>Coverage precision</th>"
            "<th>Misfire rate</th><th>Flakiness</th><th>Authoring actions</th>"
            "<th>Real findings</th><th>Tests</th><th>Seconds</th></tr>")
    rows = []
    for m in metrics:
        rows.append(
            "<tr><td>{app}</td><td>{rec}</td><td>{prec}</td><td>{mis}</td><td>{flk}</td>"
            "<td>{auth}</td><td>{real}</td><td>{tot}</td><td>{sec}</td></tr>".format(
                app=m.app, rec=_pct(m.coverage.recall), prec=_pct(m.coverage.precision),
                mis=_pct(m.misfire_rate), flk=_pct(m.flakiness), auth=m.authoring_actions,
                real=m.real_findings, tot=m.total_tests, sec=round(m.speed_seconds, 1)))
    return "<h2>ICX (measured)</h2><table border=1 cellpadding=6>" + head + "".join(rows) + "</table>"


def _cross_browser_table(metrics: list[RunMetrics]) -> str:
    rows = []
    for m in metrics:
        xb = getattr(m, "cross_browser", {}) or {}
        for label, ratio in xb.items():
            rows.append(f"<tr><td>{m.app}</td><td>{label}</td><td>{round(100 * ratio)}%</td></tr>")
    if not rows:
        return ""
    head = "<tr><th>App</th><th>Target</th><th>Pass</th></tr>"
    return ("<h2>Cross-browser and mobile (measured)</h2>"
            "<table border=1 cellpadding=6>" + head + "".join(rows) + "</table>")


def _self_heal_table(metrics: list[RunMetrics]) -> str:
    rows = []
    for m in metrics:
        sh = getattr(m, "self_heal", {}) or {}
        if not sh:
            continue
        rows.append(f"<tr><td>{m.app}</td><td>{sh.get('injected', 0)}</td>"
                    f"<td>{sh.get('recovered', 0)}</td><td>{round(100 * (sh.get('rate', 0.0)))}%</td></tr>")
    if not rows:
        return ""
    head = "<tr><th>App</th><th>Mutations injected</th><th>Recovered</th><th>Heal rate</th></tr>"
    return ("<h2>Self-healing (measured)</h2>"
            "<table border=1 cellpadding=6>" + head + "".join(rows) + "</table>")


def _visual_table(metrics: list[RunMetrics]) -> str:
    rows = []
    for m in metrics:
        v = getattr(m, "visual", {}) or {}
        if not v:
            continue
        rows.append(f"<tr><td>{m.app}</td><td>{v.get('checked', 0)}</td>"
                    f"<td>{v.get('baselines', 0)}</td><td>{v.get('regressions', 0)}</td></tr>")
    if not rows:
        return ""
    head = "<tr><th>App</th><th>Screens checked</th><th>Baselines</th><th>Regressions</th></tr>"
    return ("<h2>Visual regression (measured)</h2>"
            "<table border=1 cellpadding=6>" + head + "".join(rows) + "</table>")


def _a11y_table(metrics: list[RunMetrics]) -> str:
    rows = []
    for m in metrics:
        a = getattr(m, "a11y", {}) or {}
        if not a:
            continue
        rows.append(f"<tr><td>{m.app}</td><td>{a.get('violations', 0)}</td>"
                    f"<td>{a.get('critical', 0)}</td><td>{a.get('serious', 0)}</td></tr>")
    if not rows:
        return ""
    head = "<tr><th>App</th><th>WCAG violations</th><th>Critical</th><th>Serious</th></tr>"
    return ("<h2>Accessibility (measured)</h2><p>axe-core WCAG 2.1 AA.</p>"
            "<table border=1 cellpadding=6>" + head + "".join(rows) + "</table>")


def _dataflow_table(metrics: list[RunMetrics]) -> str:
    rows = []
    for m in metrics:
        d = getattr(m, "dataflow", {}) or {}
        if not d:
            continue
        rows.append(f"<tr><td>{m.app}</td><td>{d.get('db_checked', 0)}</td>"
                    f"<td>{d.get('db_confirmed', 0)}</td><td>{d.get('net_checked', 0)}</td></tr>")
    if not rows:
        return ""
    head = "<tr><th>App</th><th>DB checks</th><th>DB confirmed</th><th>Network checks</th></tr>"
    return ("<h2>DB and network (measured)</h2>"
            "<table border=1 cellpadding=6>" + head + "".join(rows) + "</table>")


def _competitor_table() -> str:
    head = "<tr><th>Tool</th><th>Metric</th><th>Value (published)</th><th>Source</th></tr>"
    rows = []
    for r in competitor_rows():
        rows.append("<tr><td>{tool}</td><td>{metric}</td><td>{value}</td>"
                    "<td><a href='{source}'>source</a></td></tr>".format(**r))
    return ("<h2>Competitors (published figures)</h2>"
            "<p>Values below are the vendors' own published/marketing numbers, cited for reference. "
            "They are NOT measured by this harness.</p>"
            "<table border=1 cellpadding=6>" + head + "".join(rows) + "</table>")


def scorecard_html(metrics: list[RunMetrics]) -> str:
    caveat = ("<p><em>Caveat: the ICX numbers below are MEASURED on this benchmark corpus (a small "
              "demo + open-source app set). The competitor figures are the vendors' own aggregate "
              "PUBLISHED marketing claims, not measured on this corpus - so the two are not a "
              "like-for-like head-to-head.</em></p>")
    body = ("<h1>ICX Ultimate Testing - Benchmark Scorecard</h1>" + caveat
            + _icx_table(metrics) + _cross_browser_table(metrics) + _self_heal_table(metrics) + _visual_table(metrics) + _a11y_table(metrics) + _dataflow_table(metrics) + _competitor_table())
    return "<!doctype html><html><head><meta charset='utf-8'><title>ICX Benchmark</title></head>" \
           "<body style='font-family:sans-serif'>" + body + "</body></html>"


def render_scorecard(metrics: list[RunMetrics], out_path: Path) -> Path:
    """Write the scorecard HTML to out_path and return it."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(scorecard_html(metrics), encoding="utf-8")
    return out_path
