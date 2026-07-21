"""Deterministic UI census -> executable UiStep flow.

The agent's ONLY job is the Element Census (structured, reconciliation-verified). ICX converts that
census into the ordered browser steps HERE - not the agent - so the flow is IDENTICAL and complete on
every agent (Claude, Cursor, Windsurf, ...). This is the UI analogue of `to_api_spec.py` (which does
the same census -> executable conversion for the API layer), and it is what makes accuracy and
coverage independent of the connected agent's skill.

Output: a list of UiStep dicts ({action, target, value, description}) the harness replays. Selectors
come straight from the census `domSelectors` / `triggerSelector` ladders; any that do not resolve on
the live DOM are repaired by the verify/heal pass afterwards. Pure + defensive - never raises, skips
anything the census under-specifies, always emits a runnable flow.
"""
from __future__ import annotations

import json
import time

# Cap how many fields get the full boundary matrix. Each field adds several submit+assert cases, so
# an unbounded matrix on a wide form yields a very long run; a representative subset keeps coverage
# meaningful without exploding the step count.
_BOUNDARY_MAX_FIELDS = 3

# functionality name/type -> canonical kind.
_KIND_PATTERNS = [
    ("report",     ("generate report", "report", "generate", "run report", "apply filter")),
    ("render",     ("render", "widgets", "dashboard render")),
    ("download",   ("download", "export", "csv", "excel", "xlsx", "pdf export", "save as")),
    ("search",     ("search", "filter", "lookup")),
    ("refresh",    ("refresh", "reload")),
    ("pagesize",   ("page size", "pagesize", "records per page", "per page")),
    ("pagination", ("pagination", "paging", "next page", "prev", "page nav")),
    ("sort",       ("sort", "order by")),
    ("create",     ("create", "add", "new ", "register", "insert")),
    ("edit",       ("edit", "modify", "update")),
    ("view",       ("view", "detail", "show", "read")),
    ("delete",     ("delete", "remove", "deactivate")),
    ("list",       ("list", "table", "grid", "index", "browse")),
]

# Emit order: all READ-ONLY ops first (list/search/sort/pagination/refresh + view/edit which just
# open+cancel), then the MUTATIONS last (create, delete). Mutations reload the table, so running them
# last means no later row-action (view/edit/delete-icon click) races the re-render - the single
# biggest real flake source on CRUD screens.
# E2E order: non-row ops first (list/search/download/refresh/sort/pagination), then CREATE FIRST so a
# row is GUARANTEED to exist, then the row ops that need one - VIEW (opens a row), EDIT (finds+updates
# the created record + reverts), DELETE (removes it + verifies gone). Create-first means view/edit/
# delete never fail on an empty list.
_KIND_ORDER = {"render": 0, "list": 0, "search": 1, "download": 2, "report": 2, "refresh": 3,
               "pagesize": 4, "sort": 5, "pagination": 6, "create": 7, "view": 8, "edit": 9,
               "delete": 10, "other": 11}

# non-CRUD coverage caps + graceful-state JS probes (used by render/report). SOFT assertions so a
# legitimately-empty dashboard (no backend data yet) is skipped, not failed - the HARD assertion is only
# that the widget/result region RENDERS.
_RENDER_MAX_WIDGETS = 8
_REPORT_MAX_FILTERS = 6


def _valid_css(sel: str) -> bool:
    """Reject a selector a crawler could emit malformed - e.g. an SVG className serialized as
    '[object SVGAnimatedString]' (svg.[object...]) or an unbalanced fragment. A malformed selector
    throws in the browser and cascades (it poisons the anchor), so filter it out defensively."""
    s = _s(sel)
    if not s or "[object" in s or ".[" in s or ".(" in s:
        return False
    return s.count("[") == s.count("]") and s.count("(") == s.count(")")


# the screen is not a blank page or a crashed error boundary (lenient - soft).
_NO_ERROR_EXPR = ('(function(){var b=document.body;if(!b)return false;'
                  'if((b.innerText||"").trim().length<2)return false;'
                  'return document.querySelectorAll('
                  '"[class*=error-boundary],[class*=errorBoundary],[class*=app-crash],[class*=chunk-error]").length===0;})()')


def _chart_drew_expr(sel: str) -> str:
    """A chart element that actually drew data (has vector/canvas children). Soft: an empty-data chart
    legitimately has none, so this is skipped not failed - the render (element present) is the hard check."""
    js = json.dumps(sel)
    return ('(function(){var e=document.querySelector(' + js + ');if(!e)return true;'
            'if(e.tagName==="CANVAS")return true;'
            'return e.querySelector("path,rect,circle,line,g,canvas,image")!=null;})()')


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def _first(*cands) -> str:
    """First non-empty selector from any mix of strings / lists of strings."""
    for c in cands:
        if isinstance(c, str) and c.strip():
            return c.strip()
        if isinstance(c, (list, tuple)):
            for x in c:
                if isinstance(x, str) and x.strip():
                    return x.strip()
    return ""


def _kind(func: dict, type_by_id: dict) -> str:
    hay = " ".join(_s(func.get(k)) for k in ("functionality", "type", "description", "id")).lower()
    hay += " " + _s(type_by_id.get(_s(func.get("id")))).lower()
    for kind, toks in _KIND_PATTERNS:
        if any(t in hay for t in toks):
            return kind
    return "other"


# Realistic, general test data - NO product branding in any value the app stores or displays. The
# identifying value carries a short unique numeric token so the E2E flow can search the record back and
# verify the save; edit uses a different token. Both read like ordinary sample data a QA tester enters.
_NOW = int(time.time())
_UNIQ = str(_NOW % 100000).zfill(5)               # e.g. "04217" - unique + searchable
_UNIQ_EDIT = str((_NOW * 7 + 13) % 100000).zfill(5)
_TAG = f"Test {_UNIQ}"                             # e.g. "Test 04217" - realistic record name
_TAG_EDITED = f"Test {_UNIQ_EDIT}"                 # a different realistic name for the edit


def _uniq_fit(prefix: str, ml) -> str:
    """`prefix` + the unique token, fitted to maxLength. UNIQUENESS is preserved even in a tiny field:
    if there is no room for the prefix, the unique token (or its tail) is used ALONE, so a field with a
    uniqueness constraint never collides across runs. Never exceeds maxLength."""
    full = f"{prefix}{_UNIQ}"
    if ml is None or ml >= len(full):
        return full
    if ml >= len(_UNIQ):                           # room for the token + a slice of the prefix
        return prefix[:ml - len(_UNIQ)] + _UNIQ
    return _UNIQ[-ml:]                             # tiny field: unique tail only (still unique per run)


