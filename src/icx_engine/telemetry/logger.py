"""Per-tool-call usage logging - one JSONL line per MCP tool call, under a daily directory
(~/.icx/logs/YYYY-MM-DD/tool_calls.jsonl by default). Local only, never transmitted anywhere.

An MCP server never sees the client's real token accounting (that lives in the host LLM's own
context, not on the wire) - *_tokens_est below is a local estimate (len(text) // 4, the standard
English-text rule of thumb) on the JSON payload size. Good for relative trends across tools
("sonar_report is 5x heavier than sonar_status"), not a billing-accurate count.

Timestamps and day-directory bucketing are IST (UTC+5:30, fixed offset - India has no DST, so
this needs no IANA tzdata) rather than UTC, matching the local reporting default in
telemetry/cli_commands.py (`datetime.now().date()`, i.e. the machine's local date).

Logging failures must never break the tool call itself - every public function here is
guarded and silently gives up rather than raising, mirroring testing/screen_cache.py's
"pure I/O module; never raises" convention used elsewhere in this codebase."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_IST = timezone(timedelta(hours=5, minutes=30))
_CHARS_PER_TOKEN_ESTIMATE = 4


def estimate_tokens(text: str) -> int:
    """Rough len(text)//4 estimate - see module docstring for why this can't be exact."""
    return max(1, len(text) // _CHARS_PER_TOKEN_ESTIMATE) if text else 0


class ToolCallLogger:
    """root defaults to ~/.icx/logs - inject a tmp_path in tests, exactly like
    skills/storage.py's SkillStorage(root=...) pattern."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or (Path.home() / ".icx" / "logs")

    @property
    def root(self) -> Path:
        return self._root

    def _day_dir(self, when: datetime) -> Path:
        return self._root / when.strftime("%Y-%m-%d")

    def log_call(
        self, tool: str, input_text: str, output_text: str | None,
        duration_ms: float, ok: bool, error_type: str | None,
    ) -> None:
        """Appends one record for a completed tool call. Never raises - a logging failure
        (disk full, permissions, whatever) must never take down the actual tool call it's
        describing, which has already completed by the time this runs."""
        try:
            now = datetime.now(_IST)
            day_dir = self._day_dir(now)
            day_dir.mkdir(parents=True, exist_ok=True, **({"mode": 0o700} if sys.platform != "win32" else {}))
            record = {
                "ts": now.isoformat(),
                "tool": tool,
                "duration_ms": round(duration_ms, 2),
                "input_bytes": len(input_text.encode("utf-8")),
                "output_bytes": len(output_text.encode("utf-8")) if output_text is not None else 0,
                "input_tokens_est": estimate_tokens(input_text),
                "output_tokens_est": estimate_tokens(output_text or ""),
                "ok": ok,
                "error_type": error_type,
            }
            with open(day_dir / "tool_calls.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass
