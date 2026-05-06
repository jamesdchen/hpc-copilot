"""Combiner runner primitive."""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_hpc import errors
from claude_hpc._internal import session
from claude_hpc._internal._primitive import SideEffect, primitive
from claude_hpc.infra import remote

if TYPE_CHECKING:
    from pathlib import Path


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
    cli="hpc-mapreduce aggregate --run-id <id> --wave <N> [--output-dir <path>] [--force]",
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
    record = session.load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no run record for {run_id!r}")
    if ok:
        if wave not in record.combined_waves:
            record.combined_waves = sorted({*record.combined_waves, wave})
        record.failed_waves = [w for w in record.failed_waves if w != wave]
    else:
        if wave not in record.failed_waves:
            record.failed_waves = sorted({*record.failed_waves, wave})
    session.update_run_status(
        experiment_dir,
        run_id,
        combined_waves=record.combined_waves,
        failed_waves=record.failed_waves,
    )
    return ok, stdout, stderr
