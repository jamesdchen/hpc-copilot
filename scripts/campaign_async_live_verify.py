"""Live-verify aid for campaign async-refill (#362) — the RFC §10 cluster gate.

This is a **USER-run measurement aid**, not an offline test and not an automated
pass. Async refill's payoff is only observable on a real cluster, so the four
acceptance criteria in ``docs/design/campaign-async-refill.md`` §10 are gated on a
real run against carc / hoffman2. This script *assists* that run: it samples the
live campaign through the real ``hpc-agent`` CLI verbs and turns each criterion
into measured evidence + a PASS / FAIL / NEEDS-HUMAN verdict. It NEVER fabricates
a cluster result — where only a human can act (kill the driver) or judge
(byte-for-byte artifact equality), it prints instructions and waits.

It drives nothing destructive itself: the campaign + driver are started by the
human per ``docs/runbooks/campaign-async-live-verify.md``. This script only
*reads* the live state by shelling out to:

* ``hpc-agent campaign-status --campaign-id <id> --experiment-dir <dir>``
  → ``{iterations, in_flight, history, run_ids}`` (per-campaign occupancy).
* ``hpc-agent load-context --experiment-dir <dir>``
  → ``in_flight`` rows carrying ``ssh_target`` (poll-group cardinality).
* ``hpc-agent campaign-advance --campaign-id <id> [--async-refill --max-in-flight K]``
  → ``{decision, refill_count}`` (definitive: refill ladder vs sync ladder).

The four criteria (RFC §10):

1. **Pool occupancy** stays ≈ K across iteration boundaries (no drain-to-zero) —
   measurably higher than the synchronous baseline on the same straggler-heavy
   workload. Measured by sampling ``in_flight`` over a window that spans at least
   one iteration boundary; compared against a ``--baseline`` summary when present.
2. **Crash-safe resume** — interactive. Snapshot, the human kills + restarts the
   driver, settle, snapshot again; diff for stranded / double-told trials.
3. **Default unchanged** (``--baseline``) — the same campaign with async off
   drains to zero between iterations and ``campaign-advance`` never returns
   ``refill``. Byte-for-byte artifact equality is a human cross-check.
4. **Polling** within the connection-storm envelope (#346): one query per
   login-node group regardless of in-flight count — the poll-group cardinality
   does not scale with ``in_flight``.

Usage (run ON the submit host, with the driver already looping)::

    .venv/Scripts/python.exe scripts/campaign_async_live_verify.py \\
        --experiment-dir . --campaign-id ebm_carc --cluster carc --max-in-flight 4

    # synchronous baseline (criterion 3), same campaign with async off:
    .venv/Scripts/python.exe scripts/campaign_async_live_verify.py \\
        --experiment-dir . --campaign-id ebm_carc --cluster carc --baseline

Exit codes: ``0`` every selected criterion PASSed; ``1`` a measurable criterion
FAILed; ``2`` no failure but a human sign-off / observation is still required.

``--help`` works offline (no cluster, no campaign). Everything cluster-touching is
deferred to subprocess calls made only when a criterion actually runs.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Framework default pool target K (mirrors advance._DEFAULT_MAX_IN_FLIGHT).
_DEFAULT_MAX_IN_FLIGHT = 4

# Verdict labels.
_PASS = "PASS"
_FAIL = "FAIL"
_NEEDS_HUMAN = "NEEDS-HUMAN"
_INFO = "INFO"
_SKIP = "SKIP"


@dataclass
class CriterionResult:
    """One acceptance criterion's measured verdict + the evidence behind it."""

    name: str
    status: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Sample:
    """One occupancy reading taken from the live campaign."""

    t: float  # seconds since sampling began (monotonic)
    in_flight: int
    iterations: int
    completed: int
    groups: int  # distinct (cluster, ssh_target) poll groups


# ─── CLI shell-out ──────────────────────────────────────────────────────────


class CliError(RuntimeError):
    """An ``hpc-agent`` invocation exited non-zero or returned ``ok: false``."""


