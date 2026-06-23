"""Transport-neutral pieces of the reducer contract.

The reducer contract (``docs/reference/reducer-contract.md``) is the same
whether the user's reducer runs on the cluster over SSH
(:mod:`hpc_agent.ops.aggregate.cluster_reduce`) or locally over artifacts a
pure-API backend fetched (:mod:`hpc_agent.ops.aggregate.local_reduce`): read
``$HPC_RUN_ID``, write one JSON file to ``$HPC_AGGREGATED_OUTPUT``, exit 0.
Only *where* it runs differs. These helpers are the parts both runners share —
output-path templating and output parsing — kept here so the two runners
cannot drift.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from hpc_agent import errors

if TYPE_CHECKING:
    from pathlib import Path

# Default output path the reducer writes its single JSON to, relative to the
# reduction root (``remote_path`` on the cluster, the fetched results dir
# locally). ``{run_id}`` is substituted; any other literal braces are left
# untouched.
DEFAULT_OUTPUT_REL = "_aggregated/{run_id}.json"


def format_output_rel(template: str, *, run_id: str) -> str:
    """Substitute ``{run_id}`` in *template*.

    Bare string replace so other literal braces (e.g. ``{date}``) in a
    user-supplied path don't raise ``KeyError`` from ``str.format``. Only
    ``{run_id}`` is recognised.
    """
    return template.replace("{run_id}", run_id)


def parse_reducer_output(local_output: Path, *, run_id: str) -> dict:
    """Read + JSON-parse the reducer's output file.

    Maps a missing file or invalid JSON to :class:`errors.RemoteCommandFailed`
    — the same failure type both runners raise on a non-zero reducer exit, so
    callers (campaign loop, ``/aggregate-hpc``) handle the cluster and local
    reduction paths identically.
    """
    if not local_output.is_file():
        raise errors.RemoteCommandFailed(
            f"reducer for run_id={run_id!r} reported success but "
            f"{local_output} is missing — check the reducer's output path "
            "($HPC_AGGREGATED_OUTPUT)."
        )
    try:
        parsed: dict = json.loads(local_output.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise errors.RemoteCommandFailed(
            f"reducer output at {local_output} is not valid JSON: {exc}"
        ) from exc
    return parsed


__all__ = ["DEFAULT_OUTPUT_REL", "format_output_rel", "parse_reducer_output"]
