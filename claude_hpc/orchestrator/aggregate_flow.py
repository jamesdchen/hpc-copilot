"""``aggregate-flow``: workflow atom that finalizes a run's aggregated metrics.

Third workflow atom in the :mod:`claude_hpc.orchestrator.submit_flow` /
:mod:`claude_hpc.orchestrator.monitor_flow` family. Pipeline:

1. Read the per-run sidecar to discover the wave_map + remote_path.
2. (Optional, default on) ``ensure_all_combined`` — for every wave in
   the wave_map that isn't yet in ``record.combined_waves``, invoke
   ``runner.combine_wave`` (with one ``force=true`` retry on failure).
   Idempotent: already-combined waves are no-ops; this just guarantees
   no missing partials before the pull.
3. ``rsync_pull`` the cluster's ``_combiner/`` directory locally.
4. ``reduce_partials`` over the local dir → aggregated metrics dict.
5. (Optional) ``rsync_pull`` per-task result summaries matching
   ``summary_glob`` from the cluster's ``results/`` subtree.
6. Return :class:`AggregateFlowResult` — paths + metrics + which waves
   were combined this call vs already-combined.

Composition fit: the campaign loop's per-iteration code goes
``submit-flow → monitor-flow → aggregate-flow`` (or skips aggregate-flow
when the per-iteration metric is read directly from the sidecar's
reduce JSONs). Slash commands and external orchestrators consume the
same atom indistinguishably.

Idempotency: every individual step is idempotent. ``combine-wave`` dedups
already-combined waves; ``rsync_pull`` is a directory sync; ``reduce_partials``
is a pure function over the pulled files. Re-invoking aggregate-flow on
the same run_id is safe and cheap.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claude_hpc._internal._primitive import SideEffect, primitive
from claude_hpc.infra.remote import rsync_pull, split_ssh_target
from claude_hpc.orchestrator.runs import read_run_sidecar
from claude_hpc.mapreduce.reduce.metrics import reduce_partials
from slash_commands import errors, runner, session
from slash_commands.runner import combine_wave, record_status

__all__ = ["aggregate_flow", "AggregateFlowResult"]


@dataclass(frozen=True)
class AggregateFlowResult:
    """Return shape of :func:`aggregate_flow`."""

    run_id: str
    combined_waves: list[int]
    failed_waves: list[int]
    waves_combined_this_call: list[int]
    combiner_dir_local: str
    aggregated_metrics: dict[str, dict[str, Any]]
    summaries_dir_local: str | None = None
    escalation_reason: str | None = None

    def to_envelope_data(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "combined_waves": list(self.combined_waves),
            "failed_waves": list(self.failed_waves),
            "waves_combined_this_call": list(self.waves_combined_this_call),
            "combiner_dir_local": self.combiner_dir_local,
            "aggregated_metrics": dict(self.aggregated_metrics),
            "summaries_dir_local": self.summaries_dir_local,
            "escalation_reason": self.escalation_reason,
        }


def _split_ssh_target(ssh_target: str) -> tuple[str, str]:
    """Wrap :func:`split_ssh_target` to raise the surface-appropriate
    error type. See :mod:`claude_hpc.infra.remote.split_ssh_target`.
    """
    try:
        return split_ssh_target(ssh_target)
    except ValueError as exc:
        raise errors.SpecInvalid(str(exc)) from exc


def _missing_waves(wave_map_keys: list[str], already_combined: list[int]) -> list[int]:
    """Wave numbers in the wave_map that aren't in combined_waves yet."""
    seen = set(already_combined)
    waves: list[int] = []
    for k in wave_map_keys:
        try:
            wave_num = int(k)
        except (TypeError, ValueError):
            continue
        if wave_num not in seen:
            waves.append(wave_num)
    return sorted(waves)


def _combine_missing(
    experiment_dir: Path,
    run_id: str,
    *,
    ssh_target: str,
    remote_path: str,
    waves: list[int],
    max_retries: int,
) -> tuple[list[int], list[tuple[int, str]]]:
    """Run combine-wave for each missing wave; return (combined_now, failures).

    Failures is a list of (wave, stderr_tail) for waves that exhausted retries.
    Combined-now lists the waves that went from missing → combined this call.
    """
    combined_now: list[int] = []
    failures: list[tuple[int, str]] = []
    for wave in waves:
        for attempt in range(1, max_retries + 2):  # initial + max_retries
            ok, _stdout, stderr = runner.combine_wave(
                experiment_dir,
                run_id,
                wave=wave,
                ssh_target=ssh_target,
                remote_path=remote_path,
                force=(attempt > 1),
            )
            if ok:
                combined_now.append(wave)
                break
            if attempt > max_retries:
                failures.append((wave, (stderr or "").strip()[-500:]))
                break
    return combined_now, failures


