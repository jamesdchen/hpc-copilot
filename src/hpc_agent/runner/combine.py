"""Combiner runner primitive."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._internal import session
from hpc_agent._internal.primitive import SideEffect, primitive
from hpc_agent.infra import remote

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._internal.session import RunRecord


@primitive(
    name="combine-wave",
    verb="mutate",
    side_effects=[
        SideEffect("ssh", "<cluster>"),
        SideEffect("runs", "cluster-side combiner (python3 .hpc/_hpc_combiner.py)"),
        SideEffect("writes-cluster", "<output_dir>/_combiner/wave_<N>.json"),
        SideEffect(
            "writes-journal",
            "~/.claude/hpc/<repo_hash>/runs/<run_id>.json (combined_waves / failed_waves)",
        ),
    ],
    error_codes=[errors.SshUnreachable, errors.CombinerFailed, errors.JournalCorrupt],
    idempotent=True,
    idempotency_key="(run_id, wave)",
    cli="hpc-agent aggregate --run-id <id> --wave <N> [--output-dir <path>] [--force]",
    agent_facing=True,
)
def combine_wave(
    experiment_dir: Path,
    run_id: str,
    *,
    wave: int,
    ssh_target: str,
    remote_path: str,
    force: bool = False,
) -> tuple[bool, str, str]:
    """Run the on-cluster combiner for *wave*; record the outcome.

    The cluster-side combiner (``.hpc/_hpc_combiner.py``) reads the
    per-run sidecar at ``.hpc/runs/<run_id>.json`` to discover the
    wave_map and result_dir_template. On success, append *wave* to
    ``combined_waves``. On failure, append to ``failed_waves`` and never
    mark the wave combined. Returns ``(ok, stdout, stderr)`` from
    :func:`run_combiner_checked`.
    """
    ok, stdout, stderr = remote.run_combiner_checked(
        ssh_target=ssh_target,
        remote_path=remote_path,
        wave=wave,
        run_id=run_id,
        force=force,
    )

    def _apply(record: RunRecord) -> None:
        # Mutate inside the journal's per-run lock: a concurrent
        # combine_wave for a different wave re-reads the freshly-locked
        # record, so neither call clobbers the other's wave with a list
        # snapshot derived from a stale unlocked read.
        if ok:
            if wave not in record.combined_waves:
                record.combined_waves = sorted({*record.combined_waves, wave})
            record.failed_waves = [w for w in record.failed_waves if w != wave]
        elif wave not in record.failed_waves:
            record.failed_waves = sorted({*record.failed_waves, wave})

    try:
        session.update_run_record(experiment_dir, run_id, _apply)
    except FileNotFoundError as exc:
        raise errors.JournalCorrupt(f"no run record for {run_id!r}") from exc
    return ok, stdout, stderr
