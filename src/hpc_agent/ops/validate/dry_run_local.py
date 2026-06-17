"""``dry-run-local`` primitive — the local pre-flight EXECUTION gate (#205).

Every other pre-submit validator is STATIC/structural — ``check-preflight``
probes the env, ``validate-executor-signatures`` introspects the signature
(it calls ``resolve(i)`` on a sample but never RUNS the executor),
``validate-input-dataset`` checks the filesystem, the QoS/walltime ones are
numeric, ``compute_cmd_sha`` calls ``resolve()`` only to hash. The earliest
the user's executor command is actually EXECUTED is the cluster-side canary
(``verify-canary``) — which runs *after* rsync_push + deploy_runtime +
sbatch/qsub. A purely local smoke-execution catches the broken-grid class
(bad import, mis-wired ``HPC_KW_*`` arg, broken ``result_dir_template``)
BEFORE any SSH.

This primitive does two things, split so the cheap one is default-on:

1. **Template-render check (DEFAULT-ON).** Re-uses the resolve(i) sampler
   the signature validator / ``compute_cmd_sha`` already walk. For the first
   ``sample_n_tasks`` ids it renders ``result_dir_template`` exactly as the
   cluster dispatcher's ``_format_result_dir`` will (``str.format`` over
   ``task_id`` + ``run_id`` + kwargs) and flags (a) an unfilled ``{field}``
   the kwargs don't supply (``KeyError`` cluster-side → every task dies
   ``result_dir_template references unknown key``) and (b) two distinct ids
   that render to the SAME directory (a silent overwrite — wave N clobbers
   wave M's ``metrics.json``, and the combiner under-counts).

2. **Executor smoke-exec (OPT-IN, ``smoke=true``).** Actually runs the
   executor for ONE sampled grid point locally, mirroring
   ``execution/mapreduce/dispatch.py`` semantics (export ``HPC_KW_*`` + bare
   uppercase, run the command under a shell), to catch import / arg-binding
   bugs before any cluster cost. The default command is the executor verbatim;
   a ``smoke_command`` override lets the executor opt into a cheap
   import/``--help`` probe. Scoped to "broken code, not broken cluster": a
   local run can't stand in for modules/GPUs/scale, so this COMPLEMENTS the
   canary — it never replaces it.

Like every validator: typed spec in → ``ValidatorFinding`` list out. A
finding never raises — "non-blocking" here means only that the gate
*returns* findings instead of raising, NOT that an error is advisory.
Severity decides enforcement: ``result_dir_collision`` /
``template_unfilled_field`` / the smoke-exec failures are ``severity=
"error"``, and ``meta/validate_campaign.py::_aggregate_overall`` escalates
ANY error to ``overall="fail"`` (exit 1). A ``fail`` is a hard abort the
submit cascade must honor — a collision here is a true positive (two ids
write one dir; the second clobbers the first's ``metrics.json``), never a
false positive to wave through. It only failed to bite on the cluster when
an *earlier* error killed the run first.
"""

from __future__ import annotations

import string
from typing import TYPE_CHECKING

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.validators.dry_run_local import (
    DryRunLocalResult,
    DryRunLocalSpec,
)
from hpc_agent._wire.workflows.validate_campaign import ValidatorFinding

if TYPE_CHECKING:
    from pathlib import Path
    from types import ModuleType

_VALIDATOR = "dry-run-local"

# The kwargs the dispatcher always injects into the template context on top
# of the user's resolve(i) kwargs. Mirrors ``_format_result_dir`` in
# ``execution/mapreduce/dispatch.py`` (``{"task_id": ..., "run_id": ...}``) so a
# template that legitimately references {task_id}/{run_id} isn't false-flagged
# as an unfilled placeholder here.
_RESERVED_TEMPLATE_KEYS = ("task_id", "run_id")


