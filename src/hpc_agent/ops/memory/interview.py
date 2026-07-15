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

import itertools
import warnings
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

    from hpc_agent.experiment_kit.discover import RunInfo


__all__ = ["record_interview"]


def _assert_derived_executor_runnable(executor_cmd: str, *, kind: str) -> None:
    """Assert a derived ``executor_cmd`` is a runnable per-task command.

    Run #6 finding F1: the framework's entry_point→executor derivation is the
    ONE sanctioned source of the sidecar's per-task ``executor``
    (``resolve-submit-inputs`` re-applies ``_materialized.entry_point
    .executor_cmd`` over anything else), so a derivation emitting a command
    the submit gates would refuse — or the dispatcher would exit-127 on — is
    a framework bug that must fail LOUDLY at derivation time, not on the
    cluster.

    The bar is the same pair the submit path enforces downstream:

    * :func:`hpc_agent.infra.executor_guard.check_per_task_executor`
      (format placeholders, bare ``module:function``, bare script names,
      kwarg casing) — called without an ``experiment_dir`` (the kwarg set is
      unknowable here, so that leg no-ops exactly as at sidecar-write time);
    * ``submit_flow._is_runnable_executor`` (empty / dispatcher-shaped).

    Raises :class:`errors.SpecInvalid` naming the derivation kind — the
    remedy is a framework fix, never a hand-edited command.
    """
    # Lazy imports: incorporation is substrate for this check (the same
    # predicate write-run-sidecar applies); submit_flow is reached via the
    # package alias form (the direct subject spelling trips the
    # subject-import lint from inside ``memory``).
    from hpc_agent.infra.executor_guard import check_per_task_executor
    from hpc_agent.ops import submit_flow as _submit_flow

    prefix = (
        f"interview: the materialized {kind!r} entry point derived an unrunnable "
        f"per-task executor_cmd {executor_cmd!r} — this is a FRAMEWORK bug in the "
        "entry_point→executor derivation (the derivation is the single sanctioned "
        "source of the sidecar's executor, so it must always emit a runnable "
        "command). Do not hand-edit the command; fix the derivation "
        "(incorporation/wrap_entry_point.py). "
    )
    try:
        check_per_task_executor(executor_cmd)
    except errors.SpecInvalid as exc:
        raise errors.SpecInvalid(prefix + f"Downstream gate said: {exc}") from exc
    if not _submit_flow._is_runnable_executor(executor_cmd):
        raise errors.SpecInvalid(
            prefix + "It is empty or dispatcher-shaped (_is_runnable_executor refused it)."
        )


def _interview_arg_pre(ns: Namespace) -> dict[str, Any]:
    """Resolve --campaign-dir to an absolute Path for record_interview."""
    return {"campaign_dir": Path(ns.campaign_dir).resolve()}


def _compose_audit_template_default(
    intent: Mapping[str, Any], campaign_dir: Path
) -> dict[str, str] | None:
    """Compose the audit-template default from a bound pack's ``audit_template`` seam.

    The CODE seat that replaces the on-ramp's ``confirm-default`` prose (2026-07-10
    evening ruling, CONVERSION 2 — "prose cannot be load-bearing"): when a pack is
    bound and the caller supplied NO template, the interview DEFAULTS it silently
    from the pack's audit-facing ``audit_template`` seam and DISCLOSES the composed
    default in the persisted record — the template is never brought to human
    attention (supersedes the pack-status confirm-default).

    Returns a disclosure dict ``{field, value, pack, source, rule, candidates}``
    (``value`` is the experiment-dir-relative template relpath) or ``None`` when
    there is nothing to compose:

    * ``audited_source`` present → the caller committed a ``template`` → UNTOUCHED.
    * no ``packs`` opt-in → today's behavior → ``None``.
    * no opted-in pack declares an ``audit_template`` seam → ``None``.

    Selection is the ONE no-heuristics law of
    :func:`~hpc_agent.state.pack_declarations.compose_audit_template` (run-#13
    finding 1). The old ``receipt_bindings``-first preference — "the FIRST pack
    that is the target of a receipt_bindings slot wins over the domain skeleton"
    — is RETIRED: it silently picked the wrong pack for the two-layer
    domain/program split and the pick was invisible until the sign-off surface.
    Now one candidate wins; among many, the unique derivation-edge survivor wins;
    any other shape (no lineage, siblings, or a cycle) is a loud
    :class:`~hpc_agent.errors.SpecInvalid` naming every candidate — and that
    refusal PROPAGATES out of ``record_interview`` (this is the universal submit
    intake), so ambiguity refuses at intake with the ``audited_source.template``
    remedy rather than persisting a wrong silent default. A manifest that fails to
    load is NAMED in the disclosure's ``skipped`` key, never silently dropped.
    """
    # The caller committed a template (via audited_source) → never override it.
    if "audited_source" in intent:
        return None
    packs = intent.get("packs")
    if not isinstance(packs, list) or not packs:
        return None

    # The ONE selection definition (run-#12 finding 5 extracted it so
    # audit-preflight composes from the SAME seam over the persisted block):
    # referenced-pack-first, opt-in order, best-effort manifest reads.
    from hpc_agent.state.pack_declarations import compose_audit_template

    return compose_audit_template(packs, campaign_dir)


