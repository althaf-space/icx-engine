"""Read the harness's <flow>.heals.json and score self-heal recovery against the injected mutations.
Pure; never raises."""
from __future__ import annotations

import json
from pathlib import Path


def read_heals(flow_path: str) -> list[dict]:
    """Load the heal records the harness wrote beside the flow. [] when absent/unreadable."""
    p = Path(str(flow_path))
    heals_file = p.with_name(p.stem + ".heals.json")
    if not heals_file.exists():
        return []
    try:
        data = json.loads(heals_file.read_text(encoding="utf-8"))
        return [h for h in data if isinstance(h, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def recovered_count(heals: list[dict], mutated_selectors: list[str]) -> int:
    """How many of the mutated selectors were healed (appear as an `old` in the heal log)."""
    healed_olds = {str(h.get("old")) for h in heals if isinstance(h, dict)}
    return sum(1 for s in (mutated_selectors or []) if str(s) in healed_olds)
