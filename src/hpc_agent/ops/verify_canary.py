"""``verify-canary`` workflow atom — wait + grep + output-check protocol.

Replaces the multi-step canary-verification prose at /submit-hpc Step
7b/8 with one workflow atom that:

1. Polls the canary's run record until the canary's job_ids are no
   longer alive on the cluster (terminal).
2. Greps the canary's stderr log for known failure markers
   (``[dispatch] FAILED``, ``Traceback``, ``ImportError``).
3. Optionally verifies an expected output artifact exists in the
   canary's result_dir.

Returns ``{ok, failure_kind, details, stderr_tail}`` so the caller
can branch on ``ok``: True → main array submit; False → surface
``stderr_tail`` to the user verbatim (don't paraphrase — the user
needs the raw error to fix it).

Currently the most fragile multi-step protocol in the slash command;
atomizing it eliminates the agent-judgment-on-each-step failure mode.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.clusters import resolve_ssh_target

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.infra.backends import HPCBackend

# Stderr substrings that signal a canary failure. Lowercased; matched
# case-insensitively. Order matters for ``failure_kind`` reporting —
# more specific markers first so e.g. an ImportError isn't reported as
# a generic Traceback.
_FAILURE_MARKERS: tuple[tuple[str, str], ...] = (
    ("[dispatch] failed", "dispatcher_failed"),
    ("importerror", "import_error"),
    ("modulenotfounderror", "module_not_found"),
    ("traceback", "traceback"),
    ("oom-kill", "oom_killed"),
    ("out of memory", "oom_killed"),
    ("segmentation fault", "segfault"),
)


def _failure_features(stderr_tail: str, log_path: str | None) -> dict[str, Any]:
    """Build the structured ``failure_features`` envelope for a failed canary.

    Two layers of evidence the LLM (or human) would otherwise have to
    fetch by hand:

    * **cluster_log_tail** — the raw last ~50 lines of the canary's
      cluster log, verbatim. ``stderr_tail`` already holds this; we
      attach it under the structured key so downstream consumers don't
      have to know which top-level field to read. ``log_path`` records
      the remote path (when known) so the user can ``ssh`` over and
      tail more if they need to.

    * **classified_error** — the structured pattern match from
      :data:`hpc_agent.infra.failure_signatures.CATALOG` (the one shared
      classifier catalog — ``recover``, ``reduce``, and ``reconcile`` all
      consume it). Gives the decision-maker an ``error_class`` +
      ``suggested_fix`` + the ``matched_pattern`` regex, so an agent can act
      on the structured remediation instead of paraphrasing the log tail.
      ``None`` when the stderr is empty (nothing to classify).
    """
    # Import inside the helper — keeps the classifier off the module-discovery
    # import path for callers that only ever hit the happy path (it's a pure
    # ``infra`` citizen now, so this is cheap; the laziness is just hygiene).
    from hpc_agent.infra.failure_signatures import classify

    classified: dict[str, Any] | None = None
    if stderr_tail:
        classified = classify(stderr_tail, exit_code=None)

    return {
        "cluster_log_tail": stderr_tail,
        "log_path": log_path,
        "classified_error": classified,
    }


_DEFAULT_POLL_INTERVAL_SEC = 30
_DEFAULT_WAIT_BUDGET_SEC = 1800  # 30 min — long enough for a 1-task probe

#: Consecutive DETERMINISTIC broken-env poll failures (rc 126/127) that escalate
#: the canary to a loud verdict instead of riding the full 30-min budget. A
#: broken run env (bare ``python`` the conda env never provided) fails EVERY poll
#: the same way and will never heal by waiting, so ~K polls (≈90s on the ramp)
#: is enough to conclude — finding 12. A single TRANSIENT failure resets the
#: counter (that class belongs to the connection breaker and stays on the budget).
_DETERMINISTIC_ENV_POLLS_TO_FAIL = 3

#: Floor (seconds) between watchdog liveness stamps in the canary poll loop. The
#: loop fast-starts at ~3s and ramps, so stamping every poll would churn the
#: sidecar lock every 3s; flooring the stamp keeps the sidecar fresh (no frozen
#: submit-time timestamp — finding 12) without hammering the lock.
_WATCHDOG_STAMP_FLOOR_SEC = 10.0


def _classify_poll_failure(exc: BaseException) -> str:
    """Classify a poll-loop exception as deterministic (escalate) or transient.

    Returns one of three literals:

    * ``"deterministic_env"`` — a :class:`errors.RemoteCommandFailed` whose remote
      ``returncode`` is 126 ("not executable") or 127 ("command not found"): the
      SSH connection SUCCEEDED and the remote command deterministically failed
      because the run's ``python`` / conda env is absent or wrong.
    * ``"deterministic_reporter"`` — the connection SUCCEEDED and the on-cluster
      status reporter returned a STRUCTURED deterministic error
      (:data:`errors.DETERMINISTIC_REPORTER_ERROR_CODES`: a run sidecar that was
      never shipped / is unparseable, or a missing ``tasks.py``). Finding 7's
      ~30-min false-negative spin: a ``-canary2`` sidecar the deploy never pushed
      answered EVERY poll ``sidecar_not_found`` at rc 2 — previously "transient",
      polled to the full budget against a file that will NEVER appear.

    Both deterministic classes will never heal by waiting, so the canary loop
    escalates fast on a run of them (:data:`_DETERMINISTIC_ENV_POLLS_TO_FAIL`).
    EVERYTHING else — an :class:`OSError`/``TimeoutError`` transport blip, an
    :class:`~hpc_agent.errors.SshUnreachable`, an open breaker, or a
    ``RemoteCommandFailed`` with any other returncode and no deterministic
    reporter code — is ``"transient"`` and rides the wait budget (that class
    belongs to the connection breaker). The returncode / reporter code are read
    off the exception attributes, never string-parsed from the message.
    """
    if errors.is_deterministic_env_failure(exc):
        return "deterministic_env"
    if errors.is_deterministic_reporter_failure(exc):
        return "deterministic_reporter"
    return "transient"


def _reporter_unreachable_envelope(
    canary_run_id: str,
    last_poll_error: Exception | None,
    *,
    annotation: str | None = None,
) -> dict[str, Any]:
    """Build the shared ``reporter_unreachable`` failure envelope.

    Two poll-loop arms reach the same verdict — the connection succeeded (or kept
    timing out) but the cluster-side reporter never returned a readable status,
    so the canary CANNOT be trusted as passed:

    * the wait budget elapsed with EVERY poll failing (the budget-timeout arm), and
    * :data:`_DETERMINISTIC_ENV_POLLS_TO_FAIL` consecutive DETERMINISTIC
      broken-env polls escalated early AND the env-independent marker scan
      (:func:`hpc_agent.infra.cluster_status.ssh_marker_scan`) found NO
      ``.hpc_failed`` marker to positively confirm failure — unverifiable, so
      still a loud fail, never a pass (the never-pass-unverified posture).

    The diagnosis is identical for both (wrong/absent conda env; fix before
    submitting); *annotation* appends the arm-specific evidence (rc + consecutive
    count for the escalation arm). Factoring it here lets both call sites share
    the one envelope.
    """
    details = (
        f"canary {canary_run_id!r}: every status poll failed — the "
        f"cluster-side reporter never returned (last error: {last_poll_error}). "
        "The scheduler may have run the job, but the framework cannot read its "
        "result, so the canary CANNOT be trusted as passed. Common cause: "
        "hpc-agent not importable in the cluster's python (wrong/absent conda "
        "env) or a module-load failure in the job preamble. Fix the cluster env "
        "before submitting the main array."
    )
    if annotation:
        details = f"{details} {annotation}"
    return {
        "ok": False,
        "failure_kind": "reporter_unreachable",
        "details": details,
        "stderr_tail": "",
        "metrics_fingerprint": None,
        "failure_features": _failure_features("", None),
    }


def _canary_failed_envelope(
    canary_run_id: str,
    *,
    markers: list[str],
    returncode: int | None,
) -> dict[str, Any]:
    """Build the ``canary_failed`` envelope from env-independent marker evidence.

    Reached only on the deterministic broken-env escalation path — at the ONE
    terminal status reporter (:func:`_terminal_reporter_verdict`), whose rc 126/127
    is the broken-env signature — when the plain-``sh``
    :func:`hpc_agent.infra.cluster_status.ssh_marker_scan` found the dispatcher's
    ``.hpc_failed/<run_id>.<task>.failed`` terminal marker(s) — POSITIVE proof the
    task ran and failed, surviving the broken env that blinded the python status
    reporter. The marker basenames ride out as evidence in ``details`` (the
    envelope shape is fixed by ``schemas/verify_canary.output``).
    """
    names = ", ".join(markers) if markers else "(none)"
    return {
        "ok": False,
        "failure_kind": "canary_failed",
        "details": (
            f"canary {canary_run_id!r} FAILED: the cluster-side dispatcher wrote "
            f"terminal failure marker(s) [{names}] under .hpc_failed/ — positive "
            f"proof the task ran and failed, read with a plain shell scan that "
            f"survives the broken run env (the terminal status reporter died "
            f"rc={returncode}, the command-not-found/not-executable "
            "signature). Fix the cluster env (hpc-agent importable in the run's "
            "conda env) and inspect the per-task cluster log for the underlying "
            "error before submitting the main array."
        ),
        "stderr_tail": "",
        "metrics_fingerprint": None,
        "failure_features": _failure_features("", None),
    }


def _deterministic_reporter_envelope(
    canary_run_id: str,
    *,
    reporter_code: str,
    remote_path: str,
    ssh_target: str,
) -> dict[str, Any]:
    """Build the escalated verdict for a DETERMINISTIC reporter fault (finding 7).

    Reached when the ONE terminal status reporter
    (:func:`_terminal_reporter_verdict`) returned a structured reporter error
    (:data:`errors.DETERMINISTIC_REPORTER_ERROR_CODES` — a run sidecar the deploy
    never shipped, a torn one, a missing ``tasks.py``): a file condition that will
    NEVER appear by waiting, so escalate now instead of riding the 30-min budget.

    The verdict DISCLOSES which sidecar path the reporter polled — derived from
    the SAME recorded identity the submit wrote (``.hpc/runs/<run_id>.json``, the
    one-definition rule), never re-derived — and which sibling sidecars actually
    shipped remotely (a best-effort plain-``sh`` ``ls``), so a
    ``sidecar_not_found`` on ``<run>-canary2`` reads "polled ``…canary2.json``;
    present: ``…canary.json``" — the never-shipped-sidecar bug named at a glance
    rather than a bland ``reporter_unreachable`` after a burned budget.
    """
    from hpc_agent.infra.cluster_status import ssh_list_run_sidecars

    polled = f".hpc/runs/{canary_run_id}.json"
    siblings = ssh_list_run_sidecars(ssh_target=ssh_target, remote_path=remote_path)
    present = ", ".join(siblings) if siblings else "(none)"
    return {
        "ok": False,
        "failure_kind": "reporter_unreachable",
        "details": (
            f"canary {canary_run_id!r}: the cluster-side status reporter returned "
            f"a DETERMINISTIC error ({reporter_code!r}) at the terminal poll "
            f"— a file/manifest condition that will NEVER heal by "
            f"waiting, so the canary was escalated immediately instead of riding the "
            f"full wait budget. The reporter polled {polled!r} (derived from the run "
            f"id the submit recorded); sidecars present under .hpc/runs/ on the "
            f"cluster: [{present}]. If the polled name is missing but a sibling "
            f"(e.g. the first ``-canary``) is present, the canary's sidecar was "
            f"minted locally but never SHIPPED to the cluster — re-run the submit so "
            f"every canary sidecar is deployed before its job runs. The canary "
            f"CANNOT be trusted as passed."
        ),
        "stderr_tail": "",
        "metrics_fingerprint": None,
        "failure_features": _failure_features("", None),
    }


def _env_float(name: str, default: float) -> float:
    """Read a float env var, falling back to *default* on unset/invalid/negative.

    Mirrors :func:`hpc_agent.ops.monitor_flow._env_float`: a typo in the shell
    must not silently disable the fast-start (which would re-introduce the flat
    30s dead-wait), so an unparseable or negative value falls back to *default*.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError:
        return default
    return val if val >= 0 else default