@primitive(
    name="interview",
    verb="scaffold",
    side_effects=[SideEffect("file_write", "<campaign_dir>/{interview.json,meta.json}")],
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
    # Captured only for a register_run entry — the discovered RunInfo whose flag
    # names + **kwargs visibility feed the post-tasks swept-flag cross-check.
    register_run_info: RunInfo | None = None
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
            # Emit the per-task executor command the same way register_run does
            # for its own entry. ``run-module`` imports the dotted module by name
            # and applies the same compute wrapper @register_run injects. Without
            # this the materialized entry_point carried no executor_cmd at all, so
            # a python_module submission had no runnable per-task command (a bare
            # ``module:function`` exec'd as a shell command exits 127).
            from hpc_agent.incorporation.wrap_entry_point import (
                python_module_executor_cmd,
            )

            module = ep["module"]
            function = ep.get("function", "main")
            entry_point_materialized = {
                "kind": "python_module",
                "module": module,
                "function": function,
                "executor_cmd": python_module_executor_cmd(module=module, function=function),
            }
        elif kind == "register_run":
            # Validate the named run is actually discoverable. ``discover_runs``
            # defaults to ``notebooks/`` (the canonical notebook location); a
            # mature repo with a different layout passes the path via
            # ``notebooks_dir``. The fallback to campaign_dir handles the
            # tiny-repo case where everything sits at the root.
            register_run_info = _validate_register_run_entry(ep, campaign_dir)
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
                    run_path=register_run_info.path,
                    run_name=ep["run_name"],
                ),
            }
        # Run #6 F1: the derivation is the SINGLE sanctioned source of the
        # per-task executor (resolve-submit-inputs re-applies it over anything
        # else), so a derivation that emits an unrunnable command is a
        # FRAMEWORK bug — fail loudly here, at derivation time, not exit-127
        # on the cluster after a full staging round-trip.
        if entry_point_materialized is not None:
            _assert_derived_executor_runnable(
                str(entry_point_materialized.get("executor_cmd") or ""),
                kind=str(entry_point_materialized.get("kind") or kind),
            )

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

    # Swept-flag cross-check (register_run only): every key resolve(i) sweeps must
    # name a real run() parameter, else the cluster canary is the first to notice
    # (run #8). Deferred to here because it needs BOTH the discovered run signature
    # and the now-loaded tasks module.
    if register_run_info is not None:
        _validate_swept_flags_against_run(
            register_run_info, tasks_mod, total_tasks, fixed_params=fixed_params
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
        # Run-#12 finding 14: record the REAL origin of tasks.py so a downstream
        # consumer (walk-submit-ambiguities) never mislabels an interview-
        # materialized sweep as ``hand_written``. Generator mode → the interview
        # wrote it from the typed recipe; validate mode → the interview agent
        # authored it by hand.
        "tasks_py_origin": (
            "interview_materialized" if "task_generator" in intent else "hand_written"
        ),
    }
    if entry_point_materialized is not None:
        materialized["entry_point"] = entry_point_materialized
    # CONVERSION 2 (2026-07-10 evening ruling): when a pack is bound and the caller
    # supplied no template, COMPOSE the audit-template default from the pack's
    # ``audit_template`` seam IN CODE — silently, disclosed here, never brought to
    # human attention (supersedes the on-ramp's pack-status confirm-default).
    composed_default = _compose_audit_template_default(intent, campaign_dir)
    if composed_default is not None:
        materialized["composed_defaults"] = [composed_default]
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


