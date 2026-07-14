"""Combiner runner primitive."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra import transport
from hpc_agent.state.journal import update_run_record

if TYPE_CHECKING:
    import argparse
    from pathlib import Path

    from hpc_agent.state.run_record import RunRecord

# The cluster combiner's no-force refusal (exit 1) when its output file is
# already present — ``.../combiner.py``: "output already exists: <path>
# (use --force to overwrite)" / "final aggregate already exists: <path>
# (use --force)".
_ALREADY_COMBINED_RE = re.compile(r"already exists: \S+ \(use --force")

# The run_id the combiner now stamps onto its no-force refusal (``[run_id=<id>]``)
# so this control plane can distinguish a same-run "already combined" from a
# FOREIGN partial (a prior run's wave file persisting under the delete-protected
# ``_combiner/``) without a second ssh read (F05). Absent on legacy/older
# deployed combiners → the recovery falls back to its historical fail-open.
_REFUSAL_RUN_ID_RE = re.compile(r"\[run_id=([^\]]+)\]")


def _refusal_names_foreign_run(stderr: str, run_id: str) -> bool:
    """True iff the combiner's refusal carries a run_id that is NOT *run_id*.

    Fails OPEN (returns False) when no run_id witness is present — an older
    deployed combiner whose refusal predates the stamp must keep the historical
    "recognize as already combined" recovery.
    """
    m = _REFUSAL_RUN_ID_RE.search(stderr or "")
    if not m:
        return False
    return m.group(1).strip() != run_id


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

    Idempotent on ``(run_id, wave)``: a no-``force`` replay of a wave the
    journal already records as combined returns success without touching
    the cluster, and a replay whose journal lost the wave is recognized
    by the combiner's no-force "already exists" refusal and recorded as
    combined — never as a new ``failed_waves`` entry.
    """
    # Activate the run's cluster env for the control-plane combiner — it
    # runs directly on the login node via ssh_run and would otherwise hit
    # the bare login-node python that lacks the framework.
    from hpc_agent.infra.clusters import remote_activation_for_sidecar
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import read_run_sidecar

    try:
        _sidecar = read_run_sidecar(experiment_dir, run_id)
    except Exception:  # noqa: BLE001 — missing/bad sidecar → bare python (unchanged)
        _sidecar = {}
    # fallback_cluster (run #7): the submit-flow sidecar carries no cluster, so
    # without the record's cluster to backfill, the combiner runs bare login
    # python (rc=127, the blind-watch class at the wave-combine surface).
    _rec = load_run(experiment_dir, run_id)
    if not force and _rec is not None and wave in _rec.combined_waves:
        # Idempotent replay (idempotency_key "(run_id, wave)"): the journal
        # already records this wave combined. Without this return, the
        # cluster combiner would refuse with "output already exists (use
        # --force)" and the replay would land in failed_waves.
        return (
            True,
            f"wave {wave} already combined (journal); pass --force to re-run the combiner",
            "",
        )
    _fallback_cluster = _rec.cluster if _rec is not None else None

    ok, stdout, stderr = transport.run_combiner_checked(
        ssh_target=ssh_target,
        remote_path=remote_path,
        wave=wave,
        run_id=run_id,
        force=force,
        remote_activation=remote_activation_for_sidecar(
            _sidecar, fallback_cluster=_fallback_cluster
        ),
    )

    if not ok and not force and _ALREADY_COMBINED_RE.search(stderr):
        # The combiner's no-force refusal is the on-cluster witness that
        # this wave's output already exists — the journal missed the wave
        # (e.g. wiped local state). Record it combined, not failed. But TWO
        # guards must hold before adopting it as THIS run's combined output:
        #
        #   F05: the incumbent partial must belong to this run. A prior run's
        #   wave file persists under the delete-protected ``_combiner/``; the
        #   combiner now overwrites a foreign partial (so its refusal implies a
        #   same-run collision), and it stamps the incumbent run_id onto the
        #   refusal as a second, deploy-version-independent witness. If that
        #   run_id names a different run, do NOT journal it combined — leave
        #   ok=False so ``_combine_missing``'s force retry recombines this run.
        #
        #   F06: a wave the journal marked for recombine (in ``failed_waves``,
        #   set by a resubmit that re-ran its failed tasks — see
        #   ops/recover_flow._invalidate_combined_waves_for_tasks) must be
        #   force-recombined over the recovered data, never re-adopted from the
        #   stale partial that was combined over the pre-recovery subset.
        foreign = _refusal_names_foreign_run(stderr, run_id)
        needs_recombine = _rec is not None and wave in _rec.failed_waves
        if not foreign and not needs_recombine:
            ok = True
            stdout = f"wave {wave} already combined on cluster; pass --force to re-run\n{stdout}"

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
        update_run_record(experiment_dir, run_id, _apply)
    except FileNotFoundError as exc:
        raise errors.JournalCorrupt(f"no run record for {run_id!r}") from exc
    return ok, stdout, stderr
