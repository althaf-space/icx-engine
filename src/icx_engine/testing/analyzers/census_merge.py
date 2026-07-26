"""Merge a runtime-DISCOVERED census with a source-read census for top accuracy.

Each source is strong where the other is weak:
  - DISCOVERY (live DOM) has the REAL rendered selectors, the real control kinds (native vs react-
    select), and the real wizard-step structure - it can never name a selector that does not exist.
  - SOURCE (agent reading code) has constraints the DOM does not expose - a maxLength / regex / format
    enforced only in JavaScript on submit, a per-country/config rule - and functionalities the crawler
    may miss (a download behind a menu, a rarely-rendered action).

The merge keeps DISCOVERY's structure + selectors (they are live-verified) and layers SOURCE's
constraints on top: for each discovered field, if a source field matches (by selector or label), any
validations/type/format the discovered field lacks are copied in. Source-only functionalities are
appended. Discovery's submit/trigger win when present; source fills them when discovery is empty.

Pure; never raises. Either input may be None/empty."""
from __future__ import annotations

import re


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def _norm_sel(sel: str) -> str:
    """Normalize a selector for matching: strip a trailing numeric row-id and quotes/spaces."""
    s = _s(sel).lower().replace('"', "'").replace(" ", "")
    s = re.sub(r"[-_]?\d+(['\]]?)$", r"\1", s)          # team-name-EN vs team-name-EN-5
    return s


def _norm_label(v) -> str:
    return re.sub(r"[^a-z0-9]", "", _s(v).lower())


def _field_key(f: dict) -> tuple[str, str]:
    sel = ""
    for c in (f.get("domSelectors") or [f.get("selector")]):
        if _s(c):
            sel = _norm_sel(c)
            break
    return sel, _norm_label(f.get("label") or f.get("fieldName"))


def _all_source_fields(source: dict) -> list[dict]:
    out = []
    for fn in (source.get("functionalities") or []):
        if not isinstance(fn, dict):
            continue
        out += [f for f in (fn.get("fields") or []) if isinstance(f, dict)]
        for st in (fn.get("steps") or []):
            if isinstance(st, dict):
                out += [f for f in (st.get("fields") or []) if isinstance(f, dict)]
    return out


def _index(fields: list[dict]) -> tuple[dict, dict]:
    by_sel, by_label = {}, {}
    for f in fields:
        sel, lab = _field_key(f)
        if sel:
            by_sel.setdefault(sel, f)
        if lab:
            by_label.setdefault(lab, f)
    return by_sel, by_label


def _merge_field(disc: dict, src: dict) -> None:
    """Layer source constraints onto a discovered field IN PLACE (discovery keeps its selector+kind)."""
    if not isinstance(src, dict):
        return
    # constraints the DOM could not expose
    sv = src.get("validations") if isinstance(src.get("validations"), dict) else {}
    if sv:
        dv = disc.get("validations") if isinstance(disc.get("validations"), dict) else {}
        for k in ("maxLength", "minLength", "min", "max", "pattern", "regex"):
            if _s(sv.get(k)) and not _s(dv.get(k)):
                dv[k] = sv[k]
        if dv:
            disc["validations"] = dv
    # a semantic type (email/tel/url/number) the source inferred and discovery left as plain text
    st = _s(src.get("type")).lower()
    if st in ("email", "tel", "url", "number") and _s(disc.get("type")).lower() in ("", "text"):
        disc["type"] = st
    # a masked/phone interactionPattern hint
    sp = _s(src.get("interactionPattern")).lower()
    if sp in ("masked", "phone", "msisdn", "tel") and not _s(disc.get("interactionPattern")):
        disc["interactionPattern"] = sp
    if src.get("required") and "required" not in disc:
        disc["required"] = True


