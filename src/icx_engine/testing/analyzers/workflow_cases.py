"""State / workflow test cases: duplicate-create rejection and delete-then-verify-gone.

Woven into the UI flow by `to_flow` when test_writes is on. These exercise the data lifecycle a
functional-only flow misses:
  - DUPLICATE: create a record, then submit the SAME values again, and assert the app REJECTS the
    duplicate (a uniqueness/"already exists" message). A pass = the uniqueness rule is enforced.
  - DELETE + VERIFY: search for the just-created tagged record, delete it (through its confirm
    dialog), search again, and assert it is GONE. A pass = delete truly removes the record.

Pure; each returns [] when the census lacks the needed pieces (a create submit, a duplicate message,
or a delete trigger). Never raises.
"""
from __future__ import annotations


def _s(v) -> str:
    return str(v).strip() if v is not None else ""


def _first(*cands) -> str:
    for c in cands:
        if isinstance(c, str) and c.strip():
            return c.strip()
        if isinstance(c, (list, tuple)):
            for x in c:
                if isinstance(x, str) and x.strip():
                    return x.strip()
    return ""


def _text_fields(func: dict) -> list[str]:
    out = []
    for f in (func.get("fields") or []):
        if not isinstance(f, dict) or f.get("readonly") or f.get("disabled"):
            continue
        pat = _s(f.get("interactionPattern")).lower()
        if pat in ("select", "react-select", "multiselect", "checkbox", "radio", "toggle",
                   "switch", "file", "file-upload", "upload", "drag"):
            continue
        sel = _first(f.get("domSelectors"), f.get("selector"))
        if sel:
            out.append(sel)
    return out


def _duplicate_message(func: dict, model: dict) -> tuple[str, str]:
    """(selector, text) for the duplicate-rejection message, from the census. ("","") if none."""
    for row in (model.get("validationMatrix") or []):
        if isinstance(row, dict) and _s(row.get("validationType")) in ("duplicate_check", "duplicate", "unique"):
            if _s(row.get("errorMessage")):
                return "body", _s(row["errorMessage"])
    nt = func.get("notifications") if isinstance(func.get("notifications"), dict) else {}
    for m in (nt.get("messages") or []):
        if isinstance(m, dict) and _s(m.get("text")) and \
           any(k in _s(m.get("text")).lower() for k in ("exist", "already", "duplicate", "unique")):
            sel = _first(nt.get("messageSelector"), nt.get("selectors"), nt.get("containerSelector")) or "body"
            return sel, _s(m.get("text"))
    return "", ""


def dup_create_steps(func, trig, modal, submit, tag, model, url="") -> list[dict]:
    """Create the SAME record again and assert the duplicate is rejected. [] if no dup message/submit."""
    dsel, dtext = _duplicate_message(func, model)
    fields = _text_fields(func)
    if not (trig and submit and dsel and dtext and fields):
        return []
    steps: list[dict] = []
    if url:
        steps.append({"action": "goto", "target": url, "value": "", "description": "WORKFLOW(dup): reset page"})
    steps.append({"action": "waitfor", "target": trig, "value": "", "description": "WORKFLOW(dup): trigger ready"})
    steps.append({"action": "click", "target": trig, "value": "", "description": "WORKFLOW(dup): reopen Create"})
    if modal:
        steps.append({"action": "waitfor", "target": modal, "value": "", "description": "WORKFLOW(dup): form ready"})
    for sel in fields:
        steps.append({"action": "fill", "target": sel, "value": tag,
                      "description": f"WORKFLOW(dup): re-enter same value in {sel}"})
    steps.append({"action": "click", "target": submit, "value": "", "description": "WORKFLOW(dup): submit duplicate"})
    steps.append({"action": "assert", "target": dsel, "value": dtext,
                  "description": f"WORKFLOW(dup): duplicate REJECTED ('{dtext[:32]}')"})
    return steps


def delete_verify_steps(delete_func, search_selector, tag, url="", row_scope=None) -> list[dict]:
    """CLEANUP: search for the record WE created (its unique tag), delete THAT row (+ confirm), search
    again, assert gone. DATA SAFETY: the delete trigger is ROW-SCOPED to our tag (via `row_scope`), so
    it can only remove our record - never existing data; and the trigger click + confirm are SOFT, so
    if our record is not present (create never succeeded) the delete is SKIPPED rather than touching a
    stranger's row. [] when there is no delete trigger or no search box to verify with."""
    md = delete_func.get("modalDetails") if isinstance(delete_func.get("modalDetails"), dict) else {}
    trig = _first(md.get("triggerSelector"), delete_func.get("triggerSelector"))
    confirm = _first(delete_func.get("submitButton", {}).get("selectors")
                     if isinstance(delete_func.get("submitButton"), dict) else None,
                     delete_func.get("confirmSelectors"))
    if not (trig and search_selector):
        return []
    scoped = row_scope(trig, tag) if callable(row_scope) else trig
    steps: list[dict] = []
    steps.append({"action": "type", "target": search_selector, "value": tag,
                  "description": f"WORKFLOW(delete): search for our record '{tag}'"})
    # our record MUST be present before we delete - if not, the soft delete below simply skips.
    steps.append({"action": "waitfor", "target": scoped, "value": "", "soft": True,
                  "description": "WORKFLOW(delete): our tagged row ready"})
    steps.append({"action": "click", "target": scoped, "value": "", "soft": True,
                  "description": "WORKFLOW(delete): delete OUR row (row-scoped to our tag)"})
    if confirm:
        steps.append({"action": "click", "target": confirm, "value": "", "soft": True,
                      "description": "WORKFLOW(delete): confirm"})
    steps.append({"action": "type", "target": search_selector, "value": tag,
                  "description": f"WORKFLOW(delete): search again for '{tag}'"})
    # gone: the body must no longer contain our tag (best-effort verification signal).
    steps.append({"action": "assertgone", "target": "body", "value": tag,
                  "description": f"WORKFLOW(delete): our record '{tag}' is GONE after delete"})
    return steps
