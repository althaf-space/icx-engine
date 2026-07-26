"""Atomic read/write + lightweight scan for the global skills store. One store for the whole machine
(~/.icx/skills/<name>/SKILL.md by default) - not per-project. See design spec Section 2.1 for why."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from icx_engine.skills.schema import SkillEntry

_SAFE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\Z")


class SkillStorage:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root or (Path.home() / ".icx" / "skills")

    def _path(self, name: str) -> Path:
        if not _SAFE_NAME_RE.match(name):
            raise ValueError(f"invalid skill name: {name!r}")
        return self._root / name / "SKILL.md"

    def read(self, name: str) -> SkillEntry | None:
        try:
            path = self._path(name)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            return SkillEntry.from_markdown(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    def write(self, entry: SkillEntry) -> None:
        path = self._path(entry.name)
        path.parent.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
        tmp = path.parent / f"{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
        try:
            tmp.write_text(entry.to_markdown(), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def list_all(self) -> list[SkillEntry]:
        """All parseable skills in the store. A corrupt/unparseable file is skipped, never fatal."""
        if not self._root.exists():
            return []
        out: list[SkillEntry] = []
        for skill_dir in sorted(p for p in self._root.iterdir() if p.is_dir()):
            path = skill_dir / "SKILL.md"
            if not path.exists():
                continue
            try:
                out.append(SkillEntry.from_markdown(path.read_text(encoding="utf-8")))
            except (ValueError, OSError):
                continue
        return out