#: Fast-start floor (seconds) for the canary poll loop. The canary is a 1-task
#: probe on the critical path of EVERY fresh submit (the cache at
#: ``submit_flow._should_canary`` already skips it for a repeat ``cmd_sha``), so
#: a flat ``poll_interval_sec`` (default 30s) means a canary that lands in
#: ~5-10s is not OBSERVED for a full interval — pure dead wait the submit
#: pipeline pays before the main array can launch. We start polling at this
#: floor and ramp geometrically toward ``poll_interval_sec`` (the steady-state
#: ceiling), so a fast canary is caught in seconds while a slow one settles to
#: the configured cadence. This is the inverse of ``monitor_flow``'s adaptive
#: backoff: there the long idle loop ramps the interval UP to save SSH polls;
#: here the short critical-path loop ramps UP FROM a fast start to save
#: latency, never exceeding the configured ceiling (so steady-state SSH load is
#: unchanged). Env-tunable via ``HPC_CANARY_FAST_POLL_SEC``; set to 0 to opt out
#: (fall back to the flat interval).
_CANARY_FAST_POLL_SEC: float = _env_float("HPC_CANARY_FAST_POLL_SEC", 3.0)


def _initial_poll_interval(poll_interval_sec: float) -> float:
    """The interval the canary loop starts at: the fast-start floor, capped by
    the caller's configured interval (a caller asking for a *faster* steady
    cadence than the floor is honored as-is, and the floor never slows it down).
    """
    if _CANARY_FAST_POLL_SEC <= 0:
        return float(poll_interval_sec)
    return min(float(poll_interval_sec), _CANARY_FAST_POLL_SEC)


def _next_poll_interval(current: float, ceiling: float) -> float:
    """Geometric fast-start ramp: double *current* toward *ceiling*, never past it.

    Bounds the early burst (3 → 6 → 12 → 24 → 30 → 30 … for the default 30s
    ceiling — ~4 extra polls in the first window) so the speedup costs only a
    handful of cheap multiplexed round-trips, then holds the configured cadence.
    """
    return min(current * 2.0, float(ceiling))


# Remote snippet that round-trips the canary's latest checkpoint ON THE CLUSTER
# — where ``hpc_agent`` is importable in the run's own env and where a resume
# would actually reload it. Emits a single JSON line on stdout:
#   {"status": "missing"}                                → no checkpoint written
#   {"status": "unloadable", "path", "format", ...}      → present, won't restore
#   {"status": "ok", "path", "format", "level", ...}     → restorable
# Format-aware via ``checkpoint_formats.describe_latest_checkpoint``: the
# pickle format actually deserializes (level "loadable", with next_iteration —
# ``read_latest_checkpoint`` walks newest→oldest and skips corrupt files, so a
# present-but-unloadable verdict really means NO checkpoint deserializes);
# adapter formats like petsc_binary verify structurally (level "structural" —
# loading would need the solver library, which this probe env may lack).
#
# Version skew: the run's cluster env may carry an OLDER hpc-agent without
# ``checkpoint_formats``. The except-ImportError arm reproduces the historical
# pickle-only probe verbatim so a new control plane still verifies runs on an
# old cluster env (pickle checkpoints only — exactly what that env supports).
#
# After reading, it REMOVES the canary's _checkpoints/ dir: the canary checkpoint
# is a throwaway probe, and a result_dir_template WITHOUT {run_id} (e.g.
# "results/task_{task_id}") would otherwise have the MAIN run's task 0 share the
# dir and resume off the canary's checkpoint (run_iterations always reads latest).
# Cleanup runs after the JSON is emitted so the verdict is unaffected; harmless
# for the recommended {run_id}-scoped template where there is no sharing.
_REMOTE_CHECKPOINT_SNIPPET = (
    "import json,sys,shutil\n"
    "from hpc_agent.experiment_kit.checkpoint import "
    "latest_checkpoint, read_latest_checkpoint, checkpoint_dir\n"
    "d=sys.argv[1]\n"
    "try:\n"
    "    from hpc_agent.experiment_kit.checkpoint_formats import describe_latest_checkpoint\n"
    "    print(json.dumps(describe_latest_checkpoint(d)))\n"
    "except ImportError:\n"
    "    p=latest_checkpoint(d)\n"
    "    if p is None:\n"
    "        print(json.dumps({'status':'missing'}))\n"
    "    else:\n"
    "        _,nxt=read_latest_checkpoint(d)\n"
    "        if int(nxt)<=0:\n"
    "            print(json.dumps({'status':'unloadable','path':str(p)}))\n"
    "        else:\n"
    "            print(json.dumps({'status':'ok','path':str(p),'next_iteration':int(nxt)}))\n"
    "shutil.rmtree(str(checkpoint_dir(d)), ignore_errors=True)\n"
)


def _verify_remote_checkpoint(
    *,
    ssh_target: str,
    remote_path: str,
    ckpt_result_dir: str,
    remote_activation: str,
) -> dict[str, Any]:
    """Round-trip the canary's checkpoint on the cluster; return a status dict.

    Runs :data:`_REMOTE_CHECKPOINT_SNIPPET` over SSH under the run's activation
    (so ``hpc_agent`` imports in the right env, exactly like the status reporter).
    *ckpt_result_dir* is the canary task's result dir (absolute, or relative to
    *remote_path*); the snippet looks under its ``_checkpoints/``.

    Returns ``{"status": "ok"|"missing"|"unloadable"|"probe_failed", ...}``. A
    ``probe_failed`` (ssh error / unparseable output) carries ``detail`` so the
    caller can fail the gate loudly rather than silently pass an unverified run.
    """
    import json
    import shlex

    from hpc_agent.infra.remote import ssh_run

    target = ckpt_result_dir
    if not target.startswith("/"):
        target = f"{remote_path.rstrip('/')}/{target.lstrip('/')}"
    # remote_activation already ends in " && " (or is empty); cd into remote_path
    # so a relative result dir resolves the same way the dispatcher rendered it.
    cmd = (
        f"{remote_activation}cd {shlex.quote(remote_path)} && "
        f"python3 -c {shlex.quote(_REMOTE_CHECKPOINT_SNIPPET)} {shlex.quote(target)}"
    )
    try:
        res = ssh_run(cmd, ssh_target=ssh_target)
    except (errors.RemoteCommandFailed, OSError) as exc:
        return {"status": "probe_failed", "detail": f"ssh checkpoint probe raised: {exc}"}
    if res.returncode != 0:
        return {
            "status": "probe_failed",
            "detail": (
                f"remote checkpoint probe exited {res.returncode}: "
                f"{(res.stderr or '').strip()[:300]}"
            ),
        }
    line = (res.stdout or "").strip().splitlines()
    if not line:
        return {"status": "probe_failed", "detail": "remote checkpoint probe produced no output"}
    try:
        parsed = json.loads(line[-1])
    except json.JSONDecodeError:
        return {
            "status": "probe_failed",
            "detail": f"remote checkpoint probe output not JSON: {line[-1][:200]!r}",
        }
    if not isinstance(parsed, dict) or "status" not in parsed:
        return {"status": "probe_failed", "detail": f"unexpected probe payload: {parsed!r}"}
    return parsed


def _classify_runtime_exit(
    exit_code: int, *, canary_run_id: str, result_dir: str
) -> tuple[str, str] | None:
    """Map a recorded ``_runtime.json`` exit code to ``(failure_kind, details)``.

    ``None`` for exit 0 (no verdict — the canary's exit leg is clean).
    Dispatcher SCAFFOLD codes get their own vocabulary (run-#10 lesson: a
    contract violation surfacing under a generic name nearly bought a wrong
    diagnosis after a burned wait budget). Kept in lock-step with
    ``dispatch.py``'s ``_EXIT_NO_RUNNER`` (3) / ``_EXIT_NO_OUTPUT`` (4);
    everything else non-zero stays ``nonzero_exit`` (#351-3).
    """
    if exit_code == 0:
        return None
    if exit_code == 4:
        return (
            "output_contract",
            (
                f"canary {canary_run_id!r} recorded exit_code=4 "
                "(dispatcher no-output gate): the executor exited 0 but wrote "
                "NOTHING into $HPC_RESULT_DIR — its outputs went to a literal "
                "path instead of the write-isolation dir, so there was nothing "
                "to promote. Fix the entry point to write under $HPC_RESULT_DIR "
                "when the dispatcher exports it; retrying cannot fix this."
            ),
        )
    if exit_code == 3:
        return (
            "no_runner",
            (
                f"canary {canary_run_id!r} recorded exit_code=3 "
                "(dispatcher no-runner gate): the sidecar's executor is empty or "
                "would re-invoke the dispatcher itself. A deterministic scaffold "
                "error — fix the executor command; retrying cannot fix this."
            ),
        )
    return (
        "nonzero_exit",
        (
            f"canary {canary_run_id!r} recorded exit_code={exit_code} in "
            f"{result_dir.rstrip('/')}/_runtime.json — it wrote a result file but "
            "the task FAILED (a non-zero exit whose traceback may fall outside the "
            "50-line stderr tail). Refusing to pass the canary; inspect the stderr "
            "tail / classified_error for the cause before submitting the main array."
        ),
    )


