"""SSH helpers shared by the runner submodules."""

from __future__ import annotations

import json
from typing import Any

from hpc_agent.errors import RemoteCommandFailed


def _parse_remote_json(stdout: str, *, source_label: str) -> dict[str, Any]:
    """Parse JSON emitted by a remote process; raise typed error on failure.

    Centralises the ``json.loads + JSONDecodeError → RemoteCommandFailed``
    pattern that ``_ssh_status_report`` and ``_read_remote_sidecar`` both
    needed. *source_label* is interpolated into the error message so the
    caller's diagnostic still pinpoints which remote read failed.
    """
    try:
        result: dict[str, Any] = json.loads(stdout)
    except json.JSONDecodeError as exc:
        snippet = stdout[:200]
        raise RemoteCommandFailed(
            f"{source_label} returned invalid JSON: {exc}; first 200 chars: {snippet!r}"
        ) from exc
    return result
