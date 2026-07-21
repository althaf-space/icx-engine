"""Local SQLite store of test-run history. Append-only; parameterized queries; never the user's repo.
Same stdlib-sqlite3 pattern as testing/session_store.py."""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class RunRecord:
    run_id: str
    app: str
    ts: float
    total: int
    passed: int
    failed: int
    skipped: int
    duration: float
    heals: int


def _default_db() -> Path:
    env = os.environ.get("ICX_ANALYTICS_DB")
    if env:
        return Path(env)
    return Path.home() / ".icx" / "testing" / "analytics.db"


class AnalyticsStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else _default_db()
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.db_path.parent, 0o700)
            except OSError:
                pass
        except OSError:
            pass
        self._conn = sqlite3.connect(str(self.db_path))
        self._init_schema()

    def _init_schema(self) -> None:
        c = self._conn
        c.execute("CREATE TABLE IF NOT EXISTS runs ("
                  "run_id TEXT, app TEXT, ts REAL, total INTEGER, passed INTEGER, "
                  "failed INTEGER, skipped INTEGER, duration REAL, heals INTEGER)")
        c.execute("CREATE TABLE IF NOT EXISTS cases ("
                  "run_id TEXT, name TEXT, status TEXT, time REAL)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_runs_ts ON runs(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cases_run ON cases(run_id)")
        c.commit()

    def record_run(self, rec: RunRecord, cases) -> None:
        """Append a run and its per-test rows. `cases` is an iterable of (name, status, time)."""
        c = self._conn
        c.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?)",
                  (rec.run_id, rec.app, rec.ts, rec.total, rec.passed, rec.failed,
                   rec.skipped, rec.duration, rec.heals))
        c.executemany("INSERT INTO cases VALUES (?,?,?,?)",
                      [(rec.run_id, str(n), str(s), float(t or 0)) for (n, s, t) in (cases or [])])
        c.commit()

    def recent_runs(self, limit: int = 50):
        rows = self._conn.execute(
            "SELECT run_id, app, ts, total, passed, failed, skipped, duration, heals "
            "FROM runs ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()
        return [RunRecord(run_id=r[0], app=r[1], ts=r[2], total=r[3], passed=r[4],
                          failed=r[5], skipped=r[6], duration=r[7], heals=r[8]) for r in rows]

    def test_history(self, limit: int = 10):
        """test_name -> [(status, time), ...] over the most recent `limit` runs, newest first."""
        recent_ids = [r[0] for r in self._conn.execute(
            "SELECT run_id FROM runs ORDER BY ts DESC LIMIT ?", (int(limit),)).fetchall()]
        if not recent_ids:
            return {}
        order = {rid: i for i, rid in enumerate(recent_ids)}
        placeholders = ",".join("?" for _ in recent_ids)
        rows = self._conn.execute(
            f"SELECT run_id, name, status, time FROM cases WHERE run_id IN ({placeholders})",
            tuple(recent_ids)).fetchall()
        hist: dict[str, list] = {}
        rows.sort(key=lambda r: order.get(r[0], 999))     # newest run first
        for run_id, name, status, t in rows:
            hist.setdefault(name, []).append((status, t))
        return hist

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass
