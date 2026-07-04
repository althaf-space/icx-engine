from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Durable, user-editable rulebook. Source of truth lives in ~/.icx/testing_rules/;
# bundled defaults seed it on first use and never overwrite user edits. ICX loads
# the relevant <gate>.md and injects its text into every gate payload, so the agent
# always confronts the current mandatory rules - no dependency on the agent reaching
# the filesystem, and no drift across sessions.

_DEFAULTS_DIR = Path(__file__).parent / "rules_defaults"
_COMMON = "_common"
_NESTED_REPORT_CAP = 40  # keep the re-ask message bounded when many nested keys are missing


def rules_dir() -> Path:
    return Path.home() / ".icx" / "testing_rules"


def gate_rules_path(gate: str) -> str:
    return str(rules_dir() / f"{gate}.md")


def ensure_seeded() -> None:
    """Copy any missing bundled default into ~/.icx/testing_rules/. Never overwrites
    a file the user has already edited or created."""
    d = rules_dir()
    d.mkdir(parents=True, exist_ok=True,
            **({"mode": 0o700} if sys.platform != "win32" else {}))
    if not _DEFAULTS_DIR.exists():
        return
    for src in _DEFAULTS_DIR.glob("*.md"):
        dst = d / src.name
        if not dst.exists():
            try:
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                pass


def _read_one(gate: str) -> str:
    """Read a single gate's md - user copy first, bundled default as fallback."""
    f = rules_dir() / f"{gate}.md"
    if f.exists():
        try:
            return f.read_text(encoding="utf-8")
        except OSError:
            pass
    df = _DEFAULTS_DIR / f"{gate}.md"
    if df.exists():
        try:
            return df.read_text(encoding="utf-8")
        except OSError:
            pass
    return ""


def load_gate_rules(gate: str) -> str:
    """Full rulebook text for a gate: shared _common rules followed by the gate's own.
    This is what ICX injects into the gate payload."""
    ensure_seeded()
    common = _read_one(_COMMON)
    specific = _read_one(gate)
    parts = [p for p in (common, specific) if p.strip()]
    return "\n\n".join(parts)


def _marker(gate: str, name: str) -> list[str]:
    """Parse a comma-separated `<!-- NAME: a, b, c -->` marker from a gate's md."""
    m = re.search(r"<!--\s*" + name + r":\s*(.*?)\s*-->", _read_one(gate), re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    return [s.strip() for s in m.group(1).replace("\n", " ").split(",") if s.strip()]


def required_sections(gate: str) -> list[str]:
    """Top-level sections that MUST be present AND non-empty (REQUIRED_SECTIONS)."""
    return _marker(gate, "REQUIRED_SECTIONS")


def required_present(gate: str) -> list[str]:
    """Top-level sections whose KEY must be present but MAY be empty - e.g. an array
    that is legitimately empty on some screens (REQUIRED_PRESENT)."""
    return _marker(gate, "REQUIRED_PRESENT")


def required_per_functionality(gate: str) -> list[str]:
    """Keys that must appear in every entry of functionalities[] (REQUIRED_PER_FUNCTIONALITY).
    Presence only - the agent must include the key even when its value is empty/NA."""
    return _marker(gate, "REQUIRED_PER_FUNCTIONALITY")


def required_per_field(gate: str) -> list[str]:
    """Keys that must appear in every field of every functionality (REQUIRED_PER_FIELD).
    Presence only."""
    return _marker(gate, "REQUIRED_PER_FIELD")


def _as_obj(spec: object) -> dict | None:
    if isinstance(spec, dict):
        return spec
    if isinstance(spec, str):
        try:
            parsed = json.loads(spec)
            return parsed if isinstance(parsed, dict) else None
        except ValueError:
            return None
    return None


def _is_empty(v: object) -> bool:
    return v is None or v == "" or v == [] or v == {}


def missing_sections(gate: str, spec: object) -> list[str]:
    """Objective completeness check against the user-owned markers in the gate md - NOT
    a quality judgment. Reports:
      - REQUIRED_SECTIONS keys that are absent or empty,
      - REQUIRED_PRESENT keys that are absent,
      - functionalities[i].<key> for REQUIRED_PER_FUNCTIONALITY keys that are absent,
      - functionalities[i].fields[j].<key> for REQUIRED_PER_FIELD keys that are absent.
    An unparseable spec reports every required top-level section as missing."""
    secs = required_sections(gate)
    pres = required_present(gate)
    per_func = required_per_functionality(gate)
    per_field = required_per_field(gate)
    if not (secs or pres or per_func or per_field):
        return []

    obj = _as_obj(spec)
    if obj is None:
        return (secs + pres) if (secs or pres) else ["<json_spec is not valid JSON>"]

    missing: list[str] = []
    for key in secs:
        if key not in obj or _is_empty(obj.get(key)):
            missing.append(key)
    for key in pres:
        if key not in obj:
            missing.append(key)

    funcs = obj.get("functionalities")
    if (per_func or per_field) and isinstance(funcs, list):
        for i, fn in enumerate(funcs):
            if not isinstance(fn, dict):
                missing.append(f"functionalities[{i}] is not an object")
            else:
                for key in per_func:
                    if key not in fn:
                        missing.append(f"functionalities[{i}].{key}")
                flds = fn.get("fields")
                if per_field and isinstance(flds, list):
                    for j, fld in enumerate(flds):
                        if not isinstance(fld, dict):
                            missing.append(f"functionalities[{i}].fields[{j}] is not an object")
                            continue
                        for key in per_field:
                            if key not in fld:
                                missing.append(f"functionalities[{i}].fields[{j}].{key}")
            if len(missing) > _NESTED_REPORT_CAP:
                extra = len(missing) - _NESTED_REPORT_CAP
                return missing[:_NESTED_REPORT_CAP] + [f"...(+{extra} more)"]
    return missing
