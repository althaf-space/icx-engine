"""Render + write a peak, deeply-explanatory single HTML report for one test run, built for a
non-technical end user: what every test does, HOW it was run, the PASS CRITERIA, the result, and why
any test failed - so the whole testing story is understandable from one page. Self-contained, styled,
theme-aware. All dynamic text is html.escape'd. Never raises - a report-write failure must not affect a
test run. (The agent/MCP path keeps the machine-readable result separately; this file is the human view.)"""
from __future__ import annotations

import json
import os
import time
from html import escape
from pathlib import Path

from icx_engine.testing.reporting.index import update_index

_CATEGORY_RULES = (
    ("security", ("security(", "security:")),
    ("accessibility", ("accessibility", "a11y:")),
    ("visual", ("visual:",)),
    ("dataflow", ("dataflow:",)),
    ("heal", ("heal:",)),
    ("constraint", ("constraint",)),
    ("render", ("render:",)),
)

# Plain-English name + description of what each category of test checks (shown once per category).
_CATEGORY_INFO = {
    "functional": ("Core functionality",
                   "The everyday actions on the screen - creating, viewing, editing and deleting "
                   "records - and confirming each change is really saved and shown back to the user."),
    "security": ("Security",
                 "Hostile inputs are deliberately fed into the screen to prove the app is safe: "
                 "cross-site scripting (XSS) scripts and SQL-injection payloads."),
    "accessibility": ("Accessibility (WCAG 2.1 AA)",
                      "An industry-standard axe-core audit that checks the screen is usable by people "
                      "relying on screen readers and assistive technology."),
    "constraint": ("Field rules",
                   "Every field's rules - length limits, formats (email, phone, URL, number) and "
                   "required fields - are pushed with a bad value to confirm the app enforces them."),
    "render": ("Screen rendering",
               "Every chart, table and card on the screen is confirmed to actually appear and draw "
               "its data - not a blank or half-loaded page."),
    "visual": ("Visual regression",
               "The screen is compared against a saved reference image to catch any unintended change "
               "in how it looks."),
    "dataflow": ("Data and network",
                 "A record saved in the screen is checked all the way through to the database, and the "
                 "screen is checked to stay usable under a slow or dropped network."),
    "heal": ("Self-healing",
             "When the app's markup changed, the test automatically re-found the moved elements and "
             "kept running instead of breaking - so a UI tweak does not force a test rewrite."),
}
_CATEGORY_ORDER = ["functional", "security", "constraint", "render", "visual", "accessibility",
                   "dataflow", "heal"]

