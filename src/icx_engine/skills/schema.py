"""SkillEntry - one learned, human-readable skill (SKILL.md). Frontmatter is written as JSON: any JSON
document is also valid YAML 1.2, so files stay parseable by tools expecting YAML frontmatter
(agentskills.io, Claude Code Skills) without adding a YAML dependency ICX doesn't already have."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

_DELIM = "---"
_SECTIONS = ("When to Use", "Procedure", "Pitfalls", "Verification")


@dataclass
class SkillEntry:
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    origin_projects: list[str] = field(default_factory=list)
    origin_issue_keys: list[str] = field(default_factory=list)
    scope_hint: str = "repo-specific"          # "repo-specific" | "generic"
    title: str = ""
    when_to_use: str = ""
    procedure: str = ""
    pitfalls: str = ""
    verification: str = ""
    icx_hash: str = ""
    created_at: str = ""
    updated_at: str = ""

    def body_text(self) -> str:
        """Markdown body sections - what the hash is computed over. Frontmatter metadata (origin
        lists, timestamps) must NOT affect the hash, or every metadata-only update would look like a
        user edit and permanently block future auto-updates (see test_compute_hash_ignores_metadata_only_changes)."""
        return (
            f"# {self.title or self.name}\n\n"
            f"## When to Use\n{self.when_to_use}\n\n"
            f"## Procedure\n{self.procedure}\n\n"
            f"## Pitfalls\n{self.pitfalls}\n\n"
            f"## Verification\n{self.verification}\n"
        )

    def compute_hash(self) -> str:
        return hashlib.sha256(self.body_text().encode("utf-8")).hexdigest()

    def to_markdown(self) -> str:
        frontmatter = {
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "origin_projects": list(self.origin_projects),
            "origin_issue_keys": list(self.origin_issue_keys),
            "scope_hint": self.scope_hint,
            "icx_hash": self.icx_hash,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return f"{_DELIM}\n{json.dumps(frontmatter, indent=2)}\n{_DELIM}\n\n{self.body_text()}"

    @classmethod
    def from_markdown(cls, text: str) -> "SkillEntry":
        """Parse a SKILL.md file. Raises ValueError - and only ValueError - on any malformed
        input; storage.py catches that single type and skips the file rather than propagate."""
        lines = text.split("\n")
        if not lines or lines[0].strip() != _DELIM:
            raise ValueError("SKILL.md missing opening '---' frontmatter delimiter")
        close_idx = None
        for i in range(1, len(lines)):
            if lines[i].strip() == _DELIM:
                close_idx = i
                break
        if close_idx is None:
            raise ValueError("SKILL.md missing closing '---' frontmatter delimiter")

        try:
            meta = json.loads("\n".join(lines[1:close_idx]))
            if not isinstance(meta, dict):
                raise ValueError(f"SKILL.md frontmatter must be a JSON object, got {type(meta).__name__}")
            body = "\n".join(lines[close_idx + 1:]).strip("\n")

            body_lines = body.split("\n")
            n = len(body_lines)
            cursor = 0
            sections: dict[str, str] = {}
            for i, section_name in enumerate(_SECTIONS):
                marker = f"## {section_name}"
                start = None
                for j in range(cursor, n):
                    if body_lines[j] == marker:
                        start = j
                        break
                if start is None:
                    sections[section_name] = ""
                    continue
                # Only headings for sections AFTER this one can terminate it - a fake heading line
                # matching an EARLIER section's name inside this section's body is never a boundary.
                # Known limitation: an EARLIER section's body containing a line that exactly matches a
                # LATER section's heading still truncates that earlier section early (out of scope here).
                later_markers = {f"## {nm}" for nm in _SECTIONS[i + 1:]}
                end = n
                for k in range(start + 1, n):
                    if body_lines[k] in later_markers:
                        end = k
                        break
                sections[section_name] = "\n".join(body_lines[start + 1:end]).strip()
                cursor = end

            first_line = body.split("\n", 1)[0]
            title = first_line[2:].strip() if first_line.startswith("# ") else meta.get("name", "")

            return cls(
                name=meta["name"],
                description=meta.get("description", ""),
                tags=list(meta.get("tags", [])),
                origin_projects=list(meta.get("origin_projects", [])),
                origin_issue_keys=list(meta.get("origin_issue_keys", [])),
                scope_hint=meta.get("scope_hint", "repo-specific"),
                title=title,
                when_to_use=sections["When to Use"],
                procedure=sections["Procedure"],
                pitfalls=sections["Pitfalls"],
                verification=sections["Verification"],
                icx_hash=meta.get("icx_hash", ""),
                created_at=meta.get("created_at", ""),
                updated_at=meta.get("updated_at", ""),
            )
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"malformed SKILL.md: {exc}") from exc
