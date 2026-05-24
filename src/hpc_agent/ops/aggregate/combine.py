"""Combiner runner primitive."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra import remote
from hpc_agent.state import session

if TYPE_CHECKING:
    import argparse
    from pathlib import Path

    from hpc_agent.state.session import RunRecord


def _aggregate_handler(ns: argparse.Namespace) -> int:
    """Tier 2 escape hatch — delegate to the hand-written cmd_aggregate body.

    The CLI verb is ``aggregate`` (legacy name); the primitive is
    ``combine-wave``. The handler lives in :mod:`hpc_agent.cli.aggregate`
    so the heavy ~130-LOC body stays out of this atom file.
    """
    from hpc_agent.cli.aggregate import cmd_aggregate

    return cmd_aggregate(ns)


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
    cli=CliShape(
        help="Run the on-cluster combiner for one wave; records outcome to journal.",
        verb="aggregate",
        requires_ssh=True,
        experiment_dir_arg=True,
        args=(
            CliArg("--run-id", type=str, required=True),
            CliArg("--wave", type=int, required=True),
            CliArg(
                "--force",
                action="store_true",
                help="Re-run the combiner even if the wave appears combined.",
            ),
            CliArg(
                "--require-outputs",
                type=str,
                default=None,
                help=(
                    "Path template (with {task_id}) checked on the cluster before "
                    "the combiner runs. Refuses to combine if any task in this "
                    "wave is missing its expected output. Default reads from the "
                    "run sidecar's aggregate_defaults.require_outputs."
                ),
            ),
            CliArg(
                "--expect-output",
                type=str,
                default=None,
                help=(
                    "Remote path (relative to remote_path) that the combiner must "
                    "produce. Verified after the combiner exits 0; .json files "
                    "are also checked for parseability. Default reads from the "
                    "run sidecar's aggregate_defaults.expect_output."
                ),
            ),
        ),
        handler=_aggregate_handler,
    ),
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
