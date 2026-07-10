"""Catalog of (stderr pattern, exit code) -> (error_class, suggested_fix).

Pattern adapted from VASPilot's failure-signatures table. Integrating
agents branch on ``classify()`` to auto-resubmit with adjusted
resources rather than asking the user --- e.g. a CUDA OOM gets an
``increase-mem-per-gpu`` fix suggestion that the campaign loop can
apply automatically.

The catalog is ordered by ``priority`` (descending). The first matching
entry wins. ``priority=100`` are the high-confidence resource-error
patterns (OOM, walltime); ``priority=80`` are the user-error
patterns (import, file_not_found, permission); ``priority=10`` is the
generic Python traceback fallback.

Why a separate module from :func:`hpc_agent.ops.recover.runner_failures._categorize`:
the runner only emits a category string. ``classify()`` returns the
full ``{error_class, suggested_fix, matched_pattern}`` triple so the
caller can both display the error and act on the fix recommendation.
The runner keeps its old ``_categorize`` shape; new callers consume
``classify()`` directly.

The ``error_class`` strings align with
:class:`hpc_agent._kernel.contract.vocabulary.FailureCategory` once that StrEnum is
on the branch (B2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "FailureSignature",
    "classify",
    "CATALOG",
    "CLASSIFIER_CATEGORIES",
    "CONDA_RUN_BLIND_CLASS",
    "classify_conda_run_blind",
]


@dataclass(frozen=True)
class FailureSignature:
    """One row of the catalog.

    *priority*: higher = more specific, tried first. Two entries with
    the same priority and overlapping patterns are still deterministic
    because we iterate in the catalog list order.
    """

    error_class: str
    stderr_pattern: re.Pattern[str] | None
    exit_code: int | None
    suggested_fix: dict[str, Any]
    priority: int = 50


CATALOG: list[FailureSignature] = [
    FailureSignature(
        error_class="preempted",
        stderr_pattern=re.compile(
            r"\[hpc-agent\] SIGTERM received; cluster preemption imminent",
        ),
        # Cluster-side dispatch.py exits 130 after trapping SIGTERM —
        # the campus user got bumped by higher-priority work, not
        # failed. The harness should resubmit cleanly.
        exit_code=130,
        suggested_fix={"action": "resubmit-preempted"},
        priority=100,
    ),
    FailureSignature(
        error_class="gpu_oom",
        stderr_pattern=re.compile(
            r"CUDA out of memory|RuntimeError: cuda runtime error.*out of memory|"
            r"torch\.cuda\.OutOfMemoryError|cuda.*OOM",
            re.I,
        ),
        exit_code=None,
        suggested_fix={"action": "increase-mem-per-gpu", "factor": 1.5},
        priority=100,
    ),
    FailureSignature(
        error_class="system_oom",
        stderr_pattern=re.compile(
            r"oom-kill|out of memory.*killed|\bMemoryError\b|killed.*signal 9", re.I
        ),
        exit_code=137,
        suggested_fix={"action": "increase-mem", "factor": 1.5},
        priority=100,
    ),
    FailureSignature(
        error_class="walltime",
        # Scheduler-specific markers only. The bare ``\bwalltime\b`` token
        # and ``signal SIGTERM.*15`` previously included here collide with
        # preemption (SLURM/SGE preemption is delivered via SIGTERM with
        # exit 143). The narrowed set keeps a preempted task from getting
        # ``suggested_fix=increase-walltime`` from this catalog; the runner's
        # exit-130/143 fallback routes it to ``preempted`` instead.
        stderr_pattern=re.compile(
            r"DUE TO TIME LIMIT|CANCELLED.*TIME LIMIT|"
            r"wall.?time.*expired|wall.?time.*exceeded|"
            r"Time limit exceeded|h_rt.*exceeded|"
            # SGE qacct prints "qmaster enforced h_rt, h_cpu, or h_vmem
            # limit" when a job is killed for exceeding walltime. This
            # is distinct enough from the bare ``\bwalltime\b`` token
            # (which collided with preemption) to be safe.
            r"qmaster enforced h_rt",
            re.I,
        ),
        exit_code=271,
        suggested_fix={"action": "increase-walltime", "factor": 1.5},
        priority=100,
    ),
    FailureSignature(
        error_class="node_failure",
        # ``NODE FAILURE`` (with a space, not just NODE_FAIL),
        # ``slurmstepd: error: *** NODE`` and SGE's ``Eqw`` error state
        # were folded in from mapreduce/reduce/classify.py during the
        # dedup so the wrapper there can delegate fully to this catalog.
        stderr_pattern=re.compile(
            r"NODE_FAIL|NODE FAILURE|node failed|"
            r"slurmstepd:\s*error:\s*\*\*\*\s*NODE|"
            r"\bEqw\b|"
            r"connection (closed|reset by peer)|"
            r"ssh: connect.*refused",
            re.I,
        ),
        exit_code=None,
        suggested_fix={"action": "retry-on-different-node"},
        priority=90,
    ),
    FailureSignature(
        error_class="file_not_found",
        stderr_pattern=re.compile(r"FileNotFoundError|No such file or directory", re.I),
        exit_code=2,
        suggested_fix={"action": "user-fix-paths"},
        priority=80,
    ),
    FailureSignature(
        error_class="import_error",
        stderr_pattern=re.compile(r"ModuleNotFoundError|ImportError", re.I),
        exit_code=1,
        suggested_fix={"action": "user-fix-deps"},
        priority=80,
    ),
    FailureSignature(
        error_class="permission_denied",
        stderr_pattern=re.compile(r"PermissionError|Permission denied", re.I),
        exit_code=13,
        suggested_fix={"action": "user-fix-permissions"},
        priority=80,
    ),
    FailureSignature(
        error_class="disk_full",
        stderr_pattern=re.compile(r"No space left on device|disk.*full|\bENOSPC\b", re.I),
        exit_code=28,
        suggested_fix={"action": "user-clean-disk"},
        priority=80,
    ),
    FailureSignature(
        error_class="python_traceback",
        stderr_pattern=re.compile(r"^Traceback \(most recent call last\):", re.I | re.M),
        exit_code=1,
        suggested_fix={"action": "user-debug"},
        priority=10,
    ),
    # ── empirical canary signatures ─────────────────────────────────────
    # Learned from real demo failures where the orchestrator gave up at
    # ``dispatcher_failed`` without inspecting the cluster log. Each carries
    # a *specific* remediation so an agent (or human) can act without
    # rerunning the failure to gather context. Priority is set above the
    # bare ``python_traceback`` fallback so they win when the same stderr
    # carries both a structural marker and an incidental traceback.
    FailureSignature(
        error_class="uv_not_on_path",
        # The cluster preamble explicitly errors out with this token when
        # ``HPC_RUNTIME=uv`` was set but no ``uv`` binary is available after
        # the activation block ran. See common/hpc_preamble.sh.
        stderr_pattern=re.compile(
            r"HPC_RUNTIME=uv but ['\"]?uv['\"]? (is )?not on PATH",
            re.I,
        ),
        exit_code=2,
        suggested_fix={
            "action": "drop-runtime-uv-or-install",
            "hint": (
                'uv missing on cluster — drop `runtime: "uv"` from the spec, OR '
                "install uv into the cluster conda env "
                "(`~/.conda/envs/<env>/bin/pip install uv`) and resubmit."
            ),
        },
        priority=95,
    ),
    FailureSignature(
        error_class="conda_command_not_found",
        # ``conda activate`` fails because conda was never sourced — usually
        # ``clusters.yaml`` has the wrong ``conda_source`` or the cluster's
        # module-load step that exports conda is missing.
        stderr_pattern=re.compile(
            r"conda:\s*command not found|conda:\s*not found|"
            r"command not found:\s*conda",
            re.I,
        ),
        exit_code=None,
        suggested_fix={
            "action": "fix-cluster-conda-source",
            "hint": (
                "Cluster preamble couldn't source conda — verify the "
                "`conda_source` path in `clusters.yaml` for this cluster, and "
                "that the conda module is loaded by the preamble."
            ),
        },
        priority=95,
    ),
    FailureSignature(
        error_class="output_file_required",
        # The executor's argparse rejected its invocation because the
        # ``--output-file`` flag (auto-injected for ``@register_run`` entry
        # points) was not supplied. Means the framework's auto-inject didn't
        # fire — the @register_run decorator may not be on disk, or the
        # entry_point.kind is wrong.
        stderr_pattern=re.compile(
            r"error: the following arguments are required:[^\n]*--output-file",
            re.I,
        ),
        exit_code=2,
        suggested_fix={
            "action": "verify-register-run-on-disk",
            "hint": (
                "Executor expects `--output-file` but the framework didn't "
                "auto-inject it. Verify `entry_point.kind` is `register_run` "
                "AND the executor's `@register_run` decorator is on disk."
            ),
        },
        priority=95,
    ),
    FailureSignature(
        error_class="module_not_found_hpc_agent",
        # The cluster-side python can't import ``hpc_agent``. The likeliest
        # cause is that the running python is not the conda env's python —
        # either the activation didn't fire, or the wrong env was activated.
        stderr_pattern=re.compile(
            r"ModuleNotFoundError:.*hpc_agent|No module named ['\"]hpc_agent['\"]",
            re.I,
        ),
        exit_code=1,
        suggested_fix={
            "action": "fix-cluster-env-activation",
            "hint": (
                "Cluster-side python isn't the conda env's python — verify "
                "conda activation in the preamble + that "
                "`remote_activation_for_sidecar` threads `conda_env` through "
                "to the cluster status reporter."
            ),
        },
        # Higher than the generic ``import_error`` so the hpc_agent-specific
        # signature wins when both match.
        priority=85,
    ),
    # ── multi-rank (MPI) signatures (#293 PR4) ──────────────────────────
    # Each carries a specific remediation so the canary verifier / recover
    # path can act without re-running. Priority above the bare traceback
    # fallback so an MPI launch failure that also prints a Python traceback
    # still classifies as the structural MPI error.
    FailureSignature(
        error_class="mpi_launcher_missing",
        # The launcher binary (srun/mpirun/aprun) the dispatcher prefixes the
        # per-task command with isn't on PATH — usually a missing MPI module.
        stderr_pattern=re.compile(
            r"(srun|mpirun|mpiexec|aprun):\s*command not found|"
            r"command not found:\s*(srun|mpirun|mpiexec|aprun)|"
            r"(srun|mpirun|mpiexec|aprun):\s*not found",
            re.I,
        ),
        exit_code=127,
        suggested_fix={
            "action": "fix-mpi-launcher",
            "hint": (
                "The MPI launcher (srun/mpirun/aprun) isn't on PATH cluster-side. "
                "Load the MPI module in the spec's `modules` (e.g. 'openmpi' / "
                "'intel-mpi'), or set the spec's mpi.launcher to one the cluster "
                "provides (SLURM clusters always have srun)."
            ),
        },
        priority=95,
    ),
    FailureSignature(
        error_class="mpi_pe_invalid",
        # SGE rejects the `-pe <name> <n>` request because the parallel
        # environment doesn't exist (wrong/stale pe_name) or its slot range
        # excludes the requested rank count.
        stderr_pattern=re.compile(
            r'parallel environment ".*" does not exist|'
            r"no parallel environment|"
            r"invalid parallel environment|"
            r"job .* does not (fit|use) the parallel environment",
            re.I,
        ),
        exit_code=None,
        suggested_fix={
            "action": "fix-mpi-pe-name",
            "hint": (
                "SGE rejected the parallel environment. Pick a PE with kind='mpi' "
                "from inspect-cluster's parallel_environments and set it as the "
                "spec's mpi.pe_name; verify the requested ranks fit the PE's slot range."
            ),
        },
        priority=95,
    ),
    FailureSignature(
        error_class="mpi_init_failed",
        # The MPI runtime itself failed to start the ranks — too few slots for
        # the requested ranks (the runtime surfacing a ranks>capacity ask),
        # an MPI_Init/MPI_ABORT abort, or an ORTE/PMIx wire-up error.
        stderr_pattern=re.compile(
            r"There are not enough slots available|not enough slots|"
            r"MPI_(?:Init|ABORT)|PMPI_Init|mpirun (?:detected|noticed) that|"
            r"\bORTE\b.*(?:fail|error|abort)|PMIx?.*(?:error|failed)|"
            r"error initializing.*MPI",
            re.I,
        ),
        exit_code=None,
        suggested_fix={
            "action": "check-mpi-topology",
            "hint": (
                "The MPI runtime couldn't launch the ranks. Common causes: ranks "
                "exceed the allocation's slots (lower mpi.ranks or raise the "
                "node/slot ask), an MPI library mismatch between build and runtime, "
                "or a rank/topology mismatch. Re-run the ranks=2 canary to isolate."
            ),
        },
        priority=95,
    ),
    FailureSignature(
        error_class="undefined_var_expansion",
        # argparse rejects an empty value because an env-var reference in the
        # executor command (e.g. ``--samples $SAMPLES``) expanded to "". The
        # marker pattern catches the argparse error; we cannot infer which
        # variable was empty from a single line, so the hint is generic.
        stderr_pattern=re.compile(
            r"error: argument [^:]+: expected one argument",
            re.I,
        ),
        exit_code=2,
        suggested_fix={
            "action": "fix-empty-env-var-in-executor",
            "hint": (
                "An executor flag expected a value but got an empty string — "
                "most likely an env-var reference (`$VAR`) in the executor "
                "command expanded to empty. Verify every `$VAR` referenced in "
                "the executor is exported in `job_env`."
            ),
        },
        priority=80,
    ),
    # ── cluster env-init failures (notebook-audit Addendum 10, item 15) ──────
    # The contentless env-init failure both Grid Engine and Lmod emit when a
    # task's environment could not be set up — the tail names NO cause, so the
    # generic "check the stderr" remediation punts at exactly the moment the
    # stderr is empty. Run #11: HOFFMAN2 (UGE) surfaced the exact Grid Engine
    # string on ONE ``rlin_tune`` array instance while its siblings ran
    # healthily (quota clean, login-init + module-load green minutes later) —
    # a transient, PER-TASK / PER-NODE flake, not a run-wide fault.
    FailureSignature(
        error_class="cluster_env_init",
        # One conservative phrase anchors both dialects: Grid Engine (UGE/SGE)
        # prints "Unable to initialize environment because of error" and Lmod
        # emits the lookalike "Unable to initialize environment ..." on a
        # module-init failure. Nothing benign carries this exact phrase, so the
        # bare substring (case-insensitive) is safe and covers both without a
        # scheduler-specific branch. Deliberately NOT anchored on the trailing
        # "because of error" / a diagnosis token — the whole point is that the
        # message is contentless, so we match the stable stem only.
        stderr_pattern=re.compile(
            r"Unable to initialize environment",
            re.I,
        ),
        # Pattern-only: no reliable exit code (the job/task env-init failure is
        # surfaced in the log, not a distinct scheduler exit), so this never
        # fires on exit code alone.
        exit_code=None,
        suggested_fix={
            # ``retry-task`` is the retry-forward signal the structure carries
            # (the ``action`` string IS the retry marker — cf. preempted's
            # ``resubmit-preempted`` and node_failure's ``retry-on-different-node``):
            # a transient per-node env-init flake usually clears on a retry, and
            # the reduce-side status map routes ``cluster_env_init`` to the
            # ``node_failure`` infra category rather than a code-bug escalation.
            "action": "retry-task",
            "hint": (
                "Grid Engine / Lmod could not initialize the task's environment "
                "and the log tail names no cause. This is typically a transient, "
                "PER-TASK / PER-NODE flake — sibling array tasks are unaffected — "
                "so RETRY the task/op first. If it recurs, check in priority "
                "order: (1) a transient scheduler-or-module flake on that exec "
                "node, (2) home-directory quota exhaustion (a full $HOME breaks "
                "login-init), (3) a stale module cache (clear the Lmod cache / "
                "`module --purge` and retry), (4) a broken module or line in the "
                "login init (.bashrc / .modulerc / a module load in the profile). "
                "Which scheduler + task the failure landed on is carried in "
                "failure_features (the remote host is surfaced when the log or a "
                "scheduler probe names it)."
            ),
        },
        # Same band as node_failure (90): a transient infra class that outranks
        # the bare-traceback fallback but sits below the exact-token config
        # signatures (95). No pattern overlap with any other row, so the tie is
        # moot — list order is deterministic regardless.
        priority=90,
    ),
]


# ── conda-run blindness: a SILENT-SUCCESS signature (NOT a CATALOG row) ──────
# Documented in the demo-env lore, prose-only until now: on some clusters
# ``conda run -n <env> ...`` under NON-INTERACTIVE SSH produces SILENTLY EMPTY
# stdout — conda was never initialized, so the wrapper exits 0 having run
# nothing. Indistinguishable from a working no-op, so stale/absent results get
# misread as success (cost: a whole harvest of "nothing changed" read as done).
#
# SEAM NOTE — why this deliberately does NOT ride ``classify()`` / ``CATALOG``:
# ``classify(stderr, exit_code)`` sees ONLY stderr and the exit code. This class
# has EMPTY stderr and ``exit_code == 0`` (a "success"); its entire
# discriminating signal is the COMBINATION {the command was ``conda run``-shaped,
# stdout was empty, rc was 0} — none of which the failure seam can observe.
# Forcing a ``stderr_pattern``/``exit_code`` catalog row would either never fire
# (empty stderr, rc 0) or fire on every clean no-op. So the signature lives at
# the closest seam that CAN see all three features: a dedicated matcher a caller
# invokes with the command text + stdout + rc it just observed. Keeping it out of
# ``CATALOG`` also keeps ``CLASSIFIER_CATEGORIES`` / ``FailureCategory`` /
# ``FailureCategoryResubmittable`` untouched — this is not a resubmittable
# scheduler failure class, and ``retry_worthy`` is False by construction.
#
# GAP disclosed honestly: no EXISTING failure-exit seam carries a rc=0
# empty-stdout "success" to classify (the failure path only runs on non-success),
# so wiring a live consumer is a caller's future concern. This module supplies
# the classifier so the detection lives in ONE place the day a caller (a
# ``conda run`` ssh op that got empty output) wants to ask "was this blindness?".
CONDA_RUN_BLIND_CLASS = "conda_run_blind"

_CONDA_RUN_RE = re.compile(r"\bconda\s+run\b", re.I)


def classify_conda_run_blind(
    *,
    command: str | None,
    stdout: str | None,
    exit_code: int | None,
) -> dict[str, Any] | None:
    """Detect the conda-run-blindness silent-success signature.

    Returns the ``{error_class, suggested_fix, matched_pattern}`` triple (the
    same shape :func:`classify` returns) ONLY when ALL three hold:

    * *command* is ``conda run``-shaped (``conda run ...``),
    * *stdout* is empty or whitespace-only,
    * *exit_code* is 0 or ``None`` (the wrapper "succeeded" having produced
      nothing; an unknown rc is not treated as a non-zero failure).

    Returns ``None`` otherwise — a legitimately-empty NON-conda command, or a
    real ``conda run`` that failed with a non-zero rc, is never mis-tagged.

    ``retry_worthy=False``: re-running the same non-interactive ``conda run``
    reproduces the blindness. The remediation names, in priority order, the
    DIRECT env-python invocation (``~/.conda/envs/<env>/bin/python -m ...``),
    which needs no conda init at all.
    """
    if not _CONDA_RUN_RE.search(command or ""):
        return None
    if (stdout or "").strip():
        return None
    if exit_code is not None and int(exit_code) != 0:
        return None
    return {
        "error_class": CONDA_RUN_BLIND_CLASS,
        "suggested_fix": {
            "action": "use-direct-env-python",
            "retry_worthy": False,
            "hint": (
                "`conda run -n <env> ...` produced EMPTY stdout with exit 0 under "
                "non-interactive SSH — conda was never initialized, so the wrapper "
                "ran NOTHING and still 'succeeded'. This is indistinguishable from a "
                "working no-op, so absent/stale results get misread as done. Do NOT "
                "retry (a non-interactive `conda run` reproduces it). In priority "
                "order: (1) invoke the env's python DIRECTLY — "
                "`~/.conda/envs/<env>/bin/python -m <module> ...` (also "
                "`~/.conda/envs/<env>/bin/<tool>`) — which needs no conda init; "
                "(2) if a conda wrapper is unavoidable, SOURCE conda first "
                "(`source <conda_source> && conda activate <env>`) in the same "
                "non-interactive shell before the command; (3) verify the env name "
                "and that `~/.conda/envs/<env>/bin/python` actually exists."
            ),
        },
        "matched_pattern": _CONDA_RUN_RE.pattern,
    }


# Every ``error_class`` the catalog (and thus ``classify()``) can emit — the
# single source for "categories the classifier produces". Consumed by the
# ``FailureCategoryResubmittable`` contract test and the ``FailureCategory``
# enum round-trip checks (which previously read the now-removed
# ``runner_failures._FAILURE_CATEGORY_PATTERNS``). ``classify()`` also returns
# ``"unknown"`` on no match; that is not a catalog row.
CLASSIFIER_CATEGORIES: frozenset[str] = frozenset(sig.error_class for sig in CATALOG)


def classify(stderr: str | None, exit_code: int | None) -> dict[str, Any]:
    """Return ``{error_class, suggested_fix, matched_pattern}``.

    Iterates the catalog in priority order (descending). The first hit
    wins. Returns ``{error_class: "unknown", ...}`` on no match.

    *exit_code* is only used as a tiebreaker --- a ``stderr_pattern``
    match alone is sufficient, since exit codes are noisy on schedulers
    that wrap them (qsub returns 0 even when the inner job dies). The
    exit-code-alone path only fires for priority>=90 entries (resource
    errors) to avoid mis-classifying a generic exit=1 as a python
    traceback.
    """
    text = stderr or ""
    sorted_catalog = sorted(CATALOG, key=lambda s: -s.priority)
    # Two passes so the docstring promise — "stderr_pattern match alone
    # is sufficient" — holds in priority order. The single-pass version
    # let a high-priority exit-only hit win against a lower-priority but
    # actually-matching pattern hit.
    for sig in sorted_catalog:
        if sig.stderr_pattern is not None and sig.stderr_pattern.search(text):
            return {
                "error_class": sig.error_class,
                "suggested_fix": dict(sig.suggested_fix),
                "matched_pattern": sig.stderr_pattern.pattern,
            }
    for sig in sorted_catalog:
        exit_hit = (
            sig.exit_code is not None
            and exit_code is not None
            and int(exit_code) == int(sig.exit_code)
        )
        if exit_hit and sig.priority >= 90:
            return {
                "error_class": sig.error_class,
                "suggested_fix": dict(sig.suggested_fix),
                "matched_pattern": None,
            }
    return {
        "error_class": "unknown",
        "suggested_fix": {"action": "user-debug"},
        "matched_pattern": None,
    }