def _validate_register_run_entry(ep: Mapping[str, Any], campaign_dir: Path) -> RunInfo:
    """Confirm a ``@register_run`` function named ``run_name`` is discoverable.

    Walks ``campaign_dir`` recursively (via ``discover_runs``) — same primitive
    the rest of the framework keys off, so this validation matches the runtime
    discovery behavior exactly. The skip list (``.hpc``, ``.git``,
    ``__pycache__``, ``.mypy_cache``) is owned by ``discover_runs``.

    Returns the matched :class:`RunInfo`. The caller reads ``.path`` to thread
    into :func:`register_run_executor_cmd` (so the materialized ``executor_cmd``
    knows which file to import on the cluster — without it the framework would
    default to invoking the file as a bare script, ``python3 <file>``, which
    fails because the dispatcher passes kwargs only via env vars, never argv),
    and ``.flags`` / ``.has_var_keyword`` to cross-check the swept flag names
    against the run signature (see :func:`_validate_swept_flags_against_run`).
    """
    from hpc_agent.experiment_kit.discover import discover_runs

    run_name = ep["run_name"]
    matches = [run for run in discover_runs(campaign_dir) if run.name == run_name]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # AMBIGUOUS: >1 @register_run function shares this name across files.
        # The old code returned the FIRST match (discover_runs sorts by path, so
        # `executors/monte_carlo_pi.py` silently beat a root `train.py` — run #8:
        # the wrong file's run() was materialized, its `samples` kwarg and its
        # executor_cmd, and the canary failed on the actually-intended run). A
        # run_name is not a unique key across files; picking one silently runs
        # the WRONG code. Fail loud, naming every file, so the human makes the
        # name unique (rename or remove the stale one) — matching
        # classify_axis_auto's ``ambiguous_run`` contract.
        listed = ", ".join(str(p.path.relative_to(campaign_dir)) for p in matches)
        raise errors.SpecInvalid(
            f"ambiguous_run: {len(matches)} @register_run functions are named "
            f"{run_name!r}, in: {listed}. A run_name is not unique across files, so "
            f"the framework cannot know which one to run — rename all but one, or "
            f"remove the stale file(s), so exactly one @register_run function has "
            f"this name. (Scanned {campaign_dir} recursively.)"
        )
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


# Keys ``resolve(i)`` may legitimately carry that are NOT user run() parameters,
# so they are diffed out before a swept key is judged "maps to no parameter":
# the framework-injected result path + planner ``halo``, plus the MPI launcher's
# ``rank`` / ``world_size`` (which ``flags_from_ast(mpi=True)`` also excludes, so
# they'd never appear in ``RunInfo.flags``). Built lazily to keep the
# experiment_kit.signature import off this CLI-reachable module's top level.
_FRAMEWORK_INJECTED_KEYS: frozenset[str] | None = None
# How many tasks to sample from resolve(i) — swept keys are uniform across tasks,
# so a bounded sample suffices and keeps a huge campaign fast (mirrors
# validate-executor-signatures' sample_n_tasks default).
_SWEPT_FLAG_SAMPLE_N = 8


def _framework_injected_keys() -> frozenset[str]:
    global _FRAMEWORK_INJECTED_KEYS
    if _FRAMEWORK_INJECTED_KEYS is None:
        from hpc_agent.experiment_kit.signature import MPI_INJECTED_PARAMS

        _FRAMEWORK_INJECTED_KEYS = frozenset({"output_file", "halo"}) | MPI_INJECTED_PARAMS
    return _FRAMEWORK_INJECTED_KEYS