def _template_field_names(template: str) -> list[str]:
    """Return the ``{field}`` names a ``str.format`` template references.

    Uses ``string.Formatter().parse`` — the same parser ``str.format`` uses —
    so we see exactly the named replacement fields the dispatcher will try to
    fill, ignoring literal text and escaped ``{{``/``}}``. Positional /
    auto-numbered fields (``{}`` / ``{0}``) have a ``None`` or all-digit field
    name and are skipped: result-dir templates are keyed by name in practice,
    and a positional field would already explode under ``.format(**ctx)`` the
    same way the dispatcher does.
    """
    names: list[str] = []
    for _literal, field_name, _spec, _conv in string.Formatter().parse(template):
        if field_name is None:
            continue
        # ``{kwargs[col]}`` / ``{obj.attr}`` — take the root identifier, which
        # is the key that must exist in the format context.
        root = field_name.split("[", 1)[0].split(".", 1)[0]
        if root and not root.isdigit():
            names.append(root)
    return names


def _render_result_dir(template: str, *, task_id: int, run_id: str, kwargs: dict) -> str:
    """Render *template* exactly as the cluster dispatcher's ``_format_result_dir``.

    Kept byte-for-byte consistent with ``execution/mapreduce/dispatch.py``: the
    context is ``{task_id, run_id, **kwargs}`` (kwargs win on collision, the
    documented behaviour — the user's tasks.py controls the namespace), and a
    missing key raises ``KeyError``. We render here so a template that would
    ``KeyError`` cluster-side fails LOCALLY instead, before any SSH.
    """
    ctx = {"task_id": task_id, "run_id": run_id, **kwargs}
    return template.format(**ctx)


