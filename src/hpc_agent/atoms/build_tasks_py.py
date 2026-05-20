"""``build-tasks-py`` primitive — scaffold the per-experiment ``.hpc/tasks.py``.

Given a cartesian-product axes spec + per-executor flag declarations,
emit a ``tasks.py`` that follows the canonical contract (FLAGS / total()
/ resolve()). Replaces the slash-command prose that walked the agent
through writing the file by hand.

Defaults to Pattern 1 (cartesian product) from ``tasks_example.py`` —
the 80% case for grid sweeps. For Pattern 2 (chunking) / Pattern 3
(date windows), the user hand-edits the generated file; this primitive
just gets the agent to a known-good starting point instead of
re-deriving the framework contract from prose.
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._internal.primitive import SideEffect, primitive
from hpc_agent._schema_models.actions.build_tasks_py import BuildTasksPyInput

if TYPE_CHECKING:
    from pathlib import Path


# Axis names whose uppercase form would shadow a real env var when the
# dispatcher exports them. The dispatcher (mapreduce/dispatch.py) does
# ``env[key.upper()] = str(value)`` for every kwarg in tasks.resolve()'s
# return dict; an axis named ``home`` becomes ``HOME=...``, silently
# breaking the executor's environment.
#
# Three groups:
#   1. POSIX/standard env vars every shell relies on.
#   2. Library env vars that change Python/BLAS/CUDA behavior.
#   3. Framework-reserved keys the dispatcher itself uses.
#
# An axis name is rejected if its uppercase form is in this set (or
# matches one of the prefix-reserved patterns below). The kwarg-name
# space is large; any single experiment's axes are ~5 names; collision
# is rare but always wrong, so fail-fast at scaffold time.
_RESERVED_AXIS_NAMES: frozenset[str] = frozenset(
    {
        # POSIX shell / OS
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "SHELL",
        "PWD",
        "OLDPWD",
        "TERM",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        # Library: Python
        "PYTHONPATH",
        "PYTHONHASHSEED",
        "PYTHONUNBUFFERED",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONIOENCODING",
        "PYTHONHOME",
        # Library: BLAS / threading
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        # Library: CUDA / GPU
        "CUDA_VISIBLE_DEVICES",
        "CUDA_HOME",
        "LD_LIBRARY_PATH",
        "CUBLAS_WORKSPACE_CONFIG",
        "XLA_FLAGS",
        "PYTORCH_CUDA_ALLOC_CONF",
        # Framework-reserved
        "RESULT_DIR",
        "HPC_RESULT_DIR",
        "HPC_TASK_ID",
        "HPC_RUN_ID",
        "HPC_GPU_TYPE",
        "HPC_RUNTIME",
        "HPC_PREEMPT_GRACE_SEC",
        "HPC_NFS_DATA_DIR",
        "HPC_KW_NAMESPACE_ONLY",
        "HPC_FORCE_RERUN",
        "LOCAL_DATA_DIR",
        # Scheduler-injected (subset; prefix check below covers the rest)
        "JOB_ID",
        "TASK_ID",
        "NSLOTS",
    }
)

# Prefix patterns: any axis name whose uppercase starts with one of
# these is reserved. Catches the long tail of scheduler-injected vars
# (SLURM_JOB_ID, SGE_TASK_ID, ...) and the framework's own kwarg
# namespace (HPC_KW_*).
_RESERVED_AXIS_PREFIXES: tuple[str, ...] = (
    "SLURM_",
    "SGE_",
    "PBS_",
    "HPC_KW_",
)


def _validate_axis_name(name: str) -> None:
    """Raise :class:`errors.SpecInvalid` if *name* would collide with an env var.

    Called per axis at scaffold time so a problem name fails before
    ``.hpc/tasks.py`` is written, rather than as a silent runtime
    divergence after the executor reads (e.g.) the wrong ``$HOME``.
    """
    upper = name.upper()
    if upper in _RESERVED_AXIS_NAMES or any(upper.startswith(p) for p in _RESERVED_AXIS_PREFIXES):
        raise errors.SpecInvalid(
            f"axis name {name!r} would shadow the env var {upper!r} "
            "when the dispatcher exports kwargs to the executor's "
            "environment. Rename the axis (a per-experiment prefix like "
            f"`exp_{name}` is the canonical fix), or set "
            "HPC_KW_NAMESPACE_ONLY=1 in the spec's job_env to disable "
            "the bare-uppercase export entirely (HPC_KW_<NAME> still works)."
        )


# Whitelist of types we know how to emit. Anything else (numpy.float32,
# user-defined classes, Decimal) routes to ``str`` since cluster-side
# argparse downcasts via the type ctor anyway and emitting opaque types
# would break the generated file.
_FLAG_TYPE_NAMES: dict[type, str] = {
    int: "int",
    float: "float",
    str: "str",
    bool: "bool",
}


def _flag_type_repr(tp: Any) -> str:
    """Return a Python source token for the given flag type.

    Strings in *tp* (e.g. ``"int"``) pass through unchanged so callers
    can spell types without importing them. Class objects map via
    :data:`_FLAG_TYPE_NAMES`.
    """
    if isinstance(tp, str):
        return tp
    return _FLAG_TYPE_NAMES.get(tp, "str")


def _render_flag(flag: dict[str, Any]) -> str:
    """Render one flag dict as a ``flag(...)`` source expression.

    Required keys: ``name``, ``type``. Optional: ``default``.
    """
    name = flag["name"]
    type_token = _flag_type_repr(flag["type"])
    if "default" in flag:
        return f"flag({name!r}, {type_token}, default={flag['default']!r})"
    return f"flag({name!r}, {type_token})"


def _render_flags_block(
    flags_by_executor: dict[str, list[dict[str, Any]]], *, planner: bool = False
) -> str:
    """Render the FLAGS dict assignment as multi-line Python source.

    In *planner* mode each executor additionally gets a ``halo`` flag —
    the planner sets it per task alongside ``generic_args()``'s
    ``start`` / ``end``.
    """
    lines = ["FLAGS: dict[str, list] = {"]
    for module_path, flag_list in flags_by_executor.items():
        lines.append(f"    {module_path!r}: [")
        lines.append("        *generic_args(),")
        if planner:
            lines.append('        flag("halo", int, default=0),')
        for f in flag_list:
            lines.append(f"        {_render_flag(f)},")
        lines.append("    ],")
    lines.append("}")
    return "\n".join(lines)


def _render_tasks_block(axes: list[dict[str, Any]], *, var_name: str = "_TASKS") -> str:
    """Render a cartesian-product list comprehension bound to *var_name*.

    *axes* is ``[{"name": ..., "values": [...]}, ...]``. We emit a list
    comprehension that mirrors the canonical Pattern 1 from
    ``tasks_example.py``. Single-axis sweeps render as a simple list
    comprehension; multi-axis as ``itertools.product``. *var_name* is
    ``_TASKS`` for cartesian mode and ``_SWEEP`` for planner mode.
    """
    from hpc_agent import errors

    if not axes:
        raise errors.SpecInvalid(
            "build-tasks-py requires at least one axis; received an empty axes list"
        )
    if len(axes) == 1:
        ax = axes[0]
        return (
            f"{var_name}: list[dict] = [\n"
            f"    {{{ax['name']!r}: v}}\n"
            f"    for v in {ax['values']!r}\n]"
        )
    names = [ax["name"] for ax in axes]
    values = [ax["values"] for ax in axes]
    var_tuple = ", ".join(names)
    dict_body = ", ".join(f"{n!r}: {n}" for n in names)
    args = ", ".join(repr(v) for v in values)
    return (
        f"{var_name}: list[dict] = [\n"
        f"    {{{dict_body}}}\n"
        f"    for {var_tuple} in itertools.product({args})\n"
        f"]"
    )


# Halo expressions are rendered verbatim into ``lambda params: <expr>``;
# constrain them to arithmetic over the ``params`` dict so a spec cannot
# smuggle a call / import into the generated tasks.py (same threat the
# ``_FlagSpec.type`` Literal hardening closed for flag type tokens).
_HALO_ALLOWED_NODES: tuple[type[ast.AST], ...] = (
    ast.Expression,
    ast.BinOp,
    ast.UnaryOp,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.Subscript,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
)


def _validate_halo_expr(expr: str) -> None:
    """Raise :class:`errors.SpecInvalid` unless *expr* is arithmetic-only."""
    from hpc_agent import errors

    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise errors.SpecInvalid(f"halo_expr is not a valid Python expression: {expr!r}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _HALO_ALLOWED_NODES):
            raise errors.SpecInvalid(
                f"halo_expr must be plain arithmetic over the `params` dict "
                f"(no {type(node).__name__}); got {expr!r}"
            )


def _render_axis_expr(data_axis: dict[str, Any]) -> str:
    """Render the ``DataAxis`` constructor expression for planner mode."""
    from hpc_agent import errors

    kind = data_axis["kind"]
    if kind == "independent":
        return "Independent()"
    if kind == "sequential":
        return "Sequential()"
    if kind == "associative":
        monoid = (data_axis.get("monoid") or "moments").upper()
        return f"Associative({monoid})"
    if kind == "bounded_halo":
        halo = data_axis.get("halo_expr")
        if not halo:
            raise errors.SpecInvalid("data_axis kind 'bounded_halo' requires 'halo_expr'")
        _validate_halo_expr(halo)
        return f"BoundedHalo(lambda params: {halo})"
    raise errors.SpecInvalid(f"unknown data_axis kind {kind!r}")


_TEMPLATE = '''"""Per-experiment task list — cartesian product over the configured axes.

