"""Deterministic census linter - enforces census QUALITY independent of which agent produced it.

The deterministic generators (`to_flow`, `to_api_spec`) already make the flow identical on every agent
GIVEN a correct census. The one place agent variance leaks in is the census itself. This linter runs in
ICX (not the agent) right after reconciliation and catches the structural defects any agent can make -
the exact class of errors that broke live E2E runs:
  - a create and an edit functionality sharing the SAME submit selector (copy error - each mutating
    mode has its own Save/Update button),
  - a create/edit form with fields but no submit (cannot save),
  - a functionality that needs a trigger but declares none,
  - a field with no selector,
  - duplicate functionality ids,
  - a VIEW mode carrying a submit it should not have.

HARD defects are returned so the caller RE-ASKS the agent with the exact problem (bounded), so a bad
census cannot pass regardless of agent skill. SOFT warnings (e.g. a text field with no length
constraint, a create with no search to verify against) are advisory - recorded, not blocking, because
they cannot be proven wrong from the census alone.

Pure; never raises. UI/frontend census only (backend/grpc/iac have their own shapes)."""
from __future__ import annotations

from dataclasses import dataclass, field


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def _submit_selectors(func: dict) -> list[str]:
    sb = func.get("submitButton")
    out: list[str] = []
    if isinstance(sb, dict):
        for c in (sb.get("selectors") or []):
            if _s(c):
                out.append(_s(c))
    if isinstance(func.get("submitButtons"), list):     # wizard: one per step
        for step in func["submitButtons"]:
            if isinstance(step, dict):
                for c in (step.get("selectors") or []):
                    if _s(c):
                        out.append(_s(c))
    return out


def _trigger(func: dict) -> str:
    md = func.get("modalDetails") if isinstance(func.get("modalDetails"), dict) else {}
    return _s(md.get("triggerSelector")) or _s(func.get("triggerSelector"))


def _kind(func: dict) -> str:
    name = (_s(func.get("functionality")) + " " + _s(func.get("type"))).lower()
    for k, words in (("download", ("download", "export", "csv", "excel", "xlsx")),
                     ("create", ("create", "add", "new", "register", "insert")),
                     ("edit", ("edit", "modify", "update")),
                     ("view", ("view", "detail", "show")),
                     ("delete", ("delete", "remove", "deactivate")),
                     ("search", ("search", "filter", "lookup")),
                     ("clone", ("clone", "duplicate", "copy"))):
        if any(w in name for w in words):
            return k
    return "other"


def _fields(func: dict) -> list[dict]:
    return [f for f in (func.get("fields") or []) if isinstance(f, dict)]


def _has_selector(fld: dict) -> bool:
    if any(_s(c) for c in (fld.get("domSelectors") or [])):
        return True
    return bool(_s(fld.get("selector")))


def _field_kind(fld: dict) -> str:
    pat = _s(fld.get("interactionPattern")).lower()
    if pat in ("", "default", "text", "textarea", "masked", "phone", "tel", "msisdn"):
        return "text"
    return pat


def _has_length_or_format(fld: dict) -> bool:
    v = fld.get("validations") if isinstance(fld.get("validations"), dict) else {}
    if any(_s(v.get(k)) for k in ("maxLength", "minLength", "pattern", "regex", "min", "max")):
        return True
    if _s(fld.get("type")).lower() in ("email", "tel", "url", "number"):
        return True
    return False


@dataclass
class LintReport:
    hard: list[str] = field(default_factory=list)     # block + re-ask
    soft: list[str] = field(default_factory=list)     # advisory
    ok: bool = True

    def __post_init__(self):
        self.ok = not self.hard