def _check_templates(
    tasks_module: ModuleType,
    spec: DryRunLocalSpec,
) -> list[ValidatorFinding]:
    """Default-on layer: render ``result_dir_template`` for sampled ids.

    Flags unfilled placeholder fields (a ``KeyError`` the dispatcher would
    raise per-task) and cross-id collisions (two distinct ids → one dir →
    silent overwrite). Re-uses the resolve(i) sampler shape from
    ``validate-executor-signatures`` rather than duplicating a walk.
    """
    findings: list[ValidatorFinding] = []
    try:
        n = int(tasks_module.total())
    except Exception as exc:  # noqa: BLE001 — validator boundary, never crash
        return [
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code="tasks_py_contract_error",
                message=f"tasks.total() raised: {type(exc).__name__}: {exc}",
                suggested_fix=(
                    "tasks.total() must return an int. Inspect the module for "
                    "import-time failures or a broken total() implementation."
                ),
                evidence={"tasks_py_path": spec.tasks_py_path},
            )
        ]

    # id -> rendered dir, for the collision pass. Keyed by the dir string so
    # the first id that produced it is recoverable for the evidence payload.
    rendered: dict[str, int] = {}
    for i in range(min(n, spec.sample_n_tasks)):
        try:
            kwargs = tasks_module.resolve(i)
        except Exception as exc:  # noqa: BLE001 — validator boundary
            findings.append(
                ValidatorFinding(
                    validator=_VALIDATOR,
                    severity="error",
                    code="tasks_py_contract_error",
                    message=f"tasks.resolve({i}) raised: {type(exc).__name__}: {exc}",
                    suggested_fix=(
                        "tasks.resolve(i) must return a dict for every i in "
                        "range(tasks.total()). Inspect the module for an "
                        "off-by-one or a broken resolve() branch."
                    ),
                    evidence={"tasks_py_path": spec.tasks_py_path, "task_id": i},
                )
            )
            return findings
        if not isinstance(kwargs, dict):
            findings.append(
                ValidatorFinding(
                    validator=_VALIDATOR,
                    severity="error",
                    code="resolve_returned_non_dict",
                    message=(
                        f"tasks.resolve({i}) returned {type(kwargs).__name__}; "
                        "the framework requires a dict so kwargs can be **-unpacked"
                    ),
                    evidence={"task_id": i},
                )
            )
            return findings

        try:
            result_dir = _render_result_dir(
                spec.result_dir_template, task_id=i, run_id=spec.run_id, kwargs=kwargs
            )
        except (KeyError, IndexError) as exc:
            # The dispatcher raises exactly this (KeyError) and dies the task
            # with "result_dir_template references unknown key". Surface the
            # missing field + what WAS available so the fix is mechanical.
            available = sorted({*_RESERVED_TEMPLATE_KEYS, *kwargs})
            referenced = _template_field_names(spec.result_dir_template)
            missing = [f for f in referenced if f not in {*_RESERVED_TEMPLATE_KEYS, *kwargs}]
            findings.append(
                ValidatorFinding(
                    validator=_VALIDATOR,
                    severity="error",
                    code="template_unfilled_field",
                    message=(
                        f"result_dir_template {spec.result_dir_template!r} references "
                        f"{exc.args[0]!r} which tasks.resolve({i}) does not supply — "
                        "the cluster dispatcher would KeyError and die every task."
                    ),
                    suggested_fix=(
                        f"Either add {missing or [exc.args[0]]} to tasks.resolve(...)'s "
                        f"kwargs or change result_dir_template to use one of {available}."
                    ),
                    evidence={
                        "task_id": i,
                        "template": spec.result_dir_template,
                        "missing_fields": missing or [str(exc.args[0])],
                        "available_keys": available,
                    },
                )
            )
            # Every id with this template would KeyError identically; one
            # finding is enough to act on. Stop the render loop.
            return findings
        except (ValueError, AttributeError, TypeError) as exc:
            # A malformed format spec (e.g. ``{seed:d}`` against a str) or an
            # attribute/index access the dispatcher would also hit. Report and
            # keep going — a different id might render fine and we still want
            # the collision view.
            findings.append(
                ValidatorFinding(
                    validator=_VALIDATOR,
                    severity="error",
                    code="template_render_error",
                    message=(
                        f"result_dir_template {spec.result_dir_template!r} failed to "
                        f"render for task {i}: {type(exc).__name__}: {exc}"
                    ),
                    suggested_fix=(
                        "Check the template's format specs against the kwarg types "
                        "tasks.resolve(i) returns."
                    ),
                    evidence={"task_id": i, "template": spec.result_dir_template},
                )
            )
            continue

        prior = rendered.get(result_dir)
        if prior is not None:
            findings.append(
                ValidatorFinding(
                    validator=_VALIDATOR,
                    severity="error",
                    code="result_dir_collision",
                    message=(
                        f"tasks {prior} and {i} both render result_dir {result_dir!r} — "
                        "the second task would overwrite the first's metrics.json and "
                        "the combiner would silently under-count the grid."
                    ),
                    suggested_fix=(
                        "Add a kwarg that differs between these tasks (or {task_id}) "
                        "to result_dir_template so every task writes a distinct dir."
                    ),
                    evidence={
                        "task_ids": [prior, i],
                        "result_dir": result_dir,
                        "template": spec.result_dir_template,
                    },
                )
            )
        else:
            rendered[result_dir] = i

    return findings


def _dispatch_env(kwargs: dict, *, task_id: int, run_id: str) -> dict[str, str]:
    """Build the per-task env the cluster dispatcher exports, for the smoke run.

    Mirrors the kwarg-export contract in ``execution/mapreduce/dispatch.py``:
    each kwarg ships as ``HPC_KW_<KEY>`` (namespaced, collision-free) and —
    unless ``HPC_KW_NAMESPACE_ONLY=1`` is already in the inherited env — also
    as bare uppercase ``<KEY>`` (the legacy contract). Plus the per-task /
    per-run identity vars. We layer onto the current ``os.environ`` so a
    ``python ...`` smoke command still sees PATH / the active venv exactly as
    the dispatcher's child does on the cluster.
    """
    import os

    env = dict(os.environ)
    env["HPC_TASK_ID"] = str(task_id)
    env["HPC_RUN_ID"] = run_id
    namespace_only = env.get("HPC_KW_NAMESPACE_ONLY", "").strip() == "1"
    for key, value in kwargs.items():
        s = str(value)
        env[f"HPC_KW_{key.upper()}"] = s
        if not namespace_only:
            env[key.upper()] = s
    return env


