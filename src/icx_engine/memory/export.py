from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

from icx_engine.exceptions import MemoryError
from icx_engine.memory.schema import MemoryEntry


def export_to_json(entries: list[MemoryEntry], output_path: Path) -> None:
    """Serialize all MemoryEntry records to a JSON file."""
    data = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "entry_count": len(entries),
        "entries": [e.model_dump() for e in entries],
    }
    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def import_from_json(input_path: Path) -> list[MemoryEntry]:
    """Deserialize MemoryEntry records from a JSON export file."""
    input_path = input_path.resolve()
    if not input_path.exists():
        raise MemoryError(
            f"Import file not found: {input_path}. "
            "Provide the full path to the export file, e.g. icx memory import /path/to/icx-memory-export.json"
        )
    try:
        data = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MemoryError(f"Failed to read export file {input_path}: {exc}") from exc
    if not isinstance(data, dict) or "entries" not in data:
        raise MemoryError(
            f"Export file {input_path} is not a valid ICX memory export "
            "(missing 'entries' key). Check the file was created by `icx memory export`."
        )
    try:
        return [MemoryEntry.model_validate(e) for e in data["entries"]]
    except Exception as exc:
        raise MemoryError(f"Failed to parse entries in {input_path}: {exc}") from exc