# Per-test explainer: given a lowercased test name, return (what we checked, how we did it, pass
# criteria). First matching rule wins; a per-category fallback covers anything unmatched. Keyed on the
# structured names ICX's own generator emits - deterministic, not app-specific.
_EXPLAIN_RULES = (
    (("wait for the authenticated", "open the screen"),
     "The screen loads for a logged-in user.",
     "Restored the saved login session and navigated straight to the screen's URL.",
     "A real element of the screen appears (proving we are past the login page)."),
    (("performance:",),
     "The screen loads quickly enough.",
     "Measured the browser's Navigation-Timing load duration.",
     "Load time is under the budget (default 8 seconds)."),
    (("render:", ),
     "A chart, table or card on the screen actually renders.",
     "Waited for the widget on the live page after the screen painted.",
     "The widget is present in the page (an empty-data chart still counts as rendered)."),
    (("negative:", "validation"),
     "The form rejects an empty / invalid submit.",
     "Submitted the create form with nothing filled in.",
     "The app shows its required-field validation message."),
    (("constraint",),
     "A field enforces its length / format rule.",
     "Typed a value that breaks the rule (too long, or a bad email / phone / URL / number).",
     "The field caps or rejects the bad value (checked via the browser's own validity)."),
    (("security(xss)", "xss"),
     "The screen is safe from cross-site scripting.",
     "Injected a script canary into every text field and submitted it.",
     "The script did NOT run - the app escaped it, so no injected code executes."),
    (("security(sqli)", "sqli"),
     "The screen is safe from SQL injection.",
     "Sent a hostile SQL-injection query through the input.",
     "The app stays up and does not crash or leak a database error."),
    (("accessibility", "a11y"),
     "The screen meets WCAG 2.1 AA accessibility.",
     "Ran a full axe-core audit against the live page.",
     "Zero accessibility violations (images have alt text, controls have labels, and so on)."),
    (("create:", "fill "),
     "A new record can be created and saved.",
     "Filled every field with realistic, valid, uniquely-tagged data and clicked Save.",
     "The save is accepted, the form closes, and the record appears in the list."),
    (("verify:",),
     "A saved change really persisted.",
     "Searched the list for the record's unique tag after saving.",
     "The record is found in the list - the write was real, not just a UI flash."),
    (("edit:", "open edit"),
     "An existing record can be edited and saved.",
     "Found our record, opened its edit form, changed the identifying field and saved.",
     "The update is accepted and the new value shows in the list."),
    (("revert:",),
     "An edit leaves no lasting change (clean test data).",
     "After verifying the edit, reopened the record and restored its original value.",
     "The record is back to its original value - the test left the data as it found it."),
    (("workflow(delete)", "delete:"),
     "A record can be deleted and is truly gone.",
     "Deleted only the record WE created (row-scoped so existing data is never touched) and confirmed.",
     "Searching for the record afterwards finds nothing - it was really removed."),
    (("open view", "view modal", "view:", "view user", "header"),
     "A record's detail view opens correctly.",
     "Clicked a row's view icon and waited for its modal.",
     "The detail modal opens with the expected header."),
    (("search", "type a query", "reacts to payload"),
     "The list search works.",
     "Typed a query into the search box and watched the list react.",
     "The list filters / refetches in response to the query."),
    (("visual:",),
     "The screen looks the same as its approved baseline.",
     "Took a screenshot and compared it pixel-by-pixel to the saved baseline (or captured the first one).",
     "The changed-pixel ratio is under the threshold (a bigger change is flagged for review)."),
    (("dataflow: db verify", "db verify", "db confirmed"),
     "A record saved in the UI really reached the database.",
     "Ran the operator-configured database check for the record's unique value.",
     "The database returns the record - the UI save and the stored data agree."),
    (("dataflow: graceful", "graceful under slow"),
     "The screen stays usable under a slow network.",
     "Applied a slow-network profile, then re-checked the screen.",
     "The screen's main content is still present - it degrades gracefully, no white screen."),
    (("dataflow: apply slow", "dataflow: reset", "netprofile"),
     "The network condition was applied / cleared for the graceful-degradation check.",
     "Turned a slow-network profile on (and off again afterwards).",
     "The profile switch succeeded."),
    (("heal:",),
     "A moved element was recovered automatically.",
     "The original selector no longer matched, so the element was re-found by scoring its saved "
     "fingerprint (text, role, position, neighbours).",
     "The right element was re-identified and the test continued."),
    (("download",),
     "An export / download works.",
     "Clicked the export control and waited for the download.",
     "A file was produced."),
    (("error", "route", "offline"),
     "The screen handles a backend failure gracefully.",
     "Forced the backend request to fail (HTTP 500), then checked the screen.",
     "The app shows its error state or stays visible - no crash or blank page."),
)
_CATEGORY_FALLBACK = {
    "functional": ("A core action on the screen.", "Drove the action as a real user would.",
                   "The action completed and the expected result was shown."),
    "render": ("A part of the screen renders.", "Waited for it on the live page.", "It is present."),
    "dataflow": ("A data or network check.", "Exercised the data path / network condition.",
                 "The expected state held."),
}


