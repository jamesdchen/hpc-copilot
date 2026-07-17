"""Combiner runner primitive."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra import transport

# The fused multi-wave runner is not part of the transport package's public
# facade (its ``__init__`` re-exports the single-wave verbs); import it from the
# leaf module directly. Bound at module scope so tests patch it here.
from hpc_agent.infra.transport._combiner import run_combiner_batch_checked
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


def _reconcile_combiner_result(
    *,
    run_id: str,
    wave: int,
    ok: bool,
    stdout: str,
    stderr: str,
    force: bool,
    rec: RunRecord | None,
) -> tuple[bool, str]:
    """Fold the combiner's no-force "already exists" refusal into a verdict.

    ONE definition of the F05/F06 adoption guard, shared by the single-wave
    :func:`combine_wave` and the fused :func:`combine_waves` (so batching cannot
    drift the recovery). A no-``force`` refusal whose on-cluster output already
    exists is the idempotent success — the journal simply missed the wave —
    UNLESS one of two guards holds:

      * **F05 (foreign)** — the refusal names a DIFFERENT run's partial persisting
        under the delete-protected ``_combiner/``. Do not adopt it; leave
        ``ok=False`` so the force retry recombines this run over its own data.
      * **F06 (needs-recombine)** — the wave sits in ``failed_waves`` because a
        resubmit invalidated its stale partial. It must be force-recombined over
        the recovered data, never re-adopted from the pre-recovery partial.

    Returns ``(ok, stdout)`` — ``ok`` flipped to True and ``stdout`` prefixed with
    the "already combined on cluster" note only when the refusal is a genuine
    same-run idempotent hit.
    """
    if not ok and not force and _ALREADY_COMBINED_RE.search(stderr):
        foreign = _refusal_names_foreign_run(stderr, run_id)
        needs_recombine = rec is not None and wave in rec.failed_waves
        if not foreign and not needs_recombine:
            ok = True
            stdout = f"wave {wave} already combined on cluster; pass --force to re-run\n{stdout}"
    return ok, stdout


def _apply_wave_outcome(record: RunRecord, wave: int, ok: bool) -> None:
    """Move *wave* between ``combined_waves`` / ``failed_waves`` per *ok*.

    Called inside the journal's per-run lock (both the single-wave and fused
    paths), so a concurrent combine for a different wave re-reads the freshly-
    locked record and neither clobbers the other's list with a stale snapshot.
    A wave that succeeds drops off ``failed_waves`` (a retry that finally landed).
    """
    if ok:
        if wave not in record.combined_waves:
            record.combined_waves = sorted({*record.combined_waves, wave})
        record.failed_waves = [w for w in record.failed_waves if w != wave]
    elif wave not in record.failed_waves:
        record.failed_waves = sorted({*record.failed_waves, wave})


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
        SideEffect("writes-cluster", "<output_dir>/_combiner/<run_id>/wave_<N>.json"),
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

    # The combiner's no-force refusal is the on-cluster witness that this wave's
    # output already exists — the journal missed the wave (e.g. wiped local
    # state). ONE-definition adoption guard (F05 foreign / F06 needs-recombine)
    # shared with the fused ``combine_waves`` path.
    ok, stdout = _reconcile_combiner_result(
        run_id=run_id, wave=wave, ok=ok, stdout=stdout, stderr=stderr, force=force, rec=_rec
    )

    try:
        update_run_record(
            experiment_dir, run_id, lambda record: _apply_wave_outcome(record, wave, ok)
        )
    except FileNotFoundError as exc:
        raise errors.JournalCorrupt(f"no run record for {run_id!r}") from exc
    return ok, stdout, stderr


def combine_waves(
    experiment_dir: Path,
    run_id: str,
    *,
    waves: list[int],
    forces: dict[int, bool] | None = None,
    ssh_target: str,
    remote_path: str,
) -> dict[int, tuple[bool, str, str]]:
    """Combine a BURST of waves in ONE ssh exec; record each outcome individually.

    P4 tier-1: a tick that finds N newly-complete waves used to pay N cold SSH
    round-trips (one :func:`combine_wave` each — the serial-wave-combines
    head-of-line stall). This fuses the cluster leg into a single
    :func:`run_combiner_batch_checked` exec while keeping the accounting HONEST:
    every wave's ``(ok, stdout, stderr)`` is reconciled and journaled
    *individually* (F05/F06 guards per wave, no whole-batch verdict), so a partial
    batch failure records the ok waves combined and the failed ones failed in the
    SAME per-run journal write.

    ``forces`` maps a wave to its ``--force`` decision (default False — a fresh
    wave; a retry of a previously-failed wave passes True). Returns
    ``{wave: (ok, stdout, stderr)}`` for every requested wave.

    Fallbacks preserve correctness over the latency win:

    * a wave already recorded ``combined`` in the journal (and not forced) is an
      idempotent hit resolved with NO cluster contact (same as
      :func:`combine_wave`), and is excluded from the fused exec;
    * a truncated batch (missing ``__HPC_BATCH_END__``) or a wave cut mid-frame
      degrades to a per-wave :func:`combine_wave` call for the affected waves
      (E3 — never parse-and-trust a truncated fused stream).
    """
    from hpc_agent.infra.clusters import remote_activation_for_sidecar
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import read_run_sidecar

    forces = forces or {}
    ordered = sorted({int(w) for w in waves})
    if not ordered:
        return {}

    try:
        _sidecar = read_run_sidecar(experiment_dir, run_id)
    except Exception:  # noqa: BLE001 — missing/bad sidecar → bare python (unchanged)
        _sidecar = {}
    _rec = load_run(experiment_dir, run_id)
    _fallback_cluster = _rec.cluster if _rec is not None else None
    activation = remote_activation_for_sidecar(_sidecar, fallback_cluster=_fallback_cluster)

    results: dict[int, tuple[bool, str, str]] = {}
    wave_forces: list[tuple[int, bool]] = []
    for wave in ordered:
        force = bool(forces.get(wave, False))
        if not force and _rec is not None and wave in _rec.combined_waves:
            # Idempotent journal hit — no cluster contact.
            # MIRROR: combine_wave's journal-hit short-circuit pinned-by
            # tests/ops/aggregate/test_combine_wave_idempotent.py
            results[wave] = (
                True,
                f"wave {wave} already combined (journal); pass --force to re-run the combiner",
                "",
            )
            continue
        wave_forces.append((wave, force))

    if not wave_forces:
        return results

    batch = run_combiner_batch_checked(
        ssh_target=ssh_target,
        remote_path=remote_path,
        wave_forces=wave_forces,
        run_id=run_id,
        remote_activation=activation,
    )

    for wave, force in wave_forces:
        if batch is not None and wave in batch:
            ok, stdout, stderr = batch[wave]
            ok, stdout = _reconcile_combiner_result(
                run_id=run_id,
                wave=wave,
                ok=ok,
                stdout=stdout,
                stderr=stderr,
                force=force,
                rec=_rec,
            )
            results[wave] = (ok, stdout, stderr)
        else:
            # E3 fallback: the fused stream was truncated (batch is None) or this
            # wave was cut mid-frame (absent from batch) — combine it on its own
            # rather than trust a partial. ``combine_wave`` re-checks the journal
            # and journals its own outcome, so it is skipped in the fused-journal
            # write below.
            results[wave] = combine_wave(
                experiment_dir,
                run_id,
                wave=wave,
                ssh_target=ssh_target,
                remote_path=remote_path,
                force=force,
            )

    # Journal every FUSED wave's outcome in ONE per-run write (honest per-wave
    # accounting — no silent whole-batch verdict). Waves that fell back to
    # ``combine_wave`` already journaled themselves; exclude them here so the
    # outcome is written exactly once.
    fused = [w for w, _ in wave_forces if batch is not None and w in batch]
    if fused:

        def _apply(record: RunRecord) -> None:
            for wave in fused:
                _apply_wave_outcome(record, wave, results[wave][0])

        try:
            update_run_record(experiment_dir, run_id, _apply)
        except FileNotFoundError as exc:
            raise errors.JournalCorrupt(f"no run record for {run_id!r}") from exc

    return results
