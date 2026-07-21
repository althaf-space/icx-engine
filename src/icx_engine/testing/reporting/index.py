"""Render index.html for the reports dir from the reports.jsonl ledger (newest-first). Styled, self-
contained, theme-aware. Pure; never raises."""
from __future__ import annotations

import json
from html import escape
from pathlib import Path


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _rows(reports_dir: Path) -> list[dict]:
    ledger = reports_dir / "reports.jsonl"
    if not ledger.exists():
        return []
    out = []
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    out.sort(key=lambda r: _int(r.get("ts", 0)), reverse=True)          # newest first
    return out


_CSS = """
:root{--bg:#f4f6f8;--card:#fff;--ink:#1c2530;--muted:#5b6b7b;--line:#e2e8ee;--pass:#178a3a;--fail:#d02b2b;
--skip:#8a94a0;--accent:#2b6cb0;}
@media (prefers-color-scheme:dark){:root{--bg:#12161c;--card:#1b222b;--ink:#e6ecf2;--muted:#9fb0c0;
--line:#2a333f;--accent:#5ea0e0;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.5}
.wrap{max-width:960px;margin:0 auto;padding:24px}h1{font-size:1.5em;margin:0 0 4px}
.sub{color:var(--muted);margin:0 0 20px}
.run{display:flex;align-items:center;gap:14px;background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:14px 18px;margin-bottom:10px;text-decoration:none;color:inherit}
.run:hover{border-color:var(--accent)}
.run .name{font-weight:600}.run .meta{color:var(--muted);font-size:.85em}
.rate{margin-left:auto;font-weight:700}.rate.ok{color:var(--pass)}.rate.bad{color:var(--fail)}
.counts{font-size:.85em;color:var(--muted);white-space:nowrap}
.counts b.p{color:var(--pass)}.counts b.f{color:var(--fail)}
.empty{color:var(--muted)}
"""


def update_index(reports_dir: Path) -> Path:
    reports_dir = Path(reports_dir)
    rows = _rows(reports_dir)
    cards = []
    for r in rows:
        total = _int(r.get("total", 0))
        passed = _int(r.get("passed", 0))
        failed = _int(r.get("failed", 0))
        rate = _int(r.get("pass_rate", 0))
        ok = failed == 0 and total > 0
        name = escape(str(r.get("screen") or r.get("app") or "Run"))
        app = escape(str(r.get("app", "")))
        tt = escape(str(r.get("test_type", "")))
        f = escape(str(r.get("file", "")))
        cards.append(
            "<a class='run' href='{f}'><div><div class='name'>{name}</div>"
            "<div class='meta'>{app}{ttp}</div></div>"
            "<div class='counts'><b class='p'>{p}</b> passed, <b class='f'>{fl}</b> failed of {t}</div>"
            "<div class='rate {cls}'>{rate}%</div></a>".format(
                f=f, name=name, app=app or "run", ttp=(" - " + tt) if tt else "",
                p=passed, fl=failed, t=total, cls=("ok" if ok else "bad"), rate=rate))
    inner = ("".join(cards) if cards else "<p class='empty'>No test runs recorded yet.</p>")
    html = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>ICX Test Reports</title><style>{css}</style></head><body><div class='wrap'>"
            "<h1>Test Reports</h1><p class='sub'>Most recent first - click a run to see its full report.</p>"
            "{inner}</div></body></html>").format(css=_CSS, inner=inner)
    out = reports_dir / "index.html"
    try:
        out.write_text(html, encoding="utf-8")
    except OSError:
        pass
    return out