@primitive(
    name="aggregate-flow",
    verb="workflow",
    composes=[combine_wave, record_status],
    side_effects=[
        SideEffect("ssh", "<cluster>"),
        SideEffect("rsync", "<ssh_target>:<remote_path> -> <experiment_dir>/_aggregated/"),
        SideEffect("writes-journal", "~/.claude/hpc/<repo_hash>/runs/<run_id>.json"),
    ],
    error_codes=[
        errors.SshUnreachable,
        errors.CombinerFailed,
        errors.OutputsMissing,
        errors.JournalCorrupt,
    ],
    idempotent=True,
    idempotency_key="run_id",
    exit_codes=[(0, "ok"), (1, "user-error"), (2, "cluster"), (3, "internal")],
)
def aggregate_flow(
    *,
    experiment_dir: Path,
    run_id: str,
    output_dir: str | Path | None = None,
    ensure_all_combined: bool = True,
    combiner_max_retries: int = 1,
    pull_summaries: bool = False,
    summary_glob: str | None = None,
    results_subdir: str = "results",
) -> AggregateFlowResult:
    """Finalize a run's aggregation; return paths + reduced metrics.

    Parameters
    ----------
    experiment_dir:
        Repo root containing ``.hpc/runs/<run_id>.json``.
    run_id:
        Identifies the run; sidecar must already exist (via submit-flow
        or submit-spec).
    output_dir:
        Local destination for pulled artifacts. Defaults to
        ``<experiment_dir>/_aggregated/<run_id>/`` when None.
    ensure_all_combined:
        When True, invoke combine-wave for every wave_map entry not in
        ``combined_waves`` before pulling. Cheap when monitor-flow already
        combined everything; necessary when the caller skipped monitor-flow.
    combiner_max_retries:
        Per-wave retry budget for combine-wave failures. After this, the
        wave lands in failed_waves and the run continues with whatever
        partials did combine.
    pull_summaries:
        When True, also rsync per-task summary files matching
        ``summary_glob`` from the cluster's ``results/`` subtree.
    summary_glob:
        Required when pull_summaries=True. Rsync include pattern.
    results_subdir:
        Cluster-side subdir under ``remote_path`` that holds per-task
        outputs. Defaults to ``"results"``.

    Raises
    ------
    JournalCorrupt:
        No sidecar for run_id.
    SpecInvalid:
        pull_summaries=True without summary_glob; malformed ssh_target.
    SshUnreachable, RemoteCommandFailed:
        rsync or SSH layer errors propagated from the underlying helpers.
    """
    record = session.load_run(experiment_dir, run_id)
    if record is None:
        raise errors.JournalCorrupt(f"no journal record for {run_id!r}; submit the run first")

    if pull_summaries and not summary_glob:
        raise errors.SpecInvalid("summary_glob is required when pull_summaries=true")

    # Resolve output_dir.
    out = experiment_dir / "_aggregated" / run_id if output_dir is None else Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    user, host = _split_ssh_target(record.ssh_target)

    # Read the sidecar's wave_map (record carries combined_waves but not
    # wave_map — that lives in the per-run sidecar JSON, under
    # <experiment_dir>/.hpc/runs/). ``read_run_sidecar`` guarantees
    # ``wave_map`` is a dict; missing/unreadable sidecars yield empty.
    wave_map_keys: list[str] = []
    try:
        sidecar_data = read_run_sidecar(experiment_dir, run_id)
        wave_map_keys = list((sidecar_data.get("wave_map") or {}).keys())
    except (FileNotFoundError, OSError):
        # No wave_map → no waves to ensure. Aggregation falls back to
        # whatever's already in _combiner/ on the cluster.
        pass

    waves_combined_this_call: list[int] = []
    combiner_failures: list[tuple[int, str]] = []
    if ensure_all_combined and wave_map_keys:
        missing = _missing_waves(wave_map_keys, list(record.combined_waves))
        if missing:
            waves_combined_this_call, combiner_failures = _combine_missing(
                experiment_dir,
                run_id,
                ssh_target=record.ssh_target,
                remote_path=record.remote_path,
                waves=missing,
                max_retries=combiner_max_retries,
            )
            # Re-read after combine-wave updated the journal.
            record = session.load_run(experiment_dir, run_id)
            if record is None:  # pragma: no cover — defensive
                raise errors.JournalCorrupt(f"record vanished for {run_id!r}")

    # Pull the combined partials.
    combiner_local = out / "_combiner"
    pull = rsync_pull(
        host=host,
        user=user,
        remote_path=record.remote_path,
        remote_subdir="_combiner",
        local_dir=str(combiner_local),
    )
    if pull.returncode != 0:
        # No partials at all is a terminal pull failure; partials present
        # but rsync hiccupped is recoverable on retry — surface either way.
        raise errors.RemoteCommandFailed(
            f"rsync_pull of _combiner failed (exit {pull.returncode}): "
            f"{(pull.stderr or '').strip()[:300]}"
        )

    # Reduce locally.
    aggregated = reduce_partials(combiner_local)

    # Optionally pull summaries.
    summaries_local: str | None = None
    if pull_summaries:
        sl = out / "summaries"
        sp = rsync_pull(
            host=host,
            user=user,
            remote_path=record.remote_path,
            remote_subdir=results_subdir,
            local_dir=str(sl),
            include=[summary_glob] if summary_glob else None,
        )
        if sp.returncode != 0:
            # Non-fatal — caller may have asked for a glob that matches
            # nothing yet. Surface via escalation_reason so they see it.
            combiner_failures.append(
                (-1, f"summary rsync failed: {(sp.stderr or '').strip()[:300]}")
            )
        summaries_local = str(sl)

    escalation: str | None = None
    if combiner_failures:
        escalation = "combiner_failed_max_retries:waves=" + ",".join(
            str(w) for w, _ in combiner_failures
        )

    return AggregateFlowResult(
        run_id=run_id,
        combined_waves=list(record.combined_waves),
        failed_waves=list(record.failed_waves),
        waves_combined_this_call=waves_combined_this_call,
        combiner_dir_local=str(combiner_local),
        aggregated_metrics=aggregated,
        summaries_dir_local=summaries_local,
        escalation_reason=escalation,
    )
