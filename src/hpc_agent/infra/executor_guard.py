"""Executor-shape boundary guards — shared substrate for the submit spine.

The per-task ``executor`` and the job-script ``EXECUTOR`` are hand-authorable
(or divergent-build-materialized) command strings that the cluster-side
dispatcher runs verbatim. A structurally-broken value fails silently on the
cluster (exit 127 / argparse exit 2 / a ``{placeholder}`` written to disk), so
this module owns the static shape checks that refuse the known-doomed forms at
build/write time — the ``#292`` / ``#293`` / proving-run findings.

Lives under ``infra`` (not ``incorporation`` or ``ops``) because BOTH the
authoring surface (``incorporation.build.submit_spec.build_submit_spec``) and
the submit spine (``ops.submit_flow`` and the ``ops`` sidecar/interview verbs)
consume it. Homing the library here — the shared ``hpc_agent.infra.*`` substrate
ops/meta/incorporation all compose through — is what lets
``incorporation`` stop importing from ``ops`` and ``ops`` stop importing from
``incorporation``: the two packages no longer reach into each other, restoring
``incorporation``'s "never consumes from ops/meta" invariant (see
``incorporation/README.md``). ``incorporation.build.submit_spec`` and
``ops.submit_flow`` re-export the names their tests pin so the public import
paths are unchanged.

The entry point is :func:`check_per_task_executor` (the sidecar's per-task
``executor``); ``build_submit_spec`` additionally runs the job_env-aware guards
(:func:`_check_executor_var_references` / :func:`_check_executor_in_deploy_manifest`
/ ...) where the assembled job_env is known.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from hpc_agent import errors

# Job-script ``EXECUTOR`` default — the cluster-side dispatcher command. The
# per-task command lives in the sidecar's ``executor`` field, NOT here; this is
# the space-safe, comma-free dispatcher token ``cpu_array.sh`` ships via
# ``qsub -v ...,EXECUTOR=...`` and runs as ``time $EXECUTOR`` unquoted.
_DEFAULT_EXECUTOR_CMD = "python3 .hpc/_hpc_dispatch.py"


# Matches ``python`` / ``python3`` (possibly version-suffixed) followed by
# exactly one positional ``<path>.py`` token — the naive bare-script shape.
# A ``-c`` / ``-m`` / any other flag short-circuits the match: those forms
# are presumed correct (the auto-generated ``python3 -c "..."`` one-liner
# is exactly the path we want to allow through).
_BARE_SCRIPT_RE = re.compile(r"^python[0-9.]*$")


def _check_register_run_executor(executor: str, *, base_dir: Path | None = None) -> None:
    """Raise :class:`errors.SpecInvalid` if *executor* is a bare-script invocation
    of a ``@register_run``-decorated file.

    Fires on any ``python[3] <file>.py [...]`` shape against a
    ``register_run``-decorated file — including the with-trailing-args form
    (``python executors/foo.py --samples 100000 --seed $SEED``) that the
    pre-0.10.11 strict ``len(parts) == 2`` check let slip through. Trailing
    args are not the safe path — they are the *exact* smoking gun for an
    agent that forgot the canonical ``python -c "..."`` form and shell-
    templated kwargs into argv instead, which the cluster-side dispatcher
    drops on the floor (it routes task kwargs via ``HPC_KW_<NAME>`` env vars,
    not argv). Anything with a flag *before* the script (``python -c "..."``,
    ``python -m pkg``, ``python -O file.py``) short-circuits at the
    ``script.endswith(".py")`` check — those forms are presumed correct.

    *base_dir* (#292 Bug A): the experiment tree the (relative) script path is
    resolved against. The pre-#292 code did ``Path(script).is_file()`` — a
    CWD-relative probe that returned False (and silently passed the guard)
    whenever ``build_submit_spec`` ran in a worker whose CWD wasn't the
    experiment dir, exactly the contract the 0.10.11 CHANGELOG asserts holds.
    When *base_dir* is given, a relative script resolves against it; when None,
    the old CWD-relative behaviour is preserved (correct for an invocation run
    from the experiment dir).
    """
    try:
        parts = shlex.split(executor)
    except ValueError:
        return  # unparseable shell — leave it to the cluster to surface
    if len(parts) < 2:
        return
    interp, script, *_trailing = parts
    if not _BARE_SCRIPT_RE.match(interp):
        return
    if not script.endswith(".py"):
        return
    local_path = Path(script)
    if base_dir is not None and not local_path.is_absolute():
        local_path = Path(base_dir) / local_path
    if not local_path.is_file():
        return
    try:
        source = local_path.read_text(encoding="utf-8")
    except OSError:
        return
    # Cheap substring probe — discover.py does the rigorous AST walk, but
    # for a defensive boundary guard the combined presence of both names
    # is a strong-enough signal. A false positive on a comment-only file
    # that mentions both strings is recoverable via the SpecInvalid below.
    if "register_run" not in source or "hpc_agent" not in source:
        return
    raise errors.SpecInvalid(
        f"EXECUTOR is the bare-script form {executor!r}, but {script} is a "
        "@register_run-decorated file. The cluster-side dispatcher passes "
        "task kwargs only via HPC_KW_<NAME> env vars, never argv, so this "
        "invocation will hit the file's argparse __main__ block and fail "
        "with 'required argument missing' (the failure is often silent — "
        "argparse's exit 2 gets eaten and no metrics.json is written).\n"
        "Use the one-liner form instead, e.g.:\n"
        f"  python3 -c \"import runpy as _r; _m = _r.run_path('{script}'); "
        '_n = next(v for v in _m.values() if getattr(v, "_hpc_run", False)); '
        '_m.compute(_n)"\n'
        "The framework's interview path generates this automatically for "
        "register_run entry points — if you're seeing this error, you're "
        "probably constructing the spec by hand or carrying an older "
        "interview from before the auto-generation fix. Re-run the "
        "interview (`hpc-agent setup` / `/submit-hpc`) to regenerate."
    )


def _path_excluded(rel_path: str, patterns: list[str]) -> bool:
    """Whether *rel_path* (deploy-relative, ``/``-separated) is rsync-excluded.

    A deliberately conservative subset of rsync's matching — enough to catch the
    common "this file/dir is excluded" cases without false positives:

    * a directory pattern (``foo/`` or ``foo``) excludes the dir and everything
      under it, matched at any depth (rsync's bare-name semantics);
    * a ``*.ext`` glob excludes any path whose basename matches;
    * an exact relative path matches itself.

    Only used to PROVE an exclusion; an unrecognised pattern shape simply
    doesn't match, so the guard never refuses on a pattern it can't reason about.
    """
    import fnmatch

    parts = rel_path.split("/")
    basename = parts[-1]
    for raw in patterns:
        pat = raw.strip()
        if not pat:
            continue
        core = pat.strip("/")
        if not core:
            continue
        # Glob on the basename (``*.pyc``, ``*.log``).
        if ("*" in core or "?" in core) and "/" not in core:
            if fnmatch.fnmatch(basename, core):
                return True
            continue
        # Bare name (``__pycache__``, ``results``, ``src`` …): excludes any path
        # component equal to it (rsync matches a bare name at any depth).
        if "/" not in core:
            if core in parts:
                return True
            continue
        # Anchored relative path (``a/b.py``): exact match or a prefix dir.
        if rel_path == core or rel_path.startswith(core + "/"):
            return True
    return False


def _check_executor_in_deploy_manifest(
    executor: str,
    *,
    experiment_dir: Path | None,
    rsync_excludes: list[str] | None,
) -> None:
    """Refuse an EXECUTOR whose script file won't be in the deployed bundle.

    S5 / incident 6, static layer. The per-task command runs
    ``cd "$REPO_DIR" && python <file>.py`` on the cluster, so ``<file>.py`` must
    be one of the files rsync ships under ``remote_path``. A script that exists
    locally but is stripped by an rsync exclude (or is otherwise outside the
    deploy set) lands NO file at REPO_DIR and fails every task exactly as a
    REPO_DIR drift would — but it can be caught with zero network at build time.

    Conservative by construction (only refuse on a PROVABLE miss, matching the
    rest of this module's guards):

    * no ``experiment_dir`` → the deploy set's local root is unknown, skip.
    * the executor carries no relative ``.py`` script token (the ``python3 -c``
      one-liner, a ``-m`` / ``run-module`` dispatch, or an absolute path that the
      job inherits rather than the deployed tree) → nothing manifest-shaped to
      check, skip.
    * the script file is NOT present locally under ``experiment_dir`` → that is
      the LOCAL-presence guard's job (``_check_register_run_executor`` already
      no-ops a missing file); this manifest check only fires for a file that IS
      present locally but would be EXCLUDED from the deploy — the genuine
      "present here, absent there" drift.

    Raises :class:`errors.SpecInvalid` only when the file is present locally yet
    an effective rsync exclude would strip it from the deploy bundle.
    """
    from hpc_agent.infra.backends._remote_base import executor_script_path

    if experiment_dir is None:
        return
    script = executor_script_path(executor)
    if script is None or script.startswith("/"):
        return
    base = Path(experiment_dir)
    local_path = base / script
    if not local_path.is_file():
        # Absent locally is a different failure mode (handled elsewhere); this
        # guard is strictly about a locally-present file being excluded from the
        # deploy, so a positively-established "would be deployed" baseline.
        return
    # Build the effective exclude set the deploy will actually apply: the
    # framework's mandatory + default excludes unioned with the caller's, minus
    # the generated-but-needed carve-out submit-flow restores
    # (``_keep_generated_shippable``). Mirror that logic so this static check and
    # the real push agree on what ships.
    from hpc_agent.infra.transport import (
        _GENERATED_SHIPPABLE,
        DEFAULT_RSYNC_EXCLUDES,
        MANDATORY_RSYNC_EXCLUDES,
    )

    caller = list(rsync_excludes) if rsync_excludes is not None else list(DEFAULT_RSYNC_EXCLUDES)
    effective = [*caller, *MANDATORY_RSYNC_EXCLUDES]
    # Drop the generated-shippable carve-out: those patterns never strip a file
    # from the deploy because submit-flow re-includes them.
    effective = [e for e in effective if e.strip().strip("/") not in _GENERATED_SHIPPABLE]

    rel = script.lstrip("./")
    if _path_excluded(rel, effective):
        raise errors.SpecInvalid(
            f"executor_not_in_deploy_manifest: the executor's script {script!r} is "
            f"present locally ({local_path}) but an effective rsync exclude would "
            f"strip it from the deploy bundle, so it would NOT exist under "
            f"REPO_DIR on the cluster. The per-task command runs "
            '`cd "$REPO_DIR" && <executor>`, so every task would fail as if the '
            "executor were missing (the 2026-06 REPO_DIR/deploy-drift class, "
            "caught statically here). Remove the matching pattern from "
            "rsync_excludes, or move the executor into the deployed tree."
        )


# Matches a lone ``<dotted.module>:<function>`` token — the shape a divergent
# build (or a hand-rolled spec) stamps for a python_module entry when it skips
# the run-module dispatch. The module side is a dotted Python identifier path
# and the function side a single identifier, so a Windows drive path (``C:\x``)
# or a URL (``http://``) can't match: those carry a backslash/slash the class
# excludes.
_BARE_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")


def _check_bare_module_executor(executor: str) -> None:
    """Raise :class:`errors.SpecInvalid` if *executor* is a bare ``module:function``.

    A ``python_module`` entry point must dispatch via
    ``python3 -m hpc_agent.executor_cli run-module <module>:<function>`` — never
    the bare ``<module>:<function>`` token alone. The bare form reaches the
    cluster as the per-task command, is exec'd as a shell command, and exits 127
    (command not found): the ridge_imp incident, where a divergent local build
    materialized ``hpc_wrappers.ridge_imp:ridge_imp`` into the sidecar's
    ``executor``. The interview's python_module branch emits the correct
    ``run-module`` form (:func:`wrap_entry_point.python_module_executor_cmd`);
    this guard catches a hand-rolled spec or a stale/divergent-build sidecar.
    """
    try:
        parts = shlex.split(executor)
    except ValueError:
        return  # unparseable shell — leave it to the cluster to surface
    if len(parts) != 1 or not _BARE_MODULE_RE.match(parts[0]):
        return
    raise errors.SpecInvalid(
        f"EXECUTOR is the bare module:function form {executor!r}, which is not a "
        "runnable command — exec'd on the cluster it exits 127 (command not "
        "found). A python_module entry point must dispatch through the deployed "
        "executor_cli:\n"
        f"  python3 -m hpc_agent.executor_cli run-module {executor}\n"
        "The interview's python_module path generates this automatically; if "
        "you're seeing this you're hand-rolling the spec or carrying a stale "
        "sidecar from a divergent build. Re-run the interview (`/submit-hpc`)."
    )


def _check_executor_is_dispatcher(executor: str) -> None:
    """Refuse an agent-supplied ``job_env["EXECUTOR"]`` that is a per-task one-liner.

    This is the exact INVERSE of ``write-run-sidecar``'s guard
    (:func:`hpc_agent.ops.submit_flow._is_runnable_executor`), which refuses a
    *dispatcher*-shaped value in the *sidecar*: here we refuse a *per-task*-shaped
    value in *EXECUTOR*. ``EXECUTOR`` MUST be the comma-free, space-safe dispatcher
    (:data:`_DEFAULT_EXECUTOR_CMD`, ``python3 .hpc/_hpc_dispatch.py``), because
    ``cpu_array.sh`` ships it via ``qsub -v …,EXECUTOR=…`` (comma-delimited) and
    then runs ``time $EXECUTOR`` **unquoted**. A per-task one-liner like
    ``python3 -c "import argparse, sys; ..."`` breaks that transport twice — the
    comma truncates the ``-v`` value, and word-splitting hands ``-c`` only the
    first bare token (``import``) → ``SyntaxError`` (the actual proving-run-#2
    canary failure). The real per-task command belongs in the sidecar's
    ``executor`` field (``write-run-sidecar``), which the dispatcher reads from
    JSON on the cluster and runs correctly.

    Refuses on exactly the two shapes that break the transport, so a direct
    per-task command that survives it (``python3 analyze.py --seed $SEED`` — no
    comma, no quoting-dependent argument) and every legitimate dispatcher variant
    (``python .hpc/_hpc_dispatch.py``, a ``python3 -m <module>`` custom dispatcher)
    pass through untouched:

    * a comma anywhere (truncates the ``qsub -v`` value), or
    * an inline ``python -c`` one-liner (its quoted code argument cannot survive
      the unquoted ``$EXECUTOR`` word-split; it belongs in the sidecar).
    """
    if "," in executor:
        reason = "it contains a comma, which truncates the `qsub -v ...,EXECUTOR=...` value"
    else:
        try:
            parts = shlex.split(executor)
        except ValueError:
            return  # unparseable shell — leave it to the cluster to surface
        if "-c" not in parts:
            return
        reason = (
            "it is a `python -c` inline one-liner, whose quoted code argument "
            "cannot survive the unquoted `time $EXECUTOR` word-split"
        )
    raise errors.SpecInvalid(
        f"job_env['EXECUTOR'] {executor!r} is not the dispatcher command: {reason}. "
        "EXECUTOR is shipped comma-delimited via `qsub -v ...,EXECUTOR=...` and run "
        "as `time $EXECUTOR` UNQUOTED, so it MUST be the comma-free, space-safe "
        f"dispatcher (default {_DEFAULT_EXECUTOR_CMD!r}) — the proving-run-#2 canary "
        "died `SyntaxError` when a per-task one-liner was placed here (the comma "
        "truncated the -v value and `-c import` word-split). The REAL per-task "
        "command belongs in the sidecar's `executor` field (write it with "
        "`write-run-sidecar`); the cluster-side dispatcher reads it from JSON and "
        "runs it correctly. Drop the EXECUTOR override — build-submit-spec defaults "
        "it to the dispatcher."
    )


# --- #292 Bug B: EXECUTOR $VAR ↔ exported-env cross-check -------------------
#
# Vars the cluster-side dispatcher / array template inject per-task that are
# NOT carried in the built ``job_env`` (so they wouldn't show up in
# ``job_env.keys()``): the per-task result dir and the task/run identity. A
# ``$RESULT_DIR`` / ``$TASK_ID`` reference is legitimate and must not be
# flagged. Everything else the framework forwards rides ``job_env`` itself.
_FRAMEWORK_INJECTED_VARS: frozenset[str] = frozenset(
    {"RESULT_DIR", "HPC_RESULT_DIR", "TASK_ID", "HPC_TASK_ID", "RUN_ID", "HPC_RUN_ID"}
)

# Common cluster shell vars an EXECUTOR may legitimately inherit from the job
# environment (the user's ``--data $SCRATCH/...`` etc.). The dispatcher's own
# ``_warn_unset_kwarg_refs`` deliberately stays in the unambiguous ``HPC_KW_``
# namespace because a bare ``$SAMPLES`` "can't be reliably told apart from a
# genuine env var"; the build-time refuse resolves that ambiguity with an
# explicit allowlist (exact names + scheduler/runtime prefixes) so a real
# inherited var is never mistaken for an unset-kwarg typo.
_INHERITED_SHELL_VARS: frozenset[str] = frozenset(
    {
        "HOME", "PATH", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "SHLVL",
        "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "TERM", "HOSTNAME", "HOST",
        "SCRATCH", "WORK", "PROJECT", "GROUP", "LD_LIBRARY_PATH", "LIBRARY_PATH",
        "PYTHONPATH", "MANPATH", "CPATH", "CUDA_VISIBLE_DEVICES", "NSLOTS",
        "JOB_ID", "JOB_NAME", "NHOSTS", "NQUEUES", "REPO_DIR",
    }
)  # fmt: skip
_INHERITED_SHELL_PREFIXES: tuple[str, ...] = (
    "SLURM_", "SGE_", "PBS_", "OMPI_", "PMI_", "PMIX_", "MPI_", "OMP_",
    "CUDA_", "NCCL_", "I_MPI_", "HPC_AGENT_", "HPC_SERVICE_",
)  # fmt: skip
# ``$NAME`` or ``${NAME}`` / ``${NAME:-default}``. The braced form keeps any
# trailing modifier so a default-providing reference (``:-``/``-``/``:=``/``=``)
# can be recognised as safe (it never expands to empty on an unset var).
_VAR_REF_RE = re.compile(
    r"\$\{(?P<bname>[A-Za-z_][A-Za-z0-9_]*)(?P<bmod>[^}]*)\}"
    r"|\$(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
)
_DEFAULT_MOD_RE = re.compile(r"^:?[-=]")


def _is_inherited_shell_var(name: str) -> bool:
    """True when *name* is a cluster shell var an EXECUTOR may legitimately use."""
    return name in _INHERITED_SHELL_VARS or name.startswith(_INHERITED_SHELL_PREFIXES)


def _iter_var_refs(executor: str):
    """Yield ``(var_name, is_defaulted)`` for every ``$VAR`` ref in *executor*.

    ``is_defaulted`` is True for the ``${VAR:-x}`` / ``${VAR-x}`` (and ``:=``)
    fallback forms, which are safe even when ``VAR`` is unset and so are never
    flagged.
    """
    for m in _VAR_REF_RE.finditer(executor):
        if m.group("name") is not None:
            yield m.group("name"), False
        else:
            yield m.group("bname"), bool(_DEFAULT_MOD_RE.match(m.group("bmod") or ""))


def _resolve_kwargs_keys(experiment_dir: Path | None) -> set[str] | None:
    """Lowercased per-task kwarg names from ``<experiment_dir>/.hpc/tasks.py``.

    Returns None when the kwarg set can't be *positively* established — no
    experiment_dir, no tasks.py, an import/resolve error, or a zero-task
    sweep. The var-reference check skips entirely on None, so an unknowable
    kwarg set can never produce a false refusal. Best-effort: importing the
    user's tasks.py is a read the framework already does to compute cmd_sha
    (``compute_cmd_sha(load_tasks_module(...))``), so this introduces no new
    class of side effect; any failure degrades to "skip the check".
    """
    if experiment_dir is None:
        return None
    tasks_py = Path(experiment_dir) / ".hpc" / "tasks.py"
    if not tasks_py.is_file():
        return None
    try:
        from hpc_agent import load_tasks_module

        mod = load_tasks_module(tasks_py)
        if int(mod.total()) < 1:
            return None
        kwargs = mod.resolve(0)
        if not isinstance(kwargs, dict):
            return None
        return {str(k).lower() for k in kwargs}
    except Exception:  # noqa: BLE001 — any failure → degrade to "skip"
        return None


def _check_executor_var_references(
    executor: str, *, job_env_keys: set[str], kwargs_keys: set[str] | None
) -> None:
    """Refuse an EXECUTOR that references a ``$VAR`` the dispatcher never exports.

    Covered references (never flagged): a key already in *job_env* (forwarded
    to the job env verbatim), a framework-injected identity/result var, an
    inherited cluster shell var, a ``:-``-defaulted reference, and — the point
    of the check — a real task kwarg, exported by the dispatcher as both bare
    ``$<NAME>`` and ``$HPC_KW_<NAME>``. Anything else (the empirical
    ``$SAMPLES`` for a ``samples`` that isn't a swept axis) is an unset-expands-
    to-empty bug; raise :class:`errors.SpecInvalid` with the two resolutions.

    No-ops when *kwargs_keys* is None (the kwarg set couldn't be established) —
    the conservative posture that only refuses on a *provable* miss.
    """
    # A wrong-case reference to a REAL kwarg ($seed for kwarg seed) is its own
    # provable miss — the dispatcher exports the bare/namespaced form uppercased,
    # so the lowercase spelling expands to empty. Caught here so the build path
    # surfaces it alongside the unset-var check below.
    _check_executor_kwarg_casing(executor, kwargs_keys=kwargs_keys)
    if kwargs_keys is None or "$" not in executor:
        return
    covered = _FRAMEWORK_INJECTED_VARS | set(job_env_keys)
    for ref, defaulted in _iter_var_refs(executor):
        if defaulted or ref in covered or _is_inherited_shell_var(ref):
            continue
        kwarg = ref[len("HPC_KW_") :].lower() if ref.startswith("HPC_KW_") else ref.lower()
        if kwarg in kwargs_keys:
            continue
        raise errors.SpecInvalid(
            f"EXECUTOR references ${ref} but no {kwarg!r} kwarg is exported and it "
            "is not a framework or inherited cluster variable. The cluster-side "
            "dispatcher exports a task kwarg as $<NAME> / $HPC_KW_<NAME> only for "
            f"keys tasks.resolve(i) returns (here: {sorted(kwargs_keys)}). A "
            f"reference the dispatcher never sets expands to EMPTY and the command "
            "fails downstream (e.g. argparse 'expected one argument'). Resolve by "
            "either:\n"
            f"  • adding {kwarg!r} to a homogeneous_axes / fixed_params block so "
            "tasks.resolve() returns it (then it's exported), or\n"
            f"  • dropping the ${ref} reference from the EXECUTOR command."
        )


# --- str.format {placeholder} leakage into the EXECUTOR --------------------
#
# The cluster-side dispatcher str.format()s ONLY result_dir_template (with
# run_id / task_id / swept kwargs); it runs the EXECUTOR through the shell
# verbatim (``subprocess.Popen(executor, shell=True)``). A ``{run_id}`` /
# ``{seed}`` token in the EXECUTOR therefore never expands — it reaches the
# program LITERALLY (the empirical 2026-06-06 demo:
# ``--output-file results/{run_id}/seed_{seed}/metrics.json`` would write under
# a directory named ``{run_id}``). The per-task output dir is ``$RESULT_DIR``;
# the {placeholders} belong in result_dir_template.
#
# Negative lookbehind on ``$`` so shell parameter expansion ``${VAR}`` is not
# mistaken for a format placeholder. Empty ``{}`` (``find -exec``), comma lists
# (``{a,b}``) and numeric ranges (``{1..9}``) don't match the named-identifier
# shape, so they're left alone.
_FORMAT_PLACEHOLDER_RE = re.compile(r"(?<!\$)\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _check_executor_format_placeholders(executor: str) -> None:
    """Raise :class:`errors.SpecInvalid` if *executor* carries ``{name}`` tokens.

    Those are result_dir_template syntax; the dispatcher never ``str.format``\\ s
    the EXECUTOR, so the token reaches the program verbatim.
    """
    found = sorted(set(_FORMAT_PLACEHOLDER_RE.findall(executor or "")))
    if not found:
        return
    refs = ", ".join("{" + name + "}" for name in found)
    raise errors.SpecInvalid(
        f"EXECUTOR carries str.format placeholder(s) {refs}, but the cluster-side "
        "dispatcher str.format()s only result_dir_template — it runs the EXECUTOR "
        "through the shell verbatim, so these tokens reach the program LITERALLY "
        "(e.g. output written under a directory named '{run_id}'). Resolve by:\n"
        "  • routing per-task output through $RESULT_DIR (the dispatcher sets it "
        "per task and promotes metrics.json into the result_dir_template dir), "
        'e.g. --output-file "$RESULT_DIR/metrics.json"; and\n'
        "  • moving the {run_id}/{task_id}/{<kwarg>} placeholders into "
        "result_dir_template, where the dispatcher renders them — reference a swept "
        "kwarg in the command itself as $<NAME> / $HPC_KW_<NAME> (uppercase)."
    )


def _check_executor_kwarg_casing(executor: str, *, kwargs_keys: set[str] | None) -> None:
    """Raise :class:`errors.SpecInvalid` for a swept-kwarg ``$ref`` in the wrong case.

    The dispatcher exports each ``tasks.resolve(i)`` kwarg as ``$<KEY.upper()>``
    AND ``$HPC_KW_<KEY.upper()>`` (dispatch.py does ``env[key.upper()]``). A
    lowercase/mixed-case reference to a real kwarg (``$seed`` for the ``seed``
    kwarg) is never set under that spelling and expands to EMPTY — the empirical
    2026-06-06 demo, where the agent "fixed" a correct ``$SEED`` into a broken
    ``$seed``. No-ops when the kwarg set is unknowable (only refuse on a provable
    miss).
    """
    if kwargs_keys is None or "$" not in executor:
        return
    for ref, defaulted in _iter_var_refs(executor):
        if defaulted:
            continue
        if ref.startswith("HPC_KW_"):
            kwarg = ref[len("HPC_KW_") :].lower()
            exported = "HPC_KW_" + kwarg.upper()
        else:
            kwarg = ref.lower()
            exported = kwarg.upper()
        if kwarg in kwargs_keys and ref != exported:
            raise errors.SpecInvalid(
                f"EXECUTOR references ${ref}, but the cluster-side dispatcher exports "
                f"the {kwarg!r} kwarg only as ${exported} / $HPC_KW_{kwarg.upper()} "
                f"(it does env[key.upper()]). The lowercase/mixed-case ${ref} is never "
                "set and expands to EMPTY (the command then fails downstream, e.g. "
                f"argparse 'expected one argument'). Use ${exported} or "
                f"$HPC_KW_{kwarg.upper()}."
            )


# Script-file extensions that, as a *bare* single token, need an interpreter
# (or an executable path) to run. A per-task ``executor`` of just ``train.py``
# reaches the cluster as ``cd "$REPO_DIR" && train.py`` and exits 127 — the
# shell has no interpreter to hand it to (proving-run-5 finding 17).
_BARE_SCRIPT_EXTENSIONS: tuple[str, ...] = (".py", ".sh", ".r", ".jl")


def _is_bare_script_name(executor: str | None) -> bool:
    """True when *executor* is a lone script filename with no interpreter/path.

    The unrunnable shape proving-run-5 finding 17 named: a single token (no
    whitespace ⇒ no ``python`` / ``Rscript`` interpreter prefix) that ends in a
    script extension (``.py`` / ``.sh`` / ``.R`` / ``.jl``) and carries no path
    separator (``/`` or ``\\`` ⇒ not a ``./run.sh``-style runnable path). Run
    verbatim by the cluster dispatcher it becomes ``command not found`` (exit
    127). ``python train.py`` (interpreter prefix), ``./train.py`` and
    ``scripts/train.py`` (path) are all runnable and return False.

    The single owner of this shape check: ``ops.submit_flow._is_runnable_executor``
    (the sidecar gate) and :func:`_check_bare_script_executor` (the build/write
    boundary) both call it, so a doomed sidecar is never written and never shipped.
    """
    if not executor:
        return False
    token = executor.strip()
    if len(token.split()) != 1:
        return False
    if "/" in token or "\\" in token:
        return False
    return token.lower().endswith(_BARE_SCRIPT_EXTENSIONS)


def _check_bare_script_executor(executor: str) -> None:
    """Refuse a bare script-file token (``train.py``) as a per-task executor.

    Proving-run-5 finding 17: the dispatcher reads ``sidecar.executor`` and runs
    it verbatim, so a lone ``train.py`` (no interpreter prefix, no path
    separator) becomes ``cd "$REPO_DIR" && train.py`` and exits 127 (command not
    found). The shape check is the single owner in :func:`_is_bare_script_name`
    (the same predicate the submit-flow sidecar gate uses); applying it here keeps
    ``write-run-sidecar`` from ever writing a doomed sidecar in the first place.
    """
    if _is_bare_script_name(executor):
        token = executor.strip()
        raise errors.SpecInvalid(
            f"per-task executor {executor!r} is a bare script name with no "
            "interpreter and no path separator, so the cluster dispatcher runs it "
            'verbatim (`cd "$REPO_DIR" && '
            f"{token}`) and exits 127 (command not found). Prefix the interpreter "
            f"— e.g. `python {token}` (or `bash {token}` / `Rscript {token}`) — "
            f"or use an executable path like `./{token}`."
        )


def _warn_task_interface_blind_executor(executor: str) -> None:
    """WARN (never refuse) on an executor that consumes NONE of the task contract.

    Run #6 finding F1 generalized finding 17 from an extension proxy to the
    underlying PROPERTY: the per-task contract offers ``$RESULT_DIR`` /
    ``$HPC_RESULT_DIR``, ``$TASK_ID`` / ``$HPC_TASK_ID``, and the swept
    kwargs as ``$HPC_KW_*`` / bare ``$<NAME>`` refs — an executor that is a
    single bare token with no arguments and no ``$`` reference consumes none
    of them, so every task would run the IDENTICAL argument-less command.
    The empirical case was the hand-authored extension-less token
    ``monte_carlo_pi`` (exit 127 on the cluster, canary_failed).

    Warn-loud, not refuse, by decision: a blanket refusal is UNWINNABLE —
    this gate cannot know the cluster's ``$PATH``, and a bare ``mybinary``
    may be a real installed wrapper that reads ``$HPC_TASK_ID`` /
    ``$HPC_KW_*`` internally (the legitimate escape hatch the message
    names). The canary stays the hard backstop: a genuinely broken one
    hard-fails there on ONE task ("survival over strictness"). The REFUSAL
    set is unchanged — extension-bearing bare script names
    (:func:`_check_bare_script_executor`), bare ``module:function``,
    dispatcher-shaped, format placeholders, wrong-case kwargs.
    """
    import warnings

    token = (executor or "").strip()
    if not token or len(token.split()) != 1:
        return  # arguments present — the command engages the task interface
    if "$" in token:
        return  # references a contract/env var — not interface-blind
    warnings.warn(
        f"per-task executor {token!r} is TASK-INTERFACE-BLIND: a single bare "
        "token with no arguments and no $RESULT_DIR/$HPC_RESULT_DIR, "
        "$TASK_ID/$HPC_TASK_ID, or $HPC_KW_*/swept-kwarg reference — every "
        "task would run the identical argument-less command. If it is not a "
        "real installed command on the cluster's $PATH it will exit 127; if "
        "it produces no per-task output the canary will hard-fail it on one "
        "task before the array launches. This is legitimate ONLY for a PATH "
        "wrapper that reads $HPC_TASK_ID / $HPC_KW_* internally; otherwise "
        "use a real per-task command (e.g. `python executors/train.py "
        '--out "$RESULT_DIR/metrics.json"`).',
        RuntimeWarning,
        stacklevel=3,
    )


def check_per_task_executor(executor: str, *, experiment_dir: Path | None = None) -> None:
    """Boundary guard for the REAL per-task EXECUTOR (the sidecar's ``executor``).

    The cluster dispatcher reads ``sidecar.executor`` and runs it per task, so a
    structurally broken command here fails silently cluster-side. Catches the
    shapes the ``#162`` dispatcher-self-recursion guard does NOT cover:

    1. str.format ``{placeholder}`` tokens — the dispatcher formats only
       result_dir_template (:func:`_check_executor_format_placeholders`).
    2. a bare ``module:function`` (:func:`_check_bare_module_executor`) or a bare
       script name like ``train.py`` (:func:`_check_bare_script_executor`,
       proving-run-5 finding 17) — both exec as command-not-found (exit 127).
    3. a swept-kwarg ``$ref`` in the wrong case
       (:func:`_check_executor_kwarg_casing`).

    Additionally WARNS — never refuses — on a task-interface-blind executor
    (a single bare token consuming none of the per-task contract,
    :func:`_warn_task_interface_blind_executor`, run #6 F1): the refusal is
    unwinnable without knowing the cluster's ``$PATH``, so the canary stays
    the hard backstop.

    Deliberately omits the job_env-aware unset-var check
    (:func:`_check_executor_var_references`): at sidecar-write time the assembled
    job_env (MODULES / CONDA_* / REPO_DIR / ...) isn't known, and the per-task
    command legitimately inherits those at runtime, so flagging them would
    false-positive. ``build-submit-spec`` runs the full check where job_env IS
    known.
    """
    _check_executor_format_placeholders(executor)
    # A bare ``module:function`` here is the ridge_imp exit-127 class: the
    # dispatcher reads THIS field and execs it as a shell command, so a lone
    # dotted-module:function (a hand-rolled / divergent-build sidecar) becomes
    # command-not-found. The interview emits the correct ``run-module`` form and
    # resolve-submit-inputs writes it deterministically; this is defense-in-depth
    # at the field the dispatcher actually consumes.
    _check_bare_module_executor(executor)
    # A bare script name (``train.py``) is the sibling exit-127 shape (finding 17).
    _check_bare_script_executor(executor)
    # Run #6 F1: the extension-LESS bare token (``monte_carlo_pi``) is
    # refusal-unwinnable (it may be a real $PATH binary) — WARN loudly on the
    # task-interface-blind property instead; the canary is the hard backstop.
    _warn_task_interface_blind_executor(executor)
    if "$" in (executor or ""):
        _check_executor_kwarg_casing(executor, kwargs_keys=_resolve_kwargs_keys(experiment_dir))
