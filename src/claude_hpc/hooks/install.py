"""Install / preview claude-hpc Stop hooks in ~/.claude/settings.json.

The CLI surface is ``hpc-agent hook-install`` and lives in
``agent_cli.py``; this module provides the merge logic so it can also
be invoked programmatically (from setup_hpc skill, tests, etc.).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_hpc.hooks import monitor_armed_check

__all__ = [
    "DEFAULT_SETTINGS_PATH",
    "MANAGED_HOOKS",
    "build_planned_settings",
    "install_hooks",
]


def DEFAULT_SETTINGS_PATH() -> Path:
    """Return ``~/.claude/settings.json`` (does not create the file)."""
    return Path.home() / ".claude" / "settings.json"


# Stop-hook entries claude-hpc owns. Keyed by a stable "id" comment in the
# command string so install_hooks can detect prior installs without
# string-matching the whole entry. The id ("claude-hpc:monitor-armed") is
# embedded in a leading comment via shell ``true`` so it's harmless if
# Claude Code ever decides to forward the comment to a real shell.
MANAGED_HOOKS: dict[str, dict[str, Any]] = {
    "monitor-armed": monitor_armed_check.settings_entry(),
}


def _hook_matches(entry: dict[str, Any], target: dict[str, Any]) -> bool:
    """Cheap structural match: same type + same command string."""
    return entry.get("type") == target.get("type") and entry.get("command") == target.get("command")


def build_planned_settings(existing: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return ``(new_settings, added_ids)`` after merging managed hooks into *existing*.

    Idempotent: re-running with already-installed hooks returns the same
    settings dict and an empty ``added_ids`` list.
    """
    settings = json.loads(json.dumps(existing))  # deep copy via JSON round-trip
    hooks_block = settings.setdefault("hooks", {})
    if not isinstance(hooks_block, dict):
        # Defensive: someone wrote an array there. Don't clobber; bail.
        raise ValueError("hooks block in settings.json is not an object; refusing to overwrite")
    stop_entries = hooks_block.setdefault("Stop", [])
    if not isinstance(stop_entries, list):
        raise ValueError("hooks.Stop in settings.json is not an array; refusing to overwrite")

    added: list[str] = []
    for hook_id, entry in MANAGED_HOOKS.items():
        if any(_hook_matches(existing_entry, entry) for existing_entry in stop_entries):
            continue
        stop_entries.append(entry)
        added.append(hook_id)
    return settings, added


def install_hooks(*, settings_path: Path | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Install claude-hpc Stop hooks; return a summary dict.

    Reads *settings_path* (default ``~/.claude/settings.json``), merges
    every entry in :data:`MANAGED_HOOKS` into ``hooks.Stop``, and writes
    back atomically. With ``dry_run=True`` does not write — the returned
    dict still reports what would have changed.

    Result shape:

        {
            "settings_path": "<resolved path>",
            "added": ["monitor-armed", ...],
            "already_installed": [...],
            "wrote": <bool>,
        }
    """
    path = (settings_path or DEFAULT_SETTINGS_PATH()).expanduser()
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path} is not valid JSON: {exc}") from exc
        if not isinstance(existing, dict):
            raise ValueError(f"{path} top-level must be a JSON object")

    new_settings, added = build_planned_settings(existing)
    already_installed = [hid for hid in MANAGED_HOOKS if hid not in added]

    wrote = False
    if added and not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(new_settings, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)
        wrote = True

    return {
        "settings_path": str(path),
        "added": added,
        "already_installed": already_installed,
        "wrote": wrote,
    }