def lint_ui_census(model: dict) -> LintReport:
    """Structural quality gate for a UI census. hard = must-fix (re-ask the agent); soft = advisory."""
    r = LintReport()
    if not isinstance(model, dict):
        r.hard.append("census is not an object")
        r.ok = False
        return r
    funcs = [f for f in (model.get("functionalities") or []) if isinstance(f, dict)]
    if not funcs:
        r.hard.append("no functionalities in the census")
        r.ok = False
        return r

    # duplicate ids
    ids = [_s(f.get("id")) for f in funcs if _s(f.get("id"))]
    dups = sorted({i for i in ids if ids.count(i) > 1})
    for d in dups:
        r.hard.append(f"duplicate functionality id '{d}'")

    kinds = [(f, _kind(f)) for f in funcs]

    # submit-selector cross-check: two MUTATING modes (create/edit/clone) sharing an identical submit
    # selector is almost always a copy error - each has its own Save/Update/Clone button. This is the
    # exact defect that broke a live edit E2E (edit reused create's team-save instead of team-update).
    mut = [(f, k) for f, k in kinds if k in ("create", "edit", "clone")]
    seen: dict[str, str] = {}
    for f, k in mut:
        for sel in _submit_selectors(f):
            if sel in seen and seen[sel] != k:
                r.hard.append(
                    f"the '{k}' and '{seen[sel]}' forms share the SAME submit selector '{sel}' - each "
                    f"mutating mode has its OWN submit button (e.g. Save vs Update); capture the real "
                    f"selector for each from the code")
            else:
                seen[sel] = k

    for f, k in kinds:
        label = _s(f.get("functionality")) or _s(f.get("id")) or k
        wiz = f.get("steps") if isinstance(f.get("steps"), list) else None
        # create/edit must be BUILDABLE: it needs either flat fields OR a wizard steps array, plus a
        # submit. An empty create/edit form means the census under-captured the form.
        if k in ("create", "edit", "clone"):
            if not _fields(f) and not wiz:
                r.hard.append(f"'{label}' ({k}) has neither fields nor steps - the form was not "
                              f"captured; enumerate its fields (or wizard steps) from the code")
            if _fields(f) and wiz:
                r.hard.append(f"'{label}' ({k}) has BOTH fields and steps - use ONE: a flat form uses "
                              f"fields[], a multi-step wizard uses steps[]")
            if (_fields(f) or wiz) and not _submit_selectors(f):
                r.hard.append(f"'{label}' ({k}) has a form but no submitButton - it cannot be saved")
        # anything that opens a modal / acts on a row needs a trigger
        if k in ("create", "edit", "view", "delete", "clone", "download") and not _trigger(f):
            r.hard.append(f"'{label}' ({k}) has no triggerSelector")
        # WIZARD structural completeness: every step needs its fields' selectors; every step except the
        # last needs a nextButton (the last step submits via the functionality's submit).
        if wiz:
            for si, st in enumerate(wiz):
                if not isinstance(st, dict):
                    continue
                sflds = [x for x in (st.get("fields") or []) if isinstance(x, dict)]
                for fld in sflds:
                    if not _has_selector(fld):
                        flab = _s(fld.get("label")) or "a field"
                        r.hard.append(f"'{label}' wizard step {si + 1} field '{flab}' has no selector")
                nb = st.get("nextButton")
                nb_ok = isinstance(nb, dict) and any(_s(c) for c in (nb.get("selectors") or []))
                if si < len(wiz) - 1 and not nb_ok:
                    r.hard.append(f"'{label}' wizard step {si + 1} ('{_s(st.get('name'))}') has no "
                                  f"nextButton - every step but the last needs one to advance")
        # every flat field needs a selector
        for fld in _fields(f):
            if not _has_selector(fld):
                flab = _s(fld.get("label")) or _s(fld.get("fieldName")) or "a field"
                r.hard.append(f"'{label}' field '{flab}' has no domSelectors/selector")
        # VIEW should not carry a submit
        if k == "view" and _submit_selectors(f):
            r.soft.append(f"'{label}' is a VIEW but declares a submitButton - view is usually read-only")
        # SOFT: a text field with no length/format constraint - likely the code HAS a limit that was
        # not captured (the missing-maxLength class of bug). Advisory: cannot be proven from census.
        for fld in _fields(f):
            if _field_kind(fld) == "text" and not _has_length_or_format(fld):
                flab = _s(fld.get("label")) or _s(fld.get("fieldName")) or "a text field"
                r.soft.append(f"'{label}' field '{flab}' has no length/format constraint - re-check the "
                              f"code for a maxLength/minLength/regex and add it if present")

    has_create = any(k == "create" for _f, k in kinds)
    has_search = any(k == "search" for _f, k in kinds)
    if has_create and not has_search:
        r.soft.append("a create exists but there is no search functionality - the create/edit SAVE "
                      "cannot be VERIFIED by searching the record back")

    r.ok = not r.hard
    return r
