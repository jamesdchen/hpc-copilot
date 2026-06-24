"""``interview`` primitive — persist campaign intent alongside an agent-written tasks.py.

The interview-time leak today is that the chat between hpc-agent and
either an external orchestrator or a human produces *only* a
tasks.py; the *why* (goal, budget, abort criterion, transcript, who
decided) lives in transient session context and is gone after the
campaign starts.

This primitive reads a ``interview.input.json`` payload and an
already-existing ``tasks.py`` in the campaign workdir, validates that
they agree (``tasks.total() == intent.task_count``), then persists the
intent — plus a ``cmd_sha`` fingerprint of the produced tasks.py and a
materialization timestamp — to ``<campaign_dir>/interview.json``.

The primitive is deliberately small. It does NOT generate tasks.py;
that would require typing the search space (``logspace``, ``grid``,
``items_x_seeds``, …) which narrows the otherwise experiment-agnostic
``total() + resolve(i) -> Any`` contract. The interview agent (the
external orchestrator or claude-the-interviewer) writes tasks.py
themselves, and this primitive records the intent alongside.

A future opt-in field — ``intent.task_generator`` — is reserved in the
schema for the case where the operator *does* want a typed recipe to
regenerate tasks.py. The schema documents the slot; the materializer
that consumes it is a separate primitive (not yet written).

Idempotent on (intent, campaign_dir): re-running with the same intent
overwrites interview.json with byte-equivalent content modulo the
``_materialized.at`` timestamp.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import RepoLayout, errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.interview import InterviewSpec
from hpc_agent.cli._dispatch import CliArg, CliShape, SchemaRef
from hpc_agent.infra.io import atomic_locked_update, atomic_write_json
from hpc_agent.infra.time import utcnow

if TYPE_CHECKING:
    from argparse import Namespace
    from collections.abc import Mapping


__all__ = ["record_interview"]


def _interview_arg_pre(ns: Namespace) -> dict[str, Any]:
    """Resolve --campaign-dir to an absolute Path for record_interview."""
    return {"campaign_dir": Path(ns.campaign_dir).resolve()}


@primitive(
    name="interview",
    verb="scaffold",
    side_effects=[
        SideEffect("file_write", "<campaign_dir>/{interview.json,meta.json,.claude/settings.json}")
    ],
    idempotent=True,
    idempotency_key="campaign_dir",
    cli=CliShape(
        help=(
            "Validate an agent-written tasks.py against the structured intent "
            "from an interview, then persist intent + cmd_sha + dry-resolve "
            "preview to <campaign-dir>/interview.json."
        ),
        spec_arg=True,
        spec_model=InterviewSpec,
        schema_ref=SchemaRef(input="interview"),
        args=(
            CliArg(
                "--campaign-dir",
                type=str,
                required=True,
                help=(
                    "Campaign workdir; must already contain a tasks.py written by the "
                    "interview agent. interview.json (and optionally meta.json) is "
                    "written into this directory."
                ),
            ),
        ),
        arg_pre=_interview_arg_pre,
    ),
    agent_facing=True,
)
def record_interview(
    spec: InterviewSpec,
    *,
    campaign_dir: Path,
) -> dict[str, Any]:
    """Validate or materialize a tasks.py against *spec* and persist interview.json.

    *spec* is an :class:`InterviewSpec` Pydantic model (the wire-validated
    authoring SoT for ``schemas/interview.input.json``). The body
    operates on a ``model_dump`` view (``intent``) so the existing dict
    access pattern ``intent["task_count"]`` etc. survives unchanged.
    *campaign_dir* is created if needed.

    Two modes, picked by whether ``intent.task_generator`` is present:

    1. **Generator mode** — ``intent.task_generator`` is set. The materializer
       writes tasks.py from the typed recipe (``enumerated``,
       ``cartesian_product``, ``items_x_seeds``, ``numeric_logspace``,
       ``numeric_linspace``) and the produced count is cross-checked against
       ``intent.task_count`` *before* any disk write — a recipe-vs-count
       mismatch never leaves a partial tasks.py behind.
    2. **Validate mode** — ``intent.task_generator`` is absent. The interview
       agent must have already written tasks.py into ``campaign_dir``;
       this primitive validates the cross-checks.

    Returns the envelope ``data`` block from ``schemas/interview.output.json``.

    Raises ``ValueError`` (mapped by the CLI adapter to spec_invalid):
    - validate mode: tasks.py missing from campaign_dir
    - either mode: ``tasks.total() != intent.task_count``
    - either mode: ``tasks.total() < 1``
    - generator mode: unknown ``task_generator.kind`` or invalid params
    """
    campaign_dir.mkdir(parents=True, exist_ok=True)

    intent: dict[str, Any] = spec.model_dump(exclude_none=True, mode="json")
    declared = int(intent["task_count"])
    artifacts: list[str] = []

    # Validate the entry_point (if present) and materialize the wrapper
    # (if shell_command). All entry-point validation happens BEFORE any
    # tasks.py write so a bad spec leaves no residue.
    frozen_shas: dict[str, str] = {}
    # Fixed (non-axis) params declared on the entry point (#195). Baked into
    # every materialized task's kwargs alongside the frozen-config shas, so a
    # required executor param the user didn't sweep is still supplied per task.
    fixed_params: dict[str, Any] = {}
    entry_point_materialized: dict[str, Any] | None = None
    if "entry_point" in intent:
        ep = intent["entry_point"]
        kind = ep["kind"]
        # Every entry kind may carry fixed_params; like frozen_configs they are
        # threaded into kwargs only on a materialized tasks.py, so they require
        # task_generator (we can't safely edit a hand-written tasks.py).
        fixed_params = dict(ep.get("fixed_params") or {})
        if fixed_params and "task_generator" not in intent:
            raise errors.SpecInvalid(
                "entry_point.fixed_params requires task_generator; a hand-written "
                "tasks.py must include the constant kwargs itself. Either add "
                "task_generator to the intent or thread the fixed params through "
                "your own tasks.resolve() return dict."
            )
        if kind == "shell_command":
            # Reject ``frozen_configs`` without ``task_generator``. The framework
            # threads ``<stem>_sha`` into kwargs only on materialized tasks.py;
            # for a hand-written tasks.py we can't safely edit the user's file,
            # and silently dropping the shas would defeat the identity guarantee
            # the field promises. The user can either switch to task_generator or
            # include the shas in their own tasks.py kwargs.
            if ep.get("frozen_configs") and "task_generator" not in intent:
                raise errors.SpecInvalid(
                    "shell_command.frozen_configs requires task_generator; "
                    "a hand-written tasks.py must include the shas itself. "
                    "Either add task_generator to the intent or drop "
                    "frozen_configs and thread the shas through your own "
                    "tasks.py kwargs."
                )
            from hpc_agent.incorporation.wrap_entry_point import (
                materialize_shell_wrapper,
                wrapper_executor_cmd,
            )

            result = materialize_shell_wrapper(
                campaign_dir=campaign_dir,
                run_name=ep["run_name"],
                argv=ep["argv"],
                signature=ep.get("signature", {}),
                frozen_configs=ep.get("frozen_configs", []),
                solver=ep.get("solver"),
            )
            frozen_shas = dict(result.frozen_shas)
            # Cluster-bound path: it's rsynced to and resolved on the Linux
            # cluster, so it must be POSIX no matter the authoring OS. str() on
            # a Windows Path emits backslashes, which the cluster-side dispatcher
            # (and the artifacts manifest) would then fail to resolve.
            wrapper_rel = result.wrapper_path.relative_to(campaign_dir).as_posix()
            artifacts.append(wrapper_rel)
            entry_point_materialized = {
                "kind": "shell_command",
                "run_name": ep["run_name"],
                "wrapper_path": wrapper_rel,
                "executor_cmd": wrapper_executor_cmd(
                    campaign_dir=campaign_dir, run_name=ep["run_name"]
                ),
                "frozen_shas": dict(frozen_shas),
            }
            if "data_axis_hint" in ep:
                entry_point_materialized["data_axis"] = ep["data_axis_hint"]
            # Persist the solver hint so downstream consumers (resubmit
            # --from-checkpoint, the canary verifier) can see the wrapper is
            # checkpoint-instrumented and which option family it exports.
            if ep.get("solver") is not None:
                entry_point_materialized["solver"] = dict(ep["solver"])
        elif kind == "python_module":
            # Validate the module/function imports; surface the same spec_invalid
            # the rest of the interview uses so a typo is loud at intake.
            _validate_python_module_entry(ep, campaign_dir)
            entry_point_materialized = {
                "kind": "python_module",
                "module": ep["module"],
                "function": ep.get("function", "main"),
            }
        elif kind == "register_run":
            # Validate the named run is actually discoverable. ``discover_runs``
            # defaults to ``notebooks/`` (the canonical notebook location); a
            # mature repo with a different layout passes the path via
            # ``notebooks_dir``. The fallback to campaign_dir handles the
            # tiny-repo case where everything sits at the root.
            run_path = _validate_register_run_entry(ep, campaign_dir)
            # Generate the per-task executor command the same way
            # ``shell_command`` does for its materialized wrapper. The
            # ``@register_run`` decorator injects ``compute(args)`` into
            # the module's namespace at decoration time; the one-liner
            # imports the user's file by path and dispatches with an
            # argparse Namespace built from ``HPC_KW_*`` env vars. Without
            # this, the framework would default to ``python3 <file>``,
            # which fails because the dispatcher passes kwargs only as
            # env vars, never argv (empirical case observed in live
            # demos — 100 tasks ran with exit 0 but no metrics.json).
            from hpc_agent.incorporation.wrap_entry_point import (
                register_run_executor_cmd,
            )

            entry_point_materialized = {
                "kind": "register_run",
                "run_name": ep["run_name"],
                "executor_cmd": register_run_executor_cmd(
                    campaign_dir=campaign_dir,
                    run_path=run_path,
                    run_name=ep["run_name"],
                ),
            }

    if "task_generator" in intent:
        # Generator mode — pre-validate count, then materialize tasks.py.
        generator = intent["task_generator"]
        expected = _expected_count(generator)
        if expected != declared:
            raise errors.SpecInvalid(
                f"task_generator would produce {expected} tasks but "
                f"intent.task_count = {declared}; recipe and stated count "
                f"disagree (refusing to write tasks.py)"
            )
        # tasks.py is a framework artifact — materialize it into the
        # canonical <campaign_dir>/.hpc/tasks.py that deploy_runtime, the
        # cluster dispatcher, build-tasks-py and RepoLayout all read.
        # interview.json + frozen_configs stay at the campaign_dir root.
        tasks_py = RepoLayout(campaign_dir).tasks
        # Both the fixed (non-axis) params (#195) and the frozen-config shas are
        # constant-per-task kwargs threaded via the same _INJECT seam. On a key
        # collision the frozen sha wins (identity must not be overridden) —
        # hence fixed_params first, frozen_shas last.
        inject_kwargs = {**fixed_params, **frozen_shas}
        _materialize_tasks_py(generator, tasks_py, inject_kwargs=inject_kwargs)
        artifacts.append(".hpc/tasks.py")
    else:
        # Validate mode — the interview agent already wrote the canonical
        # .hpc/tasks.py. One location everywhere, matching deploy_runtime,
        # the cluster dispatcher, build-tasks-py and RepoLayout.
        tasks_py = campaign_dir / ".hpc" / "tasks.py"
        if not tasks_py.is_file():
            raise errors.SpecInvalid(
                f"campaign_dir is missing .hpc/tasks.py: {tasks_py}. Either the "
                f"interview agent must produce .hpc/tasks.py before invoking this "
                f"primitive, or intent.task_generator must specify a recipe."
            )

    from hpc_agent import compute_cmd_sha, load_tasks_module

    tasks_mod = load_tasks_module(tasks_py)
    total_tasks = int(tasks_mod.total())
    if total_tasks < 1:
        raise errors.SpecInvalid(
            f"tasks.total() = {total_tasks}; campaign has no tasks to dispatch"
        )

    if declared != total_tasks:
        raise errors.SpecInvalid(
            f"intent.task_count = {declared} but tasks.total() = {total_tasks}; "
            f"interview agent's stated count disagrees with the produced tasks.py"
        )

    preview = {
        "first": tasks_mod.resolve(0),
        "mid": tasks_mod.resolve(total_tasks // 2),
        "last": tasks_mod.resolve(total_tasks - 1),
    }
    cmd_sha = compute_cmd_sha(tasks_mod)

    interview_path = campaign_dir / "interview.json"
    materialized: dict[str, Any] = {
        "at": utcnow().isoformat(),
        "cmd_sha": cmd_sha,
        "total_tasks": total_tasks,
    }
    if entry_point_materialized is not None:
        materialized["entry_point"] = entry_point_materialized
    interview_doc = {
        **dict(intent),
        "_materialized": materialized,
    }
    # Atomic write: a SIGINT or crash during a plain ``write_text``
    # would leave half a JSON file that downstream readers
    # (``load_context``, monitor flow) crash on.
    atomic_write_json(interview_path, interview_doc)
    artifacts.append("interview.json")

    if _maybe_update_meta(intent=intent, campaign_dir=campaign_dir, total_tasks=total_tasks):
        artifacts.append("meta.json")

    # Grant the experiment dir the Bash(hpc-agent:*) allow rule so a spawned
    # bare worker can drive the CLI headlessly (#190). Idempotent merge — only
    # reported as an artifact when the rule was newly added.
    if _maybe_write_claude_permissions(campaign_dir):
        artifacts.append(".claude/settings.json")

    return {
        "campaign_dir": str(campaign_dir.resolve()),
        "artifacts": artifacts,
        "total_tasks": total_tasks,
        "cmd_sha": cmd_sha,
        "preview": preview,
    }


def _maybe_update_meta(*, intent: Mapping[str, Any], campaign_dir: Path, total_tasks: int) -> bool:
    """Side-write ``meta.json`` only for keys the interview owns; return True iff written.

    Keys: cluster / profile / constraint (from cluster_target) and budget.
    Existing meta.json keys win on conflict EXCEPT total_tasks, which is
    always authoritative (must match tasks.total()).
    """
    meta_updates: dict[str, Any] = {}
    if "cluster_target" in intent:
        ct = intent["cluster_target"]
        meta_updates["cluster"] = ct["cluster"]
        meta_updates["profile"] = ct["profile"]
        if ct.get("constraint") is not None:
            meta_updates["constraint"] = ct["constraint"]
    if "budget" in intent:
        meta_updates["budget"] = dict(intent["budget"])
    if not meta_updates:
        return False
    meta_path = campaign_dir / "meta.json"

    def _mutate(existing: dict[str, Any] | None) -> dict[str, Any]:
        prior = existing or {}
        # Existing meta.json keys win on conflict EXCEPT total_tasks,
        # which is always authoritative (must match tasks.total()).
        merged = {**meta_updates, **prior}
        merged["total_tasks"] = total_tasks
        return merged

    # ``atomic_locked_update`` serializes concurrent interview runs
    # against the same campaign dir — without it the read/merge/write
    # window loses updates (a parallel agent + driver scenario).
    atomic_locked_update(meta_path, _mutate)
    return True


# The permission rule the spawned `claude -p --bare` worker needs to call the
# `hpc-agent` CLI headlessly. A bare worker has no human to approve prompts, so
# without an allow rule Claude Code's auto-mode classifier blocks its first
# `hpc-agent ...` Bash call and the default worker path silently degrades (#190).
# Project-scoped `<campaign_dir>/.claude/settings.json` merges on top of the
# user-global config, so anyone launching `claude` from the experiment dir gets
# the grant with zero manual config and no global mutation.
_HPC_AGENT_ALLOW_RULE = "Bash(hpc-agent:*)"


def _maybe_write_claude_permissions(campaign_dir: Path) -> bool:
    """Idempotently grant the experiment dir the `Bash(hpc-agent:*)` allow rule.

    Writes/merges ``<campaign_dir>/.claude/settings.json`` so a spawned bare
    worker (or any `claude` launched from here) can call the `hpc-agent` CLI
    without a permission prompt it cannot answer (#190). Returns True iff the
    file was created or the rule was newly added; a no-op (rule already present)
    returns False so the artifacts list doesn't churn on re-runs.

    Merge, never overwrite: an existing settings.json keeps every other key and
    every other allow entry; we only ensure our one rule is present (deduped).
    """
    settings_path = campaign_dir / ".claude" / "settings.json"
    added = False

    def _mutate(existing: dict[str, Any] | None) -> dict[str, Any]:
        nonlocal added
        merged = dict(existing or {})
        permissions = dict(merged.get("permissions") or {})
        allow = list(permissions.get("allow") or [])
        if _HPC_AGENT_ALLOW_RULE not in allow:
            allow.append(_HPC_AGENT_ALLOW_RULE)
            added = True
        permissions["allow"] = allow
        merged["permissions"] = permissions
        return merged

    # Serialize against a concurrent interview/driver touching the same file —
    # same rationale as the meta.json write above. ``atomic_locked_update``
    # creates parent dirs and the file on first write.
    atomic_locked_update(settings_path, _mutate)
    return added


# ─── task_generator: typed recipes that materialize tasks.py ───────────────
#
# Generated tasks.py files are stdlib-only and human-readable. An operator
# who wants to diverge from the recipe drops `task_generator` from intent
# and edits tasks.py directly; subsequent re-runs in validate mode pick
# up the hand edits.

_GENERATED_HEADER = '''"""Generated by `hpc-agent interview` from intent.task_generator.

Re-running the interview with the same intent regenerates this file
byte-equivalently. To diverge from the recipe, drop `task_generator`
from the next intent.json — subsequent runs will accept the file you
hand-edit.
"""
from __future__ import annotations
'''


def _validate_python_module_entry(ep: Mapping[str, Any], campaign_dir: Path) -> None:
    """Confirm ``module`` imports and ``function`` exists on it.

    Catches the typo / packaging mistake at intake. Without this the
    failure would land much later — during cluster-side dispatch — as
    an opaque ``ImportError`` in a per-task log.

    The import runs with *campaign_dir* prepended to ``sys.path`` so a
    ``python_module`` entry that resolves on the cluster — where
    ``hpc_preamble.sh`` puts ``$REPO_DIR`` on ``PYTHONPATH`` — also
    resolves during local intake. Without it a valid ``executors.X``
    false-fails here because the ``hpc-agent`` console-script's path does
    not include the experiment dir (#178).
    """
    from hpc_agent.infra.executor_import import import_executor_module

    module = ep["module"]
    function = ep.get("function", "main")
    try:
        mod = import_executor_module(module, campaign_dir)
    except ImportError as exc:
        raise errors.SpecInvalid(
            f"python_module.entry_point: module {module!r} does not import "
            f"({exc.__class__.__name__}: {exc})"
        ) from exc
    if not hasattr(mod, function):
        raise errors.SpecInvalid(
            f"python_module.entry_point: module {module!r} has no attribute {function!r}"
        )
    if not callable(getattr(mod, function)):
        raise errors.SpecInvalid(f"python_module.entry_point: {module}.{function} is not callable")


def _validate_register_run_entry(ep: Mapping[str, Any], campaign_dir: Path) -> Path:
    """Confirm a ``@register_run`` function named ``run_name`` is discoverable.

    Walks ``campaign_dir`` recursively (via ``discover_runs``) — same primitive
    the rest of the framework keys off, so this validation matches the runtime
    discovery behavior exactly. The skip list (``.hpc``, ``.git``,
    ``__pycache__``, ``.mypy_cache``) is owned by ``discover_runs``.

    Returns the absolute path of the matched run's file. The caller threads
    this into :func:`register_run_executor_cmd` so the materialized
    ``executor_cmd`` knows which file to import on the cluster — without
    this the framework would default to invoking the file as a bare script
    (``python3 <file>``), which fails because the dispatcher passes
    kwargs only via env vars, never argv.
    """
    from hpc_agent.experiment_kit.discover import discover_runs

    run_name = ep["run_name"]
    for run in discover_runs(campaign_dir):
        if run.name == run_name:
            return run.path
    candidates = _undecorated_candidates(campaign_dir, run_name)
    hint = ""
    if candidates:
        listed = ", ".join(str(p.relative_to(campaign_dir)) for p in candidates[:3])
        hint = (
            f" Found a function named {run_name!r} without @register_run in: "
            f"{listed}. Either add `@register_run` to that function (the "
            f"two-line edit: `from hpc_agent import register_run`; "
            f"`@register_run` above the def), or set "
            f"`entry_point.kind=shell_command` to invoke it as a subprocess."
        )
    raise errors.SpecInvalid(
        f"register_run.entry_point: no @register_run function named "
        f"{run_name!r} found by scanning {campaign_dir} recursively "
        f"(skips .hpc/, .git/, __pycache__/, .mypy_cache/). The function "
        f"may not be decorated yet, its name may not match `run_name`, or "
        f"its file may not be on disk.{hint}"
    )


def _undecorated_candidates(campaign_dir: Path, run_name: str) -> list[Path]:
    """Find ``.py`` files that define a top-level function named ``run_name``
    but lack the ``@register_run`` decorator. Lets the SpecInvalid message
    name the likely fix site (decorate this file) instead of leaving the
    agent to grep. Notebooks are skipped — the agent can't AST-edit them
    cleanly anyway, and the false-negative ("missed an .ipynb candidate")
    is cheaper than walking nb cells here.
    """
    import ast as _ast

    _SKIP = {".hpc", ".git", "__pycache__", ".mypy_cache"}
    found: list[Path] = []
    if not campaign_dir.is_dir():
        return found
    for path in sorted(campaign_dir.rglob("*.py")):
        if any(part in _SKIP for part in path.parts):
            continue
        try:
            tree = _ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            continue
        for node in tree.body:
            if not isinstance(node, _ast.FunctionDef | _ast.AsyncFunctionDef):
                continue
            if node.name != run_name:
                continue
            # ``discover_runs`` already would have returned this file if it
            # had the decorator, so a hit here is by definition undecorated.
            found.append(path)
            break
    return found


def _expected_count(generator: Mapping[str, Any]) -> int:
    """Compute total tasks the recipe will produce. Pre-flight cross-check."""
    kind = generator["kind"]
    params = generator["params"]
    if kind == "enumerated":
        return len(params["items"])
    if kind == "cartesian_product":
        axes = params["axes"]
        if not axes:
            # Mirror the v1 ``build_tasks_py`` axes=[] fix — an empty
            # axes mapping silently produces the degenerate `n=1` "one
            # empty-kwargs task" outcome. Reject up-front so the
            # interview cross-check catches it.
            raise errors.SpecInvalid("cartesian_product requires at least one axis")
        n = 1
        for axis_values in axes.values():
            n *= len(axis_values)
        return n
    if kind == "items_x_seeds":
        return len(params["items"]) * len(params["seeds"])
    if kind in ("numeric_logspace", "numeric_linspace"):
        return int(params["n"])
    raise errors.SpecInvalid(f"unknown task_generator.kind: {kind!r}")


def _materialize_tasks_py(
    generator: Mapping[str, Any],
    path,
    *,
    inject_kwargs: Mapping[str, Any] | None = None,
) -> None:
    """Write tasks.py from the recipe. Caller has already cross-checked count.

    ``inject_kwargs`` is merged into every materialized task's kwargs as
    constant fields. Two callers feed it: frozen-config shas
    (``<basename>_sha`` strings, so ``cmd_sha`` covers them) and fixed
    (non-axis) executor params (#195 — e.g. ``{"samples": 10000}``, which
    may be int / float / bool / str). Renders as a static ``_INJECT`` dict
    via ``repr()`` so resolve() returns the merged dict without per-call
    work; ``repr`` round-trips every JSON scalar correctly.
    """
    kind = generator["kind"]
    params = generator["params"]
    inject = dict(inject_kwargs or {})
    inject_prefix = f"_INJECT = {inject!r}\n" if inject else ""
    # When inject is non-empty, every resolve() return gets merged with
    # _INJECT. Inject takes second place (``{**task, **_INJECT}``) so an
    # explicit task kwarg with the same name wins — defensive against an
    # axis named ``foo_sha`` colliding with an inject key.
    merge_resolve = (
        "def resolve(i: int) -> dict: return {**_TASKS[i], **_INJECT}\n"
        if inject
        else "def resolve(i: int) -> dict: return _TASKS[i]\n"
    )
    if kind == "enumerated":
        body = (
            f"{inject_prefix}"
            f"_TASKS = {list(params['items'])!r}\n\n"
            f"def total() -> int: return len(_TASKS)\n"
            f"{merge_resolve}"
        )
    elif kind == "cartesian_product":
        keys = list(params["axes"].keys())
        body = (
            f"import itertools\n\n"
            f"{inject_prefix}"
            f"_KEYS = {keys!r}\n"
            f"_AXES = {[list(params['axes'][k]) for k in keys]!r}\n"
            f"_TASKS = [dict(zip(_KEYS, row)) for row in itertools.product(*_AXES)]\n\n"
            f"def total() -> int: return len(_TASKS)\n"
            f"{merge_resolve}"
        )
    elif kind == "items_x_seeds":
        body = (
            f"{inject_prefix}"
            f"_ITEMS = {list(params['items'])!r}\n"
            f"_SEEDS = {list(params['seeds'])!r}\n"
            f"_TASKS = [{{**item, 'seed': seed}} for item in _ITEMS for seed in _SEEDS]\n\n"
            f"def total() -> int: return len(_TASKS)\n"
            f"{merge_resolve}"
        )
    elif kind == "numeric_logspace":
        base = params.get("base", 10)
        body = (
            f"import math\n\n"
            f"{inject_prefix}"
            f"_LOW = {params['low']!r}\n"
            f"_HIGH = {params['high']!r}\n"
            f"_N = {int(params['n'])}\n"
            f"_BASE = {base!r}\n"
            f"_LOG_LO = math.log(_LOW, _BASE)\n"
            f"_LOG_HI = math.log(_HIGH, _BASE)\n"
            # _N == 1 is a single-point sweep; division by (_N - 1)
            # would otherwise raise ZeroDivisionError at task-resolve
            # time.
            f"_TASKS = [{{{params['param']!r}: _LOW}}] if _N == 1 else [\n"
            f"    {{{params['param']!r}: _BASE ** "
            f"(_LOG_LO + (_LOG_HI - _LOG_LO) * i / (_N - 1))}}\n"
            f"    for i in range(_N)\n"
            f"]\n\n"
            f"def total() -> int: return len(_TASKS)\n"
            f"{merge_resolve}"
        )
    elif kind == "numeric_linspace":
        body = (
            f"{inject_prefix}"
            f"_LOW, _HIGH, _N = {params['low']!r}, {params['high']!r}, {int(params['n'])}\n"
            # _N == 1 → single-point sweep; avoid division by (_N - 1).
            f"_TASKS = [{{{params['param']!r}: _LOW}}] if _N == 1 else [\n"
            f"    {{{params['param']!r}: _LOW + (_HIGH - _LOW) * i / (_N - 1)}}\n"
            f"    for i in range(_N)\n"
            f"]\n\n"
            f"def total() -> int: return len(_TASKS)\n"
            f"{merge_resolve}"
        )
    else:
        raise errors.SpecInvalid(f"unknown task_generator.kind: {kind!r}")
    path.write_text(_GENERATED_HEADER + "\n" + body, encoding="utf-8")