def _smoke_exec(
    tasks_module: ModuleType,
    spec: DryRunLocalSpec,
    experiment_dir: Path,
) -> list[ValidatorFinding]:
    """Opt-in layer: run the executor once locally under dispatch semantics.

    Resolves kwargs for ``spec.smoke_task_id``, exports the ``HPC_KW_*`` env
    the dispatcher would, and runs ``smoke_command`` (or ``executor``) under a
    shell with a hard timeout. A non-zero exit / timeout / import error
    becomes a finding carrying the failing id + the captured stderr tail, so
    the cascade can surface the raw error verbatim (as verify-canary does).
    """
    import subprocess

    from hpc_agent.execution.mapreduce.dispatch import _executor_reinvokes_dispatcher

    if not spec.executor:
        return [
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code="smoke_executor_missing",
                message="smoke=true requires `executor` (the real per-task command).",
                suggested_fix="Pass the per-task command, e.g. `python train.py --seed $SEED`.",
                evidence={},
            )
        ]
    # Refuse the #162 self-recursion footgun: a smoke command that IS the
    # dispatcher would re-enter dispatch forever. The cluster dispatcher
    # rejects this too; catch it before spawning anything locally.
    command = spec.smoke_command or spec.executor
    if _executor_reinvokes_dispatcher(spec.executor, dispatcher_path="_hpc_dispatch.py"):
        return [
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code="smoke_executor_is_dispatcher",
                message=(
                    f"executor {spec.executor!r} re-invokes the dispatcher itself — "
                    "refusing to smoke-run a self-recursive command (#162)."
                ),
                suggested_fix=(
                    "Set executor to the real per-task command, not the job-script's "
                    "dispatcher command."
                ),
                evidence={"executor": spec.executor},
            )
        ]

    try:
        kwargs = tasks_module.resolve(spec.smoke_task_id)
    except Exception as exc:  # noqa: BLE001 — validator boundary
        return [
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code="tasks_py_contract_error",
                message=(
                    f"tasks.resolve({spec.smoke_task_id}) raised: {type(exc).__name__}: {exc}"
                ),
                evidence={"task_id": spec.smoke_task_id, "tasks_py_path": spec.tasks_py_path},
            )
        ]
    if not isinstance(kwargs, dict):
        return [
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code="resolve_returned_non_dict",
                message=(
                    f"tasks.resolve({spec.smoke_task_id}) returned {type(kwargs).__name__}; "
                    "the framework requires a dict"
                ),
                evidence={"task_id": spec.smoke_task_id},
            )
        ]

    env = _dispatch_env(kwargs, task_id=spec.smoke_task_id, run_id=spec.run_id)
    try:
        proc = subprocess.run(
            command,
            shell=True,  # noqa: S602 — same shell-string contract as the dispatcher
            cwd=str(experiment_dir),
            env=env,
            capture_output=True,
            timeout=spec.smoke_timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        tail = _decode_tail(exc.stderr)
        return [
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code="smoke_timeout",
                message=(
                    f"local smoke run of {command!r} exceeded {spec.smoke_timeout_sec}s "
                    f"for task {spec.smoke_task_id} and was killed."
                ),
                suggested_fix=(
                    "Provide a cheap smoke_command (import / --help probe) instead of "
                    "the full per-task run, or raise smoke_timeout_sec."
                ),
                evidence={
                    "task_id": spec.smoke_task_id,
                    "command": command,
                    "timeout_sec": spec.smoke_timeout_sec,
                    "stderr_tail": tail,
                },
            )
        ]
    except OSError as exc:
        return [
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code="smoke_spawn_error",
                message=f"could not launch local smoke run of {command!r}: {exc}",
                evidence={"task_id": spec.smoke_task_id, "command": command},
            )
        ]

    if proc.returncode != 0:
        tail = _decode_tail(proc.stderr)
        # Classify a couple of high-signal cases so the cascade can branch
        # the same way verify-canary does on the cluster side.
        haystack = tail.lower()
        if "modulenotfounderror" in haystack:
            code, hint = (
                "smoke_import_error",
                "Install the missing module or fix the import path before submitting.",
            )
        elif "importerror" in haystack:
            code, hint = (
                "smoke_import_error",
                "Fix the executor's import error before submitting — the cluster will "
                "fail identically.",
            )
        else:
            code, hint = (
                "smoke_nonzero_exit",
                "Fix the executor so a local smoke run exits 0, or supply an "
                "import/--help smoke_command that does.",
            )
        return [
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code=code,
                message=(
                    f"local smoke run of {command!r} for task {spec.smoke_task_id} "
                    f"exited {proc.returncode} — the cluster would fail the same way."
                ),
                suggested_fix=hint,
                evidence={
                    "task_id": spec.smoke_task_id,
                    "command": command,
                    "returncode": proc.returncode,
                    "stderr_tail": tail,
                },
            )
        ]
    return []