def _uniq_email(ml) -> str:
    """A VALID, UNIQUE email fitted to maxLength (plus-free unique local part)."""
    for local in (f"test{_UNIQ}", f"t{_UNIQ}", _UNIQ):
        v = f"{local}@example.com"
        if ml is None or len(v) <= ml:
            return v
    return f"{_UNIQ}@x.co"[:ml] if ml else f"test{_UNIQ}@example.com"


def _uniq_phone(ml) -> str:
    """A UNIQUE numeric phone/MSISDN: prefix digit + the unique token, padded/trimmed to a valid length
    (10 digits by default, or maxLength when smaller)."""
    digits = ("9" + _UNIQ + "0000000000")
    n = 10 if (ml is None or ml >= 10) else max(len(_UNIQ), ml)
    return digits[:n]


def _valid_value(field: dict) -> str:
    """A VALID, realistic, UNIQUE value for a real save, derived from the field's code-declared
    constraints. Every value embeds the run's unique token (fitted to maxLength) so a field with a
    uniqueness / duplicate-check constraint never collides across runs - while never exceeding the
    declared length. General sample data - no product branding."""
    ml = _maxlen(field)
    if _s(field.get("defaultValue")):
        return _s(field.get("defaultValue"))[:ml or None]
    opts = field.get("dropdownOptions") or []
    if isinstance(opts, list) and opts:
        return _s(opts[0])                         # a real option - uniqueness N/A
    from icx_engine.testing.analyzers.constraint_cases import _format_of
    fmt = _format_of(field)
    if fmt == "email":
        return _uniq_email(ml)
    if fmt == "phone":
        return _uniq_phone(ml)
    if fmt == "url":
        return f"https://example.com/{_UNIQ}.png"  # unique path (urls rarely length-capped)
    if fmt == "number":
        return _num_in_range(field)                # range constraint dominates
    label = (_s(field.get("label")) + " " + _s(field.get("fieldName")) + " " + _s(field.get("type"))).lower()
    if any(t in label for t in ("date",)):
        return "2025-01-01"
    return _uniq_fit("Sample ", ml)                # generic text: "Sample <uniq>", unique + length-safe


def _maxlen(field: dict):
    v = field.get("validations") if isinstance(field.get("validations"), dict) else {}
    for k in (v.get("maxLength"), field.get("maxLength"), field.get("maxlength")):
        try:
            n = int(k)
            if n > 0:
                return n
        except (TypeError, ValueError):
            continue
    return None