def merge_census(discovered: dict | None, source: dict | None) -> dict:
    """Return DISCOVERY augmented with SOURCE constraints + source-only functionalities. If one input is
    missing, return the other (so merged mode degrades to whichever exists)."""
    if not isinstance(discovered, dict) or not discovered.get("functionalities"):
        return source if isinstance(source, dict) else (discovered or {})
    if not isinstance(source, dict) or not source.get("functionalities"):
        return discovered

    by_sel, by_label = _index(_all_source_fields(source))

    def augment_fields(fields):
        for f in fields:
            if not isinstance(f, dict):
                continue
            sel, lab = _field_key(f)
            match = (by_sel.get(sel) if sel else None) or (by_label.get(lab) if lab else None)
            if match:
                _merge_field(f, match)

    def append_into(disc_fields, source_fields):
        # add SOURCE fields (react-select containers etc) the discovery field list missed.
        have = set()
        for f in disc_fields:
            s, l = _field_key(f)
            if s:
                have.add(("s", s))
            if l:
                have.add(("l", l))
        for sf in [f for f in (source_fields or []) if isinstance(f, dict)]:
            s, l = _field_key(sf)
            if (s and ("s", s) in have) or (l and ("l", l) in have):
                continue
            disc_fields.append(dict(sf))
            if s:
                have.add(("s", s))
            if l:
                have.add(("l", l))

    for fn in discovered["functionalities"]:
        if not isinstance(fn, dict):
            continue
        sfn = _match_func(fn, source)
        augment_fields(fn.get("fields") or [])
        if isinstance(fn.get("fields"), list) and sfn:
            append_into(fn["fields"], sfn.get("fields"))
        # wizard: match steps by index, augment + append per step.
        dsteps = fn.get("steps") if isinstance(fn.get("steps"), list) else []
        ssteps = sfn.get("steps") if (sfn and isinstance(sfn.get("steps"), list)) else []
        for i, st in enumerate(dsteps):
            if not isinstance(st, dict):
                continue
            augment_fields(st.get("fields") or [])
            if isinstance(st.get("fields"), list) and i < len(ssteps) and isinstance(ssteps[i], dict):
                append_into(st["fields"], ssteps[i].get("fields"))
        sfn = _match_func(fn, source)
        # fill an empty submit from a same-kind source functionality
        if not (isinstance(fn.get("submitButton"), dict) and fn["submitButton"].get("selectors")):
            if sfn and isinstance(sfn.get("submitButton"), dict) and sfn["submitButton"].get("selectors"):
                fn["submitButton"] = {"selectors": list(sfn["submitButton"]["selectors"])}
        # TRIGGER: keep the MORE SPECIFIC of discovery-vs-source (an id/data-testid beats a class or a
        # placeholder/attribute selector). Discovery is live-verified but may pick an ambiguous selector
        # (e.g. input[placeholder="Search"] that also matches a nav search) - source's #id is exact.
        if sfn:
            dtrig = (fn.get("modalDetails") or {}).get("triggerSelector")
            strig = (sfn.get("modalDetails") or {}).get("triggerSelector")
            if strig and _specificity(strig) > _specificity(dtrig):
                fn.setdefault("modalDetails", {})["triggerSelector"] = strig

    # append SOURCE-only functionalities (e.g. a download the crawler missed).
    disc_kinds = {_kind_of(fn) for fn in discovered["functionalities"] if isinstance(fn, dict)}
    for sfn in source["functionalities"]:
        if isinstance(sfn, dict) and _kind_of(sfn) not in disc_kinds:
            discovered["functionalities"].append(sfn)
            discovered.setdefault("functionalitySummaryTable", []).append(
                {"id": _s(sfn.get("id")) or _kind_of(sfn), "type": _kind_of(sfn).title()})
    return discovered


def _kind_of(fn: dict) -> str:
    name = (_s(fn.get("functionality")) + " " + _s(fn.get("type"))).lower()
    for k, ws in (("download", ("download", "export", "csv", "excel")),
                  ("create", ("create", "add", "new", "register")),
                  ("edit", ("edit", "modify", "update")), ("view", ("view", "detail", "show")),
                  ("delete", ("delete", "remove")), ("search", ("search", "filter"))):
        if any(w in name for w in ws):
            return k
    return "other"


def _specificity(sel) -> int:
    """Rank a selector's stability/uniqueness: id/data-testid (3) > class (2) > attribute/placeholder/
    text/other (1) > empty (0). Used to keep the more-precise trigger when merging."""
    s = _s(sel)
    if not s:
        return 0
    if "#" in s or "data-testid" in s:
        return 3
    if s.startswith(".") or re.search(r"\.[a-zA-Z]", s):
        return 2
    return 1


def _match_func(disc_fn: dict, source: dict) -> dict | None:
    k = _kind_of(disc_fn)
    for sfn in (source.get("functionalities") or []):
        if isinstance(sfn, dict) and _kind_of(sfn) == k:
            return sfn
    return None
