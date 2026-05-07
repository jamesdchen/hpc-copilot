"""``interview`` primitive — persist campaign intent alongside an agent-written tasks.py.

The interview-time leak today is that the chat between hpc-agent and
either MARs or a human produces *only* a tasks.py; the *why* (goal,
budget, abort criterion, transcript, who decided) lives in transient
session context and is gone after the campaign starts.

This primitive reads a ``interview.input.json`` payload and an
already-existing ``tasks.py`` in the campaign workdir, validates that
they agree (``tasks.total() == intent.task_count``), then persists the
intent — plus a ``cmd_sha`` fingerprint of the produced tasks.py and a
materialization timestamp — to ``<campaign_dir>/interview.json``.

The primitive is deliberately small. It does NOT generate tasks.py;
that would require typing the search space (``logspace``, ``grid``,
``items_x_seeds``, …) which narrows the otherwise experiment-agnostic
``total() + resolve(i) -> Any`` contract. The interview agent (MARs or
claude-the-interviewer) writes tasks.py themselves, and this primitive
records the intent alongside.

A future opt-in field — ``intent.task_generator`` — is reserved in the
schema for the case where the operator *does* want a typed recipe to
regenerate tasks.py. The schema documents the slot; the materializer
that consumes it is a separate primitive (not yet written).

Idempotent on (intent, campaign_dir): re-running with the same intent
overwrites interview.json with byte-equivalent content modulo the
``_materialized.at`` timestamp.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from claude_hpc._internal.primitive import SideEffect, primitive
from claude_hpc._internal.time import utcnow
from claude_hpc._schema_models.interview import InterviewSpec

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path


__all__ = ["record_interview"]


@primitive(
    name="interview",
    verb="scaffold",
    side_effects=[SideEffect("file_write", "<campaign_dir>/{interview.json,meta.json}")],
    idempotent=True,
    idempotency_key="campaign_dir",
    cli="hpc-mapreduce interview",
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

    Raises ``ValueError`` (mapped by the agent_cli adapter to spec_invalid):
    - validate mode: tasks.py missing from campaign_dir
    - either mode: ``tasks.total() != intent.task_count``
    - either mode: ``tasks.total() < 1``
    - generator mode: unknown ``task_generator.kind`` or invalid params
    """
    campaign_dir.mkdir(parents=True, exist_ok=True)

    intent: dict[str, Any] = spec.model_dump(exclude_none=True, mode="json")
    tasks_py = campaign_dir / "tasks.py"
    declared = int(intent["task_count"])
    artifacts: list[str] = []

    if "task_generator" in intent:
        # Generator mode — pre-validate count, then materialize tasks.py.
        generator = intent["task_generator"]
        expected = _expected_count(generator)
        if expected != declared:
            raise ValueError(
                f"task_generator would produce {expected} tasks but "
                f"intent.task_count = {declared}; recipe and stated count "
                f"disagree (refusing to write tasks.py)"
            )
        _materialize_tasks_py(generator, tasks_py)
        artifacts.append("tasks.py")
    elif not tasks_py.is_file():
        raise ValueError(
            f"campaign_dir is missing tasks.py: {tasks_py}. Either the "
            f"interview agent must produce tasks.py before invoking this "
            f"primitive, or intent.task_generator must specify a recipe."
        )

    from claude_hpc import compute_cmd_sha, load_tasks_module

    tasks_mod = load_tasks_module(tasks_py)
    total_tasks = int(tasks_mod.total())
    if total_tasks < 1:
        raise ValueError(f"tasks.total() = {total_tasks}; campaign has no tasks to dispatch")

    if declared != total_tasks:
        raise ValueError(
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
    interview_doc = {
        **dict(intent),
        "_materialized": {
            "at": utcnow().isoformat(),
            "cmd_sha": cmd_sha,
            "total_tasks": total_tasks,
        },
    }
    interview_path.write_text(json.dumps(interview_doc, indent=2, sort_keys=True) + "\n")
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
    existing: dict[str, Any] = {}
    if meta_path.exists():
        existing = json.loads(meta_path.read_text())
    merged = {**meta_updates, **existing}
    merged["total_tasks"] = total_tasks
    meta_path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
    return True


# ─── task_generator: typed recipes that materialize tasks.py ───────────────
#
# Generated tasks.py files are stdlib-only and human-readable. An operator
# who wants to diverge from the recipe drops `task_generator` from intent
# and edits tasks.py directly; subsequent re-runs in validate mode pick
# up the hand edits.

_GENERATED_HEADER = '''"""Generated by `hpc-mapreduce interview` from intent.task_generator.

Re-running the interview with the same intent regenerates this file
byte-equivalently. To diverge from the recipe, drop `task_generator`
from the next intent.json — subsequent runs will accept the file you
hand-edit.
"""
from __future__ import annotations
'''


def _expected_count(generator: Mapping[str, Any]) -> int:
    """Compute total tasks the recipe will produce. Pre-flight cross-check."""
    kind = generator["kind"]
    params = generator["params"]
    if kind == "enumerated":
        return len(params["items"])
    if kind == "cartesian_product":
        n = 1
        for axis_values in params["axes"].values():
            n *= len(axis_values)
        return n
    if kind == "items_x_seeds":
        return len(params["items"]) * len(params["seeds"])
    if kind in ("numeric_logspace", "numeric_linspace"):
        return int(params["n"])
    raise ValueError(f"unknown task_generator.kind: {kind!r}")


def _materialize_tasks_py(generator: Mapping[str, Any], path) -> None:
    """Write tasks.py from the recipe. Caller has already cross-checked count."""
    kind = generator["kind"]
    params = generator["params"]
    if kind == "enumerated":
        body = (
            f"_TASKS = {list(params['items'])!r}\n\n"
            f"def total() -> int: return len(_TASKS)\n"
            f"def resolve(i: int) -> dict: return _TASKS[i]\n"
        )
    elif kind == "cartesian_product":
        keys = list(params["axes"].keys())
        body = (
            f"import itertools\n\n"
            f"_KEYS = {keys!r}\n"
            f"_AXES = {[list(params['axes'][k]) for k in keys]!r}\n"
            f"_TASKS = [dict(zip(_KEYS, row)) for row in itertools.product(*_AXES)]\n\n"
            f"def total() -> int: return len(_TASKS)\n"
            f"def resolve(i: int) -> dict: return _TASKS[i]\n"
        )
    elif kind == "items_x_seeds":
        body = (
            f"_ITEMS = {list(params['items'])!r}\n"
            f"_SEEDS = {list(params['seeds'])!r}\n"
            f"_TASKS = [{{**item, 'seed': seed}} for item in _ITEMS for seed in _SEEDS]\n\n"
            f"def total() -> int: return len(_TASKS)\n"
            f"def resolve(i: int) -> dict: return _TASKS[i]\n"
        )
    elif kind == "numeric_logspace":
        base = params.get("base", 10)
        body = (
            f"import math\n\n"
            f"_LOW = {params['low']!r}\n"
            f"_HIGH = {params['high']!r}\n"
            f"_N = {int(params['n'])}\n"
            f"_BASE = {base!r}\n"
            f"_LOG_LO = math.log(_LOW, _BASE)\n"
            f"_LOG_HI = math.log(_HIGH, _BASE)\n"
            f"_TASKS = [\n"
            f"    {{{params['param']!r}: _BASE ** "
            f"(_LOG_LO + (_LOG_HI - _LOG_LO) * i / (_N - 1))}}\n"
            f"    for i in range(_N)\n"
            f"]\n\n"
            f"def total() -> int: return len(_TASKS)\n"
            f"def resolve(i: int) -> dict: return _TASKS[i]\n"
        )
    elif kind == "numeric_linspace":
        body = (
            f"_LOW, _HIGH, _N = {params['low']!r}, {params['high']!r}, {int(params['n'])}\n"
            f"_TASKS = [\n"
            f"    {{{params['param']!r}: _LOW + (_HIGH - _LOW) * i / (_N - 1)}}\n"
            f"    for i in range(_N)\n"
            f"]\n\n"
            f"def total() -> int: return len(_TASKS)\n"
            f"def resolve(i: int) -> dict: return _TASKS[i]\n"
        )
    else:
        raise ValueError(f"unknown task_generator.kind: {kind!r}")
    path.write_text(_GENERATED_HEADER + "\n" + body)