def _parse_runtime_json(raw: str) -> dict[str, Any]:
    """Map a raw ``_runtime.json`` read to the ``{status: ...}`` verdict dict.

    #351-3: the dispatcher records the per-task ``exit_code`` to
    ``<result_dir>/_runtime.json`` (``dispatch.py`` ~:984/:1007); a canary that
    wrote a partial result then exited non-zero (e.g. a ``TypeError`` whose
    traceback fell outside the fetched tail) must FAIL the gate. The fused verify
    tail (:func:`_fused_verify_tail`) reads that file in the same ssh as the stderr
    tail; this is the ONE parse of its raw bytes into a verdict dict:

    * ``{"status": "absent"}`` — empty or the ``__HPC_NO_RUNTIME__`` sentinel (a
      preamble crash before the dispatcher wrote it — caught by other paths). The
      caller falls through to the unchanged success logic.
    * ``{"status": "unreadable", "detail": ...}`` — non-JSON / no ``exit_code`` /
      a non-int one (never mint a false failure from a read miss).
    * ``{"status": "present", "exit_code": int, "elapsed_sec": int | None}`` —
      parsed. ``elapsed_sec`` is the dispatcher's measured task wall-clock
      (``dispatch.py`` ~:1224), threaded out so the two-phase gate can size the
      main-array walltime against the MEASURED canary runtime
      (:func:`hpc_agent.ops.submit.canary_calibration.calibrate_array_walltime`).
      ``None`` when the field is absent / not a positive int — a read miss never
      mints a false measurement.
    """
    import json

    if not raw or raw == "__HPC_NO_RUNTIME__":
        return {"status": "absent"}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "unreadable", "detail": f"_runtime.json not JSON: {raw[:200]!r}"}
    if not isinstance(parsed, dict) or "exit_code" not in parsed:
        return {"status": "unreadable", "detail": f"_runtime.json has no exit_code: {parsed!r}"}
    try:
        exit_code = int(parsed["exit_code"])
    except (TypeError, ValueError):
        return {
            "status": "unreadable",
            "detail": f"_runtime.json exit_code not an int: {parsed.get('exit_code')!r}",
        }

    def _pos_int(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        return int(value)

    return {
        "status": "present",
        "exit_code": exit_code,
        "elapsed_sec": _pos_int(parsed.get("elapsed_sec")),
        # Peak resident memory (MB) the dispatcher measured for the reaped
        # executor (run-14 memory priors). None = not measured, never zero.
        "peak_rss_mb": _pos_int(parsed.get("peak_rss_mb")),
        # The task's recorded gpu_type — keys the per-gpu prior bucket.
        "gpu_type": (str(parsed.get("gpu_type")) if parsed.get("gpu_type") else None),
    }


def _resolve_canary_checkpoint_dir(
    sidecar: dict[str, Any],
    *,
    canary_run_id: str,
    explicit: str | None,
) -> str:
    """The canary task-0 result dir whose ``_checkpoints/`` the probe inspects.

    An explicit *explicit* wins. Otherwise derive it from the canary sidecar's
    ``result_dir_template`` rendered for task 0 (``{task_id}`` / ``{run_id}``) —
    the common case. A template that also references per-task kwargs cannot be
    rendered without ``tasks.resolve(0)``, so it raises
    :class:`errors.SpecInvalid` asking the caller to pass ``checkpoint_result_dir``
    explicitly (the same path it would pass for ``expect_output``).
    """
    if explicit:
        return explicit
    template = sidecar.get("result_dir_template")
    if not isinstance(template, str) or not template:
        raise errors.SpecInvalid(
            f"checkpoint verification for canary {canary_run_id!r} needs the canary's "
            "result dir, but its sidecar carries no result_dir_template and no explicit "
            "checkpoint_result_dir was passed."
        )
    try:
        rendered: str = template.format(task_id=0, run_id=canary_run_id)
        return rendered
    except (KeyError, IndexError) as exc:
        raise errors.SpecInvalid(
            f"cannot derive the canary checkpoint dir: result_dir_template "
            f"{template!r} references {exc} (a per-task kwarg) so it can't be "
            "rendered locally. Pass checkpoint_result_dir explicitly (the canary's "
            "task-0 result dir, relative to remote_path)."
        ) from None


# Section sentinels for the fused post-terminal verify-tail read. Chosen so they
# cannot collide with a log tail / JSON body / sha line.
_VTAIL_SECS = ("TAIL", "RUNTIME", "OUTPUT", "FPRINT")

# Positive-evidence ack for the fused tail (finding-24 sentinel-ack rule): the
# token proves the remote shell reached the END of the sectioned script, so a
# truncated stream can never be parsed as a shorter-but-valid section set.
_VTAIL_ACK_PREFIX = "__HPC_VTAIL_ACK__="


def _vtail_marker(name: str) -> str:
    return f"<<<HPC_VTAIL:{name}>>>"


def _split_vtail_sections(stdout: str) -> dict[str, str]:
    """Split the fused-tail stdout into ``{section_name: body}`` by its sentinels.

    A section absent from the output (its marker never printed — a truncated
    stream, or an optional section not requested) simply has no key; every caller
    treats a missing section exactly like the old "read miss" (fall through, never
    a false verdict), so a torn read degrades safely.
    """
    out: dict[str, str] = {}
    parts = stdout.split("<<<HPC_VTAIL:")
    for chunk in parts[1:]:
        head, _, body = chunk.partition(">>>\n")
        name = head.strip(">").strip()
        if name in _VTAIL_SECS:
            out[name] = body
    return out


def _fused_verify_tail(
    *,
    ssh_target: str,
    remote_path: str,
    job_name: str,
    job_ids: list[str],
    scheduler: str,
    result_dir: str | None,
    expect_output: str | None,
    fingerprint: str | None,
) -> dict[str, Any]:
    """Read the WHOLE post-terminal verify tail in ONE ssh round trip (RANK 18).

    Once the poll loop breaks terminal, verification used to issue a CHAIN of
    independent cold-SSH execs — stderr-tail fetch, ``cat _runtime.json``, an
    optional ``expect_output`` existence/JSON test, an optional ``sha256sum`` — each
    its own connection + handshake (no ControlMaster on native Windows; the broker
    was deleted), 6–12 round trips per gated submit under the double canary. Every
    byte is reachable in one shell, so fold them into ONE sentinel-sectioned
    ``bash`` script and parse it back into the SAME fields the verdict branches
    already consume. Verdict logic is unchanged — only the transport is batched.

    (The checkpoint round-trip probe stays its OWN ssh: it needs remote ``python``
    to deserialize a pickle, which no shell read can fold — and it is the rare
    ``auto_resume_on_kill`` path, not this default tail.)

    Returns ``{stderr_tail, log_path, runtime, output, fingerprint_sha}``:

    * ``stderr_tail`` / ``log_path`` — the task-0 log tail (``fetch_task_logs``
      semantics for a 1-task canary: try job_ids newest-first, first existing wins).
    * ``runtime`` — :func:`_parse_runtime_json`'s dict (``present``/``absent``/
      ``unreadable``) when *result_dir* is given, else ``None`` (skip the check).
    * ``output`` — ``verify_combiner_artifact``'s ``(ok, detail)`` when
      *expect_output* is given, else ``None``.
    * ``fingerprint_sha`` — the sha when *fingerprint* is given and read, else
      ``None`` (best-effort; a miss never fails the canary).

    A missing section (torn stream / optional section unrequested) falls through
    exactly like the per-call read miss it replaces — never a false verdict.
    """
    import json as _json
    import shlex

    from hpc_agent.infra import remote
    from hpc_agent.infra.backends import get_backend_class

    parts: list[str] = []

    # --- TAIL: task-0 stderr, fetch_task_logs semantics (newest job_id first) ---
    parts.append(f"echo {shlex.quote(_vtail_marker('TAIL'))}")
    backend_cls = get_backend_class(scheduler)
    candidates = [
        backend_cls.stderr_log_path(remote_path, job_name, jid, 0)
        for jid in reversed(job_ids or [])
    ]
    if candidates:
        arms = []
        for path in candidates:
            q = shlex.quote(path)
            arms.append(f"if [ -f {q} ]; then printf 'FOUND\\t%s\\n' {q}; tail -n 50 {q};")
        parts.append(" el".join(arms) + " else echo MISSING; fi")
    else:
        parts.append("echo MISSING")

    # --- RUNTIME: cat <result_dir>/_runtime.json (absence marker distinguishes
    #     a preamble crash from an unreadable file), only when a dir was resolved ---
    if result_dir is not None:
        rt = result_dir
        if not rt.startswith("/"):
            rt = f"{remote_path.rstrip('/')}/{rt.lstrip('/')}"
        runtime_path = f"{rt.rstrip('/')}/_runtime.json"
        parts.append(f"echo {shlex.quote(_vtail_marker('RUNTIME'))}")
        parts.append(f"cat {shlex.quote(runtime_path)} 2>/dev/null || echo __HPC_NO_RUNTIME__")

    # --- OUTPUT: expect_output existence (+ JSON parse for .json) ---
    if expect_output:
        full_path = f"{remote_path.rstrip('/')}/{expect_output.lstrip('/')}"
        parts.append(f"echo {shlex.quote(_vtail_marker('OUTPUT'))}")
        if expect_output.endswith(".json"):
            py_src = f"import json,sys; json.load(open({_json.dumps(full_path)}))"
            parts.append(
                f"if [ ! -f {shlex.quote(full_path)} ]; then echo MISSING; "
                f"else python3 -c {shlex.quote(py_src)} && echo OK || echo INVALID_JSON; fi"
            )
        else:
            parts.append(f"[ -f {shlex.quote(full_path)} ] && echo OK || echo MISSING")

    # --- FPRINT: sha256 of the fingerprint target (best-effort) ---
    if fingerprint:
        target = fingerprint
        if not target.startswith("/"):
            target = f"{remote_path.rstrip('/')}/{target.lstrip('/')}"
        parts.append(f"echo {shlex.quote(_vtail_marker('FPRINT'))}")
        parts.append(f"sha256sum {shlex.quote(target)} 2>/dev/null | awk '{{print $1}}'")

    from hpc_agent.infra.ssh_validation import split_ack, wrap_with_ack

    script = wrap_with_ack("\n".join(parts), _VTAIL_ACK_PREFIX)
    stderr_tail = ""
    log_path: str | None = None
    runtime: dict[str, Any] | None = {"status": "absent"} if result_dir is not None else None
    output: tuple[bool, str] | None = None
    fingerprint_sha: str | None = None
    try:
        proc = remote.ssh_run(script, ssh_target=ssh_target)
    except (errors.RemoteCommandFailed, OSError):
        # A torn/failed tail read never mints a false verdict: an empty tail +
        # runtime "absent" fall through to the unchanged success logic, exactly
        # like the old per-call miss. Output/fingerprint stay unknown (None).
        return {
            "stderr_tail": stderr_tail,
            "log_path": log_path,
            "runtime": runtime,
            "output": output,
            "fingerprint_sha": fingerprint_sha,
        }
    clean, ack_rc = split_ack(proc.stdout or "", _VTAIL_ACK_PREFIX)
    if ack_rc is None:
        # No ack token = the remote shell never reached the end of the script
        # (severed/truncated channel, finding-24 class). Treat exactly like the
        # failed-read branch above: fall through with miss-shaped fields rather
        # than parsing a possibly-torn stream (a torn RUNTIME section could mask
        # a nonzero canary exit).
        return {
            "stderr_tail": stderr_tail,
            "log_path": log_path,
            "runtime": runtime,
            "output": output,
            "fingerprint_sha": fingerprint_sha,
        }
    secs = _split_vtail_sections(clean) if proc.returncode == 0 else {}

    tail_body = secs.get("TAIL", "")
    first_line, _, rest = tail_body.partition("\n")
    if first_line.startswith("FOUND\t"):
        log_path = first_line[len("FOUND\t") :].strip() or None
        # Drop the trailing newline the section separator adds, mirroring the
        # per-call tail (content is everything after the FOUND line).
        stderr_tail = rest[:-1] if rest.endswith("\n") else rest

    if result_dir is not None and "RUNTIME" in secs:
        runtime = _parse_runtime_json(secs["RUNTIME"].strip())

    if expect_output and "OUTPUT" in secs:
        tok = secs["OUTPUT"].strip().splitlines()[-1] if secs["OUTPUT"].strip() else ""
        full_path = f"{remote_path.rstrip('/')}/{expect_output.lstrip('/')}"
        if tok == "OK":
            output = (True, "ok")
        elif tok == "MISSING":
            output = (False, f"is missing at {full_path}")
        elif tok == "INVALID_JSON":
            output = (False, f"at {full_path} is not valid JSON")
        else:
            output = (False, f"unrecognised verifier output: {tok[:200]!r}")

    if fingerprint and "FPRINT" in secs:
        fingerprint_sha = secs["FPRINT"].strip() or None

    return {
        "stderr_tail": stderr_tail,
        "log_path": log_path,
        "runtime": runtime,
        "output": output,
        "fingerprint_sha": fingerprint_sha,
    }


def _light_liveness_summary(
    *,
    record: Any,
    backend_cls: type[HPCBackend],
    canary_run_id: str,
) -> dict[str, int]:
    """LIGHT per-poll liveness for the canary — NO cluster-side python reporter.

    The canary poll loop used to run the FULL status reporter every tick
    (``ssh_status_report`` → ``python -m …reduce.status``, a per-task result-file
    walk under the run's conda env) — a heavy cluster-side python invocation on
    the critical path of every fresh submit. But the lifecycle verdict a poll
    tick needs is only "is the job still alive, and did its task(s) reach a
    terminal marker" — both answerable WITHOUT the run's python:

    * :func:`hpc_agent.ops.monitor.announce.read_announcements` — a bare ``ls`` of
      the dispatcher's per-task terminal markers (``complete``/``failed``),
      present the instant the array starts (crash-only-monitoring Phase 1). When
      present it is the FAST terminal signal (``missing == 0`` ⇒ every task
      announced), mapped to the canonical 5-key summary exactly as
      :func:`hpc_agent.ops.monitor_flow._announce_status` does (an unannounced
      task ⇒ ``pending`` so a PARTIAL census stays in-flight).
    * :func:`hpc_agent.infra.cluster_status.ssh_batch_scheduler_states` — ONE
      ``qstat``/``squeue`` for liveness before the dispatcher has announced (a
      still-queued run), folded via ``backend_cls.batch_status``. A job absent
      from the live queue is terminal-by-absence (all-zero here → the caller's
      vanished-grace arm), never a ``complete`` (``batch_status`` never emits it).

    Returns the canonical 5-key ``{complete, failed, running, pending, unknown}``
    the poll loop classifies through :func:`classify.classify_polling` (the ONE
    count→verdict definition). This light read only decides WHEN to break; the
    AUTHORITATIVE terminal summary is the ONE heavy reporter the caller runs once
    the loop is terminal (:func:`_terminal_status_report`). Both light reads use
    plain ``sh`` — a broken run env (rc 126/127) / a torn-sidecar reporter fault
    can only surface at that single terminal reporter, where the finding-12/7
    escalation now fires (:func:`_terminal_reporter_verdict`).
    """
    from hpc_agent._kernel.contract.vocabulary import TaskStatus
    from hpc_agent.infra.cluster_status import ssh_batch_scheduler_states
    from hpc_agent.ops.monitor.announce import read_announcements

    census = read_announcements(
        ssh_target=resolve_ssh_target(record),
        remote_path=record.remote_path,
        run_id=canary_run_id,
        task_count=int(record.total_tasks),
    )
    if census.get("present"):
        return {
            "complete": int(census["complete"]),
            "failed": int(census["failed"]),
            "running": 0,
            "pending": int(census["missing"]),
            "unknown": 0,
        }
    # No announce dir yet — ONE scheduler-state query for liveness. A completed
    # job has already left the queue (``batch_status`` never emits ``complete``),
    # so an all-gone fold is all-zero here and the caller's vanished-grace arm
    # owns the "job left the queue" verdict.
    states = ssh_batch_scheduler_states(
        ssh_target=resolve_ssh_target(record),
        backend_cls=backend_cls,
        job_ids=list(record.job_ids),
    )
    folded = backend_cls.batch_status(states)
    running = sum(1 for v in folded.values() if v == TaskStatus.RUNNING.value)
    pending = sum(1 for v in folded.values() if v == TaskStatus.PENDING.value)
    failed = sum(1 for v in folded.values() if v == TaskStatus.FAILED.value)
    return {
        "complete": 0,
        "failed": failed,
        "running": running,
        "pending": pending,
        "unknown": 0,
    }


def _terminal_status_report(
    *,
    record: Any,
    remote_activation: str,
    log_dir: str,
    file_glob: str,
    canary_run_id: str,
) -> dict[str, Any]:
    """Run the ONE heavy status reporter at terminal, G1 breaker-wrapped.

    The liveness split runs the FULL cluster-side reporter exactly ONCE — here,
    after the light poll loop has broken terminal — to read the authoritative
    per-task summary the verdict rests on. G1 (the e79edc2c terminal-harvest
    pattern): a transient breaker trip (:class:`~hpc_agent.errors.SshCircuitOpen`)
    at this single call must not lose a finished job's verdict, so we wait out one
    BASE cooldown (the breaker names its own deadline via ``exc.deadline``) and
    retry ONCE — the sanctioned half-open probe, not the hammering the breaker
    forbids. A missing/doubled-cooldown deadline (the probe already failed, the
    host is genuinely unhealthy) re-raises, exactly as
    :func:`hpc_agent.ops.monitor.harvest_guard.harvest_on_terminal` does; the
    caller then routes it to ``reporter_unreachable``.
    """
    from hpc_agent.infra.cluster_status import ssh_status_report
    from hpc_agent.ops.monitor.harvest_guard import _circuit_wait_sec

    def _report() -> dict[str, Any]:
        return ssh_status_report(
            ssh_target=resolve_ssh_target(record),
            remote_path=record.remote_path,
            run_id=canary_run_id,
            job_ids=list(record.job_ids),
            job_name=record.job_name,
            log_dir=log_dir,
            file_glob=file_glob,
            remote_activation=remote_activation,
        )

    try:
        return _report()
    except errors.SshCircuitOpen as exc:
        wait = _circuit_wait_sec(exc, now=time.monotonic())
        if wait is None:
            raise
        time.sleep(wait)
        return _report()


def _terminal_reporter_verdict(
    exc: Exception,
    *,
    record: Any,
    canary_run_id: str,
) -> dict[str, Any]:
    """Escalated verdict when the ONE terminal status reporter raises (finding 12/7).

    The liveness split moved the poll-failure escalation from a per-poll streak to
    this single terminal call — the light poll loop can't produce rc 126/127 (it
    never runs the run's python), so a broken run env / a torn-or-never-shipped
    sidecar reporter fault can only surface HERE. Same three classes, same
    loud+fast (never full-budget) posture, same envelopes as the retired per-poll
    escalation:

    * deterministic broken env (rc 126/127) → the env-independent ``.hpc_failed``
      marker scan (plain ``sh``, survives the broken env): present ⇒ positive
      ``canary_failed``; absent ⇒ ``reporter_unreachable`` (never-pass-unverified).
    * a DETERMINISTIC structured reporter fault (a never-shipped/torn sidecar,
      ``sidecar_not_found`` &c.) → the sidecar-disclosing ``reporter_unreachable``
      envelope (finding 7).
    * anything else transient → ``reporter_unreachable``: the reporter never
      returned a readable status, so the run cannot be trusted as passed.
    """
    failure_class = _classify_poll_failure(exc)
    rc = exc.returncode if isinstance(exc, errors.RemoteCommandFailed) else None
    reporter_code = exc.reporter_error_code if isinstance(exc, errors.RemoteCommandFailed) else None
    if failure_class == "deterministic_reporter" and reporter_code is not None:
        return _deterministic_reporter_envelope(
            canary_run_id,
            reporter_code=reporter_code,
            remote_path=record.remote_path,
            ssh_target=resolve_ssh_target(record),
        )
    if failure_class == "deterministic_env":
        from hpc_agent.infra.cluster_status import ssh_marker_scan

        try:
            scan = ssh_marker_scan(
                ssh_target=resolve_ssh_target(record),
                remote_path=record.remote_path,
                run_id=canary_run_id,
            )
        except (errors.RemoteCommandFailed, OSError):
            scan = {"failed_markers": [], "count": 0}
        if int(scan.get("count") or 0) > 0:
            return _canary_failed_envelope(
                canary_run_id,
                markers=list(scan.get("failed_markers") or []),
                returncode=rc,
            )
        return _reporter_unreachable_envelope(
            canary_run_id,
            exc,
            annotation=(
                f"The terminal status reporter failed rc={rc} (the "
                "command-not-found/not-executable broken-env signature); no "
                ".hpc_failed marker was found for this run, so failure could not "
                "be positively confirmed either — the canary is unverifiable, "
                "not passed."
            ),
        )
    return _reporter_unreachable_envelope(canary_run_id, exc)


@primitive(
    name="verify-canary",
    verb="workflow",
    composes=["poll-run-status"],
    side_effects=[
        SideEffect("ssh", "<cluster> (poll status + tail stderr)"),
    ],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable, errors.SubmissionIncomplete],
    idempotent=True,
    idempotency_key="canary_run_id",
    cli=CliShape(
        help=(
            "Wait + grep + output-check for a 1-task canary submission. "
            "Polls until terminal, scans stderr for known failure markers, "
            "optionally checks expect_output exists. Returns "
            "{ok, failure_kind, details, stderr_tail}."
        ),
        experiment_dir_arg=True,
        requires_ssh=True,
        args=(
            CliArg(
                "--canary-run-id",
                type=str,
                required=True,
                help="Run ID of the canary (typically <main_run_id>-canary).",
            ),
            CliArg(
                "--expect-output",
                type=str,
                default=None,
                help="Optional path (relative to remote_path) the canary should have written.",
            ),
            CliArg(
                "--fingerprint",
                type=str,
                default=None,
                help=(
                    "Optional remote path (relative to result_dir or absolute) to "
                    "SHA256 over SSH; hex digest returned as data.metrics_fingerprint."
                ),
            ),
            CliArg(
                "--verify-checkpoint",
                action="store_true",
                help=(
                    "Checkpoint canary (#294 PR4): the canary wrote a checkpoint then "
                    "killed itself; assert a restorable checkpoint survived under the "
                    "stable _checkpoints/ dir. Format-aware: pickle checkpoints are "
                    "fully reloaded on the cluster; adapter formats (petsc_binary) are "
                    "verified structurally. Replaces the exit-0/output success "
                    "criteria — a preempted canary is EXPECTED. Use for "
                    "auto_resume_on_kill runs."
                ),
            ),
            CliArg(
                "--checkpoint-result-dir",
                type=str,
                default=None,
                help=(
                    "Canary task-0 result dir (relative to remote_path or absolute) "
                    "whose _checkpoints/ the round-trip probe inspects. Omit to derive "
                    "it from the canary sidecar's result_dir_template (works unless the "
                    "template references per-task kwargs). Only used with "
                    "--verify-checkpoint."
                ),
            ),
            CliArg(
                "--poll-interval-sec",
                type=int,
                default=30,
                help=(
                    "Steady-state ceiling (seconds) between status polls (default 30). "
                    "The loop fast-starts below this (HPC_CANARY_FAST_POLL_SEC, default "
                    "3s) and ramps up to it, so a canary that lands in seconds is caught "
                    "immediately without dead-waiting a full interval."
                ),
            ),
            CliArg(
                "--wait-budget-sec",
                type=int,
                default=1800,
                help="Total seconds to wait for terminal before giving up (default 1800).",
            ),
        ),
    ),
    agent_facing=True,
)
def verify_canary(
    experiment_dir: Path,
    *,
    canary_run_id: str,
    expect_output: str | None = None,
    fingerprint: str | None = None,
    verify_checkpoint: bool = False,
    checkpoint_result_dir: str | None = None,
    poll_interval_sec: int = _DEFAULT_POLL_INTERVAL_SEC,
    wait_budget_sec: int = _DEFAULT_WAIT_BUDGET_SEC,
    log_dir: str = "logs",
    file_glob: str = "*",
) -> dict[str, Any]:
    """Wait for the canary to land terminal, then verify exit + outputs.

    Parameters
    ----------
    canary_run_id:
        Run ID of the 1-task canary submission. Lookup happens via the
        journal — the run record carries ``ssh_target``, ``remote_path``,
        ``job_ids``, ``job_name`` for the SSH polls.
    expect_output:
        Path (relative to ``remote_path`` or absolute) the canary
        should have written. Verified via SSH + ``[ -f ... ]``. ``None``
        skips the output check; the exit-code + log scan still run.
    fingerprint:
        Relative path (under the canary's result_dir) of a file to
        SHA256 over SSH. The hex digest is returned as
        ``data.metrics_fingerprint``. Lets the caller diff the canary
        output against a local reference run of the same task to detect
        framework-induced divergence (different GPU SKU, library drift,
        env-var collision). ``None`` skips the fingerprint.
    verify_checkpoint:
        Checkpoint-canary mode (#294 PR4). When True, the canary was fired with
        ``HPC_CHECKPOINT_CANARY=1`` so it wrote a checkpoint at iteration 1 then
        killed itself at iteration 2 via the dispatcher's SIGTERM path. This
        SWAPS the success criteria: instead of "exit 0 + output present" (a
        preempted canary never completes), the gate passes iff a *restorable*
        checkpoint survived under the canary's ``_checkpoints/`` dir — proven by
        running ``checkpoint_formats.describe_latest_checkpoint`` on the
        cluster. Format-aware: a pickle checkpoint is fully deserialized
        (level ``loadable``); an adapter format like ``petsc_binary`` is
        verified structurally (level ``structural`` — the Vec class-id/block
        walk, since loading would need the solver library). Fails the gate with
        ``checkpoint_missing`` (no checkpoint written) or ``checkpoint_unloadable``
        (a wrong/non-portable checkpoint format) so the "my checkpoint can't be
        reloaded" class is caught BEFORE the long main array launches. The
        reporter_unreachable / timeout poll-failure paths still apply.
    checkpoint_result_dir:
        Only with *verify_checkpoint*. The canary task-0 result dir (relative to
        ``remote_path`` or absolute) whose ``_checkpoints/`` the round-trip probe
        inspects. ``None`` derives it from the canary sidecar's
        ``result_dir_template`` (task 0) — pass it explicitly when the template
        references per-task kwargs that can't be rendered locally.
    poll_interval_sec, wait_budget_sec:
        Adaptive poll knobs. ``poll_interval_sec`` is the steady-state
        *ceiling*: the loop fast-starts at ``HPC_CANARY_FAST_POLL_SEC``
        (default 3s) and ramps geometrically up to it, so a canary that
        lands in a few seconds is observed immediately instead of
        dead-waiting a full interval (the dominant submit-pipeline latency
        for a fresh ``cmd_sha`` the canary cache can't skip). Exits early
        once the canary is terminal; otherwise gives up after
        *wait_budget_sec* with ``failure_kind="timeout"``.
    log_dir, file_glob:
        Threaded through to the cluster-side status reporter so it
        finds the canary's stderr log.

    Returns
    -------
    ``{ok, failure_kind, details, stderr_tail}``:

    * ``ok=True`` iff the canary exited 0, no known failure markers in
      stderr, and the expected output (if any) exists.
    * ``failure_kind`` is None on success; otherwise one of
      ``"dispatcher_failed"`` / ``"import_error"`` / ``"module_not_found"`` /
      ``"traceback"`` / ``"oom_killed"`` / ``"segfault"`` /
      ``"nonzero_exit"`` (the canary task-0 ``_runtime.json`` recorded a
      non-zero ``exit_code`` even though it wrote a result file — #351-3) /
      ``"output_contract"`` (dispatcher exit 4: executor exited 0 but wrote
      nothing into ``$HPC_RESULT_DIR`` — run-#10 finding; deterministic,
      never retried) / ``"no_runner"`` (dispatcher exit 3: empty or
      self-recursive executor) /
      ``"missing_output"`` / ``"reporter_unreachable"`` (every status poll
      failed — broken cluster-side reporter; also the fast-escalation verdict
      when K consecutive deterministic broken-env polls (rc 126/127) found NO
      ``.hpc_failed`` marker to confirm failure — finding 12) /
      ``"canary_failed"`` (the same K-poll broken-env escalation, but the
      env-independent ``.hpc_failed`` marker scan positively proved the task ran
      and failed — finding 12) / ``"completed_unknown"`` (the
      job left the scheduler queue without recording a completion and no
      stderr marker explains why — resolved fast instead of timing out) /
      ``"timeout"`` / ``"abandoned"`` / (checkpoint mode) ``"checkpoint_missing"``
      / ``"checkpoint_unloadable"``.
    * ``details`` is a one-line human-readable summary the slash
      command can surface to the user above the raw stderr_tail.
    * ``stderr_tail`` is the last 50 lines of the canary's stderr
      log (or empty when not retrievable).

    Raises :class:`errors.SpecInvalid` on missing journal record or
    bad knobs; :class:`errors.SshUnreachable` on persistent SSH
    failure (the budget elapsed in failed polls).
    """
    if not canary_run_id:
        raise errors.SpecInvalid("canary_run_id is required")
    if int(poll_interval_sec) <= 0:
        raise errors.SpecInvalid("poll_interval_sec must be > 0")
    if int(wait_budget_sec) <= 0:
        raise errors.SpecInvalid("wait_budget_sec must be > 0")
    if expect_output and canary_run_id not in expect_output:
        # The canary writes its output under ``results/<canary_run_id>/...``
        # (the ``-canary`` suffix is part of the run_id). An ``expect_output``
        # built for the MAIN run_id — or copied from a literal example like
        # ``results/seed_42/metrics.json`` — can never match, so the check
        # would report ``missing_output`` for a canary that actually passed
        # and silently gate the main array. Refuse it at the boundary rather
        # than let a divined path mint a false negative (the per-task
        # completion count already verifies the canary produced output).
        raise errors.SpecInvalid(
            f"expect_output {expect_output!r} does not reference the canary run_id "
            f"{canary_run_id!r}: the canary writes under results/{canary_run_id}/... (note "
            f"the '-canary' suffix), so a path built for the main run_id or a literal example "
            f"like 'results/seed_42/metrics.json' can never match and would falsely fail a "
            f"canary that succeeded. Omit expect_output (the completion count already verifies "
            f"the canary's output) or pass the canary's real result path."
        )

    from hpc_agent.infra.clusters import remote_activation_for_sidecar
    from hpc_agent.ops.monitor.classify import classify_polling
    from hpc_agent.state.journal import (
        clear_poll_health,
        load_run,
        stamp_poll_health,
        stamp_watchdog_tick,
    )
    from hpc_agent.state.runs import read_run_sidecar

    record = load_run(experiment_dir, canary_run_id)
    if record is None:
        raise errors.SpecInvalid(f"no journal record for canary run_id={canary_run_id!r}")
    if not record.job_ids:
        # The qsub call structurally succeeded (a sidecar exists) but
        # cluster-side init crashed before ``job_ids`` got populated, so
        # we cannot poll scheduler state at all. Without this guard the
        # poll loop silently classifies the canary as "abandoned" (the gap
        # `errors.SubmissionIncomplete` closes; see
        # `docs/proposals/recovery-registry.md`, the `submission_incomplete`
        # section), masking a structural submission failure as a recoverable one.
        # The registry-backed remediation routes the operator at the
        # sidecar + cluster-side logs to find the real failure.
        raise errors.SubmissionIncomplete(
            f"canary run_id={canary_run_id!r} has no job_ids in its run record — "
            "the qsub/sbatch call structurally succeeded but cluster-side init "
            "crashed before the sidecar was fully populated; scheduler state "
            "cannot be polled. Inspect the run sidecar and cluster-side logs "
            "to find the precise failure.",
            run_id=canary_run_id,
            experiment_dir=str(experiment_dir),
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
        )
    if int(record.total_tasks) <= 0:
        # A 0-task canary can never satisfy `complete >= total_tasks > 0`,
        # so the poll loop would always exit via timeout. Reject up-front
        # with a clearer error.
        raise errors.SpecInvalid(
            f"canary run_id={canary_run_id!r} has total_tasks={record.total_tasks}; "
            "a canary must have at least 1 task"
        )

    # The control-plane status reporter runs on the login node via ssh —
    # it needs the run's conda activation, else it falls to the bare
    # login-node python that lacks ``hpc_agent``, every poll raises, and
    # the loop reports ``reporter_unreachable`` instead of the canary's
    # real status (#176). Derive the activation from the canary's sidecar
    # (cluster + resolved env) exactly as ``ops/monitor/status.py`` does
    # for the normal status path. Best-effort: an unreadable sidecar
    # yields ``""`` → bare python (the unchanged fallback).
    try:
        _canary_sidecar = read_run_sidecar(experiment_dir, canary_run_id)
    except (OSError, ValueError, errors.HpcError):
        _canary_sidecar = {}
    # The canary sidecar this flow writes carries neither ``env`` nor
    # ``cluster`` (run #7 live: activation derived to "" → bare login-node
    # ``python`` → ``No module named hpc_agent`` → rc=1 every poll, riding the
    # full wait budget). The deriver's cluster-backfill arm (#281) only fires
    # when the sidecar names a cluster — seed it from the journal record,
    # which always knows, so the sidecar's own pins still win per-field.
    if not _canary_sidecar.get("cluster") and record.cluster:
        _canary_sidecar["cluster"] = record.cluster
    remote_activation = remote_activation_for_sidecar(_canary_sidecar)

    # Resolve the scheduler UP FRONT (moved above the loop): the LIGHT liveness
    # poll needs the backend to fold ``ssh_batch_scheduler_states`` tokens, and
    # the post-terminal stderr fetch needs it too. Substring-matching the cluster
    # name misroutes any cluster whose name lacks "slurm" (discovery, hoffman2,
    # cascade, …) to the SGE template, so resolve from clusters.yaml or refuse.
    from hpc_agent.infra.backends import get_backend_class
    from hpc_agent.infra.clusters import load_clusters_config

    try:
        clusters_cfg = load_clusters_config()
    except Exception:  # noqa: BLE001
        clusters_cfg = {}
    scheduler = (clusters_cfg.get(record.cluster) or {}).get("scheduler")
    if not scheduler:
        raise errors.SpecInvalid(
            f"cannot resolve scheduler for canary cluster {record.cluster!r}: "
            f"absent from clusters.yaml or missing a 'scheduler' key — refusing "
            f"to guess 'slurm' and risk misrouting the SGE log fetch"
        )
    backend_cls = get_backend_class(scheduler)

    deadline = time.monotonic() + int(wait_budget_sec)
    last_summary: dict[str, Any] = {}
    last_poll_error: Exception | None = None
    got_report = False
    # A vanished canary (finished/failed fast and left the scheduler queue
    # before we polled) shows an all-zero LIVE summary: no result file yet
    # (complete=0), and the scheduler no longer lists the job so it adds
    # nothing to running/pending/failed. That is NOT terminal on its own —
    # it also describes the transient window right after qsub, before the
    # scheduler registers the array. So we require the all-zero state to
    # PERSIST across consecutive polls before declaring the job gone, rather
    # than riding the full wait_budget_sec polling an absent job (#193).
    vanished_polls = 0
    _VANISHED_POLLS_TO_TERMINAL = 2
    # The vanished verdict also requires the all-zero state to SPAN at least this
    # much wall-clock, not just N consecutive polls. The 2-poll heuristic relied
    # on polls being ``poll_interval_sec`` apart (≈30s) to give the scheduler
    # time to register the array after qsub; the fast-start ramp below now spaces
    # the first polls only seconds apart, so without a time floor a canary whose
    # array is merely slow to appear in qstat (all-zero for the first few rapid
    # polls) would be falsely declared ``completed_unknown`` and wrongly fail the
    # gate. Tying the grace to ``poll_interval_sec`` reproduces the original
    # timing (the two confirming polls used to be exactly that far apart).
    _vanished_grace_sec = float(poll_interval_sec)
    first_vanished_at: float | None = None
    job_vanished = False
    # Adaptive fast-start: poll quickly at first (so a canary that lands in a
    # few seconds is caught immediately) and ramp toward ``poll_interval_sec``,
    # which stays the steady-state ceiling. See ``_CANARY_FAST_POLL_SEC``.
    _poll_ceiling = float(poll_interval_sec)
    effective_poll = _initial_poll_interval(poll_interval_sec)
    # Finding 12 poll-loop honesty: the LIGHT poll (announce-census / one qstat)
    # can only fail transiently — the deterministic broken-env (rc 126/127) /
    # torn-sidecar escalation moved to the ONE terminal reporter below
    # (_terminal_reporter_verdict). Liveness is still stamped every poll so the
    # sidecar never freezes at its submit stamp; the watchdog stamp is floored
    # (≥ _WATCHDOG_STAMP_FLOOR_SEC) so the fast-start ramp doesn't churn the
    # sidecar lock every 3s, and transient poll-failure EVIDENCE (error class +
    # rc) is stamped under last_status.poll_health so status-snapshot shows a
    # live-but-struggling poller instead of a frozen timestamp.
    poll_health_dirty = False
    last_watchdog_stamp = float("-inf")
    # No `while...else`: the budget-timeout / reporter-unreachable arm sits at the
    # loop head (`if now >= deadline`). A terminal ``break`` falls through to the
    # ONE heavy status reporter + the post-loop stderr fetch.
    while True:
        now = time.monotonic()
        if now >= deadline:
            # Budget elapsed. Distinguish a broken reporter (EVERY poll raised, we
            # never got a single status read) from a genuine slow/stuck run (polls
            # succeeded but the run never went terminal). A broken reporter must
            # fail the canary LOUDLY with the real cause — otherwise it masquerades
            # as a timeout, the agent retries, and the main array submits against a
            # cluster whose results can't even be read. This is exactly the failure
            # mode where 8 tasks die on a module/env error but the canary "passes"
            # because verification couldn't run.
            if not got_report and last_poll_error is not None:
                return _reporter_unreachable_envelope(canary_run_id, last_poll_error)
            return {
                "ok": False,
                "failure_kind": "timeout",
                "details": (
                    f"canary {canary_run_id!r} did not terminate within "
                    f"{wait_budget_sec}s (last summary: {last_summary})"
                ),
                "stderr_tail": "",
                "metrics_fingerprint": None,
                "failure_features": _failure_features("", None),
            }
        # §5 watchdog liveness — floored so the 3s fast-start ramp can't churn the
        # sidecar lock every poll. ONE shared tick definition (state.journal), the
        # same one the monitor poll loop routes through.
        if now - last_watchdog_stamp >= _WATCHDOG_STAMP_FLOOR_SEC:
            stamp_watchdog_tick(
                canary_run_id, next_tick_seconds=effective_poll, experiment_dir=experiment_dir
            )
            last_watchdog_stamp = now
        try:
            summary = _light_liveness_summary(
                record=record, backend_cls=backend_cls, canary_run_id=canary_run_id
            )
        except errors.TRANSIENT_TRANSPORT_ERRORS as exc:
            # The LIGHT poll (a bare ``ls`` announce-census / one ``qstat``) never
            # runs the run's python, so it can only fail TRANSIENTLY — a broken run
            # env (rc 126/127) or a torn/never-shipped sidecar reporter fault
            # surfaces only at the ONE terminal reporter below
            # (_terminal_reporter_verdict), never here. A transient blip rides the
            # wait budget (that class belongs to the connection breaker); the
            # budget-timeout arm above then returns reporter_unreachable.
            # SshCircuitOpen (an HpcError, NOT an OSError) is in
            # TRANSIENT_TRANSPORT_ERRORS, so an open-circuit poll rides the budget
            # too instead of crashing the gate with an undeclared exception. Stamp
            # the evidence under the DISTINCT poll_health key so status-snapshot
            # shows a live-but-struggling poller, not a frozen submit-time stamp.
            last_poll_error = exc
            failure_class = _classify_poll_failure(exc)
            rc = exc.returncode if isinstance(exc, errors.RemoteCommandFailed) else None
            stamp_poll_health(
                canary_run_id,
                error_class=failure_class,
                consecutive=1,
                returncode=rc,
                experiment_dir=experiment_dir,
            )
            poll_health_dirty = True
            time.sleep(effective_poll)
            effective_poll = _next_poll_interval(effective_poll, _poll_ceiling)
            continue
        got_report = True
        if poll_health_dirty:
            # A poll succeeded after prior failures — drop the stale evidence so
            # the sidecar no longer reads "polling, rc=127".
            clear_poll_health(canary_run_id, experiment_dir=experiment_dir)
            poll_health_dirty = False
        last_summary = dict(summary)
        complete = int(last_summary.get("complete") or 0)
        failed = int(last_summary.get("failed") or 0)
        running = int(last_summary.get("running") or 0)
        pending = int(last_summary.get("pending") or 0)
        # Terminal detection routed through the ONE count→verdict definition
        # (classify.classify_polling): COMPLETE (complete >= total) or FAILED (no
        # live work + a failure) from the LIGHT census. ``None`` = keep polling.
        # The AUTHORITATIVE terminal summary comes from the ONE heavy reporter run
        # after the loop breaks (_terminal_status_report), not this light read.
        state, _reason = classify_polling(summary, int(record.total_tasks))
        if state is not None:
            break
        # Job absent from the scheduler's live view: no task is complete, failed,
        # running, or pending. In the LIGHT poll this is an all-zero summary: no
        # announce dir yet (census absent) AND the job is gone from the live
        # scheduler queue (``batch_status`` emitted nothing). Count consecutive
        # such polls; once they persist past the registration grace, the canary
        # finished (or died) and left the queue — break fast as ``completed_unknown``
        # rather than time out. The ONE terminal reporter + the stderr scan below
        # still run, so a real failure marker (OOM, traceback) or a late-appearing
        # completion is preferred over the bland unknown verdict.
        #
        # The light path has no ``unknown`` bucket (the reduce-reporter's F-L
        # over-bucketing of a gone 1-task canary as ``unknown == 1`` can't recur
        # here — a job absent from qstat simply contributes nothing to the fold),
        # so "all quiet ⇒ job left the queue" is the direct signal; the
        # registration grace still distinguishes the pre-qstat startup window
        # (also all-zero, but resets the moment a task shows running/pending) from
        # a job that truly left the queue.
        if complete == 0 and failed == 0 and running == 0 and pending == 0:
            vanished_polls += 1
            if first_vanished_at is None:
                first_vanished_at = time.monotonic()
            # Require BOTH enough consecutive all-zero polls AND that they span
            # the registration grace, so the fast-start ramp can't trip this
            # before the scheduler has had ``poll_interval_sec`` to list the job.
            spanned = time.monotonic() - first_vanished_at
            if vanished_polls >= _VANISHED_POLLS_TO_TERMINAL and spanned >= _vanished_grace_sec:
                job_vanished = True
                break
        else:
            vanished_polls = 0
            first_vanished_at = None
        time.sleep(effective_poll)
        effective_poll = _next_poll_interval(effective_poll, _poll_ceiling)

    # ── ONE heavy status reporter at terminal (the liveness split) ──
    # The poll loop above issued only LIGHT liveness reads (announce-census / one
    # qstat, no cluster-side python). Now that the run is terminal, read the
    # AUTHORITATIVE per-task summary the verdict rests on with the FULL reporter —
    # exactly ONCE. A DETERMINISTIC reporter fault (rc 126/127 broken env, a
    # torn/never-shipped sidecar) can only surface here (the light reads never run
    # the run's python) and escalates loud+fast via _terminal_reporter_verdict —
    # the finding-12/finding-7 escalation, moved to this single call, never a
    # full-budget spin. G1: a transient breaker trip is waited out + retried once
    # inside _terminal_status_report so a finished job's verdict is never lost.
    try:
        report = _terminal_status_report(
            record=record,
            remote_activation=remote_activation,
            log_dir=log_dir,
            file_glob=file_glob,
            canary_run_id=canary_run_id,
        )
    except errors.SshCircuitOpen as exc:
        # G1 gave up (no deadline, or a doubled cooldown — the host is genuinely
        # unhealthy): a finished job we cannot read is reporter_unreachable, loud.
        return _reporter_unreachable_envelope(canary_run_id, exc)
    except errors.TRANSIENT_TRANSPORT_ERRORS as exc:
        return _terminal_reporter_verdict(exc, record=record, canary_run_id=canary_run_id)
    last_summary = dict(report.get("summary") or {})

    # Fetch the canary's stderr tail (1 task, task_id=0) — scheduler resolved above.
    # RANK 18: fold the WHOLE post-terminal verify tail — stderr tail + cat
    # _runtime.json + optional expect_output existence + optional sha256 — into ONE
    # ssh round trip (:func:`_fused_verify_tail`) instead of 3–6 serial cold-SSH
    # execs. The verdict branches below read from the pre-fetched bundle unchanged.
    # A checkpoint canary needs only the tail here (its exit-code/output/fingerprint
    # legs don't apply — it returns early on the checkpoint probe, a separate ssh
    # that no shell read can fold), so its fused call carries only the TAIL section.
    # The exit-code result dir is resolved from the sidecar template (task 0); an
    # unrenderable template → None → the fused read skips the runtime section and
    # the verdict falls through exactly as when _runtime.json is absent.
    _exit_result_dir: str | None = None
    if not verify_checkpoint:
        try:
            _exit_result_dir = _resolve_canary_checkpoint_dir(
                _canary_sidecar, canary_run_id=canary_run_id, explicit=checkpoint_result_dir
            )
        except errors.SpecInvalid:
            _exit_result_dir = None
    tail_bundle = _fused_verify_tail(
        ssh_target=resolve_ssh_target(record),
        remote_path=record.remote_path,
        job_name=record.job_name,
        job_ids=list(record.job_ids),
        scheduler=scheduler,
        result_dir=None if verify_checkpoint else _exit_result_dir,
        expect_output=None if verify_checkpoint else expect_output,
        fingerprint=None if verify_checkpoint else fingerprint,
    )
    stderr_tail = str(tail_bundle["stderr_tail"] or "")
    log_path: str | None = tail_bundle["log_path"]

    # Checkpoint canary (#294 PR4): SWAP the success criteria. The canary was
    # SUPPOSED to be preempted (exit 130) after writing one checkpoint, so the
    # exit-0/marker/output assertions below don't apply — a preempted dispatch is
    # the expected outcome, not a failure. The ONLY thing that matters is that a
    # loadable checkpoint survived under the stable _checkpoints/ dir. Run the
    # round-trip probe on the cluster (where a resume would reload it) and gate on
    # it directly. A genuine executor crash before iteration 1 leaves no
    # checkpoint → checkpoint_missing, with the real stderr classified into
    # failure_features for the operator.
    if verify_checkpoint:
        ckpt_dir = _resolve_canary_checkpoint_dir(
            _canary_sidecar, canary_run_id=canary_run_id, explicit=checkpoint_result_dir
        )
        probe = _verify_remote_checkpoint(
            ssh_target=resolve_ssh_target(record),
            remote_path=record.remote_path,
            ckpt_result_dir=ckpt_dir,
            remote_activation=remote_activation,
        )
        status = probe.get("status")
        if status == "ok":
            # Format-aware detail: the pickle format proves a full reload
            # (next_iteration present); adapter formats (e.g. petsc_binary)
            # verify structurally — say which proof the verdict rests on.
            fmt = probe.get("format") or "pickle"
            nxt = probe.get("next_iteration")
            proof = (
                f"resumes at iteration {nxt}"
                if nxt is not None
                else f"verified {probe.get('level') or 'structurally'}: {probe.get('detail')}"
            )
            return {
                "ok": True,
                "failure_kind": None,
                "details": (
                    f"canary {canary_run_id!r} checkpoint round-trip verified: a restorable "
                    f"{fmt} checkpoint survived the preemption kill under "
                    f"{ckpt_dir}/_checkpoints ({proof})."
                ),
                "stderr_tail": stderr_tail,
                "metrics_fingerprint": None,
                "failure_features": None,
            }
        if status == "missing":
            return {
                "ok": False,
                "failure_kind": "checkpoint_missing",
                "details": (
                    f"canary {canary_run_id!r} wrote NO checkpoint under {ckpt_dir}/_checkpoints "
                    "before the iteration-2 kill — the executor never checkpointed (does it drive "
                    "its loop through run_iterations / write_checkpoint?), or it crashed before "
                    "iteration 1. A run that opted into auto_resume_on_kill would make NO progress "
                    "on resume. See the stderr tail / classified_error for the cause."
                ),
                "stderr_tail": stderr_tail,
                "metrics_fingerprint": None,
                "failure_features": _failure_features(stderr_tail, log_path),
            }
        if status == "unloadable":
            fmt = probe.get("format") or "pickle"
            why = probe.get("detail") or (
                "the checkpoint does not round-trip (e.g. a pickle that needs a "
                "class/lib absent at resume time)"
            )
            return {
                "ok": False,
                "failure_kind": "checkpoint_unloadable",
                "details": (
                    f"canary {canary_run_id!r} wrote a checkpoint ({probe.get('path')}, "
                    f"format {fmt}) but it could NOT be restored: {why}. A long "
                    "auto_resume_on_kill run would discover this only at resume, hours in. "
                    "Fix the checkpoint format before launching the main array."
                ),
                "stderr_tail": stderr_tail,
                "metrics_fingerprint": None,
                "failure_features": _failure_features(stderr_tail, log_path),
            }
        # probe_failed (ssh error / unparseable output) — fail loudly rather than
        # silently pass an unverified checkpoint, same posture as reporter_unreachable.
        return {
            "ok": False,
            "failure_kind": "reporter_unreachable",
            "details": (
                f"canary {canary_run_id!r} checkpoint probe could not run: "
                f"{probe.get('detail')}. Cannot confirm the checkpoint round-trips, so the "
                "canary CANNOT be trusted — fix the cluster env (hpc-agent importable in the "
                "run's conda env) before submitting the main array."
            ),
            "stderr_tail": stderr_tail,
            "metrics_fingerprint": None,
            "failure_features": _failure_features(stderr_tail, log_path),
        }

    # Scan for failure markers.
    haystack = stderr_tail.lower()
    for marker, kind in _FAILURE_MARKERS:
        if marker in haystack:
            return {
                "ok": False,
                "failure_kind": kind,
                "details": (
                    f"canary {canary_run_id!r} stderr contains {marker!r} — "
                    f"likely {kind.replace('_', ' ')}."
                ),
                "stderr_tail": stderr_tail,
                "metrics_fingerprint": None,
                "failure_features": _failure_features(stderr_tail, log_path),
            }

    # Silent-death hypothesis (run-14): on an SGE/UGE cluster, a canary that
    # died with NO stderr content is LIKELY the h_data VIRTUAL-memory kill (UGE
    # SIGKILLs on vmem, no traceback — two live canaries died so ~1min into
    # data prep). The seam classifier (infra.failure_signatures) owns the
    # signature; here we just look up the family and attach the hypothesis to
    # the two verdict shapes a silent kill produces (completed_unknown /
    # abandoned) so the operator gets the qacct confirmation + remediation
    # instead of a bland "inspect the job log".
    from hpc_agent.infra.backends import get_backend_class
    from hpc_agent.infra.failure_signatures import classify_sge_vmem_kill

    _sched_family: str | None
    try:
        # ``profile`` lives on ProfileBackend subclasses (every registered
        # backend today); getattr keeps the base HPCBackend type honest.
        _profile = getattr(get_backend_class(scheduler), "profile", None)
        _sched_family = getattr(_profile, "family", None)
    except Exception:  # noqa: BLE001 — a hint lookup must never fail the verdict
        _sched_family = None
    _vmem_hint = classify_sge_vmem_kill(scheduler_family=_sched_family, stderr=stderr_tail)

    def _with_vmem_hint(details: str, features: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Attach the vmem-kill hypothesis to a silent-death envelope (or no-op)."""
        if _vmem_hint is None:
            return details, features
        hinted = f"{details} LIKELY CAUSE (hypothesis): {_vmem_hint['suggested_fix']['hint']}"
        if features.get("classified_error") is None:
            features = {**features, "classified_error": _vmem_hint}
        return hinted, features

    # The canary left the scheduler queue without ever recording a completion
    # and no stderr marker explains why (#193). We can't trust it as passed —
    # something ended the job (a fast non-zero exit that cleared the queue, a
    # scheduler kill, a vanished array) — but we also resolved it in seconds
    # instead of riding the full wait budget. Fail with ``completed_unknown``
    # so the two-phase gate refuses the main array and the agent investigates,
    # rather than the old behaviour of reporting ``timeout`` after 30 minutes.
    if job_vanished and int(last_summary.get("complete") or 0) < int(record.total_tasks):
        details, features = _with_vmem_hint(
            (
                f"canary {canary_run_id!r} left the scheduler queue without "
                "recording a completion and no stderr marker explains why — it "
                "finished or was killed too fast to observe. Refusing to pass the "
                "canary; inspect the job log / scheduler accounting before "
                f"submitting the main array (last summary: {last_summary})."
            ),
            _failure_features(stderr_tail, log_path),
        )
        return {
            "ok": False,
            "failure_kind": "completed_unknown",
            "details": details,
            "stderr_tail": stderr_tail,
            "metrics_fingerprint": None,
            "failure_features": features,
        }

    # Optional output verification — read from the fused tail bundle (one ssh
    # already covered existence + the .json parse; verdict logic unchanged).
    if expect_output and tail_bundle["output"] is not None:
        output_ok, output_detail = tail_bundle["output"]
        if not output_ok:
            return {
                "ok": False,
                "failure_kind": "missing_output",
                "details": (
                    f"canary {canary_run_id!r} expected output {expect_output!r} {output_detail}"
                ),
                "stderr_tail": stderr_tail,
                "metrics_fingerprint": None,
                "failure_features": _failure_features(stderr_tail, log_path),
            }

    # Final check: complete == total_tasks, no failures.
    summary_failed = int(last_summary.get("failed") or 0)
    if summary_failed > 0:
        details, features = _with_vmem_hint(
            (
                f"canary {canary_run_id!r} reported failed={summary_failed} "
                "but no recognized stderr marker found."
            ),
            _failure_features(stderr_tail, log_path),
        )
        return {
            "ok": False,
            "failure_kind": "abandoned",
            "details": details,
            "stderr_tail": stderr_tail,
            "metrics_fingerprint": None,
            "failure_features": features,
        }

    # #351-3: positively read the canary task-0 ``exit_code`` before declaring
    # success. The scheduler-state + result-file-presence + 50-line stderr scan
    # above can ALL pass for a canary that wrote a partial result then exited
    # non-zero (a TypeError whose traceback fell outside the fetched tail). The
    # dispatcher records the real exit code to ``<result_dir>/_runtime.json``
    # (dispatch.py ~:984/:1007); read it over SSH and fail the gate on a non-zero
    # exit, so a failing canary returns ok=False and its cmd_sha never reaches the
    # #249 TTL cache (minted by submit_and_verify AFTER the full gate — B7).
    #
    # ADDITIVE positive check, not a replacement: an ABSENT/unreadable
    # _runtime.json falls through to the unchanged logic (a preamble crash before
    # the dispatcher writes it must still be caught by the stderr / failed-count
    # paths above — those have already run and not fired). We only resolve the
    # result dir from the sidecar's result_dir_template; a template that needs a
    # per-task kwarg (unrenderable locally) is treated as "can't check" and falls
    # through, exactly like the absent case, rather than failing a real canary.
    # The exit code came back in the SAME fused ssh as the stderr tail (its
    # ``runtime`` bundle field; ``_exit_result_dir`` is the dir it was read from —
    # ``None`` when the template couldn't render locally, exactly the old skip).
    canary_exit_code: int | None = None
    _result_dir = _exit_result_dir
    runtime = tail_bundle["runtime"]
    if _result_dir is not None and runtime is not None and runtime.get("status") == "present":
        canary_exit_code = int(runtime["exit_code"])
        # Persist the canary's MEASURED task wall-clock onto its own sidecar so the
        # two-phase gate can shrink the main-array walltime to the observed runtime
        # (canary_calibration). Best-effort: a stamp failure never fails the gate —
        # the array simply launches at the approved walltime, unchanged.
        # _parse_runtime_json already normalised elapsed_sec / peak_rss_mb to a
        # positive int (or None on a read miss / absent field), so no
        # measurement never mints a stamp or a prior sample.
        _canary_elapsed = runtime.get("elapsed_sec")
        _canary_rss = runtime.get("peak_rss_mb")
        if isinstance(_canary_elapsed, int):
            from hpc_agent.state.runs import stamp_canary_runtime

            stamp_canary_runtime(
                experiment_dir,
                canary_run_id,
                elapsed_sec=_canary_elapsed,
                peak_rss_mb=_canary_rss if isinstance(_canary_rss, int) else None,
            )
            # Mint the canary's RUNTIME + MEMORY prior sample (run-14): the
            # canary is a full real task measured before the array, so its
            # elapsed + peak RSS seed ``runtimes/<profile>.<cluster>.json``
            # for the next resolve — the array runs seed the same file via the
            # combiner ingest, and append_sample dedups on (run_id, task_id).
            # Scheduler-aware by keying: (profile, cluster) come off the
            # journal record. Best-effort: prior minting never fails the gate.
            try:
                from hpc_agent.state.runtime_prior import append_sample

                append_sample(
                    experiment_dir,
                    profile=record.profile,
                    cluster=record.cluster,
                    run_id=canary_run_id,
                    task_id=0,
                    # roll_up_quantiles drops gpu_type=="" samples; a CPU-only
                    # task buckets under the honest literal "cpu".
                    gpu_type=str(runtime.get("gpu_type") or "cpu"),
                    node="",
                    elapsed_sec=_canary_elapsed,
                    exit_code=int(runtime["exit_code"]),
                    cmd_sha=(str(_canary_sidecar.get("cmd_sha")) or None)
                    if _canary_sidecar.get("cmd_sha")
                    else None,
                    peak_host_mem_mb=_canary_rss if isinstance(_canary_rss, int) else None,
                    walltime_requested_sec=None,
                )
            except Exception:  # noqa: BLE001 — prior minting never fails the gate
                import logging

                logging.getLogger(__name__).warning(
                    "canary %r runtime/memory prior sample not minted",
                    canary_run_id,
                    exc_info=True,
                )
        verdict = _classify_runtime_exit(
            canary_exit_code, canary_run_id=canary_run_id, result_dir=_result_dir
        )
        if verdict is not None:
            kind, details = verdict
            return {
                "ok": False,
                "failure_kind": kind,
                "details": details,
                "stderr_tail": stderr_tail,
                "metrics_fingerprint": None,
                "failure_features": _failure_features(stderr_tail, log_path),
            }
        # status in {"absent", "unreadable"} → fall through unchanged: the run
        # either crashed before _runtime.json (caught above) or we simply could
        # not read it, and we never mint a false failure from a read miss.

    # Optional fingerprint — the sha256 already came back in the fused tail ssh.
    # Best-effort: a fingerprint miss does NOT invalidate the canary (the run
    # itself is fine; we just couldn't hash) — ``None`` when unavailable.
    metrics_fingerprint: str | None = tail_bundle["fingerprint_sha"] if fingerprint else None

    # #249 TTL cache mint moved OUT of this per-canary success path (B7). This
    # function verifies ONE canary; the gate fires a canary PAIR. Minting here
    # stamped the cache on the FIRST canary's success — mid-gate — so a failed
    # SECOND canary blocked the main once, then a retry inside the 4h TTL
    # cache-skipped BOTH canaries on a cmd_sha that never fully validated. The
    # mint now lives in submit_and_verify, reached only past BOTH canary verdicts
    # (see ``_record_canary_gate_validated``), so the cache reflects the whole
    # gate rather than a single verify.

    # #351-3: only claim "exit 0" when the exit code was ACTUALLY read as 0 from
    # _runtime.json. When the sidecar was absent/unreadable we never verified the
    # exit code — say "no error markers" without asserting an unchecked exit code,
    # rather than the old string that lied "exit 0" on every success path.
    _exit_claim = "exit 0" if canary_exit_code == 0 else "no error markers"
    return {
        "ok": True,
        "failure_kind": None,
        "details": (
            f"canary {canary_run_id!r} verified: {_exit_claim}, "
            + (f"output {expect_output!r} present." if expect_output else "no output check.")
        ),
        "stderr_tail": stderr_tail,
        "metrics_fingerprint": metrics_fingerprint,
        # ``failure_features`` is only attached to ``ok=False`` envelopes — the
        # success envelope intentionally omits it so consumers can use its
        # presence as a "this is a failed canary" sentinel.
        "failure_features": None,
    }
