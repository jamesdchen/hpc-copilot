"""Detached submit-block workers — run a cluster-bound block to terminal in a
DETACHED ``hpc-agent`` subprocess, never a ``claude -p`` worker.

The connection-storm hazard (the 0.10.63 ban) was *an LLM in the connection
loop*: a worker was spawned to **drive** a wait-until-terminal SSH poll, it
auto-backgrounded mid-poll, and a fallback inline subagent then retried SSH in
prose for ~21 minutes. The fix is to carry the deterministic-drive principle
(:mod:`hpc_agent.infra.retry`) all the way to the drive layer: the poll loop
runs in plain code with the connection owned by a single process, and the model
is out of the loop entirely.

This module is the **detach-by-contract** launcher for the human-amplification
submit blocks whose wall-clock is cluster-bound
(``docs/design/human-amplification-blocks.md`` §3, "Blocks never block the
chat"): the S2 canary-wait, the S3 main-array watch, the speculative canary, and
the S4 harvest. Each is spawned by :func:`launch_submit_block_detached` as a
DETACHED ``hpc-agent <verb>`` subprocess running the SAME verb body with its
``detach`` spec field forced OFF, so the child owns the SSH work to terminal
(stamping the journal as it polls) while the parent returns a
:class:`DetachedLaunch` handle immediately. NO ``claude -p`` worker sits in the
poll loop. This mirrors DPDispatcher's "submit and poke until they finish" loop
and jobflow-remote's Runner daemon: the lifecycle runs to completion in a
deterministic process; the orchestrator reads state from the journal
(:mod:`hpc_agent.state.journal_poll`), never by spawning an LLM to poke SSH.

The launch is idempotent-single: a filesystem lease keyed by ``(run_id, block)``
refuses a second LIVE worker for the same key (:class:`DetachedLeaseHeld`) while
self-healing on a dead pid — the proving-run-#2 failure where two workers raced
one run. The child is spawned with the platform detach flags so it OUTLIVES the
orchestrator's session (the 0.10.63 crash killed an auto-backgrounded pipeline
~1s after qsub).

(:func:`build_status_pipeline_spec` remains as the pure ``status-pipeline`` spec
builder — a caller maps run fields to the composite's spec dict in code, no LLM
renders it — pinned by ``tests/integration/test_spec_contract.py``.)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Zero-hpc_agent-dep substrate module — safe to import at load (no cycle, unlike
# infra.io which detached imports lazily). This is the single PID-liveness
# definition; _pid_alive below forwards to it.
from hpc_agent.infra.proc import pid_alive as proc_pid_alive

__all__ = [
    "DetachedLaunch",
    "DriveModeError",
    "DetachedLeaseHeld",
    "build_status_pipeline_spec",
    "SUPPORTED_DETACHED_BLOCK_VERBS",
    "launch_submit_block_detached",
]

# The human-amplification block verbs whose wall-clock is cluster-bound and so
# detach-by-contract (docs/design/human-amplification-blocks.md §3, "Blocks never
# block the chat"): the S2 canary-wait, the S3 main-array watch, the speculative
# canary, and the S4 harvest (per-wave combine SSH + rsync pull + the
# breaker-deadline wait-and-retry — minutes on a throttled host). Each is spawned
# as a DETACHED ``hpc-agent <verb>`` subprocess running the SAME verb body with
# its ``detach`` spec field flipped OFF, so the child owns the SSH work to
# terminal (the poll loops stamp the journal as they go — monitor-flow refreshes
# ``last_status`` and stamps ``next_tick_due`` each tick, so the §5
# doctor/watchdog covers a dead child via a lapsed deadline; a dead S4 child is
# covered by its lease pid — ``wait-detached`` returns and the terminal record's
# absence says the harvest must re-run, which is idempotent) while the parent
# returns immediately with a handle envelope. NO ``claude -p`` worker, no LLM in
# the poll loop — the same deterministic-drive principle as the status path
# above, carried to the submit blocks.
# ``status-watch`` joined this set on 2026-07-07 (connection-broker.md, "The
# unattended cold-dial map"): its monitor poll was the last UNGATED in-code chain
# hop that dialed the cluster synchronously on an unattended cron tick. Detaching
# it moves the ONE cold dial into a durable child (warm engine, lease-single,
# watchdog-covered, exits at terminal), so the ``snapshot→watch`` hop becomes
# spawn-and-return and no unattended path dials inline. The child polls the SAME
# ``monitor-flow`` body with ``detach`` OFF; ``ops/status_blocks.py`` records its
# terminal so a re-invoke replays instead of re-dialing.
# ``aggregate-run`` / ``aggregate-flow`` / ``campaign-run`` joined on 2026-07-08
# (run-#10 finding F-K): each is a cluster-bound harvest/iteration whose
# wall-clock is minutes-to-hours (per-wave combine SSH + rsync pull + the
# breaker-deadline wait-and-retry; a full submit→monitor→aggregate iteration for
# ``campaign-run``), yet each still ran SYNCHRONOUSLY over the single-threaded
# MCP server — a live ``aggregate-run`` call wedged the chat for 20+ minutes with
# zero observability. Detaching them gives each its own lease + spec + log under
# ``_detached/`` and a journal-polled handle, exactly like the submit blocks.
SUPPORTED_DETACHED_BLOCK_VERBS = frozenset(
    {
        "submit-s2",
        "submit-s3",
        "submit-s4",
        "submit-speculate",
        "status-watch",
        "aggregate-run",
        "aggregate-flow",
        "campaign-run",
    }
)


class DriveModeError(ValueError):
    """The detached drive mode was requested for an unsupported shape."""


class DetachedLeaseHeld(DriveModeError):
    """A LIVE detached worker already owns this ``(run_id, block)`` lease.

    Raised by :func:`_spawn_detached` when a second detached launch is attempted
    for a ``(run_id, block)`` whose prior lease still names a running pid — the
    proving-run-#2 failure where two ``submit-s2`` workers raced one run,
    self-inflicting SSH contention and a stuck ``in_flight`` journal (design
    ``proving-run-2-hardening.md`` §3 Move 3 "idempotent-single", §2 rows 8/10).
    A stale lease (dead pid) is silently reclaimed, so a crash never permanently
    blocks relaunch; only a genuinely live sibling is refused.
    """


@dataclass(frozen=True)
class DetachedLaunch:
    """Handle for a launched detached deterministic runner.

    ``run_id`` is what the orchestrator polls the journal for
    (:func:`hpc_agent.state.journal_poll.poll_until_terminal`). ``pid`` is the
    detached subprocess id (informational — the orchestrator does NOT wait on
    it; it reads the journal). ``log_path`` is where the runner's stdout/stderr
    (the composite's JSON envelope + any diagnostics) are captured, so a
    post-mortem never needs to re-open SSH.
    """

    run_id: str
    pid: int
    log_path: str
    argv: list[str]


def build_status_pipeline_spec(fields: dict[str, Any]) -> dict[str, Any]:
    """Map status run *fields* to a ``status-pipeline`` spec dict.

    ``hpc-agent status-pipeline`` takes a spec embedding the monitor spec under
    ``monitor`` (``run_id`` + poll cadence + wall-clock budget). This builds that
    spec in code from the run fields, so no LLM renders it.

    Required field: ``run_id``. Optional pass-throughs (each defaulted by the
    ``MonitorFlowSpec`` model when omitted): ``poll_interval_seconds``,
    ``wall_clock_budget_seconds``, ``auto_combine_waves``, ``combiner_max_retries``,
    ``file_glob``. Raises :class:`DriveModeError` when ``run_id`` is absent — the
    detached runner cannot poll a run it can't name.
    """
    run_id = fields.get("run_id")
    if not run_id or not isinstance(run_id, str):
        raise DriveModeError(
            "detached status drive requires a string 'run_id' in fields; got "
            f"{run_id!r}. The detached deterministic runner polls the journal by "
            "run_id, so it must be known before launch (the snapshot 'status' "
            "path discovers it, but the wait path is keyed by it)."
        )
    monitor: dict[str, Any] = {"run_id": run_id}
    for key in (
        "poll_interval_seconds",
        "wall_clock_budget_seconds",
        "auto_combine_waves",
        "combiner_max_retries",
        "file_glob",
    ):
        if key in fields and fields[key] is not None:
            monitor[key] = fields[key]
    return {"monitor": monitor}


def _detach_popen_kwargs() -> dict[str, Any]:
    """Platform flags that fully detach the child from this process.

    The runner must OUTLIVE the orchestrator: if the caller's session ends
    (the exact failure that killed the auto-backgrounded ``submit-pipeline`` in
    0.10.63 — "the harness killed the pipeline ~1s after the main qsub"), the
    deterministic poll must keep owning the connection to terminal. POSIX:
    ``start_new_session=True`` makes the child a session+group leader so it does
    not receive the parent's SIGHUP/SIGINT. Windows: ``DETACHED_PROCESS |
    CREATE_NEW_PROCESS_GROUP`` so it has no console and ignores the parent's
    Ctrl-C/console-close, plus ``CREATE_BREAKAWAY_FROM_JOB`` so the worker
    escapes an inherited kill-on-close Job Object.

    The Job Object flag is the proving-run-#3 finding (detached-worker-as-MCP-
    grandchild): agent harnesses (Claude Code among them) run their MCP servers
    inside a Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, so every
    descendant — including our "detached" worker — is killed the moment the
    session's job handle closes, regardless of ``DETACHED_PROCESS``. Measured
    empirically (2026-07-04, Windows 11): a worker spawned with only
    ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`` inside a kill-on-close job
    dies when the job handle closes; adding ``CREATE_BREAKAWAY_FROM_JOB``
    keeps it alive when the job grants ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` (and
    jobs with ``SILENT_BREAKAWAY_OK`` — this box's Claude Code session job,
    flags 0x3000 — already exempt children automatically). When the job grants
    neither, CreateProcess is documented to fail with ``ERROR_ACCESS_DENIED``
    (observed on Win11: it instead succeeds and silently keeps the child in
    the job), so :func:`_popen_detached` retries once WITHOUT the breakaway
    flag — degrading to today's behavior, never refusing the launch.
    """
    if sys.platform == "win32":
        flags = 0
        # These attributes exist only on Windows; guard for the type checker.
        flags |= getattr(subprocess, "DETACHED_PROCESS", 0)
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


def _popen_detached(argv: list[str], **popen_kwargs: Any) -> subprocess.Popen[bytes]:
    """``Popen`` with the platform detach flags, tolerating breakaway denial.

    On Windows a job without ``JOB_OBJECT_LIMIT_BREAKAWAY_OK`` may refuse a
    ``CREATE_BREAKAWAY_FROM_JOB`` spawn with ``ERROR_ACCESS_DENIED`` (winerror
    5). That host put us in an inescapable job on purpose; the launch must
    still succeed (the worker then shares the session's fate — exactly the
    pre-fix contract), so retry once without the breakaway bit. Any other
    failure propagates unchanged.
    """
    detach_kwargs = _detach_popen_kwargs()
    try:
        return subprocess.Popen(argv, **popen_kwargs, **detach_kwargs)
    except OSError as exc:
        breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
        flags = detach_kwargs.get("creationflags", 0)
        if (
            sys.platform == "win32"
            and breakaway
            and flags & breakaway
            and getattr(exc, "winerror", None) == 5  # ERROR_ACCESS_DENIED
        ):
            detach_kwargs["creationflags"] = flags & ~breakaway
            return subprocess.Popen(argv, **popen_kwargs, **detach_kwargs)
        raise


def _agent_launch_prefix(hpc_agent_bin: str | None) -> list[str]:
    """The argv prefix that invokes hpc-agent for a DETACHED worker.

    Defaults to ``[sys.executable, "-m", "hpc_agent"]`` — the *running*
    interpreter — NOT a bare ``hpc-agent`` PATH lookup. The detached child must
    run the SAME install as the parent that spawned it (the design contract:
    "the child is the same deterministic composite the synchronous path runs").
    A bare PATH ``hpc-agent`` is an independent console-script artifact that can
    resolve to a DIFFERENT install than the one driving the run — a stale wheel,
    an unactivated conda env, or (in editable/multi-venv dev) a different tree
    entirely — in which case the worker silently runs the wrong code or, when the
    block verbs are absent there, dies immediately with ``unknown command``.
    Binding to ``sys.executable`` makes the worker's code identity match the
    driver's by construction. An explicit *hpc_agent_bin* (a test stub, or a
    caller that truly wants a specific binary) still overrides.
    """
    if hpc_agent_bin:
        return [hpc_agent_bin]
    return [sys.executable, "-m", "hpc_agent"]


def _pid_alive(pid: int) -> bool:
    """Whether *pid* names a running process on this host.

    The lease liveness check (a dead pid is a reclaimable stale lease; a live one
    refuses the second launch). This is a THIN WRAPPER over the single substrate
    definition :func:`hpc_agent.infra.proc.pid_alive` (over ``psutil``) — the
    win32/POSIX probe was outsourced when the audit (2026-07-07, finding #1)
    found this copy had BYTE-DIVERGED from ``infra/ssh_slots.py``'s: this one
    read ``STILL_ACTIVE`` (259) via ``GetExitCodeProcess`` (so an exit-code-259
    process read alive and an exited-but-open-handle pid read dead), the other
    keyed off ``GetLastError() != 87`` — two definitions of one fact that could
    disagree on a zombie / access-denied edge. ``psutil.pid_exists`` is the
    canonical cross-platform probe (and sidesteps the win32
    ``os.kill``-is-``TerminateProcess`` footgun).

    The wrapper SURVIVES (rather than exporting ``proc.pid_alive`` directly)
    because every test in this module and its consumers monkeypatches THIS
    module attribute — ``monkeypatch.setattr(detached, "_pid_alive", ...)`` — and
    ``_guard_single_lease`` / the lazy-importing consumers all resolve the name
    off this module at call time. Keeping a module-level ``_pid_alive`` here
    preserves that seam with zero call-site churn; the probe logic lives in ONE
    place (``infra/proc.py``), this only forwards.
    """
    return proc_pid_alive(pid)


def _guard_single_lease(detached_dir: Path, block: str, run_id: str) -> Path:
    """Refuse a second LIVE detached worker for the same ``(run_id, block)``.

    Filesystem lease keyed by ``(run_id, block)`` (mirrors the ``.submit_lock``
    advisory-lock convention in ``ops/submit_flow.py``). Returns the lease-file
    path the caller stamps with the launched pid *after* a successful ``Popen``.

    The check-and-claim runs under an :func:`hpc_agent.infra.io.advisory_flock`
    so two racing launches can't both pass the liveness check — the exact
    proving-run-#2 race (two ``submit-s2`` against one run). Reads the prior
    lease if present: a LIVE recorded pid → :class:`DetachedLeaseHeld` (refuse);
    a DEAD one (or an unreadable/absent lease) → reclaimable, so the launch
    proceeds. A crashed worker leaves a dead pid, so the lease self-heals and
    never permanently blocks relaunch.

    NOTE: this only holds the flock long enough to *decide*; the caller stamps
    the lease under the same key immediately after spawning. Two launches for
    the same key that interleave both see "no live lease" only if neither has
    stamped yet — which the surrounding flock in :func:`_spawn_detached`
    prevents by keeping decide+spawn+stamp inside one critical section.
    """
    lease_path = detached_dir / f"{block}-{run_id}.lease.json"
    if lease_path.exists():
        try:
            prior = json.loads(lease_path.read_text(encoding="utf-8"))
            prior_pid = int(prior.get("pid", -1))
        except (OSError, ValueError, TypeError):
            prior_pid = -1
        if prior_pid > 0 and _pid_alive(prior_pid):
            raise DetachedLeaseHeld(
                f"a live detached worker (pid {prior_pid}) already owns the "
                f"({run_id!r}, {block!r}) lease at {lease_path}; refusing a second "
                "racing launch. Poll the journal for this run instead of spawning "
                "again. If the worker has died, its dead pid makes the lease stale "
                "and the next launch will reclaim it automatically."
            )
    return lease_path


def _spawn_detached(
    *, run_id: str, block: str, argv: list[str], log_path: Path, cwd: str
) -> DetachedLaunch:
    """``Popen`` *argv* fully detached, capturing stdout/stderr to *log_path*.

    The one place the platform-detach + log-capture is done, shared by the
    status-pipeline path and the submit-block path so they can never drift on
    the detach flags (the child must OUTLIVE the orchestrator's session — the
    exact 0.10.63 failure) or the tty-safe stdin. Detached: no stdin (DEVNULL so
    a poll can never block on a tty), stdout/stderr captured to a log so the
    child's envelope + diagnostics survive without the orchestrator reading them
    live. The orchestrator learns the outcome from the JOURNAL, not this pipe.

    *block* keys the idempotent-single lease with *run_id*: the whole
    decide→spawn→stamp is done under one advisory flock so a second detached
    launch for the same ``(run_id, block)`` while the first is alive is refused
    (:class:`DetachedLeaseHeld`), never two racing (design
    ``proving-run-2-hardening.md`` §3 Move 3). A stale lease (dead pid) is
    reclaimed, so a crash never blocks relaunch.
    """
    from hpc_agent.infra import io  # noqa: PLC0415 — avoid an import cycle at module load

    detached_dir = log_path.parent
    lock_path = detached_dir / f"{block}-{run_id}.lease.lock"
    with io.advisory_flock(lock_path):
        lease_path = _guard_single_lease(detached_dir, block, run_id)

        log_handle = open(log_path, "w", encoding="utf-8")  # noqa: SIM115 — handed to the child
        try:
            proc = _popen_detached(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                env={**os.environ},
            )
        finally:
            # The child inherits its own dup'd fd; this process closes its copy
            # so it isn't holding the log open for the runner's whole lifetime.
            log_handle.close()

        # Stamp the lease with the just-launched pid while still under the flock,
        # so a concurrent launch for the same key sees a live lease the moment it
        # acquires the lock.
        lease_path.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "block": block,
                    "pid": proc.pid,
                    "log_path": str(log_path.resolve()),
                    "argv": argv,
                }
            ),
            encoding="utf-8",
        )

    return DetachedLaunch(
        run_id=run_id,
        pid=proc.pid,
        log_path=str(log_path.resolve()),
        argv=argv,
    )


def _block_spec_run_id(spec: dict[str, Any]) -> str:
    """Dig the run_id out of a detach-supported block spec dict (for the handle).

    S2 / S3 / speculate embed the submit-and-verify spec under ``submit.submit``
    with the ``run_id`` on the inner submit-flow spec; S4 (harvest),
    ``aggregate-run``, and ``campaign-run`` carry it on their embedded
    aggregate-flow spec at ``aggregate.run_id``; ``status-watch`` carries it on
    its embedded monitor-flow spec at ``monitor.run_id``; ``aggregate-flow``
    carries it FLAT at ``run_id`` (it IS the aggregate spec, not an embedder).
    The run_id is what the parent hands back as the journal-poll key, so it must
    be present before the detach. Raises :class:`DriveModeError` when it can't be
    found — a detached block that can't name its run can't be polled.

    The submit shapes are checked FIRST and unchanged; the ``aggregate`` /
    ``monitor`` / flat ``run_id`` fallbacks are additive (a submit spec resolves
    its run_id before the fallbacks are reached, so the submit paths stay
    byte-identical — pinned by ``tests/integration/test_spec_contract.py``).
    """
    submit = spec.get("submit")
    inner = submit.get("submit") if isinstance(submit, dict) else None
    run_id = inner.get("run_id") if isinstance(inner, dict) else None
    if not isinstance(run_id, str) or not run_id:
        aggregate = spec.get("aggregate")
        run_id = aggregate.get("run_id") if isinstance(aggregate, dict) else None
    if not isinstance(run_id, str) or not run_id:
        monitor = spec.get("monitor")
        run_id = monitor.get("run_id") if isinstance(monitor, dict) else None
    if not isinstance(run_id, str) or not run_id:
        # aggregate-flow is itself the aggregate spec — run_id sits FLAT.
        flat = spec.get("run_id")
        run_id = flat if isinstance(flat, str) else None
    if not isinstance(run_id, str) or not run_id:
        raise DriveModeError(
            "detached block drive requires a string run_id at "
            f"spec.submit.submit.run_id (S2/S3/speculate), spec.aggregate.run_id "
            f"(S4/aggregate-run/campaign-run), spec.monitor.run_id (status-watch), "
            f"or spec.run_id (aggregate-flow); got {run_id!r}. The "
            "detached worker polls the journal by run_id, so it must be resolved "
            "before the detach."
        )
    return run_id


def launch_submit_block_detached(
    *,
    verb: str,
    experiment_dir: str,
    spec: dict[str, Any],
    hpc_agent_bin: str | None = None,
) -> DetachedLaunch:
    """Launch a detach-by-contract block (``submit-s2`` / ``submit-s3`` /
    ``submit-s4`` / ``submit-speculate`` / ``status-watch`` / ``aggregate-run`` /
    ``aggregate-flow`` / ``campaign-run``) as a DETACHED ``hpc-agent <verb>``
    subprocess (design §3, detach-by-contract).

    Generalized 2026-07-07 to cover ``status-watch`` (connection-broker.md): the
    launcher is block-family-agnostic — the run_id extractor
    (:func:`_block_spec_run_id`) and :data:`SUPPORTED_DETACHED_BLOCK_VERBS` carry
    the per-family knowledge, and the submit paths are byte-identical (the name is
    retained for call-site stability). ``status-watch`` spawns the SAME
    ``monitor-flow``-poll body with ``detach`` OFF so the child owns the one cold
    dial to terminal.

    The parent verb has ALREADY run its synchronous gate + drift guards (the
    ordering the caller enforces: gate → drift → detach) and forced ``detach``
    OFF in *spec*, so the spawned child runs the SAME verb body synchronously —
    owning the SSH poll to terminal and stamping the journal as it goes — while
    the parent returns the :class:`DetachedLaunch` handle immediately. NO
    ``claude -p`` worker is spawned anywhere on this path; the child is the same
    deterministic composite the synchronous path runs, just in its own detached
    process (survives session death, unlike harness backgrounding).

    *verb* must be one of :data:`SUPPORTED_DETACHED_BLOCK_VERBS`. The spec is
    written to the journal home's ``_detached/`` dir (always present/writable,
    never pollutes the experiment tree) and the child reads it back with
    ``--spec``. Raises :class:`DriveModeError` for an unsupported verb, a spec
    that still has ``detach`` truthy (a guard against detaching a child that would
    itself re-detach into an infinite fork), or a missing run_id.
    """
    from hpc_agent.state.run_record import _current_homedir

    if verb not in SUPPORTED_DETACHED_BLOCK_VERBS:
        raise DriveModeError(
            f"detached block drive is only supported for {sorted(SUPPORTED_DETACHED_BLOCK_VERBS)}; "
            f"got {verb!r}."
        )
    if spec.get("detach"):
        raise DriveModeError(
            "the spec handed to launch_submit_block_detached must have detach=False "
            "(the child runs synchronously); a truthy detach would fork forever."
        )
    run_id = _block_spec_run_id(spec)

    detached_dir = _current_homedir() / "_detached"
    detached_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    spec_path = detached_dir / f"{verb}-{run_id}-{token}.spec.json"
    log_path = detached_dir / f"{verb}-{run_id}-{token}.log"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    argv = [
        *_agent_launch_prefix(hpc_agent_bin),
        verb,
        "--spec",
        str(spec_path),
        "--experiment-dir",
        experiment_dir,
    ]
    return _spawn_detached(
        run_id=run_id, block=verb, argv=argv, log_path=log_path, cwd=experiment_dir
    )