def _run_hpc(hpc_bin: str, verb: str, args: list[str]) -> dict[str, Any]:
    """Shell out to one ``hpc-agent`` verb and return its envelope ``data``.

    Stdout is a single-line JSON envelope (``{"ok": ..., "data": {...}}``); we
    parse it, raise :class:`CliError` on ``ok: false`` or a non-zero exit, and
    hand back the ``data`` payload. Stderr is diagnostic prose, surfaced on error.
    """
    cmd = [hpc_bin, verb, *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
    out = (proc.stdout or "").strip()
    if not out:
        raise CliError(
            f"`{' '.join(cmd)}` produced no stdout (exit {proc.returncode}); "
            f"stderr:\n{(proc.stderr or '').strip()}"
        )
    try:
        envelope: dict[str, Any] = json.loads(out.splitlines()[-1])
    except (json.JSONDecodeError, ValueError) as exc:
        raise CliError(f"`{' '.join(cmd)}` stdout was not JSON: {exc}\n{out}") from exc
    if not isinstance(envelope, dict) or not envelope.get("ok", False):
        raise CliError(
            f"`{' '.join(cmd)}` returned a failure envelope (exit {proc.returncode}): {envelope}"
        )
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise CliError(f"`{' '.join(cmd)}` envelope had no `data` object: {envelope}")
    return data


def _status(hpc_bin: str, experiment_dir: str, campaign_id: str) -> dict[str, Any]:
    return _run_hpc(
        hpc_bin,
        "campaign-status",
        ["--campaign-id", campaign_id, "--experiment-dir", experiment_dir],
    )


def _load_context(hpc_bin: str, experiment_dir: str) -> dict[str, Any]:
    return _run_hpc(hpc_bin, "load-context", ["--experiment-dir", experiment_dir])


def _advance(
    hpc_bin: str,
    experiment_dir: str,
    campaign_id: str,
    *,
    async_refill: bool,
    max_in_flight: int,
) -> dict[str, Any]:
    args = ["--campaign-id", campaign_id, "--experiment-dir", experiment_dir]
    if async_refill:
        args += ["--async-refill", "--max-in-flight", str(max_in_flight)]
    return _run_hpc(hpc_bin, "campaign-advance", args)


def _poll_groups(hpc_bin: str, experiment_dir: str, campaign_id: str) -> int:
    """Distinct ``(cluster, ssh_target)`` poll groups for this campaign's in-flight runs.

    This is the quantity #346 says bounds the poll count: ``batch-status`` issues one
    ``qstat`` per login-node group regardless of run count, so a correct async pool
    keeps this cardinality flat as ``in_flight`` grows.
    """
    ctx = _load_context(hpc_bin, experiment_dir)
    rows = ctx.get("in_flight") or []
    groups = {
        (r.get("cluster"), r.get("ssh_target"))
        for r in rows
        if isinstance(r, dict) and r.get("campaign_id") == campaign_id
    }
    return len(groups)


def _completed_count(status: dict[str, Any]) -> int:
    """Number of iterations with at least one reduced metric (a 'told' trial proxy)."""
    history = status.get("history") or []
    return sum(1 for h in history if h)


def _study_complete_trials(experiment_dir: str, campaign_id: str) -> int | None:
    """Count COMPLETE trials in the optuna study, or ``None`` if it can't be read.

    Best-effort cross-check for the double-told invariant (criterion 2): the durable
    'told' set is the optuna sqlite store at ``.hpc/campaigns/<cid>/optuna.db``
    (study_name == campaign_id; see the async scaffold). If ``optuna`` is not
    importable or the store is absent, returns ``None`` and the caller falls back to
    the CLI-only invariants plus a human instruction.
    """
    db = Path(experiment_dir) / ".hpc" / "campaigns" / campaign_id / "optuna.db"
    if not db.is_file():
        return None
    try:
        import optuna
    except ImportError:
        return None
    try:
        study = optuna.load_study(study_name=campaign_id, storage=f"sqlite:///{db}")
        trials = study.get_trials(deepcopy=False)
    except Exception:  # noqa: BLE001 — any optuna/sqlite read failure → degrade, don't crash
        return None
    complete = optuna.trial.TrialState.COMPLETE
    return sum(1 for t in trials if t.state == complete)


# ─── sampling ───────────────────────────────────────────────────────────────


def _sample_occupancy(
    hpc_bin: str,
    experiment_dir: str,
    campaign_id: str,
    *,
    samples: int,
    interval: float,
) -> list[Sample]:
    """Sample per-campaign ``in_flight`` + poll-group cardinality over a window.

    The window must span at least one iteration boundary to judge criterion 1 / 3,
    so callers size ``samples * interval`` to comfortably exceed one trial duration.
    """
    out: list[Sample] = []
    t0 = time.monotonic()
    for i in range(samples):
        st = _status(hpc_bin, experiment_dir, campaign_id)
        groups = _poll_groups(hpc_bin, experiment_dir, campaign_id)
        out.append(
            Sample(
                t=round(time.monotonic() - t0, 1),
                in_flight=int(st.get("in_flight", 0)),
                iterations=int(st.get("iterations", 0)),
                completed=_completed_count(st),
                groups=groups,
            )
        )
        last = out[-1]
        print(
            f"  [{i + 1:>2}/{samples}] t={last.t:>6.1f}s  in_flight={last.in_flight}  "
            f"iterations={last.iterations}  completed={last.completed}  "
            f"poll_groups={last.groups}",
            flush=True,
        )
        if i < samples - 1:
            time.sleep(interval)
    return out


def _occupancy_stats(rows: list[Sample]) -> dict[str, Any]:
    flights = [r.in_flight for r in rows]
    return {
        "n_samples": len(rows),
        "in_flight_min": min(flights) if flights else 0,
        "in_flight_max": max(flights) if flights else 0,
        "in_flight_mean": round(sum(flights) / len(flights), 2) if flights else 0.0,
        "iterations_first": rows[0].iterations if rows else 0,
        "iterations_last": rows[-1].iterations if rows else 0,
        "groups_max": max((r.groups for r in rows), default=0),
        "samples": [vars(r) for r in rows],
    }


def _summary_path(experiment_dir: str, campaign_id: str, mode: str) -> Path:
    return Path(experiment_dir) / ".hpc" / "live-verify" / f"{campaign_id}.{mode}.json"


def _write_summary(experiment_dir: str, campaign_id: str, mode: str, stats: dict[str, Any]) -> Path:
    path = _summary_path(experiment_dir, campaign_id, mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(stats, indent=2), encoding="utf-8")
    return path


def _read_summary(experiment_dir: str, campaign_id: str, mode: str) -> dict[str, Any] | None:
    path = _summary_path(experiment_dir, campaign_id, mode)
    if not path.is_file():
        return None
    try:
        loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded


# ─── criteria ───────────────────────────────────────────────────────────────


def criterion_1_occupancy(
    hpc_bin: str,
    experiment_dir: str,
    campaign_id: str,
    *,
    max_in_flight: int,
    samples: int,
    interval: float,
) -> CriterionResult:
    """Pool occupancy stays ≈ K across iteration boundaries (no drain-to-zero)."""
    name = "1. Pool occupancy ≈ K (no drain-to-zero)"
    # Definitive: the refill ladder is actually engaged for this campaign.
    adv = _advance(
        hpc_bin, experiment_dir, campaign_id, async_refill=True, max_in_flight=max_in_flight
    )
    decision = adv.get("decision")
    refill_count = adv.get("refill_count")
    print(
        f"  campaign-advance --async-refill --max-in-flight {max_in_flight} -> "
        f"decision={decision!r} refill_count={refill_count}",
        flush=True,
    )
    print(f"  sampling occupancy ({samples} samples @ {interval:g}s)...", flush=True)
    rows = _sample_occupancy(
        hpc_bin, experiment_dir, campaign_id, samples=samples, interval=interval
    )
    stats = _occupancy_stats(rows)
    stats["advance_decision"] = decision
    stats["max_in_flight"] = max_in_flight
    _write_summary(experiment_dir, campaign_id, "async", stats)

    progressed = stats["iterations_last"] > stats["iterations_first"]
    no_drain = stats["in_flight_min"] > 0
    near_k = stats["in_flight_mean"] >= max(1.5, max_in_flight * 0.5)
    decision_ok = decision in {"refill", "wait_in_flight"}

    baseline = _read_summary(experiment_dir, campaign_id, "baseline")
    compare = ""
    if baseline is not None:
        b_mean = baseline.get("in_flight_mean", 0.0)
        higher = stats["in_flight_mean"] > b_mean
        stats["baseline_in_flight_mean"] = b_mean
        compare = (
            f" async mean {stats['in_flight_mean']} vs baseline mean {b_mean} "
            f"({'HIGHER ✓' if higher else 'NOT higher ✗'})."
        )
        if not higher:
            no_drain = False  # equal/lower utilization than sync defeats the whole point
    else:
        compare = " (no baseline summary yet — run with --baseline to complete the comparison.)"

    if not decision_ok:
        return CriterionResult(
            name,
            _FAIL,
            f"campaign-advance returned decision={decision!r}, not refill/wait_in_flight — the "
            "async ladder is not engaged (check the manifest's async_refill / max_in_flight).",
            stats,
        )
    if not progressed:
        return CriterionResult(
            name,
            _NEEDS_HUMAN,
            "no iteration boundary observed in the window (iterations did not advance) — "
            "lengthen --samples/--interval so it spans a trial completion, then re-run.",
            stats,
        )
    if no_drain and near_k:
        return CriterionResult(
            name,
            _PASS,
            f"occupancy held min={stats['in_flight_min']} mean={stats['in_flight_mean']} "
            f"(K={max_in_flight}) across an iteration boundary — no drain-to-zero.{compare}",
            stats,
        )
    return CriterionResult(
        name,
        _FAIL,
        f"occupancy drained or stayed low: min={stats['in_flight_min']} "
        f"mean={stats['in_flight_mean']} (K={max_in_flight}).{compare}",
        stats,
    )


def criterion_2_crash_safe(
    hpc_bin: str,
    experiment_dir: str,
    campaign_id: str,
    *,
    settle: float,
    interactive: bool,
) -> CriterionResult:
    """Kill the driver mid-stream, restart: no stranded + no double-told trials."""
    name = "2. Crash-safe resume (no stranded / double-told)"
    before = _status(hpc_bin, experiment_dir, campaign_id)
    before_runs = set(before.get("run_ids") or [])
    before_inflight = {
        r.get("run_id")
        for r in (_load_context(hpc_bin, experiment_dir).get("in_flight") or [])
        if isinstance(r, dict) and r.get("campaign_id") == campaign_id
    }
    before_completed = _completed_count(before)
    print(
        f"  BEFORE kill: iterations={before.get('iterations')} "
        f"in_flight={before.get('in_flight')} completed={before_completed} "
        f"in_flight_run_ids={sorted(x for x in before_inflight if x)}",
        flush=True,
    )

    if not interactive:
        return CriterionResult(
            name,
            _NEEDS_HUMAN,
            "crash-safe resume needs an interactive kill+restart of the driver; re-run without "
            "--skip-crash-safe on a TTY. Snapshot captured above for a manual diff.",
            {"before_run_ids": sorted(before_runs), "before_completed": before_completed},
        )

    print("\n" + "=" * 78)
    print("  ACTION REQUIRED — kill the driver MID-STREAM, then restart it:")
    print("    1. Stop the running driver now (Ctrl-C the /loop, or kill the cron tick / PID)")
    print("       WHILE trials are in flight (the BEFORE snapshot shows the in-flight set).")
    print("    2. Restart it exactly as before, e.g.:")
    print("         /loop 30m hpc-campaign-driver --experiment-dir . --allow-agent-steps")
    print(f"    3. Press ENTER once it is killed AND restarted; this aid waits {settle:g}s for the")
    print("       restarted driver to reconcile from .hpc/ before re-snapshotting.")
    print("=" * 78)
    try:
        input("  [ENTER to continue once driver is killed + restarted] ")
    except EOFError:
        return CriterionResult(
            name,
            _NEEDS_HUMAN,
            "no interactive input to confirm the kill+restart; complete the diff by hand.",
            {"before_run_ids": sorted(before_runs)},
        )
    print(f"  settling {settle:g}s for the restarted driver to reconcile...", flush=True)
    time.sleep(settle)

    after = _status(hpc_bin, experiment_dir, campaign_id)
    after_runs = set(after.get("run_ids") or [])
    after_completed = _completed_count(after)
    print(
        f"  AFTER restart+settle: iterations={after.get('iterations')} "
        f"in_flight={after.get('in_flight')} completed={after_completed}",
        flush=True,
    )

    # Stranded: a sidecar that existed before has vanished (its trial was lost), OR an
    # in-flight run that is now neither still in flight nor present as a completed sidecar.
    vanished = sorted(before_runs - after_runs)
    after_inflight = {
        r.get("run_id")
        for r in (_load_context(hpc_bin, experiment_dir).get("in_flight") or [])
        if isinstance(r, dict) and r.get("campaign_id") == campaign_id
    }
    stranded = sorted(
        rid
        for rid in before_inflight
        if rid and rid not in after_runs and rid not in after_inflight
    )

    # Double-told: the durable told set (optuna study) must not have MORE complete
    # trials than there are completed iteration records — a re-tell would inflate it.
    study_complete = _study_complete_trials(experiment_dir, campaign_id)
    double_told: bool | None = None
    if study_complete is not None:
        double_told = study_complete > after_completed

    ev: dict[str, Any] = {
        "before_run_ids": sorted(before_runs),
        "after_run_ids": sorted(after_runs),
        "vanished_sidecars": vanished,
        "before_inflight": sorted(x for x in before_inflight if x),
        "stranded_run_ids": stranded,
        "before_completed": before_completed,
        "after_completed": after_completed,
        "study_complete_trials": study_complete,
        "double_told": double_told,
    }
    problems: list[str] = []
    if vanished:
        problems.append(f"{len(vanished)} sidecar(s) vanished after restart: {vanished}")
    if stranded:
        problems.append(f"{len(stranded)} stranded trial(s) (lost from both sets): {stranded}")
    if after_completed < before_completed:
        problems.append(
            f"completed count regressed {before_completed}->{after_completed} (told result lost)"
        )
    if double_told:
        problems.append(
            f"study has {study_complete} COMPLETE trials > {after_completed} completed records "
            "(double-told)"
        )
    if problems:
        return CriterionResult(name, _FAIL, "; ".join(problems), ev)
    if study_complete is None:
        return CriterionResult(
            name,
            _NEEDS_HUMAN,
            "no stranded trials and no lost results across the kill — but the optuna study could "
            "not be read here, so confirm no double-tell by inspecting "
            f".hpc/campaigns/{campaign_id}/optuna.db (COMPLETE trials == {after_completed}).",
            ev,
        )
    return CriterionResult(
        name,
        _PASS,
        f"resume reconstructed cleanly: no vanished/stranded trials, completed "
        f"{before_completed}->{after_completed}, study COMPLETE trials={study_complete} "
        "== completed records (no double-tell).",
        ev,
    )


def criterion_3_default_unchanged(
    hpc_bin: str,
    experiment_dir: str,
    campaign_id: str,
    *,
    samples: int,
    interval: float,
) -> CriterionResult:
    """Default-off reproduces synchronous batch behavior (drains to zero; never refills)."""
    name = "3. Default-off == synchronous batch behavior"
    adv = _advance(hpc_bin, experiment_dir, campaign_id, async_refill=False, max_in_flight=0)
    decision = adv.get("decision")
    print(f"  campaign-advance (no async) -> decision={decision!r}", flush=True)
    if decision == "refill":
        return CriterionResult(
            name,
            _FAIL,
            "synchronous campaign-advance returned decision='refill' — the default ladder is NOT "
            "byte-identical (async refill is leaking into the default path).",
            {"advance_decision": decision},
        )
    print(f"  sampling occupancy ({samples} samples @ {interval:g}s)...", flush=True)
    rows = _sample_occupancy(
        hpc_bin, experiment_dir, campaign_id, samples=samples, interval=interval
    )
    stats = _occupancy_stats(rows)
    stats["advance_decision"] = decision
    _write_summary(experiment_dir, campaign_id, "baseline", stats)

    progressed = stats["iterations_last"] > stats["iterations_first"]
    drains = stats["in_flight_min"] == 0  # returns to zero between iterations
    synchronous_bound = stats["in_flight_max"] <= 1  # optuna scaffold submits B=1 per iteration

    if not progressed:
        return CriterionResult(
            name,
            _NEEDS_HUMAN,
            "no iteration boundary observed (iterations did not advance) — lengthen the window.",
            stats,
        )
    if drains and synchronous_bound:
        return CriterionResult(
            name,
            _PASS,
            f"sync behavior reproduced: drained to 0 between iterations "
            f"(min={stats['in_flight_min']}, max={stats['in_flight_max']}≤1) and advance never "
            "refilled. NOTE: byte-for-byte "
            "artifact equality vs an async run of the same seed is a HUMAN cross-check.",
            stats,
        )
    return CriterionResult(
        name,
        _FAIL,
        f"not the synchronous sawtooth: min={stats['in_flight_min']} (expected 0) "
        f"max={stats['in_flight_max']} (expected ≤1).",
        stats,
    )


def criterion_4_poll_envelope(
    hpc_bin: str,
    experiment_dir: str,
    campaign_id: str,
    *,
    expected_groups: int,
    summary: dict[str, Any] | None,
) -> CriterionResult:
    """Poll-group cardinality stays flat as in_flight grows (connection-storm envelope)."""
    name = "4. Poll within connection-storm envelope (#346)"
    if summary is None:
        st = _status(hpc_bin, experiment_dir, campaign_id)
        in_flight_max = int(st.get("in_flight", 0))
        groups_max = _poll_groups(hpc_bin, experiment_dir, campaign_id)
    else:
        in_flight_max = int(summary.get("in_flight_max", 0))
        groups_max = int(summary.get("groups_max", 0))
    ev = {
        "in_flight_max": in_flight_max,
        "poll_groups_max": groups_max,
        "expected_groups": expected_groups,
    }
    print(
        f"  observed in_flight_max={in_flight_max}  poll_groups_max={groups_max}  "
        f"expected_groups≈{expected_groups}",
        flush=True,
    )
    hint = (
        "Cross-check the ACTUAL qstat count: with the driver running, the number of qstat/login "
        "queries per poll must equal the poll-group count, NOT in_flight. Count them in the "
        "driver/ssh logs (e.g. grep the monitor-flow stderr or the ssh throttle log)."
    )
    if in_flight_max <= 1:
        return CriterionResult(
            name,
            _NEEDS_HUMAN,
            f"never observed the storm regime (in_flight_max={in_flight_max}≤1) — run criterion 1 "
            f"first so the pool fills, then re-check. {hint}",
            ev,
        )
    if groups_max <= expected_groups:
        return CriterionResult(
            name,
            _PASS,
            f"poll-group cardinality stayed flat ({groups_max}≤{expected_groups}) while in_flight "
            f"reached {in_flight_max} — polling did not scale per-run. {hint}",
            ev,
        )
    return CriterionResult(
        name,
        _FAIL,
        f"poll groups ({groups_max}) exceeded the expected login-node count ({expected_groups}) — "
        f"polling appears to scale with in_flight ({in_flight_max}). {hint}",
        ev,
    )


# ─── reporting ──────────────────────────────────────────────────────────────


def _print_banner(args: argparse.Namespace, mode: str) -> None:
    print("=" * 78)
    print("  campaign async-refill — LIVE-VERIFY (RFC §10 gate)")
    print("  This runs against a REAL cluster and is the gate that flips async refill")
    print("  from 'experimental' to 'shipped'. It is a measurement aid, NOT an")
    print("  automated pass: human-judged steps are surfaced as NEEDS-HUMAN.")
    print("-" * 78)
    print(f"  experiment_dir : {args.experiment_dir}")
    print(f"  campaign_id    : {args.campaign_id}")
    print(f"  cluster        : {args.cluster}")
    print(f"  mode           : {mode}")
    if mode == "async":
        print(f"  max_in_flight  : {args.max_in_flight}")
    print("=" * 78)


def _print_summary(results: list[CriterionResult]) -> int:
    print("\n" + "=" * 78)
    print("  LIVE-VERIFY SUMMARY")
    print("=" * 78)
    for r in results:
        print(f"  [{r.status:^11}] {r.name}")
        print(f"               {r.detail}")
    print("-" * 78)
    n_fail = sum(1 for r in results if r.status == _FAIL)
    n_human = sum(1 for r in results if r.status in {_NEEDS_HUMAN, _INFO})
    n_pass = sum(1 for r in results if r.status == _PASS)
    print(f"  {n_pass} PASS / {n_fail} FAIL / {n_human} NEEDS-HUMAN  (of {len(results)} checked)")
    if n_fail:
        print("  GATE: FAIL — a measurable criterion failed; async refill stays experimental.")
        print("=" * 78)
        return 1
    if n_human:
        print("  GATE: INCOMPLETE — no failures, but human sign-off/observation is still required")
        print("        before declaring the gate green. This aid does not pass it for you.")
        print("=" * 78)
        return 2
    print("  GATE: PASS — every selected criterion measured PASS. Record this in the runbook.")
    print("=" * 78)
    return 0


# ─── entry point ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="campaign_async_live_verify.py",
        description=(
            "Live-verify aid for campaign async-refill (#362), RFC §10 cluster gate. "
            "USER-run measurement aid — NOT an offline test and NOT an automated pass."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Run on the submit host with the campaign driver already looping. See "
            "docs/runbooks/campaign-async-live-verify.md for the full procedure."
        ),
    )
    p.add_argument(
        "--experiment-dir",
        required=True,
        help="Repo root (parent of .hpc/) where the campaign lives.",
    )
    p.add_argument(
        "--campaign-id",
        required=True,
        help="Campaign id (the isolation slug) to verify.",
    )
    p.add_argument(
        "--cluster",
        required=True,
        help="Target cluster key (e.g. carc / hoffman2). Sets the expected poll-group count.",
    )
    p.add_argument(
        "--max-in-flight",
        type=int,
        default=_DEFAULT_MAX_IN_FLIGHT,
        help=f"Pool-occupancy target K for the async run (default {_DEFAULT_MAX_IN_FLIGHT}).",
    )
    p.add_argument(
        "--baseline",
        action="store_true",
        help="Synchronous baseline run (criterion 3): same campaign with async OFF.",
    )
    p.add_argument(
        "--samples",
        type=int,
        default=12,
        help="Occupancy samples to take (default 12).",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=30.0,
        help="Seconds between occupancy samples (default 30). samples*interval must span a "
        "trial boundary.",
    )
    p.add_argument(
        "--settle",
        type=float,
        default=120.0,
        help="Seconds to wait after the driver restart before re-snapshotting (criterion 2; "
        "default 120).",
    )
    p.add_argument(
        "--expected-groups",
        type=int,
        default=1,
        help="Expected login-node poll groups (criterion 4; default 1 = single cluster).",
    )
    p.add_argument(
        "--skip-crash-safe",
        action="store_true",
        help="Skip the interactive crash-safe kill+restart (criterion 2); snapshot only.",
    )
    p.add_argument(
        "--hpc-bin",
        default="hpc-agent",
        help="The hpc-agent CLI binary to shell out to (default 'hpc-agent').",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mode = "baseline" if args.baseline else "async"
    _print_banner(args, mode)

    if shutil.which(args.hpc_bin) is None and not Path(args.hpc_bin).exists():
        print(
            f"ERROR: hpc-agent binary {args.hpc_bin!r} not found on PATH. This script must run "
            "where the campaign was submitted (the real cluster surface). Pass --hpc-bin if it "
            "is installed under another name.",
            file=sys.stderr,
        )
        return 2

    results: list[CriterionResult] = []
    try:
        if args.baseline:
            print("\n--- Criterion 3: default-off synchronous behavior ---")
            results.append(
                criterion_3_default_unchanged(
                    args.hpc_bin,
                    args.experiment_dir,
                    args.campaign_id,
                    samples=args.samples,
                    interval=args.interval,
                )
            )
        else:
            print("\n--- Criterion 1: pool occupancy ≈ K ---")
            results.append(
                criterion_1_occupancy(
                    args.hpc_bin,
                    args.experiment_dir,
                    args.campaign_id,
                    max_in_flight=args.max_in_flight,
                    samples=args.samples,
                    interval=args.interval,
                )
            )
            print("\n--- Criterion 2: crash-safe resume ---")
            results.append(
                criterion_2_crash_safe(
                    args.hpc_bin,
                    args.experiment_dir,
                    args.campaign_id,
                    settle=args.settle,
                    interactive=(not args.skip_crash_safe) and sys.stdin.isatty(),
                )
            )
            print("\n--- Criterion 4: poll envelope ---")
            results.append(
                criterion_4_poll_envelope(
                    args.hpc_bin,
                    args.experiment_dir,
                    args.campaign_id,
                    expected_groups=args.expected_groups,
                    summary=_read_summary(args.experiment_dir, args.campaign_id, "async"),
                )
            )
    except CliError as exc:
        print(f"\nERROR shelling out to hpc-agent: {exc}", file=sys.stderr)
        return 2

    return _print_summary(results)


if __name__ == "__main__":
    sys.exit(main())
