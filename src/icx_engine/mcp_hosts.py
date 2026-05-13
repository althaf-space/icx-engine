from __future__ import annotations
import json
import tomllib
import tomli_w
from dataclasses import dataclass
from pathlib import Path

ICX_MCP_ENTRY: dict = {"command": "icx", "args": ["mcp", "run"]}


# ── Path helpers ──────────────────────────────────────────────────────────────

def _home() -> Path:
    return Path.home()


# ── Data models ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MCPHost:
    name: str
    label: str
    config_path: Path
    detect_path: Path
    config_format: str  # "json" | "toml"
    extra_config_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class WriteResult:
    path: Path
    fallback: bool


# ── Host registry ─────────────────────────────────────────────────────────────

def list_hosts() -> list[MCPHost]:
    """All known MCP hosts with their config file paths."""
    home = _home()
    cwd = Path.cwd()
    return [
        MCPHost(
            "claude", "Claude Code",
            home / ".claude" / "settings.json",
            home / ".claude",
            "json",
        ),
        MCPHost(
            "cursor", "Cursor",
            home / ".cursor" / "mcp.json",
            home / ".cursor",
            "json",
        ),
        MCPHost(
            "windsurf", "Windsurf",
            home / ".codeium" / "windsurf" / "mcp_config.json",
            home / ".codeium" / "windsurf",
            "json",
        ),
        MCPHost(
            "codex", "Codex",
            home / ".codex" / "config.toml",
            home / ".codex",
            "toml",
        ),
        MCPHost(
            "antigravity", "Antigravity",
            home / ".gemini" / "antigravity" / "mcp_config.json",
            home / ".gemini",
            "json",
        ),
    ]


def detect_installed_hosts() -> list[MCPHost]:
    """Hosts whose install directory exists on this machine."""
    return [h for h in list_hosts() if h.detect_path.exists()]


def get_host(name: str) -> MCPHost | None:
    """Look up a host by its name identifier."""
    return next((h for h in list_hosts() if h.name == name), None)


# ── Write / remove ────────────────────────────────────────────────────────────

def write_icx_entry(host: MCPHost) -> WriteResult:
    """Write (or overwrite) the ICX entry in a host's MCP config file.

    Returns WriteResult(path, fallback=False) on success.
    Returns WriteResult(cwd/.mcp.json, fallback=True) when detect_path is absent.
    """
    if not host.detect_path.exists():
        fallback_path = Path.cwd() / ".mcp.json"
        _write_json(fallback_path)
        return WriteResult(path=fallback_path, fallback=True)

    host.config_path.parent.mkdir(parents=True, exist_ok=True)
    if host.config_format == "toml":
        _write_toml(host.config_path)
    else:
        _write_json(host.config_path)
    for extra in host.extra_config_paths:
        extra.parent.mkdir(parents=True, exist_ok=True)
        _write_json(extra)
    return WriteResult(path=host.config_path, fallback=False)


def remove_icx_entry(host: MCPHost) -> bool:
    """Remove ICX entry from host config. Returns True if entry was present."""
    if not host.config_path.exists():
        return False
    if host.config_format == "toml":
        removed = _remove_toml(host.config_path)
    else:
        removed = _remove_json(host.config_path)
    for extra in host.extra_config_paths:
        if extra.exists():
            _remove_json(extra)
    return removed


# ── Atomic write ──────────────────────────────────────────────────────────────

def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        tmp.replace(path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


# ── JSON helpers ──────────────────────────────────────────────────────────────

def _write_json(path: Path) -> None:
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    existing.setdefault("mcpServers", {})
    existing["mcpServers"]["icx"] = ICX_MCP_ENTRY
    _atomic_write(path, json.dumps(existing, indent=2))


def _remove_json(path: Path) -> bool:
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    removed = existing.get("mcpServers", {}).pop("icx", None)
    if removed is None:
        return False
    _atomic_write(path, json.dumps(existing, indent=2))
    return True


# ── TOML helpers (Codex) ──────────────────────────────────────────────────────

def _write_toml(path: Path) -> None:
    existing: dict = {}
    if path.exists():
        try:
            existing = tomllib.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    existing.setdefault("mcp_servers", {})
    existing["mcp_servers"]["icx"] = ICX_MCP_ENTRY
    _atomic_write(path, tomli_w.dumps(existing))


def _remove_toml(path: Path) -> bool:
    try:
        existing = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    removed = existing.get("mcp_servers", {}).pop("icx", None)
    if removed is None:
        return False
    _atomic_write(path, tomli_w.dumps(existing))
    return True
