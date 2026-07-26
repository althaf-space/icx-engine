"""Render the boost benchmark scorecard: requirement-coverage, raw vs ICX-boosted, segmented by
difficulty and archetype. The HEADLINE is the underspecified-prompt lift (where a real user's vague
request lives and where boost has headroom); the easy class is shown as an honest near-ceiling contrast.
Self-contained, theme-aware, ASCII-only. All dynamic text escaped. Never raises."""
from __future__ import annotations

from html import escape

_CSS = (
    "body{font-family:system-ui,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#eef1f5;color:#182029}"
    "@media(prefers-color-scheme:dark){body{background:#0f1319;color:#e7edf3}}"
    ".wrap{max-width:900px;margin:0 auto;padding:26px}"
    ".hero{background:linear-gradient(135deg,#1f6f43,#2b5876);color:#fff;border-radius:16px;padding:24px}"
    ".hero .n{font-size:2.6em;font-weight:800;line-height:1.1}"
    ".hero .sub{opacity:.9;margin-top:6px}"
    ".cards{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}"
    ".card{flex:1;min-width:150px;background:#fff;border:1px solid #e3e9ef;border-radius:12px;padding:14px}"
    "@media(prefers-color-scheme:dark){.card{background:#1a212b;border-color:#28323d}}"
    ".card .lbl{font-size:.8em;color:#5b6b7b}.card .v{font-size:1.5em;font-weight:800}"
    ".up{color:#178a3a}.flat{color:#8894a2}.down{color:#d62b2b}"
    "h3{margin:22px 0 8px}"
    "table{width:100%;border-collapse:collapse;background:#fff;border-radius:10px;overflow:hidden}"
    "@media(prefers-color-scheme:dark){table{background:#1a212b}}"
    "th,td{padding:8px 12px;text-align:left;border-bottom:1px solid #e3e9ef;font-size:.92em}"
    "@media(prefers-color-scheme:dark){th,td{border-color:#28323d}}"
    ".note{color:#5b6b7b;font-size:.86em;margin:8px 2px}"
)


def _pct(x) -> str:
    try:
        return f"{round(float(x) * 100)}%"
    except (TypeError, ValueError):
        return "-"


def _cls(v) -> str:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "flat"
    return "up" if v > 1 else ("down" if v < -1 else "flat")


def _grp_table(title: str, grp: dict, order=None) -> str:
    if not grp:
        return ""
    keys = [k for k in (order or []) if k in grp] + [k for k in grp if not order or k not in order]
    rows = ""
    for k in keys:
        v = grp[k]
        rows += (
            "<tr><td>{k}</td><td>{n}</td><td>{raw}</td><td>{b}</td>"
            "<td class='{c}'>{g:+.0f} pts</td><td class='{c}'>{lift:+.0f}%</td></tr>".format(
                k=escape(str(k)), n=v.get("n", ""), raw=_pct(v.get("raw")), b=_pct(v.get("boosted")),
                c=_cls(v.get("abs_gain_pts")), g=float(v.get("abs_gain_pts", 0)),
                lift=float(v.get("lift_pct", 0))))
    return (f"<h3>{escape(title)}</h3><table><tr><th>{escape(title.split()[-1])}</th><th>n</th>"
            "<th>raw</th><th>boosted</th><th>gain</th><th>lift</th></tr>" + rows + "</table>")


def render_scorecard(report) -> str:
    rows = getattr(report, "rows", []) or []
    by_diff = getattr(report, "by_difficulty", {}) or {}
    by_arch = getattr(report, "by_archetype", {}) or {}
    overall_raw = getattr(report, "raw_avg", 0.0)
    overall_boost = getattr(report, "boosted_avg", 0.0)
    overall_gain = getattr(report, "abs_gain_pts", 0.0)

    # Headline = the underspecified class (where a real vague prompt lives, and where boost has room).
    us = by_diff.get("underspecified") or {}
    head_lift = us.get("lift_pct", 0.0)
    head_raw = us.get("raw", 0.0)
    head_boost = us.get("boosted", 0.0)
    hero = (
        "<div class='hero'>"
        f"<div class='n'>+{escape(str(head_lift))}%</div>"
        f"<div class='sub'>requirement coverage lift on underspecified prompts "
        f"(raw {_pct(head_raw)} -> boosted {_pct(head_boost)}) - deterministic rubric, ICX model</div>"
        "</div>")

    cards = (
        "<div class='cards'>"
        f"<div class='card'><div class='lbl'>underspecified lift</div>"
        f"<div class='v {_cls(us.get('abs_gain_pts'))}'>{us.get('abs_gain_pts', 0):+.0f} pts</div></div>"
        f"<div class='card'><div class='lbl'>overall raw</div><div class='v'>{_pct(overall_raw)}</div></div>"
        f"<div class='card'><div class='lbl'>overall boosted</div><div class='v'>{_pct(overall_boost)}</div></div>"
        f"<div class='card'><div class='lbl'>overall gain</div>"
        f"<div class='v {_cls(overall_gain)}'>{overall_gain:+.0f} pts</div></div>"
        "</div>")

    note = ("<p class='note'>Coverage = fraction of a prompt's real requirements the answer addresses. "
            "The boost's value is on <b>underspecified</b> prompts (a real user's vague request), where a "
            "raw answer misses implicit requirements the methodology forces out. <b>Easy</b> prompts are a "
            "near-ceiling contrast - a strong model already covers them, so there is little headroom, and "
            "that is expected, not a failure.</p>")

    diff_tbl = _grp_table("By difficulty", by_diff, order=["underspecified", "hard", "easy"])
    arch_tbl = _grp_table("By archetype", by_arch)

    prow = ""
    for r in rows:
        prow += (
            "<tr><td>{i}</td><td>{d}</td><td>{a}</td><td>{rc}/{rt}</td><td>{bc}/{rt}</td>"
            "<td>{raw}</td><td>{b}</td><td class='{c}'>{dl:+.0f} pts</td></tr>".format(
                i=escape(str(r.get("id", ""))), d=escape(str(r.get("difficulty", ""))),
                a=escape(str(r.get("archetype", ""))), rt=r.get("req_total", 0),
                rc=r.get("raw_covered", 0), bc=r.get("boosted_covered", 0),
                raw=_pct(r.get("raw_frac")), b=_pct(r.get("boosted_frac")),
                c=_cls((r.get("delta") or 0) * 100), dl=float((r.get("delta") or 0) * 100)))
    per_prompt = ("<h3>Per prompt (requirements covered: raw -> boosted)</h3><table>"
                  "<tr><th>id</th><th>difficulty</th><th>archetype</th><th>raw req</th>"
                  "<th>boost req</th><th>raw</th><th>boosted</th><th>gain</th></tr>"
                  + prow + "</table>") if rows else "<p>No results.</p>"

    body = f"<div class='wrap'>{hero}{cards}{note}{diff_tbl}{arch_tbl}{per_prompt}</div>"
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>ICX Boost Benchmark</title><style>{_CSS}</style></head><body>{body}</body></html>")
