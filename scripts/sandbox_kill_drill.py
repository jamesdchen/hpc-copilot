"""U4 — the autonomous kill drill (rung-2 proving, plan §4-U4).

WHY THIS EXISTS (``docs/plans/sandbox-proving-run-2026-07-18.md`` §4-U4)
------------------------------------------------------------------------
The ONE genuinely non-idempotent actuation in the whole pipeline is the
``qsub dispatch → job-id window``: the scheduler has ACCEPTED the array but
the client has not yet recorded its id. A process drop inside that window is
exactly how the 2026-06-11 demo lost main-array id 13610902. The submit-once
contract (``tests/faultinject/test_submit_once.py`` is its hermetic rung-1
twin) makes recovery a READ: the dispatching shell persisted the id in a
cluster-durable jobmap MARKER, and ``reconcile`` ADOPTS it with **zero
re-qsub**. This drill is that contract's scheduler-API twin — it drives a
real fixture run to the submit window against the container cluster, severs
the local dispatch process IN the window, and asserts the full recovery
contract, each leg a named evidence row:

    sidecar submitting / job_ids=[]        (the drop landed before the promote)
    cluster jobmap marker pending, wave-0 rc==0 + a parseable id
    reconcile ADOPTS                        (brief: in_flight + adopted_from_marker)
    journal in_flight carrying the marker's id
    EXACTLY ONE array under <run_id>#0, ZERO re-qsub
    the adopted array harvests to terminal  (monitor → S4 results table)

The window is racy by nature (the promote follows the dispatch within
milliseconds), so a miss is NOT a failure: the drill bumps the sweep
(fresh ``n_samples`` → fresh ``run_id``, the determinism lesson) and retries,
BOUNDED at ``MAX_KILL_ATTEMPTS`` (3) — the same bounded retry the live
runsheet uses. Exhaustion exits non-zero with per-attempt evidence.

TRUST DOCTRINE (plan §3 — the part that must never bend)
--------------------------------------------------------
Identical to the U3 driver, and enforced by reusing its guard — there is NO
fourth inline copy here:

* ``HPC_JOURNAL_DIR`` is REQUIRED-EPHEMERAL at drill start: the driver's
  :func:`run_sandbox_proving.require_ephemeral_journal_home` (itself a thin
  delegate to the ONE shared ``sandbox_guard``) REFUSES when the var is unset
  or resolves inside ``~/.claude/hpc``, under every alias spelling the guard's
  red-team corpus pins.
* The human-authorship utterance is SEEDED by the same ``sandbox_seed`` sibling
  the driver uses (``seeded_by: sandbox-proving`` provenance). A sandbox drill
  proves the gates + the recovery contract FIRE correctly; it never proves a
  human approved anything.
* Rung-2 jurisdiction: this drill adjudicates the submit-once recovery
  contract against a real scheduler API. It can NEVER certify a default flip,
  a "validated live" claim, or any cluster-environment truth — rung 3 keeps
  that monopoly.

RELATIONSHIP TO THE U3 DRIVER (consumed BY PATH, never edited)
--------------------------------------------------------------
The drill imports ``scripts/run_sandbox_proving.py`` BY PATH (the same
spec-from-file-location idiom the hermetic tests use) and reuses its §3 guard,
its fixture/seed bridges, its CLI runner + envelope parsing, its spec
composers, its greenlight/launch/wait helpers, and its evidence builders. It
re-implements ONLY the U4-specific surface: the scheduler token-snapshot
parsers (window detection + the one-array/zero-re-qsub counters, built on the
framework's own ``ProfileBackend`` token query — the SAME mechanism reconcile's
rung-1b adopts by), the lease-PID extraction + local kill, the jobmap-marker
read, and the recovery-contract leg assertions. The driver is import-safe and
this module is too (the driver import has no side effects beyond defining its
own functions/constants).

USAGE
-----
::

    # The CI lane (workflow_dispatch with with_kill_drill=true) invokes:
    HPC_JOURNAL_DIR=$RUNNER_TEMP/hpc-journal-drill \\
        python scripts/sandbox_kill_drill.py --clusters-config ci_clusters.yaml

    # Docker-capable dev machine: stand the container up, drill, tear down:
    HPC_JOURNAL_DIR=$(mktemp -d)/journal \\
        python scripts/sandbox_kill_drill.py --local

Note: dev tooling — lives in ``scripts/``, never shipped in the wheel.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
_DRIVER_PATH = REPO_ROOT / "scripts" / "run_sandbox_proving.py"

# ── Kill-drill tuning constants ──────────────────────────────────────────────
# The submit window is racy by nature (the journal promote follows the dispatch
# within milliseconds), so a miss is expected — the drill bumps the sweep and
# retries, bounded, exactly like the live runsheet.
MAX_KILL_ATTEMPTS = 3
DEFAULT_N_SAMPLES_BUMP = 1  # per-attempt n_samples bump → a fresh run_id each try
# sacct is DISABLED on the container, so a completed array vanishes from squeue
# instantly: the array's squeue-visibility window IS its task walltime. The
# 2026-07-19 CI failure (scheduler-integration run 29709733724) pinned both
# halves of the deterministic 3/3 miss: ~160ms fixture tasks → a 0.9–1.4s
# squeue window, AND a fixed 2s poll whose phase locked against the pipeline's
# ~10.6s spawn→submit cadence so the window parked in the same inter-poll gap
# every attempt. The fix pairs a LONGER window (5–10s fixture tasks via the
# driver's n_samples→walltime mapping, DEFAULT_FIXTURE_N_SAMPLES) with a
# SUB-SECOND JITTERED poll: the loop sleeps AFTER each query, so the effective
# period is (sleep + ssh RTT) — a fixed sleep keeps the period constant and
# phase-lockable even with the RTT folded in, so each sleep draws a fresh
# uniform jitter and the poll phase random-walks off any fixed cadence.
WINDOW_POLL_INTERVAL_SEC = 0.5  # mean poll sleep (seconds)
WINDOW_POLL_JITTER_FRAC = 0.2  # ±20% uniform jitter per sleep — breaks phase-lock
WINDOW_POLL_BUDGET_SEC = 180  # wait for the array to enter the scheduler queue
KILL_SETTLE_SEC = 2  # let the worker's death land before reading the journal

# Attempt outcome kinds beyond hit/missed/error: the poll NEVER saw the array,
# yet a dispatch witness (the journal's promoted job_ids, or the cluster-side
# jobmap wave-0 id + results) proves it entered, ran, and exited INSIDE a poll
# gap — sacct-disabled means a completed array's squeue lifetime can be shorter
# than one poll period. DISTINCT from a genuine never-dispatched (no marker,
# no id, no results — a real dispatch failure), which stays an ``error``.
DISPATCHED_UNSEEN = "dispatched-unseen"

# The recovery-contract verdict reason that PROVES adoption (no re-qsub). Mirrors
# ``reconcile._adopt_and_promote``'s stamp — the brief-side adoption signal.
ADOPT_VERDICT_REASON = "submit_once_adopted_from_marker"
# reconcile's safe-resubmit verdict — the OUTCOME a successful drill must NOT
# land on (it would mean the marker read missed and a re-qsub was authorized).
NEVER_DISPATCHED_VERDICT_REASON = "submit_once_never_dispatched_safe_resubmit"

DEFAULT_GOAL = "sandbox-prove the submit-once kill-drill recovery contract on the container cluster"
DEFAULT_RUN_NAME = "sandbox-killdrill"
DEFAULT_WALLTIME_SEC = 900


# ── Load the U3 driver BY PATH (the sanctioned consumption surface) ──────────


def _load_driver() -> Any:
    """Import ``scripts/run_sandbox_proving.py`` as the single shared object.

    The driver is import-safe (no side effects beyond defining its own
    functions/constants), and it is the ONE place the §3 guard, the fixture/seed
    bridges, the CLI runner, and the evidence builders live — the drill reuses
    them rather than forking a fourth guard copy or a second chain. Loaded by
    path (scripts/ is not an import root) and cached in ``sys.modules`` so a
    second load binds the same object.
    """
    cached = sys.modules.get("run_sandbox_proving")
    if cached is not None:
        return cached
    if not _DRIVER_PATH.is_file():
        raise RuntimeError(
            f"sandbox_kill_drill: the U3 driver is not at {_DRIVER_PATH} — it "
            "landed in 012c3696 and the kill drill imports from it (plan §4-U4)."
        )
    spec = importlib.util.spec_from_file_location("run_sandbox_proving", _DRIVER_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"sandbox_kill_drill: cannot import the U3 driver at {_DRIVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_sandbox_proving"] = module
    spec.loader.exec_module(module)
    return module


_driver = _load_driver()
# Re-export the driver's refusal type so callers + tests catch one class. The
# §3 guard itself is the driver's (it delegates to the ONE shared sandbox_guard
# — no fourth inline copy; plan §3).
SandboxRefusal = _driver.SandboxRefusal
require_ephemeral_journal_home = _driver.require_ephemeral_journal_home


# ────────────────────────────────────────────────────────────────────────────
# Pure helpers (hermetically testable — no cluster, no docker, no subprocess).
# These are the load-bearing surface: window detection, the bounded retry loop,
# the one-array/zero-re-qsub counters, lease-PID extraction, and every
# recovery-contract leg assertion. The chain below wires them to the cluster.
# ────────────────────────────────────────────────────────────────────────────


def _backend_cls(scheduler: str) -> Any:
    """The framework's ``ProfileBackend`` class for *scheduler* (pure registry
    lookup — no cluster). Reusing it (not re-parsing squeue/qstat by hand) keeps
    the drill's token read byte-identical to reconcile's own rung-1b adopt."""
    from hpc_agent.infra.backends import get_backend_class

    return get_backend_class(scheduler)


def token_job_ids(scheduler: str, stdout: str, token: str) -> list[str]:
    """Every base job id the scheduler snapshot tags with *token* — PRE-collapse.

    The framework's ``parse_token_query`` folds all of one array's subjob rows
    (``12345_7``, ``12345_8``, …) into a single base id with ``setdefault`` —
    correct for adoption, but it HIDES a duplicate array (two distinct ids under
    one token, the exact corruption submit-once exists to prevent). This keeps
    every match so ``count_arrays_under_token`` can see a duplicate. It follows
    the per-family read in ``ProfileBackend.parse_token_query``: slurm rows are
    ``<jid>|<comment>`` (the comment IS the ``<run_id>#<attempt>`` token); sge
    pairs a ``job_number:`` line with the ``HPC_TOKEN=<token>`` on ``context:``.

    A snapshot whose scheduler-query ack did not run (``scheduler_query_ran``
    False — a severed channel) yields ``[]`` = UNKNOWN, never a settled "zero
    arrays" (the sentinel-ack doctrine: silence is not an empty result).
    """
    cls = _backend_cls(scheduler)
    clean, ran_ok = cls.scheduler_query_ran(stdout)
    if not ran_ok:
        return []
    family = cls.profile.family
    out: list[str] = []
    if family == "slurm":
        for raw in clean.splitlines():
            line = raw.strip()
            if "|" not in line:
                continue
            jid, comment = line.split("|", 1)
            if comment.strip() != token:
                continue
            base = jid.strip().split(".")[0].split("_")[0]
            if base:
                out.append(base)
    elif family == "sge":
        from hpc_agent.infra.jobmap import CORRELATION_KEY_ENV

        current: str | None = None
        for raw in clean.splitlines():
            line = raw.strip()
            if line.startswith("job_number:"):
                current = line.split(":", 1)[1].strip()
            elif line.startswith("context:") and current is not None:
                ctx = line.split(":", 1)[1].strip()
                for kv in ctx.split(","):
                    key, _, val = kv.partition("=")
                    if key.strip() == CORRELATION_KEY_ENV and val.strip() == token:
                        out.append(current)
                current = None
    return out


def count_arrays_under_token(scheduler: str, stdout: str, token: str) -> int:
    """DISTINCT arrays the snapshot tags with *token* (the exactly-one check).

    Collapses ``token_job_ids`` to distinct base ids — one array's many subjob
    rows count once, but two genuinely different arrays under one token count
    twice (the duplicate the submit-once contract forbids)."""
    return len(set(token_job_ids(scheduler, stdout, token)))


def array_present(scheduler: str, stdout: str, token: str) -> bool:
    """True when the scheduler snapshot shows ANY array tagged with *token* —
    the window-opening signal (the dispatch reached the scheduler queue)."""
    return bool(token_job_ids(scheduler, stdout, token))


# Window classification vocabulary (compose in the chain; assert in tests).
WINDOW_NOT_YET = "not_yet"  # no array under the token yet — keep polling
WINDOW_OPEN = "open"  # array live + journal still submitting/job_ids=[] → kill NOW
WINDOW_MISSED = "missed"  # array live but the journal already promoted — too late


def classify_window(
    *,
    scheduler: str,
    stdout: str,
    token: str,
    record_status: str,
    record_job_ids: Sequence[str],
) -> str:
    """The submit-window state machine over a scheduler snapshot + journal record.

    * no array under the token → ``not_yet`` (dispatch hasn't reached the queue);
    * array live AND the journal record is still ``submitting`` with empty
      ``job_ids`` → ``open`` (the durable marker holds the id, the client has not
      promoted — killing now yields the orphan the contract recovers);
    * array live but the record already carries ids / is not ``submitting`` →
      ``missed`` (the promote beat us)."""
    if not array_present(scheduler, stdout, token):
        return WINDOW_NOT_YET
    if record_status == "submitting" and list(record_job_ids) == []:
        return WINDOW_OPEN
    return WINDOW_MISSED


def count_dispatch_commands(commands: Sequence[str]) -> int:
    """Count ``sbatch``/``qsub`` dispatches in an ssh command log — the re-qsub
    counter. Follows ``tests/faultinject/test_submit_once.py``'s ``reqsub_count``:
    a successful drill's recovery issues ZERO of these (adoption is a read)."""
    return sum(("sbatch" in cmd or "qsub " in cmd) for cmd in commands)


def _channel_failure_errors() -> tuple[type[BaseException], ...]:
    """The raise surface of the drill's ``ssh_run``-backed cluster reads.

    ``hpc_agent.infra.remote.ssh_run`` NEVER raises the driver's
    :class:`SandboxRefusal`: it raises :class:`TimeoutError` on a slow/severed
    channel (remote.py converts ``subprocess.TimeoutExpired`` — and the
    engine's post-dispatch non-idempotent failure — into it), raises
    :class:`~hpc_agent.errors.SshCircuitOpen` when the per-host breaker is
    open (consecutive connection failures fail fast, NOT retryable), and can
    surface :class:`OSError` from the transport itself (a missing ssh binary,
    a syscall-layer named-pipe failure); a non-zero scheduler rc is a
    RETURNED CompletedProcess, not a raise. ``_backend_cls`` only raises
    ``SpecInvalid`` for an unknown scheduler name — a config defect the drill
    wants loud, never swallowed into a poll loop. So a bare
    ``except SandboxRefusal`` around these reads is a DEAD clause: a severed
    channel escaped ``main`` as a traceback, contradicting the drill's
    record-and-abort doctrine (every failure is an evidence row + a bounded
    abort). SandboxRefusal stays in the tuple (harmless here, and
    :func:`run_cli_argv` genuinely raises it on the CLI path), but the real
    channel modes are what this set exists to catch.
    """
    from hpc_agent.errors import SshCircuitOpen

    return (SandboxRefusal, SshCircuitOpen, TimeoutError, OSError)


# ── Lease-PID extraction (read the detached worker we must kill) ─────────────


def lease_pid(lease: Mapping[str, Any] | None) -> int | None:
    """The lease's worker pid as a positive int, or None (absent/corrupt/non-int).

    A bool is NOT an int here (``isinstance(True, int)`` is True in Python, which
    would let a stray ``pid: true`` through as pid 1) — rejected explicitly."""
    if not isinstance(lease, Mapping):
        return None
    pid = lease.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    return pid


def lease_host_matches(lease: Mapping[str, Any] | None, hostname: str) -> bool:
    """True when the lease's ``host`` equals *hostname* — the guard that the
    worker is LOCAL (killable by this process), never a pid on another host."""
    if not isinstance(lease, Mapping):
        return False
    host = lease.get("host")
    return isinstance(host, str) and host == hostname


def lease_create_time(lease: Mapping[str, Any] | None) -> float | None:
    """The lease's ``create_time`` as a float, or None (the field is conditional
    in the writer — ``_process_create_time`` returns None on any probe failure)."""
    if not isinstance(lease, Mapping):
        return None
    ct = lease.get("create_time")
    if isinstance(ct, bool) or not isinstance(ct, (int, float)):
        return None
    return float(ct)


def lease_kill_target(lease: Mapping[str, Any] | None, *, hostname: str) -> dict[str, Any] | None:
    """Validate the lease names a killable LOCAL worker; return the kill target.

    Returns ``{"pid", "host", "create_time"}`` only when the pid is a positive
    int AND the lease host matches *hostname* (a remote worker's pid is not
    ours to signal). ``create_time`` rides along for the pid-reuse guard at kill
    time. None when the lease is absent/corrupt or fails either leg — the drill
    records that as evidence, never kills a pid it cannot vouch for."""
    if not isinstance(lease, Mapping):
        return None
    pid = lease_pid(lease)
    if pid is None:
        return None
    if not lease_host_matches(lease, hostname):
        return None
    return {"pid": pid, "host": lease.get("host"), "create_time": lease_create_time(lease)}


# ── Recovery-contract leg assertions (each returns [] == pass) ───────────────


def submitting_state_problems(record: Mapping[str, Any]) -> list[str]:
    """Leg 1 — the drop landed BEFORE the promote: the durable journal record is
    ``submitting`` with empty ``job_ids`` (no id reached the journal)."""
    problems: list[str] = []
    status = record.get("status")
    job_ids = record.get("job_ids")
    if status != "submitting":
        problems.append(f"record status={status!r}, expected 'submitting'")
    if list(job_ids or []) != []:
        problems.append(
            f"record job_ids={job_ids!r}, expected [] (the id never reached the journal)"
        )
    return problems


def marker_wave0_job_id(scheduler: str, marker_stdout: str) -> str | None:
    """The wave-0 job id parsed off the marker's raw scheduler blob via the
    backend ``JOB_ID_REGEX`` (None when the marker is absent, the wave-0 id-file
    is missing, rc != 0, or the blob carries no parseable id)."""
    from hpc_agent.infra.jobmap import parse_jobmap_read, wave_key

    cls = _backend_cls(scheduler)
    parsed = parse_jobmap_read(marker_stdout)
    wave = parsed.waves.get(wave_key(0))
    if wave is None:
        return None
    blob, rc = wave
    if rc != 0:
        return None
    match = cls.JOB_ID_REGEX.search(blob or "")
    return match.group(1) if match else None


def marker_state_problems(
    scheduler: str, marker_stdout: str, run_id: str, attempt: int
) -> list[str]:
    """Leg 2 — the cluster jobmap marker is ``pending`` with a wave-0 id at rc==0.

    Reads the marker the way reconcile does (``parse_jobmap_read`` over the
    ack-gated ``build_read_shell`` stdout): it MUST be present (the positive
    ack fired — a severed read is UNKNOWN, never "no marker"), carry the
    ``<run_id>#<attempt>`` token in state ``pending``, and hold a ``wave-0``
    id-file whose rc==0 and whose blob the backend ``JOB_ID_REGEX`` parses (the
    Δ4 phantom-id gate: rc!=0 is a confirmed failed dispatch, never adopted)."""
    from hpc_agent.infra.jobmap import (
        JOBMAP_STATE_PENDING,
        jobmap_token,
        parse_jobmap_read,
        wave_key,
    )

    cls = _backend_cls(scheduler)
    parsed = parse_jobmap_read(marker_stdout)
    if not parsed.present:
        return ["jobmap marker absent/severed (no __HPC_JOBMAP_ACK__) — dispatch is not durable"]
    problems: list[str] = []
    expected_token = jobmap_token(run_id, attempt)
    if parsed.token != expected_token:
        problems.append(f"marker token={parsed.token!r}, expected {expected_token!r}")
    if parsed.state != JOBMAP_STATE_PENDING:
        problems.append(f"marker state={parsed.state!r}, expected {JOBMAP_STATE_PENDING!r}")
    wkey = wave_key(0)
    wave = parsed.waves.get(wkey)
    if wave is None:
        problems.append(f"marker carries no {wkey} id-file (dispatch never captured an id)")
    else:
        blob, rc = wave
        if rc != 0:
            problems.append(f"{wkey} rc={rc} (nonzero — the Δ4 gate refuses this id)")
        elif not cls.JOB_ID_REGEX.search(blob or ""):
            problems.append(f"{wkey} blob {blob!r} carries no parseable job id")
    return problems


def adopt_brief_problems(brief: Mapping[str, Any], expected_job_id: str | None = None) -> list[str]:
    """Leg 3 — reconcile ADOPTS (asserted FROM THE BRIEF, never internal state).

    The ``reconcile`` envelope must show ``lifecycle_state == 'in_flight'`` with
    ``last_status.verdict_reason == 'submit_once_adopted_from_marker'`` and a
    non-empty ``adopted_job_ids`` (the marker's id). A ``never_dispatched``
    verdict here is a drill FAILURE — it means the marker read missed and a
    re-qsub was authorized."""
    problems: list[str] = []
    lifecycle = brief.get("lifecycle_state")
    if lifecycle != "in_flight":
        problems.append(f"reconcile lifecycle_state={lifecycle!r}, expected 'in_flight' (adoption)")
    last_status = brief.get("last_status")
    last_status = last_status if isinstance(last_status, dict) else {}
    verdict_reason = last_status.get("verdict_reason")
    if verdict_reason != ADOPT_VERDICT_REASON:
        problems.append(
            f"last_status.verdict_reason={verdict_reason!r}, expected "
            f"{ADOPT_VERDICT_REASON!r} (adoption — NOT a safe re-qsub)"
        )
    adopted = last_status.get("adopted_job_ids")
    if not adopted:
        problems.append("last_status.adopted_job_ids is empty — reconcile adopted nothing")
    elif expected_job_id is not None and list(adopted) != [expected_job_id]:
        problems.append(
            f"adopted_job_ids={adopted!r}, expected [{expected_job_id!r}] (the marker's wave-0 id)"
        )
    return problems


def in_flight_state_problems(record: Mapping[str, Any], expected_job_id: str) -> list[str]:
    """Leg 4 — the journal record transitioned to ``in_flight`` carrying the
    marker's id (the same two-write promote reconcile performs on adoption)."""
    problems: list[str] = []
    status = record.get("status")
    job_ids = record.get("job_ids")
    if status != "in_flight":
        problems.append(f"record status={status!r}, expected 'in_flight' after adoption")
    if list(job_ids or []) != [expected_job_id]:
        problems.append(
            f"record job_ids={job_ids!r}, expected [{expected_job_id!r}] (the marker's id)"
        )
    return problems


def exactly_one_array_problems(scheduler: str, stdout: str, token: str) -> list[str]:
    """Leg 5a — EXACTLY ONE array under ``<run_id>#<attempt>`` on the scheduler.

    Zero means the adopted id vanished; two means a duplicate dispatch slipped
    through (the corruption submit-once exists to prevent) — both are failures."""
    count = count_arrays_under_token(scheduler, stdout, token)
    if count != 1:
        return [f"{count} arrays under token {token!r}, expected exactly 1"]
    return []


def harvest_problems(s4_brief: Mapping[str, Any]) -> list[str]:
    """Leg 6 — the adopted array harvested to a non-empty results table."""
    if not s4_brief.get("results_table"):
        return ["S4 brief.results_table is empty — the adopted array did not harvest"]
    return []


# ── Bounded window-miss retry loop (parameter-bump, the live-runsheet shape) ──


@dataclass
class WindowOutcome:
    """One kill attempt's result: ``hit`` (window caught), ``missed`` (the
    promote beat us), or ``error`` (a setup/chain step failed before the window)."""

    kind: str
    detail: str = ""
    run_id: str | None = None


@dataclass
class WindowAttempt:
    """The evidence row for one attempt: its index, the bumped ``n_samples``
    (fresh ``run_id`` per try — the determinism lesson), and the outcome."""

    index: int
    n_samples: int
    kind: str
    detail: str = ""
    run_id: str | None = None


def run_window_attempts(
    drive_one: Callable[[int, int], WindowOutcome],
    *,
    base_n_samples: int,
    max_attempts: int = MAX_KILL_ATTEMPTS,
    bump: int = DEFAULT_N_SAMPLES_BUMP,
) -> tuple[bool, list[WindowAttempt]]:
    """Drive ``drive_one(attempt_index, n_samples)`` until a ``hit`` or exhaustion.

    Each attempt bumps ``n_samples`` by *bump* so it mints a FRESH ``run_id``
    (an identical sweep would dedup against the prior attempt's run — the
    2026-07-18 determinism lesson). Bounded at *max_attempts*: on exhaustion
    returns ``(False, attempts)`` with every attempt's evidence so the caller
    exits non-zero naming each miss. A ``hit`` short-circuits with ``(True, …)``.
    An ``error`` does NOT short-circuit on its own — it is recorded and the next
    attempt proceeds (a transient setup failure shouldn't burn the whole drill),
    but three errors exhaust just like three misses."""
    attempts: list[WindowAttempt] = []
    for index in range(max_attempts):
        n_samples = base_n_samples + index * bump
        outcome = drive_one(index, n_samples)
        attempts.append(
            WindowAttempt(
                index=index,
                n_samples=n_samples,
                kind=outcome.kind,
                detail=outcome.detail,
                run_id=outcome.run_id,
            )
        )
        if outcome.kind == "hit":
            return True, attempts
    return False, attempts


# ────────────────────────────────────────────────────────────────────────────
# Cluster-touching seams (thin — the pure helpers above do the asserting; these
# only fetch state off the container / journal / lease). NOT hermetically tested
# (they need a live scheduler) — the chain wires them; the tests exercise the
# pure surface they feed.
# ────────────────────────────────────────────────────────────────────────────


def run_cli_argv(
    argv: Sequence[str], *, env: Mapping[str, str], timeout_sec: int = _driver._CLI_TIMEOUT_SEC
) -> Any:
    """Invoke an ARG-STYLE verb (``reconcile`` takes ``--run-id``/``--scheduler``,
    NOT a ``--spec`` file) and parse the single-line envelope off stdout. The
    driver's ``run_cli`` is spec-file-only, so arg verbs ride this thin runner —
    same envelope contract (:class:`run_sandbox_proving.CliOutcome`)."""
    full = [sys.executable, "-m", "hpc_agent", *argv]
    verb = argv[0] if argv else "?"
    try:
        proc = subprocess.run(
            full,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            env=dict(env),
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxRefusal(f"{verb}: CLI invocation timed out after {timeout_sec}s") from exc
    outcome = _driver.CliOutcome(
        verb=verb,
        rc=proc.returncode,
        envelope=_driver.parse_envelope(proc.stdout or ""),
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
    if not outcome.ok:
        raise SandboxRefusal(f"{verb}: CLI invocation failed ({outcome.describe_failure()})")
    return outcome


def read_journal_record(experiment_dir: Path, run_id: str) -> dict[str, Any]:
    """The run's durable journal RunRecord as a plain dict ({status, job_ids,
    last_status, attempt}) — the submitting→in_flight transition lives HERE (in
    ``<journal_home>/<repo_hash>/runs/<run_id>.json``), read through the real
    ``state.journal.load_run`` (``HPC_JOURNAL_DIR`` drives the home). Empty dict
    when the record is absent (a leg assertion then fails loudly)."""
    from hpc_agent.state.journal import load_run

    record = load_run(experiment_dir, run_id)
    if record is None:
        return {}
    return {
        "status": record.status,
        "job_ids": list(record.job_ids),
        "last_status": dict(record.last_status or {}),
        "attempt": int(getattr(record, "attempt", 0) or 0),
    }


def query_token_snapshot(ssh_target: str, scheduler: str) -> str:
    """One container-scheduler token query (``squeue … -o '%i|%k'`` on slurm;
    ``qstat -j`` blocks on sge) over the framework's ``ssh_run`` transport,
    returning raw stdout (ack line included — the pure parsers strip it)."""
    from hpc_agent.infra.remote import ssh_run

    cls = _backend_cls(scheduler)
    proc = ssh_run(cls.build_token_query_cmd(), ssh_target=ssh_target)
    return proc.stdout or ""


def read_jobmap_marker(ssh_target: str, remote_path: str, run_id: str) -> str:
    """The cluster jobmap marker read (the SAME ack-gated ``build_read_shell``
    reconcile reads) over ``ssh_run``, returning raw stdout for ``parse_jobmap_read``."""
    from hpc_agent.infra.jobmap import build_read_shell
    from hpc_agent.infra.remote import ssh_run

    proc = ssh_run(build_read_shell(remote_path=remote_path, run_id=run_id), ssh_target=ssh_target)
    return proc.stdout or ""


def kill_worker_pid(pid: int, *, create_time: float | None = None) -> str:
    """Terminate the LOCAL detached worker *pid*; return a one-line detail.

    The framework has NO local-pid killer (its lease machinery is
    detection/reclaim-only — ``doctor`` drafts a re-invoke proposal, never
    signals), so the drill signals the worker itself, guarded against pid reuse:
    when the lease carried a ``create_time``, the live process's create_time must
    match within 1s (psutil's tolerance) or the kill is REFUSED (the pid was
    recycled to an innocent process). TERM → 10s grace → KILL."""
    import psutil

    from hpc_agent.infra.proc import pid_alive

    if not pid_alive(pid):
        return f"pid {pid} already dead (the worker exited before the kill)"
    if create_time is not None:
        try:
            proc = psutil.Process(pid)
            if abs(proc.create_time() - create_time) > 1.0:
                return f"pid {pid} create_time mismatch (pid reuse) — kill REFUSED"
        except psutil.Error:
            return f"pid {pid} vanished during the create_time check"
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except psutil.TimeoutExpired:
            proc.kill()
        return f"killed pid {pid} (the detached S3 dispatch worker)"
    except psutil.Error as exc:
        return f"kill of pid {pid} failed: {exc}"


def jittered_poll_interval(
    base_sec: float = WINDOW_POLL_INTERVAL_SEC,
    *,
    jitter_frac: float = WINDOW_POLL_JITTER_FRAC,
    rand: Callable[[], float] = random.random,
) -> float:
    """One poll sleep drawn uniformly from ``base ± base*jitter_frac``.

    The poll loop sleeps AFTER each query, so the effective period is (sleep +
    ssh RTT): a fixed sleep keeps that period constant and phase-lockable
    against the pipeline's ~10.6s spawn→submit cadence (the 2026-07-19 3/3
    miss parked every attempt's sub-second squeue window in the same
    inter-poll gap). A fresh uniform draw per sleep makes successive periods
    differ, so the poll phase random-walks off any fixed cadence. *rand* is
    injectable so tests can pin the draw (0.0 → the floor, 1.0 → the ceiling).
    """
    span = base_sec * jitter_frac
    return base_sec + (rand() * 2.0 - 1.0) * span


def wait_for_token(
    ctx: Any,
    *,
    run_id: str,
    token: str,
    budget_sec: int = WINDOW_POLL_BUDGET_SEC,
    interval_sec: float = WINDOW_POLL_INTERVAL_SEC,
) -> str | None:
    """Poll the container scheduler until an array tagged *token* appears (the
    window opening) or the budget lapses. Returns the winning snapshot, or None
    on timeout — which is NOT proof the array never entered (sacct-disabled ⇒
    a fast array can complete inside one poll gap): the caller arbitrates via
    the dispatch witnesses (:func:`classify_unseen`)."""
    deadline = time.time() + budget_sec
    snapshot = ""
    channel_failures = _channel_failure_errors()
    while time.time() < deadline:
        try:
            snapshot = query_token_snapshot(ctx.ssh_target, ctx.backend)
        except channel_failures:
            # A slow/severed channel is UNKNOWN, never a traceback escaping the
            # drill: fold it into an empty snapshot and keep polling inside the
            # SAME budget (retry/backoff semantics unchanged). Exhaustion
            # returns None and the caller records the error row.
            snapshot = ""
        if array_present(ctx.backend, snapshot, token):
            return snapshot
        time.sleep(jittered_poll_interval(interval_sec))
    return snapshot if array_present(ctx.backend, snapshot, token) else None


# ── The dispatched-unseen witnesses (poll silence is NOT dispatch absence) ────
#
# When wait_for_token lapses, the OLD error path reported "never entered the
# scheduler" — reading poll silence as dispatch absence, which violated the
# drill's own sentinel-ack doctrine (run 29709733724: all three arrays entered
# AND completed; the poll just never landed inside their ≤1.4s squeue
# lifetimes). The honest arbitration uses TWO witnesses, cheapest first:
#
# 1. LOCAL — the journal record's ``job_ids``: a promote proves the dispatch
#    happened (the id reached the client), no ssh needed;
# 2. CLUSTER — ONE ssh read of the jobmap marker + the run's results dir: the
#    marker is written server-side BEFORE the client sees the id, so it
#    survives even a dead worker.
#
# Only a cluster read whose ack FIRED with no marker, no wave-0 id, and no
# results settles to "never dispatched".

_PROBE_RESULTS_LINE = "__HPC_PROBE_RESULTS__"

# UnseenProbe.kind vocabulary.
PROBE_DISPATCHED = "dispatched"  # a witness proves the array entered the scheduler
PROBE_NEVER_DISPATCHED = "never_dispatched"  # ack fired; marker + results genuinely absent
PROBE_UNKNOWN = "unknown"  # severed/unreadable probe — settles NOTHING


def build_unseen_probe_shell(*, remote_path: str, run_id: str) -> str:
    """ONE ack-gated ssh round-trip reading BOTH cluster-side dispatch
    witnesses: the jobmap marker + wave id-files (the SAME ``build_read_shell``
    read reconcile performs) plus the run's ``results/<run_id>/`` task count.

    The jobmap ack discipline is inherited verbatim: no ``__HPC_JOBMAP_ACK__``
    ⇒ the read is UNKNOWN (severed channel / truncated), never a settled
    "absent". The results leg rides after the jobmap read's trailing
    ``; true`` so a missing results dir never masks the marker half, and emits
    ``__HPC_PROBE_RESULTS__ <n>`` (0+ ⇒ the array demonstrably RAN) or
    ``absent``.
    """
    from hpc_agent.infra.jobmap import build_read_shell

    results_dir = f"{remote_path.rstrip('/')}/results/{run_id}"
    return (
        build_read_shell(remote_path=remote_path, run_id=run_id)
        + f"; if [ -d {shlex.quote(results_dir)} ]; then "
        f"printf '%s %s\\n' {shlex.quote(_PROBE_RESULTS_LINE)} "
        f'"$(ls -1 {shlex.quote(results_dir)} 2>/dev/null | wc -l)"; '
        f"else printf '%s %s\\n' {shlex.quote(_PROBE_RESULTS_LINE)} absent; fi; true"
    )


def probe_dispatch_evidence(ssh_target: str, remote_path: str, run_id: str) -> str:
    """The cluster witness: run :func:`build_unseen_probe_shell` over the
    framework's ``ssh_run`` transport, returning raw stdout. Raises the
    ``_channel_failure_errors()`` surface on a severed channel (the caller
    folds it into UNKNOWN, never a settled absence)."""
    from hpc_agent.infra.remote import ssh_run

    proc = ssh_run(
        build_unseen_probe_shell(remote_path=remote_path, run_id=run_id),
        ssh_target=ssh_target,
    )
    return proc.stdout or ""


@dataclass(frozen=True)
class UnseenProbe:
    """The cluster witness's answer to "the poll never saw the array — was it
    ever dispatched?" ``job_id`` is the wave-0 id parsed off the marker (None
    when the id-file is absent/rc!=0/unparseable); ``results_tasks`` is the
    per-task result-dir count (None when the results dir is absent)."""

    kind: str
    job_id: str | None = None
    results_tasks: int | None = None


def classify_unseen_probe(scheduler: str, probe_stdout: str) -> UnseenProbe:
    """Classify :func:`build_unseen_probe_shell` stdout under the sentinel-ack
    doctrine. No jobmap ack ⇒ UNKNOWN (a severed read settles NOTHING). An ack
    that FIRED with neither a parseable wave-0 id nor any task results ⇒
    genuinely NEVER_DISPATCHED. Anything else (an id at rc==0, or results on
    disk) ⇒ DISPATCHED — the poll missed an array that was really there.
    """
    from hpc_agent.infra.jobmap import parse_jobmap_read

    if not parse_jobmap_read(probe_stdout).present:
        return UnseenProbe(kind=PROBE_UNKNOWN)
    results_tasks: int | None = None
    for raw in (probe_stdout or "").splitlines():
        line = raw.strip()
        if line.startswith(_PROBE_RESULTS_LINE):
            tail = line[len(_PROBE_RESULTS_LINE) :].strip()
            if tail.isdigit():
                results_tasks = int(tail)
    job_id = marker_wave0_job_id(scheduler, probe_stdout)
    if job_id is None and not results_tasks:
        return UnseenProbe(kind=PROBE_NEVER_DISPATCHED, results_tasks=results_tasks)
    return UnseenProbe(kind=PROBE_DISPATCHED, job_id=job_id, results_tasks=results_tasks)


def classify_unseen(
    ctx: Any,
    *,
    experiment_dir: Path,
    remote_path: str,
    run_id: str,
    token: str,
) -> WindowOutcome:
    """Arbitrate a lapsed poll budget: was the array ever dispatched?

    Witness 1 is LOCAL (the journal record's job_ids — a promote proves the
    dispatch reached the client); witness 2 is the ONE-ssh cluster probe
    (marker + results). The outcome is DISPATCHED_UNSEEN whenever a witness
    proves the dispatch, an honest ``error`` naming the genuine
    never-dispatched only when the cluster ack fired on an empty marker +
    empty results, and an UNKNOWN ``error`` when the probe itself was
    unreadable — the old "never entered the scheduler" misread is gone.
    """
    record = read_journal_record(experiment_dir, run_id)
    raw_ids = record.get("job_ids") or []
    job_ids = [str(j) for j in raw_ids]
    if job_ids:
        return WindowOutcome(
            kind=DISPATCHED_UNSEEN,
            detail=(
                f"array tagged {token!r} dispatched (the journal record carries "
                f"job_ids={job_ids!r}) but never seen in the poll window — it "
                "entered and left squeue between polls (sacct disabled ⇒ a "
                "completed array vanishes instantly)"
            ),
            run_id=run_id,
        )
    try:
        probe_stdout = probe_dispatch_evidence(ctx.ssh_target, remote_path, run_id)
    except _channel_failure_errors() as exc:
        return WindowOutcome(
            kind="error",
            detail=(
                f"array tagged {token!r} unseen within the poll budget and the "
                f"cluster-side dispatch probe was unreadable ({exc}) — UNKNOWN "
                "whether it ever entered; the dispatch question stays UNSETTLED"
            ),
            run_id=run_id,
        )
    probe = classify_unseen_probe(ctx.backend, probe_stdout)
    if probe.kind == PROBE_DISPATCHED:
        completion = (
            f"completed ({probe.results_tasks} task results on disk)"
            if probe.results_tasks
            else "completion unproven (no task results yet)"
        )
        id_note = f"jobmap wave-0 id={probe.job_id}" if probe.job_id else "no parseable wave-0 id"
        return WindowOutcome(
            kind=DISPATCHED_UNSEEN,
            detail=(
                f"array tagged {token!r} dispatched + {completion} but never "
                f"seen in the poll window ({id_note}) — it entered, ran, and "
                "exited inside a poll gap (sacct disabled ⇒ a completed array "
                "vanishes from squeue instantly)"
            ),
            run_id=run_id,
        )
    if probe.kind == PROBE_UNKNOWN:
        return WindowOutcome(
            kind="error",
            detail=(
                f"array tagged {token!r} unseen within the poll budget and the "
                "dispatch probe returned no ack — UNKNOWN whether it ever "
                "entered; the dispatch question stays UNSETTLED"
            ),
            run_id=run_id,
        )
    return WindowOutcome(
        kind="error",
        detail=(
            f"array tagged {token!r} never dispatched: the cluster-side probe "
            "fired its ack but found no jobmap marker, no wave-0 id, and no "
            f"results under {remote_path} — a genuine dispatch failure"
        ),
        run_id=run_id,
    )


# ── The kill dance for ONE attempt (fixture → S3 launch → kill → classify) ───


def _drive_chain_to_s3_launch(
    state: Any, ctx: Any, *, n_samples: int
) -> tuple[str, str, str] | None:
    """Drive a fresh fixture run through the S3 detached LAUNCH (no wait), the
    way the U3 driver drives steps 3-7 but stopping the instant the S3 worker is
    spawned. Returns ``(run_id, experiment_dir, remote_path)`` or None after
    recording a failing evidence row (chain aborts). Reuses the driver's helpers
    verbatim — the greenlights fire through the ONE fused append-decision."""
    drv = _driver
    run_ref = f"{ctx.run_ref}-a{n_samples}"

    # Fixture build (fresh n_samples → fresh run_id).
    try:
        handle = drv.build_fixture_experiment(
            ctx.workdir / f"experiment-{n_samples}",
            {"n_samples": n_samples},
            run_ref,
            run_name=ctx.run_name,
            cluster=ctx.cluster,
            goal=ctx.goal,
        )
    except SandboxRefusal as exc:
        state.record("fixture", "sandbox_fixture", "fixture experiment builds", False, str(exc))
        return None
    experiment_dir = Path(str(drv.fixture_handle_value(handle, "experiment_dir"))).resolve()
    ctx.experiment_dir = experiment_dir
    run_name = str(drv.fixture_handle_value(handle, "run_name") or ctx.run_name)

    # Read the interview outputs (canonical on-disk contract).
    try:
        interview = drv.read_json(experiment_dir / "interview.json")
        entry_point = interview.get("entry_point") or {}
        executor_run_name = str(entry_point.get("run_name") or "run")
        materialized = interview.get("_materialized") or {}
        executor_cmd = str((materialized.get("entry_point") or {}).get("executor_cmd") or "")
        task_generator = interview.get("task_generator") or {}
        total_tasks = int(materialized.get("total_tasks") or interview.get("task_count") or 0)
        profile = str((interview.get("cluster_target") or {}).get("profile") or "cpu")
        if not executor_cmd or not task_generator or total_tasks < 1:
            raise SandboxRefusal(
                "interview.json carries no usable executor_cmd/task_generator/task_count"
            )
    except (SandboxRefusal, json.JSONDecodeError, OSError) as exc:
        state.record("interview", "interview.json", "interview outputs readable", False, str(exc))
        return None

    # Seed the authorship utterance into the ephemeral namespace (U2 sibling).
    utterance = drv.build_utterance_text(ctx.goal, task_generator)
    try:
        drv.seed_authorship_utterance(ctx.journal_home, experiment_dir, utterance, run_ref=run_ref)
    except SandboxRefusal as exc:
        state.record("seed", "sandbox_seed", "authorship utterance seeded", False, str(exc))
        return None

    remote_path = drv.stanza_remote_path(ctx.remote_path_stanza, experiment_dir)

    # S1 walk+resolve → run_id minted.
    recorded = drv.compute_recorded_resolutions(experiment_dir, executor_run_name)
    walk = drv.build_walk_spec(
        cluster=ctx.cluster,
        configured_clusters=ctx.configured_clusters,
        goal=ctx.goal,
        task_generator=task_generator,
        profile=profile,
        executor_run_name=executor_run_name,
        walltime_sec=ctx.walltime_sec,
        experiment_dir=experiment_dir,
        recorded=recorded,
    )
    resolve = drv.build_resolve_spec(
        run_name=run_name,
        profile=profile,
        cluster=ctx.cluster,
        ssh_target=ctx.ssh_target,
        remote_path=remote_path,
        backend=ctx.backend,
        total_tasks=total_tasks,
        executor_cmd=executor_cmd,
        walltime_sec=ctx.walltime_sec,
    )
    outcome = drv._step_cli(
        state,
        ctx,
        step="s1.resolve",
        verb="submit-s1",
        spec={"walk": walk, "run_preflight": ctx.run_preflight, "resolve": resolve},
        experiment_dir=experiment_dir,
    )
    if outcome is None:
        return None
    s1_data = outcome.data
    brief = drv._dict_or_empty(s1_data.get("brief"))
    resolve_brief = drv._dict_or_empty(brief.get("resolve"))
    run_id = s1_data.get("run_id") or resolve_brief.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        state.record("s1.resolve", "submit-s1 brief", "run_id minted", False, f"run_id={run_id!r}")
        return None
    ctx.run_id = run_id

    # S1 greenlight (brief-shaped resolved — the provenance gate fires for real).
    greenlight_resolved = drv.build_s1_greenlight_resolved(brief)
    if drv.provenance_shape_problems(greenlight_resolved, brief):
        state.record(
            "s1.greenlight",
            "driver self-check",
            "greenlight brief-shaped",
            False,
            "not brief-shaped",
        )
        return None
    if not drv._commit_greenlight(
        state,
        ctx,
        step="s1.greenlight",
        run_id=run_id,
        block="submit-s1",
        resolved=greenlight_resolved,
        next_block="submit-s2",
        proposal=f"S1 resolved: {run_id} on {ctx.cluster}; sandbox greenlight; stage+canary next",
        evidence_digest={"resolved": brief.get("resolved")},
    ):
        return None

    # S2 stage+canary (detached) → canary verified.
    s2_spec = drv.compose_s2_spec(brief)
    launched = drv._launch_block_detached(
        state, ctx, step="s2.stage", verb="submit-s2", run_id=run_id, spec=s2_spec
    )
    if launched is None:
        return None
    s2_result = drv.wait_for_detached_terminal(
        state, ctx, step="s2.canary", verb="submit-s2", run_id=run_id
    )
    if s2_result is None or s2_result.get("stage_reached") != "canary_verified":
        stage = s2_result.get("stage_reached") if s2_result else None
        state.record(
            "s2.canary", "submit-s2 terminal", "canary verified", False, f"stage_reached={stage!r}"
        )
        return None
    s2_brief = drv._dict_or_empty(s2_result.get("brief"))

    # S2 greenlight (thin next_block) — its advance leg consumes the parked R3
    # spec and launches S3 itself (observed below), exactly as in the U3 chain.
    if not drv._commit_greenlight(
        state,
        ctx,
        step="s2.greenlight",
        run_id=run_id,
        block="submit-s2",
        resolved={"next_block": "submit-s3"},
        next_block="submit-s3",
        proposal="canary green; submit the main array under HPC_SUBMIT_ONCE=1 and watch",
        evidence_digest=s2_brief,
    ):
        return None

    # S3 submit (detached) — launch only, NO wait. The fused S2 tick may already
    # have launched it (R3 consume); _launch_block_detached observes that case.
    materialized_s3 = drv.materialized_successor_path(experiment_dir, run_id, "submit-s3")
    if materialized_s3.is_file():
        s3_spec = drv.read_json(materialized_s3)
        if "detach" not in s3_spec:
            s3_spec = {**s3_spec, "detach": True}
    else:
        s3_spec = drv.compose_s3_spec(s2_spec, run_id, s2_brief)
    launched = drv._launch_block_detached(
        state, ctx, step="s3.submit", verb="submit-s3", run_id=run_id, spec=s3_spec
    )
    if launched is None:
        return None
    return run_id, str(experiment_dir), remote_path


def _drive_one_attempt(
    state: Any, ctx: Any, *, n_samples: int, attempt_index: int, hit: dict[str, Any]
) -> WindowOutcome:
    """One bounded kill attempt: drive a fresh run to the S3 launch, read the
    detached lease pid, wait for the array to enter the scheduler (the window
    opening), kill the worker IN the window, and classify. On ``open`` it stashes
    the recovery inputs on *hit* and returns ``hit``; a promoted-already record
    is ``missed``; a setup/chain failure is ``error``."""
    drv = _driver
    launched = _drive_chain_to_s3_launch(state, ctx, n_samples=n_samples)
    if launched is None:
        return WindowOutcome(kind="error", detail="chain did not reach the S3 launch")
    run_id, experiment_dir_str, remote_path = launched
    experiment_dir = Path(experiment_dir_str)

    # (a) read the detached S3 lease PID.
    lease = drv.read_detached_lease(ctx.journal_home, run_id, "submit-s3")
    target = lease_kill_target(lease, hostname=socket.gethostname())
    if target is None:
        lease_note = "absent" if lease is None else "host/pid mismatch"
        return WindowOutcome(
            kind="error", detail=f"no killable S3 lease pid (lease {lease_note})", run_id=run_id
        )

    # (b) poll the container scheduler for the array tagged <run_id>#0.
    from hpc_agent.infra.jobmap import jobmap_token

    attempt_no = read_journal_record(experiment_dir, run_id).get("attempt", 0) or 0
    token = jobmap_token(run_id, int(attempt_no))
    snapshot = wait_for_token(ctx, run_id=run_id, token=token)
    if snapshot is None:
        # The poll never saw the array — which is NOT proof it never entered
        # (sacct disabled ⇒ a fast array completes inside one poll gap; run
        # 29709733724 misreported exactly that as "never entered"). Arbitrate
        # via the dispatch witnesses, cheapest first.
        return classify_unseen(
            ctx,
            experiment_dir=experiment_dir,
            remote_path=remote_path,
            run_id=run_id,
            token=token,
        )

    # (c) kill the LOCAL detached dispatch process inside the submit-window.
    kill_detail = kill_worker_pid(int(target["pid"]), create_time=target["create_time"])
    time.sleep(KILL_SETTLE_SEC)

    # Classify the window over the scheduler snapshot + the journal record.
    record = read_journal_record(experiment_dir, run_id)
    kind = classify_window(
        scheduler=ctx.backend,
        stdout=snapshot,
        token=token,
        record_status=str(record.get("status", "")),
        record_job_ids=list(record.get("job_ids") or []),
    )
    if kind == WINDOW_OPEN:
        hit.update(
            run_id=run_id,
            experiment_dir=experiment_dir,
            remote_path=remote_path,
            token=token,
            attempt=int(attempt_no),
        )
        return WindowOutcome(kind="hit", detail=f"window open; {kill_detail}", run_id=run_id)
    detail = (
        f"{kill_detail}; window={kind} (record status={record.get('status')!r}, "
        f"job_ids={record.get('job_ids')!r})"
    )
    return WindowOutcome(
        kind="missed" if kind == WINDOW_MISSED else "error", detail=detail, run_id=run_id
    )


# ── The recovery-contract legs (run once, after a window hit) ────────────────


def _recovery_legs(state: Any, ctx: Any, hit: Mapping[str, Any]) -> None:
    """Assert the full recovery contract, each leg a named evidence row. Runs
    ONLY after a window hit — the run is an orphan the submit-once contract must
    adopt with zero re-qsub, then harvest to terminal."""
    drv = _driver
    run_id = str(hit["run_id"])
    experiment_dir = Path(str(hit["experiment_dir"]))
    remote_path = str(hit["remote_path"])
    token = str(hit["token"])
    attempt = int(hit["attempt"])

    # Leg 1 — sidecar submitting / job_ids=[] (the drop landed before the promote).
    pre_record = read_journal_record(experiment_dir, run_id)
    drv._assert_step(
        state,
        step="recover.submitting",
        where="journal record",
        check="pre-reconcile record is submitting with job_ids=[] (id never reached the journal)",
        problems=submitting_state_problems(pre_record),
    )

    # Leg 2 — cluster jobmap marker pending, wave-0 id at rc==0.
    try:
        marker_stdout = read_jobmap_marker(ctx.ssh_target, remote_path, run_id)
    except _channel_failure_errors() as exc:
        # The ssh_run-backed read raises TimeoutError/SshCircuitOpen/OSError on
        # a slow/severed channel (NEVER SandboxRefusal — a bare refusal clause
        # here was dead): record the SAME failing evidence row + bounded abort,
        # never a traceback escaping main.
        marker_stdout = ""
        state.record("recover.marker", "cluster jobmap", "jobmap marker readable", False, str(exc))
    marker_id = marker_wave0_job_id(ctx.backend, marker_stdout)
    drv._assert_step(
        state,
        step="recover.marker",
        where="cluster jobmap",
        check="jobmap marker pending with a wave-0 id at rc==0",
        problems=marker_state_problems(ctx.backend, marker_stdout, run_id, attempt),
    )

    # Leg 3 — reconcile ADOPTS (asserted from the brief, never internal state).
    try:
        reconcile_brief = run_cli_argv(
            [
                "reconcile",
                "--run-id",
                run_id,
                "--scheduler",
                ctx.backend,
                "--experiment-dir",
                str(experiment_dir),
            ],
            env=ctx.env,
        ).data
    except SandboxRefusal as exc:
        reconcile_brief = {}
        state.record("recover.adopt", "reconcile CLI", "reconcile invocation ok", False, str(exc))
    drv._assert_step(
        state,
        step="recover.adopt",
        where="reconcile brief",
        check="reconcile ADOPTS (in_flight + adopted_from_marker, NOT a re-qsub)",
        problems=adopt_brief_problems(reconcile_brief, marker_id),
    )

    # Leg 4 — journal in_flight carrying the marker's id.
    post_record = read_journal_record(experiment_dir, run_id)
    adopted = (reconcile_brief.get("last_status") or {}).get("adopted_job_ids") or []
    expected_id = marker_id or (str(adopted[0]) if adopted else "")
    leg4_problems = (
        in_flight_state_problems(post_record, expected_id)
        if expected_id
        else ["no marker/adopted id to assert the in_flight job_ids against"]
    )
    drv._assert_step(
        state,
        step="recover.in_flight",
        where="journal record",
        check="post-reconcile record is in_flight carrying the marker's id",
        problems=leg4_problems,
    )

    # Leg 5 — EXACTLY ONE array under the token, ZERO re-qsub.
    try:
        post_snapshot = query_token_snapshot(ctx.ssh_target, ctx.backend)
    except _channel_failure_errors() as exc:
        # Same live-failure contract as leg 2: a severed channel lands as a
        # recorded failing evidence row, never a traceback escaping the drill.
        post_snapshot = ""
        state.record("recover.one-array", "container scheduler", "token query ok", False, str(exc))
    one_array = exactly_one_array_problems(ctx.backend, post_snapshot, token)
    verdict_reason = (reconcile_brief.get("last_status") or {}).get("verdict_reason")
    if verdict_reason != ADOPT_VERDICT_REASON:
        one_array = [
            *one_array,
            f"verdict_reason={verdict_reason!r} — adoption (zero re-qsub) not proven",
        ]
    drv._assert_step(
        state,
        step="recover.one-array",
        where="container scheduler",
        check="exactly one array under the token, zero re-qsub (adoption is a read)",
        problems=one_array,
    )

    # Leg 6 — the adopted array harvests to terminal (monitor → S4 results table).
    watch_spec = {"monitor": {"run_id": run_id}, "detach": False}
    watch_outcome = drv._step_cli(
        state,
        ctx,
        step="recover.watch",
        verb="status-watch",
        spec=watch_spec,
        experiment_dir=experiment_dir,
        timeout_sec=ctx.wait_timeout + 120,
    )
    if watch_outcome is not None:
        watch_brief = drv._dict_or_empty(watch_outcome.data.get("brief"))
        lifecycle = watch_brief.get("lifecycle_state")
        drv._assert_step(
            state,
            step="recover.watch",
            where="status-watch brief",
            check="adopted array monitored to terminal (lifecycle complete)",
            problems=[]
            if lifecycle == "complete"
            else [f"lifecycle_state={lifecycle!r}, expected 'complete'"],
        )
    s4_spec = drv.compose_s4_spec(run_id)
    launched = drv._launch_block_detached(
        state, ctx, step="recover.harvest", verb="submit-s4", run_id=run_id, spec=s4_spec
    )
    if launched is None:
        return
    s4_result = drv.wait_for_detached_terminal(
        state, ctx, step="recover.table", verb="submit-s4", run_id=run_id
    )
    if s4_result is None:
        return
    drv._assert_step(
        state,
        step="recover.table",
        where="submit-s4 terminal",
        check="adopted array harvested; results table non-empty",
        problems=harvest_problems(drv._dict_or_empty(s4_result.get("brief"))),
    )


# ── The drill entry (retry loop over attempts, then the recovery legs) ───────


def run_kill_drill(ctx: Any, *, base_n_samples: int, max_attempts: int = MAX_KILL_ATTEMPTS) -> Any:
    """Drive the kill drill: bounded window attempts (each a fresh fixture run),
    then — on a hit — the full recovery contract. Record-and-abort semantics ride
    the driver's :class:`ChainState`; the retry loop's per-attempt evidence lands
    whether or not the window is ever caught."""
    drv = _driver
    state = drv.ChainState()
    hit: dict[str, Any] = {}

    def drive_one(index: int, n_samples: int) -> WindowOutcome:
        return _drive_one_attempt(state, ctx, n_samples=n_samples, attempt_index=index, hit=hit)

    caught, attempts = run_window_attempts(
        drive_one, base_n_samples=base_n_samples, max_attempts=max_attempts
    )
    for attempt in attempts:
        state.record(
            f"attempt-{attempt.index}",
            "kill-window",
            f"attempt {attempt.index} (n_samples={attempt.n_samples}, "
            f"run={attempt.run_id or '?'}) caught the submit-window",
            attempt.kind == "hit",
            f"{attempt.kind}: {attempt.detail}",
        )
    if not caught:
        state.record(
            "kill-window",
            "kill drill",
            f"submit-window caught within {max_attempts} attempts",
            False,
            "exhausted: " + "; ".join(f"attempt {a.index}={a.kind}" for a in attempts),
        )
        return state
    _recovery_legs(state, ctx, hit)
    return state


# ── CLI entry (guard-first refusal order mirrors the U3 driver) ──────────────


def default_drill_workdir(env: Mapping[str, str]) -> Path:
    """The drill's default workdir (specs + evidence root).

    The evidence-upload contract with the CI lane: on GitHub Actions
    (``RUNNER_TEMP`` set) the default is ``$RUNNER_TEMP/sandbox-evidence/
    kill-drill/`` — exactly the path the workflow's upload-artifact step
    points at (previously the drill dropped evidence into an unuploaded
    mkdtemp, so a failing drill left no artifact). Off Actions, keep the
    mkdtemp behavior. An explicit ``--workdir`` always wins over this default.
    """
    runner_temp = (env.get("RUNNER_TEMP") or "").strip()
    if runner_temp:
        return Path(runner_temp) / "sandbox-evidence" / "kill-drill"
    return Path(tempfile.mkdtemp(prefix="hpc-killdrill-"))


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sandbox_kill_drill.py",
        description=(
            "U4 sandbox kill drill (rung-2 proving): sever the detached S3 "
            "dispatch process inside the submit-once window against the "
            "container cluster and assert the full recovery contract "
            "(submitting → marker pending → reconcile adopts → in_flight → "
            "exactly-one array / zero re-qsub → harvest)."
        ),
    )
    parser.add_argument(
        "--clusters-config",
        type=Path,
        default=None,
        help="path to a ci_clusters.yaml (the container lane generates one).",
    )
    parser.add_argument(
        "--cluster",
        default=None,
        help="cluster name inside the config (required when it names several).",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="stand the ci/slurm container up itself (docker required; on "
        "dockerless hosts this errors with the U7 dispatch guidance).",
    )
    parser.add_argument(
        "--keep-container",
        action="store_true",
        help="--local: leave the slurmci container running after the drill.",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="scratch root for specs/evidence/fixtures (default: a fresh tmpdir).",
    )
    parser.add_argument(
        "--base-n-samples",
        type=int,
        default=_driver.DEFAULT_FIXTURE_N_SAMPLES,
        help=(
            "n_samples for the first attempt; each retry bumps it (fresh "
            "run_id). Default: the driver's fixture-walltime band (~5–10s per "
            "task on the container) so the array's squeue-visibility window "
            "clears the sub-second jittered poll."
        ),
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=MAX_KILL_ATTEMPTS,
        help="bounded window-miss retries (the live-runsheet shape).",
    )
    parser.add_argument("--run-name", default=DEFAULT_RUN_NAME, help="run name for compute-run-id.")
    parser.add_argument("--goal", default=DEFAULT_GOAL, help="the (seeded) human goal text.")
    parser.add_argument(
        "--walltime-sec", type=int, default=DEFAULT_WALLTIME_SEC, help="per-task walltime ask."
    )
    parser.add_argument(
        "--wait-timeout", type=int, default=3600, help="per-block detached-wait budget (seconds)."
    )
    parser.add_argument(
        "--poll-interval", type=int, default=5, help="wait-detached poll interval (seconds)."
    )
    parser.add_argument(
        "--no-preflight", action="store_true", help="pass run_preflight=false to S1 (debugging)."
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="evidence JSON path (default: <workdir>/evidence.json).",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="evidence markdown path (default: <workdir>/evidence.md).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    drv = _driver
    args = _parse_args(argv)
    started = time.time()
    started_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
    run_ref = f"killdrill-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(started))}-{os.getpid()}"

    # The §3 guard fires before ANY work — reusing the driver's (no fourth copy).
    try:
        journal_home = require_ephemeral_journal_home(os.environ)
    except SandboxRefusal as exc:
        print(f"sandbox-kill-drill: REFUSED — {exc}", file=sys.stderr)
        return 2
    os.environ["HPC_JOURNAL_DIR"] = str(journal_home)

    if args.base_n_samples < 1:
        print("sandbox-kill-drill: --base-n-samples must be >= 1", file=sys.stderr)
        return 2
    if args.max_attempts < 1:
        print("sandbox-kill-drill: --max-attempts must be >= 1", file=sys.stderr)
        return 2

    workdir = (args.workdir or default_drill_workdir(os.environ)).resolve()
    scratch = workdir / "specs"
    scratch.mkdir(parents=True, exist_ok=True)
    out_path = args.out or (workdir / "evidence.json")
    md_path = args.markdown or (workdir / "evidence.md")

    env = dict(os.environ)
    env["HPC_JOURNAL_DIR"] = str(journal_home)
    env["HPC_SUBMIT_ONCE"] = "1"
    env["HPC_STATUS_POLL_INTERVAL_SEC"] = drv._ENV_POLL_INTERVAL

    ctx: Any = None
    local_container = False
    state: Any = None
    try:
        if args.local:
            clusters_path, shim_env = drv.ensure_local_cluster(
                workdir, keep_container=args.keep_container
            )
            env.update(shim_env)
            local_container = True
        elif args.clusters_config is not None:
            clusters_path = args.clusters_config
        else:
            print(
                "sandbox-kill-drill: pass --clusters-config <ci_clusters.yaml> or --local",
                file=sys.stderr,
            )
            return 2
        env["HPC_CLUSTERS_CONFIG"] = str(clusters_path)
        config = drv.load_cluster_config(clusters_path)
        cluster_name, stanza = drv.select_cluster(config, args.cluster)

        ctx = drv.ChainContext(
            env=env,
            journal_home=journal_home,
            workdir=workdir,
            scratch=scratch,
            experiment_dir=None,
            cluster=cluster_name,
            configured_clusters=sorted(config),
            ssh_target=drv.stanza_ssh_target(stanza),
            backend=drv.stanza_backend(stanza),
            remote_path_stanza=stanza,
            goal=args.goal,
            run_name=args.run_name,
            run_ref=run_ref,
            wait_timeout=args.wait_timeout,
            poll_interval=args.poll_interval,
            run_preflight=not args.no_preflight,
            walltime_sec=args.walltime_sec,
        )
        state = run_kill_drill(
            ctx, base_n_samples=args.base_n_samples, max_attempts=args.max_attempts
        )
    except SandboxRefusal as exc:
        state = drv.ChainState()
        state.record("setup", "driver", "drill setup", False, str(exc))
    finally:
        if local_container and not args.keep_container:
            drv.teardown_local_container()

    meta = {
        "run_ref": run_ref,
        "run_id": ctx.run_id if ctx is not None else None,
        "cluster": ctx.cluster if ctx is not None else None,
        "base_n_samples": args.base_n_samples,
        "max_attempts": args.max_attempts,
        "submit_once": env.get("HPC_SUBMIT_ONCE"),
        "journal_home": str(journal_home),
        "started_utc": started_utc,
        "duration_sec": round(time.time() - started, 1),
        "driver": "scripts/sandbox_kill_drill.py (U4)",
        "jurisdiction": (
            "rung-2: submit-once recovery contract only — never cluster-environment truth"
        ),
    }
    evidence = drv.build_evidence(meta, state.rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    md_path.write_text(drv.render_markdown(evidence), encoding="utf-8")

    width = max((len(r["step"]) for r in state.rows), default=0)
    for row in state.rows:
        mark = "PASS" if row["pass"] else "FAIL"
        detail = f"  — {row['detail']}" if row["detail"] and not row["pass"] else ""
        print(f"[{mark}] {row['step']:<{width}}  {row['check']}{detail}")
    print(f"\nevidence: {out_path}\nmarkdown: {md_path}")
    print(f"sandbox-kill-drill: verdict {evidence['verdict'].upper()}")
    return 0 if evidence["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
