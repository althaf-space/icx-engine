"""Render the run-history dashboard: flakiness, pass-trend, slowest tests, heal-trend. Self-contained
ASCII HTML, no external assets."""
from __future__ import annotations

from pathlib import Path

from icx_engine.testing.analytics.compute import flakiness, suite_flakiness, pass_trend, slowest, heal_trend


def _flaky_table(history: dict) -> str:
    fl = flakiness(history)
    rows = "".join(f"<tr><td>{name}</td><td>{round(100 * score)}%</td></tr>"
                   for name, score in sorted(fl.items(), key=lambda x: x[1], reverse=True) if score > 0)
    if not rows:
        rows = "<tr><td colspan=2>no flaky tests</td></tr>"
    return ("<h2>Flakiness</h2><p>Suite flakiness: "
            f"{round(100 * suite_flakiness(history))}%</p>"
            "<table border=1 cellpadding=6><tr><th>Test</th><th>Flaky score</th></tr>" + rows + "</table>")


def _trend_table(runs) -> str:
    tr = pass_trend(runs)
    rows = "".join(f"<tr><td>{int(ts)}</td><td>{round(100 * rate)}%</td></tr>" for ts, rate in tr)
    ht = dict(heal_trend(runs))
    return ("<h2>Pass-rate trend</h2><table border=1 cellpadding=6>"
            "<tr><th>Run (ts)</th><th>Pass rate</th></tr>" + rows + "</table>"
            "<h2>Heals per run</h2><table border=1 cellpadding=6>"
            "<tr><th>Run (ts)</th><th>Heals</th></tr>"
            + "".join(f"<tr><td>{int(ts)}</td><td>{n}</td></tr>" for ts, n in sorted(ht.items())) + "</table>")


def _slowest_table(history: dict) -> str:
    rows = "".join(f"<tr><td>{name}</td><td>{round(mean, 2)}s</td></tr>" for name, mean in slowest(history))
    return ("<h2>Slowest tests</h2><table border=1 cellpadding=6>"
            "<tr><th>Test</th><th>Mean time</th></tr>" + rows + "</table>")


def dashboard_html(store, last_n: int = 10) -> str:
    runs = store.recent_runs(limit=max(last_n, 50))
    history = store.test_history(limit=last_n)
    body = ("<h1>ICX Testing - Run Analytics</h1>"
            + _flaky_table(history) + _trend_table(runs) + _slowest_table(history))
    return ("<!doctype html><html><head><meta charset='utf-8'><title>ICX Test Analytics</title></head>"
            "<body style='font-family:sans-serif'>" + body + "</body></html>")


def render_dashboard(store, out_path: Path, last_n: int = 10) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(dashboard_html(store, last_n=last_n), encoding="utf-8")
    return out_path
