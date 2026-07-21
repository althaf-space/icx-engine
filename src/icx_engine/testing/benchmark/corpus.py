"""Benchmark app registry + ground-truth loader. Pure data access; no live calls."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_FIXTURES = Path(__file__).parent / "fixtures"


@dataclass
class BenchmarkApp:
    name: str
    url: str
    login: str                       # RESERVED for a future login-recipe resolver; "" = public.
                                      # Not read today - the live runner authenticates via the
                                      # storageState passed to `icx test benchmark --storage-state`.
    ground_truth: Path | None        # path to the ground-truth JSON for this app's primary screen


def load_corpus() -> list[BenchmarkApp]:
    """The benchmark apps. Demo apps ship with fixtures; add open-source apps by dropping a fixture
    dir under fixtures/<name>/ and a line here."""
    return [
        BenchmarkApp(
            name="magik_ui",
            url="http://localhost:3000/Magik_3.0_UI/login#/users",
            login="magik",
            ground_truth=_FIXTURES / "magik_ui" / "users.json",
        ),
    ]


def load_ground_truth(app: BenchmarkApp) -> dict:
    """Parse the app's ground-truth JSON. Returns {} when absent or unreadable (never raises)."""
    p = app.ground_truth
    if not isinstance(p, Path) or not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}