# How many trailing chars of the smoke run's stderr to retain in a finding.
# Bounded so a chatty executor can't bloat the envelope; the tail is what a
# user needs to see the traceback, same rationale as the dispatcher's
# ``_STDERR_TAIL_BYTES``.
_STDERR_TAIL_CHARS = 4000


def _decode_tail(raw: bytes | None) -> str:
    """Decode captured stderr bytes to a bounded, UTF-8-safe tail string."""
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")[-_STDERR_TAIL_CHARS:]


@primitive(
    name=_VALIDATOR,
    verb="validate",
    side_effects=[],
    idempotent=True,
    agent_facing=True,
)
def dry_run_local(
    experiment_dir: Path,
    *,
    spec: DryRunLocalSpec,
) -> DryRunLocalResult:
    """Local pre-flight execution gate — render templates (always) + smoke-exec (opt-in).

    Returns a :class:`DryRunLocalResult` whose ``findings`` is the list of
    detected issues (empty == pass). The default-on template-render layer
    runs unconditionally; the executor smoke-exec runs only when
    ``spec.smoke`` is set. Common ``code`` values:

    * ``tasks_py_missing`` — tasks.py not on disk yet.
    * ``tasks_py_import_error`` — tasks.py raises on import.
    * ``tasks_py_contract_error`` — total()/resolve(i) raised.
    * ``resolve_returned_non_dict`` — resolve(i) returned a non-dict.
    * ``template_unfilled_field`` — result_dir_template references a key the
      kwargs don't supply (a per-task KeyError on the cluster).
    * ``template_render_error`` — the template failed to render (bad format spec).
    * ``result_dir_collision`` — two distinct ids render the same dir (silent
      overwrite of the first task's output).
    * ``smoke_executor_missing`` / ``smoke_executor_is_dispatcher`` — opt-in
      misconfig before any spawn.
    * ``smoke_import_error`` / ``smoke_nonzero_exit`` / ``smoke_timeout`` /
      ``smoke_spawn_error`` — the executor failed the local smoke run.
    """
    findings: list[ValidatorFinding] = []
    tasks_py = experiment_dir / spec.tasks_py_path

    if not tasks_py.is_file():
        findings.append(
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="warning",
                code="tasks_py_missing",
                message=f"tasks.py not found at {tasks_py}",
                suggested_fix="Run /interview to generate tasks.py.",
                evidence={"path": str(tasks_py)},
            )
        )
        return DryRunLocalResult(findings=findings)

    from hpc_agent import load_tasks_module

    try:
        tasks_module = load_tasks_module(tasks_py)
    except Exception as exc:  # noqa: BLE001 — validator surfaces, doesn't crash
        findings.append(
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code="tasks_py_import_error",
                message=f"failed to import tasks.py: {exc}",
                evidence={"path": str(tasks_py)},
            )
        )
        return DryRunLocalResult(findings=findings)

    # Layer 1 (default-on): render result_dir_template for the sampled ids.
    findings.extend(_check_templates(tasks_module, spec))

    # Layer 2 (opt-in): actually execute the executor once locally. Run it
    # even when the template layer found issues — the two layers catch
    # disjoint bug classes, and the agent benefits from seeing both at once
    # rather than fixing the template then re-running only to hit an import
    # error on the next pass.
    if spec.smoke:
        findings.extend(_smoke_exec(tasks_module, spec, experiment_dir))

    return DryRunLocalResult(findings=findings)