def _num_in_range(field: dict) -> str:
    v = field.get("validations") if isinstance(field.get("validations"), dict) else {}
    try:
        mn = int(v.get("min"))
    except (TypeError, ValueError):
        mn = None
    try:
        mx = int(v.get("max"))
    except (TypeError, ValueError):
        mx = None
    if mn is not None and mx is not None:
        return str((mn + mx) // 2)
    if mn is not None:
        return str(mn)
    if mx is not None:
        return str(mx)
    return "10"


def _field_value(field: dict) -> str:
    return _valid_value(field)


def _field_hint(field: dict) -> str:
    """The field's semantic kind for the runtime value generator: email/phone/url/number/text."""
    from icx_engine.testing.analyzers.constraint_cases import _format_of
    return _format_of(field) or "text"


# constraint-source modes: how a field's VALID value is decided.
#   "static"  - ICX generates the value in Python from the census constraints (fast, deterministic,
#               but blind to config/country/tenant rules that only apply at render time).
#   "runtime" - the harness reads the LIVE element's actually-applied constraints (real maxLength/type/
#               min/max/pattern) and generates the value in-browser (catches config-driven rules like a
#               maxLength set from appProperties, per-country MSISDN length).
#   "both"    - runtime, seeded with the census semantic hint (so it knows "this is a phone" even when
#               the DOM type is a plain text input). Recommended default.
_CONSTRAINT_MODES = ("static", "runtime", "both")


# interactionPattern -> canonical control kind. Covers the full 2026 component landscape (ARIA APG
# patterns + HTML input types + modern form-control families). Aliases fold into one kind.
_CONTROL_ALIASES = {
    "multiselect": {"multiselect", "multi-select", "multiple", "multi", "tokenselect", "tagselect"},
    "select": {"select", "dropdown", "nativeselect"},
    "reactselect": {"react-select", "reactselect", "listbox", "combobox-single", "picker", "antselect", "muiselect"},
    "combobox": {"combobox", "autocomplete", "typeahead", "async-select", "search-select", "asyncselect"},
    "tags": {"tags", "chips", "tokens", "tokenfield", "taginput", "chipinput", "tagsinput"},
    "toggle": {"checkbox", "radio", "toggle", "switch", "checkbutton", "radiobutton"},
    "segmented": {"segmented", "segmentedcontrol", "buttongroup", "togglegroup", "radiogroup", "chipgroup"},
    "rating": {"rating", "stars", "starrating", "rate"},
    "slider": {"slider", "range", "rangeslider", "track"},
    "color": {"color", "colorpicker", "colour"},
    "stepper": {"stepper", "spinbutton", "spinner", "numberstepper", "numberinput", "quantity"},
    "otp": {"otp", "pin", "pincode", "verificationcode", "onetimecode", "codeinput", "segmentedinput"},
    "date": {"date", "datepicker", "datetime", "datetime-local", "time", "timepicker", "daterange",
             "calendar", "month", "week"},
    "richtext": {"richtext", "rich-text", "wysiwyg", "contenteditable", "editor", "prosemirror",
                 "slate", "quill", "tiptap", "codeeditor", "markdown"},
    "file": {"file", "file-upload", "upload", "dropzone", "filedrop", "attachment"},
    "drag": {"drag", "drag-drop", "draggable", "sortable", "reorder"},
    "masked": {"masked", "mask", "inputmask", "phone", "tel", "msisdn", "creditcard", "ssn"},
}


def _control_kind(pat: str) -> str:
    for kind, names in _CONTROL_ALIASES.items():
        if pat in names:
            return kind
    return "text"


def _fill_step(field: dict, constraint_source: str = "static") -> list[dict]:
    """Interaction step(s) for a field, honoring its interactionPattern across EVERY control type the
    web has in 2026: text/textarea, select, multiselect, combobox/autocomplete, tags/chips, checkbox/
    radio/switch, segmented/button-group, rating, slider/range, color, number-stepper, OTP/PIN, date/
    time/range, rich-text/WYSIWYG editors, masked (phone/MSISDN), file-upload, drag. Returns [] when
    unusable (read-only, or a file-upload with no test file). A list because some controls need >1 step
    (combobox = type + Enter, tags = type + Enter per tag).

    When constraint_source is 'runtime'/'both', VALUE-bearing text/number/masked fields become a
    `fillunique` step: the harness reads the element's ACTUALLY-APPLIED constraints (real maxLength/type/
    min/max) at run time and generates the value in-browser - so config/country/tenant rules that are
    not literal in the source (a maxLength from appProperties, a per-country MSISDN length) are honored.
    'static' keeps the Python-generated value from the census."""
    sel = _first(field.get("domSelectors"), field.get("selector"))
    if not sel or field.get("readonly") or field.get("disabled"):
        return []
    kind = _control_kind(_s(field.get("interactionPattern")).lower())
    label = _s(field.get("label")) or _s(field.get("fieldName")) or sel
    val = _field_value(field)
    runtime = constraint_source in ("runtime", "both")

    def one(action, value="", desc=""):
        return [{"action": action, "target": sel, "value": value, "description": desc or f"Set {label}"}]

    def value_fill(desc):
        # runtime -> read the live constraints; static -> the census-derived value. When the census
        # DECLARES a maxLength, pass it in as `cmax=<n>`: the harness uses min(live-maxlength, cmax) so a
        # field the app JS-validates (no maxlength attr on the DOM) is still capped to the code's limit.
        if runtime:
            cmax = _maxlen(field)
            tail = f" (uniq={_UNIQ}" + (f" cmax={cmax}" if cmax else "") + ")"
            return [{"action": "fillunique", "target": sel, "value": _field_hint(field),
                     "description": f"{desc}{tail}"}]
        return one("smartfill" if kind == "text" else "fill", val, desc)

    if kind == "multiselect":
        opts = [_s(o) for o in (field.get("dropdownOptions") or []) if _s(o)][:2] or [val]
        return one("multiselect", ",".join(opts), f"Multi-select {label}")
    if kind == "select":
        return one("select", val, f"Select {label}")
    if kind == "reactselect":
        # non-native dropdown (react-select / antd / MUI): open + pick the first option via keyboard.
        return one("pickoption", "", f"Select {label} (first option)")
    if kind == "combobox":
        # type a query, then pick with Enter (comboboxes accept Enter to choose the first match).
        return [{"action": "fill", "target": sel, "value": val, "description": f"Type into {label} (combobox)"},
                {"action": "press", "target": sel, "value": "Enter", "description": f"Pick a {label} match"}]
    if kind == "tags":
        # enter two chips: type + Enter each (token/chip inputs commit on Enter).
        out = []
        for tag in (val, "sample"):
            out.append({"action": "fill", "target": sel, "value": tag, "description": f"Type tag in {label}"})
            out.append({"action": "press", "target": sel, "value": "Enter", "description": f"Commit tag in {label}"})
        return out
    if kind == "toggle":
        return one("check", "", f"Check {label}")
    if kind in ("segmented", "rating"):
        # select an option / a star: click the control (census may point domSelectors at the option).
        return one("click", "", f"{'Rate' if kind == 'rating' else 'Choose'} {label}")
    if kind == "slider":
        return one("setvalue", val if val.isdigit() else "50", f"Set slider {label}")
    if kind == "color":
        return one("setvalue", "#3366cc", f"Pick color {label}")
    if kind == "stepper":
        return value_fill(f"Fill {label}") if runtime else one("fill", val if val.isdigit() else "5", f"Set number {label}")
    if kind == "otp":
        return one("fill", "123456", f"Enter OTP {label}")
    if kind == "date":
        return one("fill", (val if "-" in val else "2025-01-01"), f"Set date {label}")
    if kind == "richtext":
        # contenteditable / WYSIWYG editors need real key events, not .fill().
        return one("type", val, f"Type into {label} (rich-text)")
    if kind == "masked":
        return value_fill(f"Fill {label}") if runtime else one("fill", "9876543210", f"Enter {label} (masked)")
    if kind == "file":
        fp = _s(field.get("testFile"))
        return one("upload", fp, f"Upload {label}") if fp else []
    if kind == "drag":
        dest = _first(field.get("dropTarget"))
        return [{"action": "draganddrop", "target": sel, "value": dest, "description": f"Drag {label}"}] if dest else []
    # DEFAULT (census gave no / an unknown pattern): runtime -> fillunique (reads live constraints);
    # static -> smartfill (Python value + dynamic control detection). Description stays "Fill ..." so the
    # create path can ride the XSS canary on it.
    return value_fill(f"Fill {label}")


def _modal_close(func: dict) -> dict | None:
    sel = _first(func.get("cancelButton", {}).get("selectors") if isinstance(func.get("cancelButton"), dict) else None,
                 func.get("closeSelectors"))
    if sel:
        return {"action": "click", "target": sel, "value": "", "description": "Close the modal"}
    return None


def _validation_for(func: dict, model: dict) -> tuple[str, str]:
    """Return (selector, expected_text) for a mandatory-validation assert on this modal, or ("","")."""
    # per-functionality inline errors first
    ie = func.get("inlineErrors") if isinstance(func.get("inlineErrors"), dict) else {}
    for m in (ie.get("messages") or []):
        if isinstance(m, dict) and _s(m.get("text")):
            sel = _first(ie.get("selectors")) or "body"
            return sel, _s(m.get("text"))
    nt = func.get("notifications") if isinstance(func.get("notifications"), dict) else {}
    for m in (nt.get("messages") or []):
        if isinstance(m, dict) and _s(m.get("text")) and _s(m.get("type")) in ("warning", "error"):
            sel = _first(nt.get("messageSelector"), nt.get("selectors"), nt.get("containerSelector")) or "body"
            return sel, _s(m.get("text"))
    # fall back to the global validationMatrix
    for row in (model.get("validationMatrix") or []):
        if isinstance(row, dict) and _s(row.get("errorMessage")):
            return "body", _s(row.get("errorMessage"))
    return "", ""


def census_to_flow(model: dict, url: str, test_writes: bool = True,
                   constraint_source: str = "static") -> list[dict]:
    """Convert a UI Element Census into a comprehensive ordered UiStep flow. Defensive: returns at
    least a goto+wait when the census is sparse; never raises."""
    if not isinstance(model, dict):
        return [{"action": "goto", "target": url, "value": "", "description": "Open the screen"}]

    funcs = [f for f in (model.get("functionalities") or []) if isinstance(f, dict)]
    type_by_id = {}
    for row in (model.get("functionalitySummaryTable") or []):
        if isinstance(row, dict) and _s(row.get("id")):
            type_by_id[_s(row["id"])] = _s(row.get("type"))
    kinded = sorted(((f, _kind(f, type_by_id)) for f in funcs),
                    key=lambda fk: _KIND_ORDER.get(fk[1], 10))

    steps: list[dict] = []
    steps.append({"action": "goto", "target": url, "value": "",
                  "description": "Open the screen (session restored)"})
    # screen-ready anchor: the first available trigger selector, else body.
    anchor = ""
    for f, _k in kinded:
        cand = _first((f.get("modalDetails") or {}).get("triggerSelector"))
        if cand and _valid_css(cand):
            anchor = cand
            break
    anchor = anchor or "body"
    # The anchor waitfor IS the authenticated-render proof (a post-login control is visible). We do
    # not assert screenName on body text - a heading like "All Teams | N" loads after an async API
    # call and would race the assert (a false failure that is not a real defect).
    steps.append({"action": "waitfor", "target": anchor, "value": "",
                  "description": "Wait for the authenticated screen to render (past login)"})
    # ACCESSIBILITY (always-on): audit the rendered screen for WCAG high-signal violations (missing
    # alt/labels/accessible-names, no html lang, duplicate ids). One scan per screen.
    steps.append({"action": "a11y", "target": "", "value": "",
                  "description": "ACCESSIBILITY: WCAG audit of the screen"})
    # PERFORMANCE (always-on): assert the screen loaded within a budget (Navigation Timing). 8s is a
    # realistic ceiling for a cold SPA load incl auth restore; a genuinely slow screen still fails.
    steps.append({"action": "perf", "target": "", "value": "8000",
                  "description": "PERFORMANCE: screen loads within budget"})
    # VISUAL REGRESSION (soft): one baseline screenshot of the rendered screen. First run captures the
    # baseline (pass); later runs pixel-diff and fail on a change over threshold. Soft so a first run
    # (no baseline) or a missing diff lib never hard-fails.
    steps.append({"action": "screenshot", "target": "", "value": "screen", "soft": True,
                  "description": "VISUAL: screen baseline"})

    # the search input lets create/edit VERIFY a saved record (search the tag, assert it is listed)
    # and lets edit TARGET the created row. "" when the screen has no search.
    search_sel = ""
    for f, k in kinded:
        if k == "search":
            search_sel = _first((f.get("modalDetails") or {}).get("triggerSelector"))
            break

    # a create is WRITABLE only if it captured a real form (flat fields or wizard steps) AND a submit -
    # a bespoke builder (privilege dual-list, AND/OR rule tree) opens a modal but exposes no fillable
    # form, so it cannot produce a record; edit/delete must then degrade to structural (no tag hunt).
    cf = next((f for f, k in kinded if k == "create"), None)
    create_writable = bool(cf) and (
        bool([x for x in (cf.get("fields") or []) if isinstance(x, dict)]) or
        bool([x for x in (cf.get("steps") or []) if isinstance(x, dict)])) and bool(
        cf.get("submitButton", {}).get("selectors") if isinstance(cf.get("submitButton"), dict) else None)

    for func, kind in kinded:
        md = func.get("modalDetails") if isinstance(func.get("modalDetails"), dict) else {}
        trig = _first(md.get("triggerSelector"))
        modal = _first(md.get("modalSelector"))
        name = _s(func.get("functionality")) or kind
        _emit(steps, kind, func, md, trig, modal, name, model, test_writes, anchor, url, search_sel,
              constraint_source, create_writable)

    # WORKFLOW (delete + verify gone) = the CLEANUP: it runs RIGHT AFTER the create/edit lifecycle so
    # the whole create -> edit(+revert) -> delete chain is contiguous, and it GUARANTEES the record the
    # create made is removed (a create test never leaves a lasting record). Because edit reverts to the
    # original value, the record's final name is _TAG (the original), so delete targets _TAG.
    create_func = next((f for f, k in kinded if k == "create"), None)
    if test_writes and create_func is not None and create_writable:
        delete_func = next((f for f, k in kinded if k == "delete"), None)
        cfields = [f for f in (create_func.get("fields") or []) if isinstance(f, dict)]
        if not cfields and isinstance(create_func.get("steps"), list):   # wizard: fields live in steps
            cfields = [f for st in create_func["steps"] if isinstance(st, dict)
                       for f in (st.get("fields") or []) if isinstance(f, dict)]
        cidf = _identifying_field(cfields)
        final = _tag_for(cidf, _TAG) if cidf else _TAG
        if delete_func is not None and search_sel:
            from icx_engine.testing.analyzers.workflow_cases import delete_verify_steps
            steps.extend(delete_verify_steps(delete_func, search_sel, final, url=url, row_scope=_row_scoped))

    # SECURITY (XSS) + DUPLICATE run after the lifecycle: both reopen the create form (XSS via a clean
    # re-goto, dup via re-submit). Placed here they act on stable state and cannot starve the delete.
    if test_writes and create_func is not None and create_writable:
        cmd = create_func.get("modalDetails") if isinstance(create_func.get("modalDetails"), dict) else {}
        ctrig = _first(cmd.get("triggerSelector"))
        cmodal = _first(cmd.get("modalSelector"))
        csubmit = _first(create_func.get("submitButton", {}).get("selectors") if isinstance(create_func.get("submitButton"), dict) else None)
        cfields = [f for f in (create_func.get("fields") or []) if isinstance(f, dict)]
        if ctrig and csubmit and cfields:
            _emit_xss_create(steps, create_func, ctrig, cmodal, csubmit, cfields, anchor, url)
            from icx_engine.testing.analyzers.workflow_cases import dup_create_steps
            steps.extend(dup_create_steps(create_func, ctrig, cmodal, csubmit, _TAG, model, url=url))

    # SECURITY (reflected DOM-XSS via the URL) - load the screen once with a hostile URL parameter and
    # assert it does not execute. Read-only; placed before the backend-faulting error case.
    if url and anchor:
        from icx_engine.testing.analyzers.security_cases import ui_url_xss_steps
        steps.extend(ui_url_xss_steps(url, anchor))

    # ERROR-HANDLING runs LAST: it deliberately faults the backend (routes an endpoint to 500 + empties
    # the list), so running it earlier would starve view/edit/create/delete of rows. Prefer an explicit
    # refresh, else ANY functionality that declares an API endpoint.
    from icx_engine.testing.analyzers.error_cases import error_steps, endpoint_of
    err_func = next((f for f, k in kinded if k == "refresh" and endpoint_of(f)), None)
    if err_func is None:
        err_func = next((f for f, _k in kinded if endpoint_of(f)), None)
    if err_func is not None:
        rtrig = _first((err_func.get("modalDetails") or {}).get("triggerSelector"))
        steps.extend(error_steps(err_func, rtrig, anchor, url=url))

    # DATAFLOW (soft, runs LAST): assert the screen degrades gracefully under a SLOW network - apply a
    # slow profile, re-assert the screen anchor is still present, then RESET so normal network is
    # restored. Soft so a genuinely slow app is flagged, not hard-failed. Reset last = no lingering
    # throttle on the context.
    steps.append({"action": "netprofile", "target": "", "value": "slow",
                  "description": "DATAFLOW: apply slow network"})
    steps.append({"action": "waitfor", "target": anchor, "value": "", "soft": True,
                  "description": "DATAFLOW: graceful under slow network"})
    steps.append({"action": "netprofile", "target": "", "value": "reset",
                  "description": "DATAFLOW: reset network"})

    return steps


def _emit(steps, kind, func, md, trig, modal, name, model, test_writes, anchor="body", url="", search_sel="",
          constraint_source="static", create_writable=True):
    if kind == "render":
        # DASHBOARD / ANALYTICS coverage: assert each discovered widget (chart / grid / KPI card)
        # actually RENDERS on the screen - this is the meaningful test for a screen with no CRUD.
        widgets = [w for w in (func.get("widgets") or []) if isinstance(w, dict)]
        emitted = 0
        for w in widgets:
            wsel = _first(w.get("selector"), w.get("domSelectors"))
            if not wsel or not _valid_css(wsel):
                continue
            wk = _s(w.get("kind")) or "widget"
            if emitted == 0:
                # the FIRST widget must be VISIBLE - this is the proof the screen actually painted past
                # login (a hard render gate).
                steps.append({"action": "waitfor", "target": wsel, "value": "",
                              "description": f"RENDER: {wk} present"})
            else:
                # every OTHER widget need only be BUILT INTO THE DOM - wait for it ATTACHED (not
                # "visible"): a chart div with no backend data collapses to zero height, and a chart
                # library often renders its container a beat AFTER first paint. `waitfor attached` waits
                # (up to the step timeout) for the element to exist in the DOM - tolerating async render
                # and zero-height empty data - yet still FAILS a widget that genuinely never renders. An
                # instant existence assert here false-failed async-drawn charts.
                steps.append({"action": "waitfor", "target": wsel, "value": "attached",
                              "description": f"RENDER: {wk} built"})
            if wk == "chart":
                steps.append({"action": "assertjs", "target": _chart_drew_expr(wsel), "value": "",
                              "soft": True, "description": f"RENDER: {wk} drew data"})
            emitted += 1
            if emitted >= _RENDER_MAX_WIDGETS:
                break
        # graceful: the screen is not blank / not a crashed error boundary (soft - data may be empty).
        steps.append({"action": "assertjs", "target": _NO_ERROR_EXPR, "value": "", "soft": True,
                      "description": "RENDER: screen is not blank / no crash"})
        return
    if kind == "report":
        # REPORT coverage: set the page-level filters, click Generate/Apply, and assert the result region
        # renders (data OR a clean empty state, never an error/blank). The result assert is graceful.
        rfields = [f for f in (func.get("fields") or []) if isinstance(f, dict)]
        for fld in rfields[:_REPORT_MAX_FILTERS]:
            steps.extend(_fill_step(fld, constraint_source))
        apply = _first(func.get("submitButton", {}).get("selectors") if isinstance(func.get("submitButton"), dict) else None)
        if apply:
            steps.append({"action": "click", "target": apply, "value": "",
                          "description": f"{name}: apply filters / generate"})
        result = _first(func.get("resultSelector")) or anchor
        steps.append({"action": "waitfor", "target": result, "value": "",
                      "description": f"{name}: result region renders"})
        steps.append({"action": "assertjs", "target": _NO_ERROR_EXPR, "value": "", "soft": True,
                      "description": f"{name}: no error after generate"})
        return
    if kind in ("list", "other") and not trig:
        return
    if kind == "list":
        return  # the goto+wait already covered rendering; assert handled at anchor
    if kind == "download":
        if trig:
            steps.append({"action": "waitfor", "target": trig, "value": "", "description": f"{name}: control ready"})
            steps.append({"action": "download", "target": trig, "value": "",
                          "description": f"DOWNLOAD: {name} produces a file"})
        return
    if kind in ("search", "filter"):
        sel = trig or _first((func.get("fields") or [{}])[0].get("domSelectors") if func.get("fields") else None)
        if sel:
            steps.append({"action": "fill", "target": sel, "value": "a", "description": f"{name}: type a query"})
            # SECURITY (always-on for any input that reaches the backend): reflected XSS must not
            # execute + a hostile SQLi query must not crash the app. Not limited to form fields.
            from icx_engine.testing.analyzers.security_cases import ui_input_security_steps
            steps.extend(ui_input_security_steps(sel, anchor, name))
        return
    if kind in ("refresh", "sort", "pagination", "pagesize"):
        if trig:
            act = "fill" if kind == "pagesize" else "click"
            steps.append({"action": act, "target": trig, "value": ("10" if kind == "pagesize" else ""),
                          "description": f"{name}"})
        return
    if kind == "view":
        if not trig:
            return
        # waitfor the row trigger to SETTLE before clicking - a create/refresh just re-rendered the
        # table, and clicking mid-render races the row out from under the click (a real flake source).
        steps.append({"action": "waitfor", "target": trig, "value": "", "description": f"{name}: row ready"})
        steps.append({"action": "click", "target": trig, "value": "", "description": f"Open {name}"})
        if modal:
            steps.append({"action": "waitfor", "target": modal, "value": "", "description": f"{name} modal opens"})
        title = _s(md.get("modalName"))
        if title and modal:
            steps.append({"action": "assert", "target": modal, "value": title,
                          "description": f"{name} header '{title}'"})
        close = _modal_close(func)
        if close:
            steps.append(close)
        return
    if kind == "delete":
        # DATA SAFETY: the ONLY delete is the ROW-SCOPED delete-verify workflow (woven in census_to_flow
        # when create+delete+search exist) - it deletes only the record WE created. A plain first-row
        # delete here would risk removing EXISTING data (a delete icon is not record-specific), so this
        # kind emits nothing on its own.
        return
    if kind == "edit" and not create_writable:
        # create did not produce a fillable form (a bespoke builder - dual-list privilege matrix, AND/OR
        # rule tree), so there is no record WE created to edit + no writable form. Cover EDIT structurally
        # (open a row's edit, assert its modal, close) - read-only, no write, no tag search that would
        # false-fail hunting a record that was never created.
        if not trig:
            return
        steps.append({"action": "waitfor", "target": trig, "value": "", "description": f"{name}: row ready"})
        steps.append({"action": "click", "target": trig, "value": "", "description": f"Open {name}"})
        if modal:
            steps.append({"action": "waitfor", "target": modal, "value": "", "description": f"{name} modal opens"})
            title = _s(md.get("modalName"))
            if title:
                steps.append({"action": "assert", "target": modal, "value": title, "description": f"{name} header '{title}'"})
        close = _modal_close(func)
        if close:
            steps.append(close)
        return
    if kind in ("create", "edit"):
        _emit_form(steps, kind, func, md, trig, modal, name, model, test_writes, anchor, url, search_sel,
                   constraint_source)


def _identifying_field(fields: list[dict]) -> dict | None:
    """The plain text field we tag so the record can be searched + verified. Prefer a text field whose
    maxLength can hold the full tag (so the tag stays unique + searchable); else the roomiest text
    field; else the first field."""
    texts = [f for f in fields if _control_kind(_s(f.get("interactionPattern")).lower()) == "text"]
    if not texts:
        return fields[0] if fields else None
    roomy = [f for f in texts if (_maxlen(f) or 999) >= len(_TAG)]
    if roomy:
        return roomy[0]
    return max(texts, key=lambda f: _maxlen(f) or 999)


def _row_scoped(trigger: str, value: str) -> str:
    """DATA SAFETY: a row-action trigger (edit/delete icon) scoped to the row that CONTAINS our unique
    tag, so it can ONLY ever act on the record WE created - existing data (different text) never
    matches, and if our record is absent the selector matches nothing (the action is skipped/fails
    rather than touching someone else's row). Uses Playwright `:has-text` on the table row (`tr`);
    a census may override with an explicit rowSelector when the list is not tr-based."""
    if not (trigger and value):
        return trigger
    v = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'tr:has-text("{v}") {trigger}'


def _tag_for(field: dict, base: str) -> str:
    """The identifying value fitted to the field's maxLength - used for the fill AND the search/verify
    so they always match what was saved. Truncation PRESERVES the unique token (trims the label prefix,
    not the token) so the record stays unique + findable even in a short field."""
    ml = _maxlen(field)
    if ml is None or ml >= len(base):
        return base
    for tok in (_UNIQ, _UNIQ_EDIT):
        if base.endswith(tok):
            prefix = base[:-len(tok)]
            return (prefix[:ml - len(tok)] + tok) if ml >= len(tok) else tok[-ml:]
    return base[:ml]


def _emit_form(steps, kind, func, md, trig, modal, name, model, test_writes, anchor, url, search_sel="",
               constraint_source="static"):
    """Full end-to-end create/edit. Both open the form, assert the header, and run the submit-free
    constraint checks. Then the real lifecycle write:
      - create: fill every field with a VALID, code-derived value (honoring length/format), tag the
        identifying field with _TAG, SAVE (real write), then VERIFY the record is listed (search _TAG,
        assert it appears). A separate XSS case (its own record via re-goto) proves fields are XSS-safe.
      - edit: search _TAG to TARGET the record just created, open its edit form, change the identifying
        field to _TAG_EDITED, SAVE (real update), then VERIFY the change persisted (search _TAG_EDITED).
    create runs before edit (see _KIND_ORDER), so edit and delete act on a record that exists.

    MULTI-STEP WIZARD: when the functionality carries a `steps` array (each step = {name, tabSelector?,
    nextButton{selectors}, fields[]}), the form is navigated step-by-step (fill a step -> click NEXT ->
    ... -> final submit) instead of one flat modal. Handled by _emit_wizard."""
    if not trig:
        return
    if isinstance(func.get("steps"), list) and func.get("steps"):
        _emit_wizard(steps, kind, func, md, trig, modal, name, model, test_writes, anchor, url,
                     search_sel, constraint_source)
        return
    submit = _first(func.get("submitButton", {}).get("selectors") if isinstance(func.get("submitButton"), dict) else None)
    fields = [f for f in (func.get("fields") or []) if isinstance(f, dict)]
    vsel, vtext = _validation_for(func, model)
    idf = _identifying_field(fields)
    id_sel = _first(idf.get("domSelectors"), idf.get("selector")) if idf else ""
    # tags truncated to the identifying field's maxLength so the app accepts them AND search/verify
    # look for exactly what was saved. This is the code-inferred length factor applied end to end.
    tag = _tag_for(idf, _TAG) if idf else _TAG
    tag_edited = _tag_for(idf, _TAG_EDITED) if idf else _TAG_EDITED

    # EDIT targets ONLY the record WE created: narrow the list to the created tag, then open via a
    # ROW-SCOPED trigger (the edit icon in the row that contains our unique tag) - it can never open a
    # stranger's row. `type` fires real keystrokes so the table's search handler (server refetch) runs.
    open_trig = trig
    if kind == "edit" and search_sel and test_writes:
        open_trig = _row_scoped(trig, tag)
        steps.append({"action": "type", "target": search_sel, "value": tag,
                      "description": f"EDIT: find OUR created record ('{tag}')"})
        steps.append({"action": "waitfor", "target": open_trig, "value": "", "description": "EDIT: our tagged row ready"})
    elif kind == "edit":
        steps.append({"action": "waitfor", "target": trig, "value": "", "description": f"{name}: row ready"})

    steps.append({"action": "click", "target": open_trig, "value": "", "description": f"Open {name}"})
    if modal:
        steps.append({"action": "waitfor", "target": modal, "value": "", "description": f"{name} modal opens"})
    title = _s(md.get("modalName"))
    if title and modal:
        steps.append({"action": "assert", "target": modal, "value": title, "description": f"{name} header '{title}'"})

    # NEGATIVE (create only): submit empty -> assert the mandatory-validation message. A reject keeps
    # the form open for the constraint checks that follow.
    if submit and vtext and kind == "create":
        steps.append({"action": "click", "target": submit, "value": "", "description": "NEGATIVE: submit the empty form"})
        steps.append({"action": "waitfor", "target": vsel, "value": "", "description": "Validation appears"})
        steps.append({"action": "assert", "target": vsel, "value": vtext,
                      "description": f"Validation message: '{vtext[:40]}'"})
    # CONSTRAINTS (submit-free): maxLength / minLength / range / format / pattern per field.
    from icx_engine.testing.analyzers.constraint_cases import constraint_steps
    bcount = 0
    for fld in fields:
        cs = constraint_steps(fld)
        if cs:
            steps.extend(cs)
            bcount += 1
        if bcount >= _BOUNDARY_MAX_FIELDS:
            break

    if not (submit and test_writes):
        # writes OFF: fill valid values then CANCEL (no mutating write).
        for fld in fields:
            steps.extend(_fill_step(fld, constraint_source))
        close = _modal_close(func)
        if close:
            steps.append(close)
        return

    if kind == "create":
        # FUNCTIONAL create: the identifying field gets the known STATIC tag (so we can search+verify
        # it); every OTHER field uses the chosen constraint_source (static or runtime).
        for fld in fields:
            fsel = _first(fld.get("domSelectors"), fld.get("selector"))
            if id_sel and fsel == id_sel:
                steps.append({"action": "fill", "target": id_sel, "value": tag,
                              "description": f"CREATE: {name} '{tag}' (identifying value)"})
                continue
            for st in _fill_step(fld, constraint_source):
                steps.append(st)
        steps.append({"action": "click", "target": submit, "value": "",
                      "description": "CREATE: SAVE the new record (real write)"})
        if modal:
            # the modal must CLOSE on a successful save - if it does not, the save was rejected and we
            # surface that here instead of a later click timing out against the covering modal.
            steps.append({"action": "waithidden", "target": modal, "value": "",
                          "description": "CREATE: form closes on successful save"})
        steps.append({"action": "waitfor", "target": anchor, "value": "", "description": "CREATE: list settles"})
        # VERIFY the save is real: search the tag and assert it is listed.
        if search_sel and id_sel:
            steps.append({"action": "type", "target": search_sel, "value": tag, "description": "VERIFY: search the saved record"})
            steps.append({"action": "waitfor", "target": anchor, "value": "", "description": "VERIFY: results react"})
            steps.append({"action": "assert", "target": "body", "value": tag,
                          "description": f"VERIFY: created record '{tag}' IS saved + listed"})
            steps.append({"action": "fill", "target": search_sel, "value": "", "description": "VERIFY: clear search"})
            # DATAFLOW: confirm the record actually landed in the DB, not just the UI list - keyed to the
            # SAME tag the UI create just saved. NOT soft: an unset ICX_SQL_VERIFY_CMD already SKIPS
            # inside the action (never throws), so a default run is unaffected; but when a DB check IS
            # configured and the record is genuinely absent, that is a real data-integrity bug and MUST
            # fail loud (the whole point of the DB check), not be swallowed as a skip.
            steps.append({"action": "dbverify", "target": "", "value": tag,
                          "description": "DATAFLOW: DB verify record"})
        # NOTE: the XSS-payload create + the duplicate case run LAST (in census_to_flow), NOT here - a
        # re-goto/reopen mid-flow would cascade into the edit that follows. Keeping the functional
        # create -> verify -> edit -> verify chain contiguous makes it stable.
        return

    # EDIT: the constraint hostile-fills above dirtied the fields - refill EVERY field CLEAN. The
    # identifying field gets the known STATIC edited tag (searchable); others use constraint_source.
    for fld in fields:
        fsel = _first(fld.get("domSelectors"), fld.get("selector"))
        if id_sel and fsel == id_sel:
            steps.append({"action": "fill", "target": id_sel, "value": tag_edited,
                          "description": f"EDIT: change to '{tag_edited}'"})
            continue
        for st in _fill_step(fld, constraint_source):
            steps.append(st)
    steps.append({"action": "click", "target": submit, "value": "",
                  "description": "EDIT: SAVE the update (real write)"})
    if modal:
        steps.append({"action": "waithidden", "target": modal, "value": "",
                      "description": "EDIT: form closes on successful save"})
    steps.append({"action": "waitfor", "target": anchor, "value": "", "description": "EDIT: list settles"})
    if search_sel and id_sel:
        steps.append({"action": "type", "target": search_sel, "value": tag_edited, "description": "VERIFY: search the updated record"})
        steps.append({"action": "waitfor", "target": anchor, "value": "", "description": "VERIFY: results react"})
        steps.append({"action": "assert", "target": "body", "value": tag_edited,
                      "description": f"VERIFY: update '{tag_edited}' IS saved + listed"})
        steps.append({"action": "fill", "target": search_sel, "value": "", "description": "VERIFY: clear search"})
    # REVERT to the original state: an edit must not leave the record permanently changed. Re-open,
    # change the identifying field back to its pre-edit value, save, verify. Delete then targets the
    # original name (the record ends where it started, then is removed by the cleanup delete).
    _revert_edit(steps, trig, modal, submit, id_sel, tag, tag_edited, anchor, search_sel)


def _revert_edit(steps, trig, modal, submit, id_sel, original, edited, anchor, search_sel):
    """Undo an edit: find the edited record, reopen it, set the identifying field BACK to its original
    value, save, and verify the revert. Leaves the record in its pre-edit state (proper state handling
    - an edit test never leaves lasting changes)."""
    if not (id_sel and submit):
        return
    # ROW-SCOPED to the edited value so revert reopens ONLY our record, never existing data.
    open_trig = _row_scoped(trig, edited) if search_sel else trig
    if search_sel:
        steps.append({"action": "type", "target": search_sel, "value": edited,
                      "description": f"REVERT: find OUR edited record ('{edited}')"})
    steps.append({"action": "waitfor", "target": open_trig, "value": "", "description": "REVERT: our row ready"})
    steps.append({"action": "click", "target": open_trig, "value": "", "description": "REVERT: reopen the record"})
    if modal:
        steps.append({"action": "waitfor", "target": modal, "value": "", "description": "REVERT: form opens"})
    steps.append({"action": "fill", "target": id_sel, "value": original,
                  "description": f"REVERT: restore original value '{original}'"})
    steps.append({"action": "click", "target": submit, "value": "", "description": "REVERT: SAVE (back to original)"})
    if modal:
        steps.append({"action": "waithidden", "target": modal, "value": "", "description": "REVERT: form closes"})
    steps.append({"action": "waitfor", "target": anchor, "value": "", "description": "REVERT: list settles"})
    if search_sel:
        steps.append({"action": "type", "target": search_sel, "value": original, "description": "VERIFY: search the reverted record"})
        steps.append({"action": "waitfor", "target": anchor, "value": "", "description": "VERIFY: results react"})
        steps.append({"action": "assert", "target": "body", "value": original,
                      "description": f"VERIFY: record reverted to original '{original}'"})
        steps.append({"action": "fill", "target": search_sel, "value": "", "description": "VERIFY: clear search"})


def _emit_wizard(steps, kind, func, md, trig, modal, name, model, test_writes, anchor, url,
                 search_sel, constraint_source):
    """Multi-step WIZARD create/edit. The census `steps` array models the wizard: each step optionally
    clicks a tab/step header to reach it, fills its fields, then clicks NEXT to advance; the last step
    uses the functionality's submit (CREATE/UPDATE). Navigation-gated wizards validate each step, so
    the hostile negative/constraint probes are NOT run here (they would block NEXT) - the wizard test
    is the real end-to-end write: fill every step validly -> advance -> submit -> verify."""
    wsteps = [s for s in func.get("steps") if isinstance(s, dict)]
    submit = _first(func.get("submitButton", {}).get("selectors") if isinstance(func.get("submitButton"), dict) else None)
    all_fields = [f for st in wsteps for f in (st.get("fields") or []) if isinstance(f, dict)]
    idf = _identifying_field(all_fields)
    id_sel = _first(idf.get("domSelectors"), idf.get("selector")) if idf else ""
    tag = _tag_for(idf, _TAG) if idf else _TAG
    tag_edited = _tag_for(idf, _TAG_EDITED) if idf else _TAG_EDITED

    writes = bool(submit and test_writes)
    last = len(wsteps) - 1

    def run_pass(value, verb, fill_others, find_value):
        # open (find the row first when editing/reverting), navigate every step, fill the identity with
        # `value` (and all fields when fill_others), submit, verify. Reusable so an edit can EDIT then
        # REVERT by running twice.
        # ROW-SCOPE the open trigger to OUR record's value so edit/revert never touch existing data.
        open_trig = trig
        if find_value is not None and search_sel:
            open_trig = _row_scoped(trig, find_value)
            steps.append({"action": "type", "target": search_sel, "value": find_value,
                          "description": f"{verb}: find OUR record ('{find_value}')"})
        if kind == "edit":
            steps.append({"action": "waitfor", "target": open_trig, "value": "", "description": f"{verb}: our row ready"})
        steps.append({"action": "click", "target": open_trig, "value": "", "description": f"{verb}: open {name}"})
        if modal:
            steps.append({"action": "waitfor", "target": modal, "value": "", "description": f"{verb}: form opens"})
        title = _s(md.get("modalName"))
        if title and modal and verb == "CREATE":
            steps.append({"action": "assert", "target": modal, "value": title, "description": f"{name} header '{title}'"})
        for i, st in enumerate(wsteps):
            sname = _s(st.get("name")) or f"step {i + 1}"
            tab = _first(st.get("tabSelector"), st.get("tab"))
            if tab:
                steps.append({"action": "click", "target": tab, "value": "", "description": f"{verb} wizard: go to '{sname}'"})
                steps.append({"action": "waitfor", "target": tab, "value": "", "description": f"{verb} wizard: '{sname}' active"})
            for fld in [f for f in (st.get("fields") or []) if isinstance(f, dict)]:
                fsel = _first(fld.get("domSelectors"), fld.get("selector"))
                if id_sel and fsel == id_sel:
                    steps.append({"action": "fill", "target": id_sel, "value": value,
                                  "description": f"{verb}: {name} '{value}' (identifying value)"})
                    continue
                if not fill_others:      # edit/revert: leave pre-filled fields untouched
                    continue
                for stp in _fill_step(fld, constraint_source):
                    steps.append(stp)
            nb = _first(st.get("nextButton", {}).get("selectors") if isinstance(st.get("nextButton"), dict) else None,
                        st.get("nextButton"))
            if i < last and nb:
                # a field's async check (e.g. an msisdn duplicate probe) can raise a confirm popup that
                # covers NEXT - dismiss it best-effort before advancing.
                steps.append({"action": "confirmdialog", "target": "", "value": "",
                              "description": f"{verb} wizard: clear any confirm popup before NEXT"})
                steps.append({"action": "click", "target": nb, "value": "", "description": f"{verb} wizard: NEXT ('{sname}' -> next)"})
        # clear any async-check confirm popup covering the submit, then save.
        steps.append({"action": "confirmdialog", "target": "", "value": "",
                      "description": f"{verb}: clear any confirm popup before save"})
        steps.append({"action": "click", "target": submit, "value": "", "description": f"{verb}: SAVE (real write)"})
        if modal:
            steps.append({"action": "waithidden", "target": modal, "value": "", "description": f"{verb}: wizard closes on save"})
        steps.append({"action": "waitfor", "target": anchor, "value": "", "description": f"{verb}: list settles"})
        if search_sel and id_sel:
            steps.append({"action": "type", "target": search_sel, "value": value, "description": f"{verb} VERIFY: search the record"})
            steps.append({"action": "waitfor", "target": anchor, "value": "", "description": f"{verb} VERIFY: results react"})
            steps.append({"action": "assert", "target": "body", "value": value,
                          "description": f"{verb} VERIFY: record '{value}' IS saved + listed"})
            steps.append({"action": "fill", "target": search_sel, "value": "", "description": f"{verb} VERIFY: clear search"})

    if not writes:
        # writes off: open, navigate filling valid values, cancel (no submit).
        steps.append({"action": "click", "target": trig, "value": "", "description": f"Open {name}"})
        if modal:
            steps.append({"action": "waitfor", "target": modal, "value": "", "description": f"{name} modal opens"})
        close = _modal_close(func)
        if close:
            steps.append(close)
        return

    if kind == "create":
        run_pass(tag, "CREATE", fill_others=True, find_value=None)
    else:
        run_pass(tag_edited, "EDIT", fill_others=False, find_value=tag)
        # REVERT: an edit must not leave a lasting change - set the identity back to the original.
        run_pass(tag, "REVERT", fill_others=False, find_value=tag_edited)


def _emit_xss_create(steps, func, trig, modal, submit, fields, anchor, url):
    """XSS as its own create: reset to a clean page, open the form, put the canary in every text field,
    save, assert no execution. Separate record so it never touches the functional create/verify."""
    from icx_engine.testing.analyzers.security_cases import XSS_PAYLOAD, XSS_SAFE_EXPR
    text_sel = [s for f in fields for s in [_first(f.get("domSelectors"), f.get("selector"))]
                if s and _control_kind(_s(f.get("interactionPattern")).lower()) in ("text", "masked")]
    if not (url and text_sel):
        return
    steps.append({"action": "goto", "target": url, "value": "", "description": "SECURITY(XSS): reset to a clean page"})
    steps.append({"action": "waitfor", "target": trig, "value": "", "description": "SECURITY(XSS): trigger ready"})
    steps.append({"action": "click", "target": trig, "value": "", "description": "SECURITY(XSS): open the form"})
    if modal:
        steps.append({"action": "waitfor", "target": modal, "value": "", "description": "SECURITY(XSS): form ready"})
    for fld in fields:
        for st in _fill_step(fld):
            if st["action"] in ("fill", "smartfill") and st["target"] in text_sel:
                st = {**st, "value": XSS_PAYLOAD, "description": f"SECURITY(XSS): inject into {st['target']}"}
            steps.append(st)
    steps.append({"action": "click", "target": submit, "value": "", "description": "SECURITY(XSS): submit the payload"})
    steps.append({"action": "assertjs", "target": XSS_SAFE_EXPR, "value": "",
                  "description": "SECURITY(XSS): payload did NOT execute (fields are XSS-safe)"})
    steps.append({"action": "waitfor", "target": anchor, "value": "", "description": "SECURITY(XSS): list settles"})