def _validate_swept_flags_against_run(
    run_info: RunInfo,
    tasks_mod: Any,
    total_tasks: int,
    *,
    fixed_params: Mapping[str, Any],
) -> None:
    """Cross-check every swept ``resolve(i)`` key against the run's signature.

    Run #8: ``tasks.py`` swept ``flag('samples')`` while the ``@register_run``
    function was ``run(n_samples=...)``; the mismatch surfaced only when the
    cluster canary crashed (``HPC_KW_SAMPLES`` exported, ``--n-samples`` never
    bound). Both sides are knowable at interview time — the AST-synthesised flag
    names (:attr:`RunInfo.flags`) and the keys ``resolve(i)`` produces — so diff
    them here, before any cluster round-trip.

    Posture follows the survival-over-strictness split:

    * a swept key mapping to NO run() parameter while the run has no ``**kwargs``
      is a certain bug → refuse loudly (:class:`errors.SpecInvalid`, naming the
      key(s) and the real parameter names), mirroring the ``ambiguous_run``
      refusal and :func:`_assert_derived_executor_runnable`;
    * a run WITH ``**kwargs`` absorbs any surplus kwarg, so the same mismatch is
      only *possibly* a typo → warn, never refuse (mirrors
      ``execution/mapreduce/dispatch._warn_unset_kwarg_refs``).

    Exempt from the diff: framework-injected params
    (:func:`_framework_injected_keys`) and the declared ``fixed_params`` (the
    operator's own constant kwargs, already threaded into every task).
    """
    param_names = {f.name for f in run_info.flags}
    exempt = _framework_injected_keys() | set(fixed_params)

    swept_keys: set[str] = set()
    for i in range(min(total_tasks, _SWEPT_FLAG_SAMPLE_N)):
        kwargs = tasks_mod.resolve(i)
        if isinstance(kwargs, dict):
            swept_keys.update(kwargs.keys())

    unknown = sorted(k for k in swept_keys if k not in param_names and k not in exempt)
    if not unknown:
        return

    known = sorted(param_names)
    if run_info.has_var_keyword:
        # WARN, don't refuse: the run's **kwargs will accept the surplus key(s),
        # so we can't prove a bug — but a typo (samples vs n_samples) would be
        # silently swallowed, so surface it.
        warnings.warn(
            f"interview: tasks.py sweeps {unknown!r} but @register_run "
            f"{run_info.name!r} declares no such parameter (its named params are "
            f"{known!r}). The run accepts **kwargs so the surplus key(s) pass "
            f"through rather than being refused — but if this is a typo (e.g. "
            f"'samples' vs 'n_samples') the run silently ignores the swept value. "
            f"Rename the swept key or the parameter if they were meant to match.",
            stacklevel=2,
        )
        return
    raise errors.SpecInvalid(
        f"register_run.entry_point: tasks.py sweeps {unknown!r} but @register_run "
        f"{run_info.name!r} ({run_info.path.name}) has no such parameter and no "
        f"**kwargs to absorb it. Its parameters are {known!r}. On the cluster each "
        f"swept key is exported as HPC_KW_<KEY> but the run's CLI never binds it, "
        f"so every task runs with the intended value dropped (the canary fails). "
        f"Fix the mismatch: rename the swept key to a real parameter (e.g. "
        f"'samples' -> 'n_samples'), rename the parameter, or add **kwargs to "
        f"{run_info.name!r} if the pass-through is intended."
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
    if kind == "chunked_series":
        # Independent count formula (chunks x product(extra-axis lens)) so it
        # cross-checks the length of the list ``_chunked_series_tasks`` emits.
        _validate_chunked_series(params)
        n = int(params["chunks"])
        for axis_values in (params.get("extra_axes") or {}).values():
            n *= len(axis_values)
        return n
    raise errors.SpecInvalid(f"unknown task_generator.kind: {kind!r}")


def _validate_chunked_series(params: Mapping[str, Any]) -> None:
    """Spec-time validation for a ``chunked_series`` recipe.

    Raises :class:`errors.SpecInvalid` on any condition that would make the
    chunk arithmetic degenerate — the off-by-one class run #11 hit with a
    hand-scripted ``enumerated`` list that only the COUNT cross-checked.
    """
    series_length = int(params["series_length"])
    chunks = int(params["chunks"])
    halo = int(params["halo"])
    start = int(params.get("start", 0))
    if chunks < 1:
        raise errors.SpecInvalid(f"chunked_series requires chunks >= 1; got {chunks}")
    if halo < 0:
        raise errors.SpecInvalid(f"chunked_series requires halo >= 0; got {halo}")
    if start + halo >= series_length:
        raise errors.SpecInvalid(
            f"chunked_series requires start + halo < series_length (a non-empty "
            f"scoring space); got start={start}, halo={halo}, "
            f"series_length={series_length}"
        )
    span = series_length - start - halo
    if span // chunks < 1:
        raise errors.SpecInvalid(
            f"chunked_series chunk width < 1: the scoring span [{start + halo}, "
            f"{series_length}) has width {span}, but chunks={chunks} would leave "
            f"an empty chunk; reduce chunks or widen the span"
        )
    empties = [name for name, vals in (params.get("extra_axes") or {}).items() if not vals]
    if empties:
        raise errors.SpecInvalid(
            f"chunked_series extra_axes must each have >=1 value; empty: {empties}"
        )


def _chunked_series_tasks(params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Emit the ordered per-task kwargs for a ``chunked_series`` recipe.

    The scoring space ``[start + halo, series_length)`` is tiled into
    ``chunks`` contiguous chunks; the last chunk absorbs the remainder so the
    final ``chunk_end`` equals ``series_length`` EXACTLY. Every task carries
    its ``chunk_start`` / ``chunk_end`` / ``halo`` bounds — a run-#11-style
    executor replays ``halo`` bars before ``chunk_start`` (reading from
    ``chunk_start - halo``, which the validation guarantees is >= ``start``,
    so the halo never underflows the series). Each ``extra_axes`` value
    multiplies the grid: the emitted order is every extra-axes combination
    (outer, in declared-key order) crossed with every chunk (inner) — the
    bucket x chunk fan-out.

    This is the ONE home of the chunk arithmetic. ``_expected_count`` derives
    the count by an independent formula and the interview cross-checks it, and
    the materializer emits the list this returns verbatim — so the bounds math
    has a single, test-seated definition.
    """
    _validate_chunked_series(params)
    series_length = int(params["series_length"])
    chunks = int(params["chunks"])
    halo = int(params["halo"])
    start = int(params.get("start", 0))
    extra_axes: Mapping[str, Any] = params.get("extra_axes") or {}

    emit_lo = start + halo
    width = (series_length - emit_lo) // chunks
    bounds: list[tuple[int, int]] = []
    for i in range(chunks):
        chunk_start = emit_lo + i * width
        # The last chunk absorbs the remainder so the union covers the space
        # exactly and the final end lands on series_length.
        chunk_end = series_length if i == chunks - 1 else emit_lo + (i + 1) * width
        bounds.append((chunk_start, chunk_end))

    keys = list(extra_axes.keys())
    axis_values = [list(extra_axes[k]) for k in keys]
    tasks: list[dict[str, Any]] = []
    # ``itertools.product()`` with no axes yields a single empty tuple, so the
    # no-extra-axes case is just the chunk sequence.
    for combo in itertools.product(*axis_values):
        bucket = dict(zip(keys, combo, strict=True))
        for chunk_start, chunk_end in bounds:
            # Chunk bounds go LAST so they win if an extra axis is (mis)named
            # like a bound key — the bounds are the point of the recipe.
            tasks.append(
                {**bucket, "chunk_start": chunk_start, "chunk_end": chunk_end, "halo": halo}
            )
    return tasks


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
    # _INJECT. Inject takes first place (``{**_INJECT, **task}``) so an
    # explicit task kwarg with the same name wins — a swept axis is
    # per-task, the inject value is only the constant fallback (the
    # ``fixed_params`` wire contract), and it also defends against an
    # axis named ``foo_sha`` colliding with an inject key.
    merge_resolve = (
        "def resolve(i: int) -> dict: return {**_INJECT, **_TASKS[i]}\n"
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
    elif kind == "chunked_series":
        # Bounds are computed here (the ONE arithmetic home) and emitted as a
        # verbatim literal list — same round-trip guarantee as ``enumerated``,
        # and the count was already cross-checked by ``_expected_count`` via an
        # independent formula.
        tasks = _chunked_series_tasks(params)
        body = (
            f"{inject_prefix}"
            f"_TASKS = {tasks!r}\n\n"
            f"def total() -> int: return len(_TASKS)\n"
            f"{merge_resolve}"
        )
    else:
        raise errors.SpecInvalid(f"unknown task_generator.kind: {kind!r}")
    path.write_text(_GENERATED_HEADER + "\n" + body, encoding="utf-8")
