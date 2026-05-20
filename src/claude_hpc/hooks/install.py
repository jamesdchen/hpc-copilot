"""Install / preview claude-hpc Stop hooks in ~/.claude/settings.json.

The CLI surface is ``hpc-agent hook-install`` and lives in
``agent_cli.py``; this module provides the merge logic so it can also
be invoked programmatically (from setup_hpc skill, tests, etc.).

The default target is the **user-global** settings file at
``~/.claude/settings.json``. To install at project scope instead, pass
``--settings <repo>/.claude/settings.json`` on the CLI (or
``settings_path=<repo>/.claude/settings.json`` to :func:`install_hooks`).
There is no automatic project-scope install path: the framework can't
tell which project you mean if you happen to be running from a
sub-directory, so the scoping is explicit.
"""

from __future__ import annotations

import contextlib
import json
import os
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


def _command_strings(obj: Any) -> set[str]:
    """Return the command strings reachable from a ``hooks.Stop`` array element.

    Handles both the correct *group* shape (``{"hooks": [{"type":
    "command", "command": ...}]}``) and the legacy *flat* shape
    (``{"type": "command", "command": ...}``) that an older buggy
    installer wrote directly into the array. This lets the installer
    recognise — and heal — entries written before the shape fix.
    """
    if not isinstance(obj, dict):
        return set()
    inner = obj.get("hooks")
    if isinstance(inner, list):
        return {
            h["command"]
            for h in inner
            if isinstance(h, dict) and h.get("type") == "command" and "command" in h
        }
    if obj.get("type") == "command" and "command" in obj:
        return {obj["command"]}
    return set()


def build_planned_settings(existing: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return ``(new_settings, added_ids)`` after merging managed hooks into *existing*.

    Idempotent: re-running with already-installed hooks returns the same
    settings dict and an empty ``added_ids`` list.

    Self-healing: a legacy flat ``{"type": "command", ...}`` entry that
    an older installer wrote straight into ``hooks.Stop`` (missing the
    required group wrapper) is rewritten into the canonical group shape
    rather than left in place or duplicated alongside a correct entry.
    Healed ids are reported in ``added_ids``.
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
    for hook_id, group in MANAGED_HOOKS.items():
        managed_cmds = _command_strings(group)
        matching = [
            e for e in stop_entries if isinstance(e, dict) and _command_strings(e) & managed_cmds
        ]
        if matching and all("hooks" in e for e in matching):
            # Already present in the correct group shape.
            continue
        # Either absent, or present only as a legacy flat entry (and/or
        # duplicated). Drop every matching entry and append one
        # canonical group; unrelated entries are left untouched.
        stop_entries = [
            e
            for e in stop_entries
            if not (isinstance(e, dict) and _command_strings(e) & managed_cmds)
        ]
        stop_entries.append(json.loads(json.dumps(group)))
        hooks_block["Stop"] = stop_entries
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
        # PID-suffixed temp + fsync before atomic replace. Mirrors
        # state/runs.py:_atomic_write_json so concurrent installers
        # (rare, but possible across shells) don't clobber each other,
        # and a crash mid-write can't leave a half-flushed settings.json.
        tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
        text = json.dumps(new_settings, indent=2) + "\n"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(tmp, path)
        wrote = True

    return {
        "settings_path": str(path),
        "added": added,
        "already_installed": already_installed,
        "wrote": wrote,
    }