def _int(v, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _flt(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def categorize(name: str) -> str:
    low = str(name or "").lower()
    for cat, keys in _CATEGORY_RULES:
        if any(low.startswith(k) for k in keys):
            return cat
    return "functional"


def _explain(name: str, cat: str):
    low = str(name or "").lower()
    for keys, what, how, crit in _EXPLAIN_RULES:
        if any(k in low for k in keys):
            return what, how, crit
    return _CATEGORY_FALLBACK.get(cat,
                                  ("A test on the screen.", "Exercised it as a real user would.",
                                   "The expected outcome held."))


def _default_dir() -> Path:
    env = os.environ.get("ICX_TEST_REPORTS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".icx" / "testing" / "reports"


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in str(s))[:60] or "run"


def _screen_name(meta: dict) -> str:
    url = str(meta.get("url", ""))
    seg = ""
    if "#" in url:
        seg = url.split("#", 1)[1].strip("/").split("/")[-1].split("?")[0]
    if not seg:
        seg = str(meta.get("app", "") or "")
    if not seg:
        return "Screen"
    words = seg.replace("_", " ").replace("-", " ")
    out, buf = [], ""
    for ch in words:
        if ch.isupper() and buf and buf[-1].islower():
            out.append(buf)
            buf = ch
        else:
            buf += ch
    if buf:
        out.append(buf)
    return " ".join(w.capitalize() for w in " ".join(out).split()) or "Screen"


def _when(ts: int) -> str:
    try:
        return time.strftime("%d %b %Y, %H:%M", time.localtime(ts)) if ts else "-"
    except (OSError, ValueError, OverflowError):
        return "-"


_CSS = """
:root{--bg:#eef1f5;--card:#fff;--ink:#182029;--muted:#5b6b7b;--line:#e3e9ef;--pass:#178a3a;
--fail:#d62b2b;--skip:#8894a2;--accent:#2b6cb0;--passbg:#e7f6ec;--failbg:#fdeaea;--skipbg:#eef1f4;
--hero1:#2b5876;--hero2:#4e4376;}
@media (prefers-color-scheme:dark){:root{--bg:#0f1319;--card:#1a212b;--ink:#e7edf3;--muted:#9db0c2;
--line:#28323d;--accent:#61a3e6;--passbg:#123321;--failbg:#3a1a1a;--skipbg:#222a34;
--hero1:#1b2a3a;--hero2:#2a2440;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;line-height:1.55}
.wrap{max-width:920px;margin:0 auto;padding:26px 18px 60px}
.hero{background:linear-gradient(135deg,var(--hero1),var(--hero2));color:#fff;border-radius:18px;
padding:26px 28px;display:flex;flex-wrap:wrap;align-items:center;gap:22px;box-shadow:0 8px 30px rgba(0,0,0,.15)}
.hero h1{margin:0 0 6px;font-size:1.7em}.hero .sub{opacity:.85;font-size:.9em;word-break:break-all}
.hero .verdict{font-weight:800;font-size:1.1em;padding:7px 20px;border-radius:999px}
.hero .verdict.pass{background:rgba(255,255,255,.2)}.hero .verdict.fail{background:#d62b2b}
.ring{margin-left:auto}
.lead{color:var(--muted);margin:18px 4px 20px;font-size:1.02em}
.stats{display:flex;flex-wrap:wrap;gap:12px;margin:18px 0 8px}
.stat{flex:1;min-width:110px;background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:16px;text-align:center}.stat .n{font-size:1.9em;font-weight:800}.stat .l{color:var(--muted);font-size:.82em}
.stat.p .n{color:var(--pass)}.stat.f .n{color:var(--fail)}.stat.s .n{color:var(--skip)}
h2{font-size:1.25em;margin:34px 0 6px}h2 .c{color:var(--muted);font-weight:400;font-size:.7em}
.about{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:6px 20px;margin-bottom:8px}
.about li{margin:12px 0;color:var(--ink)}.about b{color:var(--accent)}
.attention{background:var(--failbg);border:1px solid var(--fail);border-radius:14px;padding:4px 20px}
.attention .fi{padding:13px 0;border-bottom:1px solid var(--line)}.attention .fi:last-child{border-bottom:none}
.attention .fn{font-weight:700}.attention .fr{color:var(--fail);margin-top:3px;word-break:break-word}
.cat{background:var(--card);border:1px solid var(--line);border-radius:14px;margin-bottom:14px;overflow:hidden}
.cat>summary{list-style:none;cursor:pointer;padding:17px 20px;display:flex;align-items:center;gap:13px}
.cat>summary::-webkit-details-marker{display:none}
.tag{font-size:.66em;font-weight:800;letter-spacing:.06em;padding:4px 9px;border-radius:7px;
background:var(--accent);color:#fff;flex-shrink:0}
.cat .ttl{font-weight:700}.cat .desc{color:var(--muted);font-size:.9em;font-weight:400;display:block;margin-top:2px}
.pill{margin-left:auto;display:flex;gap:6px;flex-shrink:0}
.pill span{font-size:.78em;font-weight:700;padding:3px 10px;border-radius:999px}
.pill .p{background:var(--passbg);color:var(--pass)}.pill .f{background:var(--failbg);color:var(--fail)}
.pill .s{background:var(--skipbg);color:var(--skip)}
.tests{padding:0 20px 12px}
.t{border-top:1px solid var(--line)}
.t>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:11px;padding:12px 2px}
.t>summary::-webkit-details-marker{display:none}
.dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.dot.passed{background:var(--pass)}.dot.failed{background:var(--fail)}.dot.skipped{background:var(--skip)}
.t .nm{flex:1;word-break:break-word}
.badge{font-size:.72em;font-weight:800;padding:2px 9px;border-radius:6px;flex-shrink:0}
.badge.passed{background:var(--passbg);color:var(--pass)}.badge.failed{background:var(--failbg);color:var(--fail)}
.badge.skipped{background:var(--skipbg);color:var(--skip)}
.tm{color:var(--muted);font-size:.8em;flex-shrink:0}
.expl{padding:4px 2px 16px 21px;display:grid;grid-template-columns:130px 1fr;gap:6px 14px;font-size:.92em}
.expl dt{color:var(--muted);font-weight:600}.expl dd{margin:0}
.expl .res.ok{color:var(--pass);font-weight:600}.expl .res.no{color:var(--fail);font-weight:600}
.expl .res.sk{color:var(--skip)}
.sevbar{display:flex;flex-wrap:wrap;gap:8px;margin:2px 2px 8px}
.sev{font-size:.74em;font-weight:800;letter-spacing:.03em;padding:3px 10px;border-radius:999px;
text-transform:uppercase;color:#fff}
.sev.critical{background:#a4133c}.sev.high{background:#d62b2b}.sev.medium{background:#c77700}
.sev.low{background:#5b6b7b}.sev.info{background:#8894a2}
.secnote{color:var(--muted);font-size:.85em;margin:0 2px 12px}
.secok{background:var(--passbg);border:1px solid var(--pass);border-radius:14px;padding:16px 20px;
color:var(--pass);font-weight:600}
.find{display:flex;gap:11px;align-items:flex-start;padding:11px 0;border-top:1px solid var(--line)}
.find .fbody{flex:1;min-width:0}.find .ft{font-weight:600}
.find .fd{color:var(--muted);font-size:.9em;margin-top:2px;word-break:break-word}
.find .floc{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.82em;color:var(--accent);margin-top:3px;word-break:break-all}
.find .fsnip{background:var(--skipbg);border-radius:7px;padding:7px 10px;margin:6px 0 0;overflow-x:auto;
font-size:.8em;white-space:pre;color:var(--ink)}
.qcard{background:var(--card);border:1px solid var(--line);border-left-width:4px;border-radius:12px;
padding:14px 18px;margin-bottom:12px}
.qcard.ran{border-left-color:var(--pass)}.qcard.fail{border-left-color:var(--fail)}
.qcard.skip{border-left-color:var(--skip)}
.qcard .qttl{font-weight:700}.qcard .qdesc{color:var(--muted);font-size:.88em;display:block;margin:2px 0 8px}
.qcard .qb{font-size:.93em;word-break:break-word}
.qlist{margin:8px 0 0;padding-left:20px;color:var(--muted);font-size:.88em}
.qtable{width:100%;border-collapse:collapse;margin-top:8px;font-size:.88em}
.qtable th,.qtable td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line)}
.qtable .ok{color:var(--pass);font-weight:600}.qtable .no{color:var(--fail);font-weight:600}
.foot{color:var(--muted);font-size:.85em;text-align:center;margin-top:34px;line-height:1.7}
.rerun{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 20px;
color:var(--muted);font-size:.9em;margin-top:22px}
"""


def _donut(rate: int, ok: bool) -> str:
    r, col = 52, ("#7CFC9E" if ok else "#ffd0d0")
    circ = 2 * 3.14159 * r
    dash = round(circ * max(0, min(100, rate)) / 100, 1)
    return (
        "<svg class='ring' width='116' height='116' viewBox='0 0 116 116'>"
        "<circle cx='58' cy='58' r='{r}' fill='none' stroke='rgba(255,255,255,.25)' stroke-width='11'/>"
        "<circle cx='58' cy='58' r='{r}' fill='none' stroke='{c}' stroke-width='11' stroke-linecap='round' "
        "stroke-dasharray='{d} {circ}' transform='rotate(-90 58 58)'/>"
        "<text x='58' y='56' text-anchor='middle' font-size='25' font-weight='800' fill='#fff'>{rate}%</text>"
        "<text x='58' y='75' text-anchor='middle' font-size='10' fill='rgba(255,255,255,.8)'>pass rate</text>"
        "</svg>").format(r=r, c=col, d=dash, circ=round(circ, 1), rate=rate)


def _case_parts(c):
    if isinstance(c, (list, tuple)) and len(c) >= 2:
        name = str(c[0])
        status = str(c[1])
        t = round(_flt(c[2] if len(c) >= 3 else 0), 2)
        msg = str(c[3]) if len(c) >= 4 and c[3] else ""
        return name, status, t, msg
    return str(c), "", 0.0, ""


# Plain-English explanation of each static scanner, shown so a non-technical reader understands it.
_SCANNER_INFO = {
    "secrets": ("Leaked secrets",
                "Scanned the source code for passwords, API keys, private keys and tokens accidentally "
                "committed in plain text."),
    "sast": ("Code security (SAST)",
             "Read the source code for dangerous patterns - code that runs untrusted input, disables "
             "TLS checks, builds SQL by string-joining, or writes unescaped HTML."),
    "sca": ("Dependency risk (SCA)",
            "Checked the project's third-party libraries for unpinned versions and any that match a "
            "known-vulnerable advisory."),
}
_SEV_ORDER = ("critical", "high", "medium", "low", "info")


def _security_section(sec) -> str:
    if not isinstance(sec, dict):
        return ""
    summ = sec.get("summary") if isinstance(sec.get("summary"), dict) else {}
    findings = [f for f in (sec.get("findings") or []) if isinstance(f, dict)]
    total = _int(summ.get("total", len(findings)))
    # severity chips
    chips = []
    for s in _SEV_ORDER:
        n = _int(summ.get(s, 0))
        if n:
            chips.append("<span class='sev {s}'>{n} {s}</span>".format(s=escape(s), n=n))
    if total == 0:
        head = ("<h2>Security scan <span class='c'>(code, secrets, dependencies)</span></h2>"
                "<div class='secok'>No security issues found - no leaked secrets, no dangerous code "
                "patterns, and no flagged dependencies in the scanned source.</div>")
        return head
    # group findings by scanner
    by: dict[str, list] = {}
    for f in findings:
        by.setdefault(str(f.get("scanner", "sast")), []).append(f)
    cards = []
    for sc in ("secrets", "sast", "sca"):
        items = by.get(sc)
        if not items:
            continue
        title, desc = _SCANNER_INFO.get(sc, (sc.upper(), ""))
        rows = []
        for f in items:
            sev = escape(str(f.get("severity", "info")))
            loc = escape(str(f.get("file", "")))
            ln = _int(f.get("line", 0))
            locstr = (loc + (":" + str(ln) if ln else "")) if loc else ""
            snip = escape(str(f.get("snippet", "")))
            rows.append(
                "<div class='find'><span class='sev {sev}'>{sev}</span>"
                "<div class='fbody'><div class='ft'>{ttl}</div>"
                "<div class='fd'>{detail}</div>"
                "{loc}{snip}</div></div>".format(
                    sev=sev, ttl=escape(str(f.get("title", "finding"))),
                    detail=escape(str(f.get("detail", ""))),
                    loc=("<div class='floc'>" + locstr + "</div>") if locstr else "",
                    snip=("<pre class='fsnip'>" + snip + "</pre>") if snip else ""))
        cards.append(
            "<details class='cat' open><summary><span class='tag'>{tag}</span>"
            "<span><span class='ttl'>{title}</span><span class='desc'>{desc}</span></span>"
            "<span class='pill'><span class='f'>{n} found</span></span></summary>"
            "<div class='tests'>{rows}</div></details>".format(
                tag=escape(sc.upper()), title=escape(title), desc=escape(desc),
                n=len(items), rows="".join(rows)))
    note = ("<p class='secnote'>Static scan of the source code, deterministic and self-hosted. "
            "It finds high-frequency real issues; it is not a full taint-analysis or live-CVE "
            "scanner. Dependency advisories come from an offline file when provided.</p>")
    return ("<h2>Security scan <span class='c'>(code, secrets, dependencies)</span></h2>"
            "<div class='sevbar'>{chips}</div>{note}{cards}").format(
        chips="".join(chips), note=note, cards="".join(cards))


def _quality_section(q) -> str:
    if not isinstance(q, dict) or not q:
        return ""
    cards = []

    reg = q.get("regression") if isinstance(q.get("regression"), dict) else {}
    if reg.get("status") == "ran":
        n = _int(reg.get("relevant_count", 0))
        tests = reg.get("relevant_tests") or []
        rows = "".join("<li>{t}</li>".format(t=escape(str(t))) for t in tests[:40])
        body = ("<b>{n}</b> of {c} test files are relevant to your <b>{ch}</b> changed file(s).{ul}".format(
            n=n, c=_int(reg.get("candidate_tests", 0)), ch=_int(reg.get("changed_files", 0)),
            ul=("<ul class='qlist'>" + rows + "</ul>") if rows else
               " No existing test file maps to the change - consider adding one."))
        cards.append(_qcard("Regression selection",
                            "Which tests actually cover what changed - so a focused re-run skips the rest.",
                            body, "ran"))
    else:
        cards.append(_qcard("Regression selection",
                            "Which tests actually cover what changed.",
                            "Not run - " + escape(str(reg.get("reason", "unavailable"))), "skip"))

    perf = q.get("perf") if isinstance(q.get("perf"), dict) else {}
    if perf.get("status") == "ran":
        rows = "".join(
            "<tr><td>{m}</td><td>{b}</td><td>{a}</td><td>{p}%</td>"
            "<td class='{cls}'>{v}</td></tr>".format(
                m=escape(str(f.get("metric", ""))), b=escape(str(f.get("before", ""))),
                a=escape(str(f.get("after", ""))), p=escape(str(f.get("pct_change", ""))),
                cls=("res ok" if f.get("passed") else "res no"),
                v=("within budget" if f.get("passed") else "REGRESSED"))
            for f in (perf.get("findings") or []))
        body = ("<table class='qtable'><tr><th>metric</th><th>before</th><th>after</th>"
                "<th>change</th><th>verdict</th></tr>{rows}</table>".format(rows=rows))
        cards.append(_qcard("Performance regression",
                            "Each metric compared before vs after against its threshold.",
                            body, "ran" if perf.get("passed") else "fail"))
    else:
        cards.append(_qcard("Performance regression",
                            "Before/after metric comparison against thresholds.",
                            "Not run - " + escape(str(perf.get("reason", "unavailable"))), "skip"))

    mut = q.get("mutation") if isinstance(q.get("mutation"), dict) else {}
    if mut.get("status") == "ran":
        body = ("Mutation score <b>{s}</b> - killed {k} of {t} mutants ({tool}). {r}".format(
            s=escape(str(mut.get("score", ""))), k=_int(mut.get("killed", 0)),
            t=_int(mut.get("total", 0)), tool=escape(str(mut.get("tool", ""))),
            r=escape(str(mut.get("reason", "")))))
        cards.append(_qcard("Mutation testing",
                            "Proves the unit tests actually catch bugs (not just run the code).",
                            body, "ran" if mut.get("passed") else "fail"))
    else:
        cards.append(_qcard("Mutation testing",
                            "Proves the unit tests actually catch bugs, not just run the code.",
                            "Not run - " + escape(str(mut.get("reason", "unavailable"))), "skip"))

    return "<h2>Test quality</h2>" + "".join(cards)


def _qcard(title: str, desc: str, body: str, state: str) -> str:
    return ("<div class='qcard {state}'><div class='qh'><span class='qttl'>{title}</span>"
            "<span class='qdesc'>{desc}</span></div><div class='qb'>{body}</div></div>").format(
        state=escape(state), title=escape(title), desc=escape(desc), body=body)


def render_session_report(res: dict, meta: dict) -> str:
    res = res if isinstance(res, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    summ = res.get("summary") if isinstance(res.get("summary"), dict) else {}
    cases = list(res.get("cases") or [])
    total = _int(summ.get("total", len(cases)))
    passed = _int(summ.get("passed", 0))
    failed = _int(summ.get("failures", 0))
    skipped = _int(summ.get("skipped", 0))
    rate = round(100 * passed / total) if total else 0
    ok = total > 0 and failed == 0
    verdict = "PASS" if ok else "FAIL"
    screen = escape(_screen_name(meta))
    applabel = escape(str(meta.get("app", "") or "")) or "Test run"
    url = escape(str(meta.get("url", "")))
    tt = escape(str(meta.get("test_type", res.get("test_type", "")) or ""))

    hero = (
        "<div class='hero'><div>"
        "<h1>{screen}</h1>"
        "<div class='sub'>{app}{ttp} &bull; {when}<br>{url}</div>"
        "</div><span class='verdict {vc}'>{verdict}</span>{donut}</div>"
    ).format(screen=screen, app=applabel, ttp=(" &bull; " + tt + " test") if tt else "",
             when=_when(_int(meta.get("ts", 0))), url=url, vc=("pass" if ok else "fail"),
             verdict=verdict, donut=_donut(rate, ok))

    lead = ("<p class='lead'>This is the full record of the automated test run on the "
            "<b>{screen}</b> screen. Every check below was generated automatically from the live screen "
            "(no test was hand-written), then replayed exactly. Open any test to see what it checked, "
            "how it was run, and the exact pass criteria.</p>").format(screen=screen)

    cov = res.get("census_coverage")
    cov_card = ("<div class='stat'><div class='n'>{c}%</div><div class='l'>screen coverage</div></div>".format(
        c=round(100 * cov))) if isinstance(cov, (int, float)) else ""
    stats = ("<div class='stats'>"
             "<div class='stat'><div class='n'>{t}</div><div class='l'>tests run</div></div>"
             "<div class='stat p'><div class='n'>{p}</div><div class='l'>passed</div></div>"
             "<div class='stat f'><div class='n'>{f}</div><div class='l'>failed</div></div>"
             "<div class='stat s'><div class='n'>{s}</div><div class='l'>skipped</div></div>"
             "{cov}</div>").format(t=total, p=passed, f=failed, s=skipped, cov=cov_card)

    parsed = [_case_parts(c) for c in cases]
    buckets: dict[str, list] = {}
    for name, status, t, msg in parsed:
        buckets.setdefault(categorize(name), []).append((name, status, t, msg))

    # About / methodology - explains the approach once, so the whole report makes sense.
    about = ("<h2>How this testing works</h2><ul class='about'>"
             "<li><b>Auto-discovered:</b> ICX opened the live screen and read it to find every button, "
             "field, table, chart and action - so nothing had to be listed by hand.</li>"
             "<li><b>Generated + replayed:</b> those findings were turned into a precise test flow and "
             "replayed exactly the same way every time (no guessing, no flaky AI at run time).</li>"
             "<li><b>Many angles:</b> the same screen was checked for core actions, security, field "
             "rules, rendering, accessibility, visual changes, and data/network - listed below.</li>"
             "<li><b>Honest results:</b> a test only passes when its stated criteria are met; a real "
             "problem is shown as a failure with the reason, not hidden.</li></ul>")

    fails = [(n, s, t, m) for (n, s, t, m) in parsed if s == "failed"]
    attention = ""
    if fails:
        items = "".join(
            "<div class='fi'><div class='fn'>{n}</div><div class='fr'>Why it failed: {r}</div></div>".format(
                n=escape(n), r=escape(m or "the pass criteria were not met"))
            for (n, s, t, m) in fails)
        attention = ("<h2>Needs attention <span class='c'>({n} failing)</span></h2>"
                     "<div class='attention'>{items}</div>").format(n=len(fails), items=items)

    # Every test, grouped by category, each expandable into what/how/criteria/result.
    cat_cards = []
    ordered = [c for c in _CATEGORY_ORDER if c in buckets] + [c for c in buckets if c not in _CATEGORY_ORDER]
    for cat in ordered:
        entries = buckets[cat]
        p = sum(1 for e in entries if e[1] == "passed")
        f = sum(1 for e in entries if e[1] == "failed")
        s = sum(1 for e in entries if e[1] == "skipped")
        title, desc = _CATEGORY_INFO.get(cat, (cat.capitalize(), ""))
        pills = []
        if p:
            pills.append("<span class='p'>{p} passed</span>".format(p=p))
        if f:
            pills.append("<span class='f'>{f} failed</span>".format(f=f))
        if s:
            pills.append("<span class='s'>{s} skipped</span>".format(s=s))
        trows = []
        for name, status, t, msg in entries:
            what, how, crit = _explain(name, cat)
            st = status or "skipped"
            if st == "passed":
                res_html = "<dd class='res ok'>Passed - the pass criteria were met.</dd>"
            elif st == "failed":
                res_html = "<dd class='res no'>Failed - {m}</dd>".format(
                    m=escape(msg or "the pass criteria were not met"))
            else:
                res_html = "<dd class='res sk'>Skipped{m}</dd>".format(
                    m=(" - " + escape(msg)) if msg else " - not applicable on this run.")
            tor = " open" if st == "failed" else ""
            trows.append(
                "<details class='t'{tor}><summary><span class='dot {st}'></span>"
                "<span class='nm'>{nm}</span><span class='badge {st}'>{st}</span>"
                "<span class='tm'>{t}s</span></summary>"
                "<dl class='expl'>"
                "<dt>What we checked</dt><dd>{what}</dd>"
                "<dt>How we did it</dt><dd>{how}</dd>"
                "<dt>Pass criteria</dt><dd>{crit}</dd>"
                "<dt>Result</dt>{res}</dl></details>".format(
                    tor=tor, st=escape(st), nm=escape(name), t=t,
                    what=escape(what), how=escape(how), crit=escape(crit), res=res_html))
        cat_cards.append(
            "<details class='cat'{op}><summary><span class='tag'>{tag}</span>"
            "<span><span class='ttl'>{title}</span><span class='desc'>{desc}</span></span>"
            "<span class='pill'>{pills}</span></summary><div class='tests'>{rows}</div></details>".format(
                op=(" open" if f else ""), tag=escape(cat[:4].upper()), title=escape(title),
                desc=escape(desc), pills="".join(pills), rows="".join(trows)))
    tested = ("<h2>Every test we ran <span class='c'>(open a group, then a test, for full detail)</span></h2>"
              + "".join(cat_cards)) if cat_cards else ""

    security = _security_section(res.get("security"))
    quality = _quality_section(res.get("quality"))

    rerun = ("<div class='rerun'>Want to run this again or test another screen? Ask your AI assistant "
             "(the ICX testing tool) to re-run it - it drives everything above automatically.</div>")

    body = ("<div class='wrap'>" + hero + lead + stats + about + attention + tested + security +
            quality + rerun +
            "<div class='foot'>ICX local testing - self-hosted, deterministic replay. "
            "No test was hand-written; every check was generated from the live screen.</div></div>")
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Test Report - {screen}</title><style>{css}</style></head>"
            "<body>{body}</body></html>").format(screen=screen, css=_CSS, body=body)


def write_session_report(res: dict, meta: dict, reports_dir=None) -> Path:
    """Render + write the report HTML, append a ledger row, and refresh index.html. Never raises."""
    reports_dir = Path(reports_dir) if reports_dir is not None else _default_dir()
    ts = _int((meta or {}).get("ts", 0))
    app = _safe((meta or {}).get("app", "app"))
    out = reports_dir / f"{app}-{ts}.html"
    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(reports_dir, 0o700)
        except OSError:
            pass
        out.write_text(render_session_report(res, meta), encoding="utf-8")
        summ = res.get("summary") if isinstance(res, dict) and isinstance(res.get("summary"), dict) else {}
        total = _int(summ.get("total", 0))
        passed = _int(summ.get("passed", 0))
        row = {"run_id": str((meta or {}).get("run_id", "")), "app": str((meta or {}).get("app", "app")),
               "screen": _screen_name(meta or {}),
               "url": str((meta or {}).get("url", "")), "test_type": str((meta or {}).get("test_type", "")),
               "ts": ts, "total": total, "passed": passed, "failed": _int(summ.get("failures", 0)),
               "skipped": _int(summ.get("skipped", 0)),
               "pass_rate": round(100 * passed / total) if total else 0, "file": out.name}
        with (reports_dir / "reports.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        update_index(reports_dir)
    except Exception:
        pass
    return out