Generated by ``hpc-agent build-tasks-py``. Pattern 1 (cartesian
product) from ``hpc_agent/templates/scaffolds/tasks_example.py``. Hand-edit
to switch to Pattern 2 (chunking) or Pattern 3 (date windows) — the
contract is just FLAGS / total() / resolve().
"""

from __future__ import annotations

import itertools  # noqa: F401 — used by the multi-axis _TASKS render

from hpc_agent.executor_cli import flag, generic_args  # noqa: F401

{flags_block}

{tasks_block}


def total() -> int:
    return len(_TASKS)


def resolve(task_id: int) -> dict:
    return _TASKS[task_id]
'''


_PLANNER_TEMPLATE = '''"""Per-experiment task list — parallelization-planner variant.

Generated by ``hpc-agent build-tasks-py`` from a classified DataAxis —
the deterministic materialisation of the /submit-hpc Step 3 inference.
The cartesian axes below are the *sweep*; the series axis is partitioned
by ``hpc_agent.template.plan_tasks``. Each resolved task carries the
sweep point plus the ``start`` / ``end`` / ``halo`` slice keys that
``hpc_agent.template.load_series`` consumes.

Before submitting, the parallelization must pass the serial-elision
gate — see ``hpc_agent.template.check_elision``. A misclassified axis
runs fine and returns plausible-but-wrong numbers; the gate is the
only thing that catches it.
"""

from __future__ import annotations

import itertools  # noqa: F401 — used by the multi-axis _SWEEP render

from hpc_agent.executor_cli import flag, generic_args  # noqa: F401
from hpc_agent.template import (  # noqa: F401
    MOMENTS,
    SUM,
    Associative,
    BoundedHalo,
    Independent,
    Sequential,
    plan_tasks,
)

{flags_block}

{sweep_block}

_DATA_AXIS = {axis_expr}

_PLAN = plan_tasks(_SWEEP, _DATA_AXIS, chunks={chunks}, series_length={series_length})


def total() -> int:
    return _PLAN.total()


def resolve(task_id: int) -> dict:
    return _PLAN.resolve(task_id)
'''


@primitive(
    name="build-tasks-py",
    verb="scaffold",
    side_effects=[
        SideEffect("writes-sidecar", "<experiment>/.hpc/tasks.py"),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="experiment_dir",
    cli="hpc-agent build-tasks-py --spec <path>",
    agent_facing=True,
)
def build_tasks_py(
    experiment_dir: Path,
    *,
    spec: BuildTasksPyInput,
) -> dict[str, Any]:
    """Scaffold ``<experiment>/.hpc/tasks.py`` from the supplied axes + flags.

    The wire-validated ``spec`` carries ``axes``, ``flags_by_executor``,
    and ``force``; ``experiment_dir`` is the framework-context kwarg
    (the spec's wire surface intentionally doesn't hold it).

    Parameters
    ----------
    experiment_dir:
        Repo root. The output goes to ``<experiment>/.hpc/tasks.py``.
    axes:
        Ordered list of ``{"name": str, "values": list}``. Order
        defines the cartesian-product convention used by
        :func:`compute_wave_map`. Single-axis sweeps render as a
        plain list comprehension; multi-axis use ``itertools.product``.
    flags_by_executor:
        Map from importable executor module path (e.g.
        ``"src.ml_ridge"``) to a list of flag dicts. Each flag is
        ``{"name": str, "type": int|float|str|bool|"<token>",
        "default"?: Any}``. The dispatcher errors fast on a missing
        executor entry, so include every executor in the repo that
        ``/submit-hpc`` might pick at submit time.
    force:
        Overwrite an existing ``.hpc/tasks.py``. Default is
        refuse-without-force — same semantics as ``axes-init``,
        because the user may have hand-edited the generated file
        (Pattern 2 / Pattern 3 conversions).
    data_axis:
        Optional. When present, ``axes`` is treated as the *sweep* and
        the series axis is partitioned by
        :func:`hpc_agent.template.plan_tasks` per the classified
        ``DataAxis`` — the deterministic materialisation of the
        ``/submit-hpc`` Step 3 inference. When omitted, the cartesian
        Pattern-1 file is emitted as before.

    Returns
    -------
    ``{path, wrote, reason, n_tasks}``. ``n_tasks`` is the task count
    the rendered file reports via ``total()`` — the cartesian-product
    cardinality, or ``sweep × chunks`` in planner mode.
    """
    axes = [a.model_dump() for a in spec.axes]
    # Reject axis names that would shadow real env vars when the
    # dispatcher exports kwargs. Done before the file write so a typo
    # like ``home`` or ``path`` fails at scaffold time, not at runtime
    # when the executor's $HOME has been silently overwritten.
    for ax in axes:
        _validate_axis_name(ax["name"])
    # exclude_none on the flag dump so a flag without ``default`` doesn't
    # acquire a synthetic ``default: None`` (the renderer's ``"default"
    # in flag`` check would then emit a spurious ``default=None`` arg).
    flags_by_executor = {
        k: [f.model_dump(exclude_none=True) for f in v] for k, v in spec.flags_by_executor.items()
    }
    force = bool(spec.force)
    # When ``data_axis`` is set the agent classified a series axis at
    # /submit-hpc Step 3; emit a planner-driven tasks.py instead of a
    # cartesian one. The classification is rendered deterministically —
    # the agent never hand-writes tasks.py.
    data_axis = spec.data_axis.model_dump() if spec.data_axis is not None else None

    target = experiment_dir / ".hpc" / "tasks.py"
    if target.exists() and not force:
        return {
            "path": str(target),
            "wrote": False,
            "reason": (
                f"{target} already exists; pass force=true to overwrite. "
                "(Refuse-without-force preserves any hand-edits — e.g. a "
                "Pattern 2/3 conversion.)"
            ),
            "n_tasks": 0,
        }

    sweep_card = 1
    for ax in axes:
        sweep_card *= len(ax["values"])

    if data_axis is not None:
        source = _PLANNER_TEMPLATE.format(
            flags_block=_render_flags_block(flags_by_executor, planner=True),
            sweep_block=_render_tasks_block(axes, var_name="_SWEEP"),
            axis_expr=_render_axis_expr(data_axis),
            chunks=int(data_axis["chunks"]),
            series_length=int(data_axis["series_length"]),
        )
        series_length = int(data_axis["series_length"])
        if data_axis["kind"] == "sequential":
            chunks_used = 1
        elif series_length > 0:
            chunks_used = max(1, min(int(data_axis["chunks"]), series_length))
        else:
            chunks_used = 1
        n_tasks = sweep_card * chunks_used
    else:
        source = _TEMPLATE.format(
            flags_block=_render_flags_block(flags_by_executor),
            tasks_block=_render_tasks_block(axes),
        )
        n_tasks = sweep_card

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(source, encoding="utf-8")
    tmp.replace(target)

    return {
        "path": str(target),
        "wrote": True,
        "reason": f"wrote {target} ({n_tasks} tasks)",
        "n_tasks": n_tasks,
    }
