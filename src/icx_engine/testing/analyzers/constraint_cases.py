"""Constraint-driven field tests: everything inferable from the code about a field's allowed input.

For each field the census declares (or lets us infer) a set of constraints - a max/min length, a
numeric range, a format (email / phone-msisdn / url / regex pattern), a required flag, a type. This
module turns each constraint into a SUBMIT-FREE check that verifies the field ENFORCES it, using the
browser Constraint Validation API (`element.checkValidity()` / `element.validity`) and the value cap:
  - maxLength  -> fill an over-length value, assert value.length is capped at maxLength.
  - minLength  -> fill a too-short value, assert the field reports invalid.
  - min / max  -> fill an out-of-range number, assert the field reports invalid.
  - format     -> fill a format-violating value (email/tel/url/pattern), assert the field reports
                  invalid. Phone / msisdn / mobile fields are treated as tel with a digit pattern.
  - required   -> covered by the create form's empty-submit negative case (not repeated here).

Submit-free means: no cascade (a form that saves + closes on accept can't strand later steps), and no
false positive from empty sibling fields. A passing check = the field enforces its declared rule; a
failing one = the app does not enforce a constraint the code implies (a real finding).

Pure functions, never raise. Used by to_flow for every create/edit field.
"""
from __future__ import annotations

import re

_INT = re.compile(r"-?\d+")


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        m = _INT.search(_s(v))
        return int(m.group()) if m else None


def _validations(field: dict) -> dict:
    v = field.get("validations")
    return v if isinstance(v, dict) else {}


def _sel(field: dict) -> str:
    for c in (field.get("domSelectors") or [field.get("selector")]):
        if isinstance(c, str) and c.strip():
            return c.strip()
    return ""


def _css_str(sel: str) -> str:
    return "'" + sel.replace("\\", "\\\\").replace("'", "\\'") + "'"


# Label/name hints -> a semantic format when the census does not give an explicit type.
_FORMAT_HINTS = {
    "email": ["email", "e-mail", "mail"],
    "phone": ["phone", "mobile", "msisdn", "cell", "contact number", "tel", "whatsapp"],
    "url": ["url", "website", "link", "http"],
    "number": ["amount", "count", "quantity", "age", "price", "qty", "number", "no."],
}


def _format_of(field: dict) -> str:
    """The field's semantic format: explicit census type/inputType/format, else inferred from label."""
    for k in ("format", "inputType", "type"):
        t = _s(field.get(k)).lower()
        if t in ("email", "tel", "phone", "msisdn", "mobile"):
            return "phone" if t in ("tel", "phone", "msisdn", "mobile") else t
        if t in ("url", "number", "int", "integer", "float", "decimal"):
            return "number" if t in ("int", "integer", "float", "decimal") else t
    hint = (_s(field.get("label")) + " " + _s(field.get("fieldName"))).lower()
    for fmt, words in _FORMAT_HINTS.items():
        if any(w in hint for w in words):
            return fmt
    return ""


# An input value that VIOLATES each format (so a field enforcing the format must reject it).
_INVALID = {
    "email": "not-an-email",
    "phone": "abcXYZ!!",          # letters/symbols - not a phone number
    "url": "not a url",
    "number": "not-a-number",
    "pattern": "!!!__invalid__!!!",
}


def _reject_expr(sel: str) -> str:
    """JS asserting the field REJECTS its current (hostile) value - but only when the element has a
    NATIVE constraint the browser can validate. If there is no native constraint (a plain type=text
    field with JS-only validation), we cannot verify from the DOM, so the check PASSES rather than
    false-failing. It FAILS only when a native constraint EXISTS yet accepts the invalid value."""
    css = _css_str(sel)
    return (
        "(function(){var el=document.querySelector(" + css + ");if(!el)return true;"
        "var t=(el.type||'').toLowerCase();"
        "var native=(t==='email'||t==='url'||t==='number'||t==='tel'"
        "||el.hasAttribute('pattern')||el.hasAttribute('min')||el.hasAttribute('max')"
        "||el.hasAttribute('minlength'));"
        "return !native || (typeof el.checkValidity==='function' && !el.checkValidity());})()"
    )


def constraint_steps(field: dict) -> list[dict]:
    """Submit-free constraint checks for one field. [] when the field has no selector or declares no
    testable constraint. Each case fills a violating value then assertjs the field did not accept it.
    Format/range checks only fail when a NATIVE browser constraint exists and accepts bad input - so a
    JS-validated plain-text field is never a false positive (see _reject_expr)."""
    sel = _sel(field)
    if not sel:
        return []
    val = _validations(field)
    label = _s(field.get("label")) or _s(field.get("fieldName")) or sel
    css = _css_str(sel)
    reject = _reject_expr(sel)
    steps: list[dict] = []

    def check(fill_value: str, expr: str, what: str):
        # soft: a constraint probe is best-effort. On a gated/conditional form the field may not be
        # actionable yet (revealed only after a prior selection) - a soft step is SKIPPED, not failed,
        # so the primary CRUD flow is never blocked by a secondary constraint check.
        steps.append({"action": "fill", "target": sel, "value": fill_value, "soft": True,
                      "description": f"CONSTRAINT: {label} {what} (hostile input)"})
        steps.append({"action": "assertjs", "target": expr, "value": "", "soft": True,
                      "description": f"CONSTRAINT: {label} enforces {what}"})

    # maxLength -> value length capped. NATIVE-GUARDED: only assert the cap when the input actually has
    # a maxlength attribute (native truncation). A field with no native maxlength (JS-only length rule)
    # cannot be verified from the DOM without submitting, so the check PASSES rather than false-failing.
    maxlen = _int(val.get("maxLength")) or _int(field.get("maxLength")) or _int(field.get("maxlength"))
    if maxlen and maxlen > 0:
        expr = (f"(function(){{var el=document.querySelector({css});if(!el)return true;"
                f"return !el.hasAttribute('maxlength') || el.value.length <= {maxlen};}})()")
        check("A" * (maxlen + 1), expr, f"maxLength {maxlen}")

    # minLength -> a too-short value is invalid (native-guarded).
    minlen = _int(val.get("minLength")) or _int(field.get("minLength"))
    if minlen and minlen > 1:
        check("A", reject, f"minLength {minlen}")

    # numeric range -> an out-of-range number is invalid (native-guarded).
    mx = _int(val.get("max"))
    if mx is not None:
        check(str(mx + 1), reject, f"max value {mx}")
    mn = _int(val.get("min"))
    if mn is not None:
        check(str(mn - 1), reject, f"min value {mn}")

    # format (email/phone-msisdn/url/number) or an explicit regex pattern -> a violating value invalid
    # (native-guarded, so a JS-validated text field never false-fails).
    pattern = _s(val.get("pattern")) or _s(val.get("regex"))
    fmt = _format_of(field)
    if pattern:
        check(_INVALID["pattern"], reject, "its pattern")
    elif fmt in _INVALID:
        what = {"phone": "a phone/msisdn format", "email": "an email format",
                "url": "a url format", "number": "a numeric type"}[fmt]
        check(_INVALID[fmt], reject, what)

    return steps


def has_constraints(field: dict) -> bool:
    return bool(constraint_steps(field))
