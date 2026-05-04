"""``interview`` primitive — materialize a campaign from structured intent.

The interview-time leak today is that the chat between hpc-agent and
either MARs or a human produces *only* a tasks.py; the *why* (search
shape, budget, abort criterion, who decided) lives in transient session
context and is gone after the campaign starts.

This primitive consumes a structured ``interview.input.json`` payload
(see ``schemas/interview.input.json``) and writes three artifacts into a
campaign workdir:

* ``tasks.py`` — generated from ``search`` per the recipe in :func:`_materialize_tasks_py`.
* ``interview.json`` — the intent payload verbatim, plus a ``cmd_sha``
  fingerprint of the produced tasks.py and a ``materialized_at`` ISO
  timestamp. This is the artifact future ``cmd_recall`` queries index.
* ``meta.json`` — created or merged-into when ``cluster_target`` /
  ``budget`` are present. Existing meta.json keys are not overwritten.

The output envelope reports a dry-resolve preview (``resolve(0)``,
``resolve(n//2)``, ``resolve(n-1)``) so the calling agent can echo
"your sweep starts here, ends here" back to the human before submit —
catching off-by-one and mis-ranged sweeps at the interview stage rather
than after burning GPU-hours.

Idempotent on (intent, campaign_dir): re-running with the same intent
overwrites the same three artifacts byte-equivalently (timestamps in
interview.json are monotonic but the materialized tasks.py is stable).
"""

from __future__ import annotations

import itertools
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_hpc._internal._primitive import SideEffect, primitive
from claude_hpc._internal._time import utcnow

if TYPE_CHECKING:
    from collections.abc import Mapping


__all__ = ["materialize_interview"]


@primitive(
    name="interview",
    verb="produce",
    side_effects=[SideEffect("file_write", "<campaign_dir>/{tasks.py,interview.json,meta.json}")],
    idempotent=True,
)
def materialize_interview(
    intent: Mapping[str, Any],
    *,
    campaign_dir: Path,
) -> dict[str, Any]:
    """Materialize tasks.py + interview.json + meta.json from *intent*.

    *intent* is the structured payload conforming to
    ``schemas/interview.input.json``. *campaign_dir* is the workdir the
    artifacts land in; created if it doesn't exist.

    Returns the envelope ``data`` block from ``schemas/interview.output.json``.
    Raises ``ValueError`` for unknown ``search.shape`` values; the agent_cli
    adapter maps that to the spec_invalid error_code.
    """
    campaign_dir.mkdir(parents=True, exist_ok=True)

    artifacts: list[str] = []

    # 1. tasks.py
    tasks_py_path = campaign_dir / "tasks.py"
    _materialize_tasks_py(intent["search"], tasks_py_path)
    artifacts.append("tasks.py")

    # 2. Load it to compute total + preview + cmd_sha.
    from claude_hpc import compute_cmd_sha, load_tasks_module  # re-exports

    tasks_mod = load_tasks_module(tasks_py_path)
    total_tasks = int(tasks_mod.total())
    if total_tasks < 1:
        raise ValueError(
            f"materialized tasks.py reports total()={total_tasks}; "
            f"interview produced an empty search space"
        )
    preview = {
        "first": tasks_mod.resolve(0),
        "mid": tasks_mod.resolve(total_tasks // 2),
        "last": tasks_mod.resolve(total_tasks - 1),
    }
    cmd_sha = compute_cmd_sha(tasks_mod)

    # Cross-check: search shapes that declare `n` must agree with total().
    declared_n = intent["search"].get("n")
    if declared_n is not None and declared_n != total_tasks:
        raise ValueError(
            f"intent.search.n = {declared_n} but tasks.total() = {total_tasks}; "
            f"materializer drift — refusing to write interview.json"
        )

    # 3. interview.json — intent + provenance + fingerprint.
    interview_path = campaign_dir / "interview.json"
    interview_doc = {
        **intent,
        "_materialized": {
            "at": utcnow().isoformat(),
            "cmd_sha": cmd_sha,
            "total_tasks": total_tasks,
        },
    }
    interview_path.write_text(json.dumps(interview_doc, indent=2, sort_keys=True) + "\n")
    artifacts.append("interview.json")

    # 4. meta.json — only when interview captured cluster_target or budget.
    #    Merge into existing meta.json (don't overwrite keys the operator set).
    meta_path = campaign_dir / "meta.json"
    meta_updates: dict[str, Any] = {}
    if "cluster_target" in intent:
        meta_updates["cluster"] = intent["cluster_target"]["cluster"]
        meta_updates["profile"] = intent["cluster_target"]["profile"]
        if intent["cluster_target"].get("constraint") is not None:
            meta_updates["constraint"] = intent["cluster_target"]["constraint"]
    if "budget" in intent:
        meta_updates["budget"] = intent["budget"]
    meta_updates["total_tasks"] = total_tasks

    if meta_updates:
        existing: dict[str, Any] = {}
        if meta_path.exists():
            existing = json.loads(meta_path.read_text())
        merged = {**meta_updates, **existing}  # existing wins on conflict
        merged["total_tasks"] = total_tasks  # but total is always authoritative
        meta_path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
        artifacts.append("meta.json")

    return {
        "campaign_dir": str(campaign_dir.resolve()),
        "artifacts": artifacts,
        "total_tasks": total_tasks,
        "cmd_sha": cmd_sha,
        "preview": preview,
    }


# ─── materializers ─────────────────────────────────────────────────────────
#
# Each shape produces a self-contained tasks.py: stdlib-only imports, a
# module-level _TASKS list, and the total/resolve pair. Generated files
# stay readable so an operator can hand-edit if the interview got
# something *almost* right. They're regenerated on re-interview.


_TASKS_PY_HEADER = '''"""Generated by `hpc-mapreduce interview` — do not edit by hand.

Re-running the interview with the same intent regenerates this file
byte-equivalently. To diverge from the recipe, copy this file out of the
campaign workdir and pass `search.shape = "manual"` to subsequent
interviews so the materializer doesn't overwrite your edits.
"""
from __future__ import annotations
'''


def _materialize_tasks_py(search: Mapping[str, Any], path: Path) -> None:
    shape = search["shape"]
    if shape == "logspace":
        body = _body_logspace(search)
    elif shape == "linspace":
        body = _body_linspace(search)
    elif shape == "grid":
        body = _body_grid(search)
    elif shape == "seeds_x":
        body = _body_seeds_x(search)
    elif shape == "manual":
        src = Path(search["tasks_py"])
        if not src.is_file():
            raise ValueError(f"manual search.tasks_py not found: {src}")
        shutil.copyfile(src, path)
        return
    else:
        raise ValueError(f"unknown search.shape: {shape!r}")
    path.write_text(_TASKS_PY_HEADER + "\n" + body)


def _body_logspace(s: Mapping[str, Any]) -> str:
    base = s.get("base", 10)
    return (
        f"import math\n\n"
        f"_LOW = {s['low']!r}\n"
        f"_HIGH = {s['high']!r}\n"
        f"_N = {s['n']}\n"
        f"_BASE = {base}\n"
        f"_LOG_LOW = math.log(_LOW, _BASE)\n"
        f"_LOG_HIGH = math.log(_HIGH, _BASE)\n"
        f"_TASKS = [\n"
        f"    {{{s['param']!r}: _BASE ** (_LOG_LOW + (_LOG_HIGH - _LOG_LOW) * i / (_N - 1))}}\n"
        f"    for i in range(_N)\n"
        f"]\n\n"
        f"def total() -> int: return len(_TASKS)\n"
        f"def resolve(i: int) -> dict: return _TASKS[i]\n"
    )


def _body_linspace(s: Mapping[str, Any]) -> str:
    return (
        f"_LOW = {s['low']!r}\n"
        f"_HIGH = {s['high']!r}\n"
        f"_N = {s['n']}\n"
        f"_TASKS = [\n"
        f"    {{{s['param']!r}: _LOW + (_HIGH - _LOW) * i / (_N - 1)}}\n"
        f"    for i in range(_N)\n"
        f"]\n\n"
        f"def total() -> int: return len(_TASKS)\n"
        f"def resolve(i: int) -> dict: return _TASKS[i]\n"
    )


def _body_grid(s: Mapping[str, Any]) -> str:
    axes = s["axes"]
    keys = list(axes.keys())
    return (
        f"import itertools\n\n"
        f"_KEYS = {keys!r}\n"
        f"_AXES = {[axes[k] for k in keys]!r}\n"
        f"_TASKS = [dict(zip(_KEYS, row)) for row in itertools.product(*_AXES)]\n\n"
        f"def total() -> int: return len(_TASKS)\n"
        f"def resolve(i: int) -> dict: return _TASKS[i]\n"
    )


def _body_seeds_x(s: Mapping[str, Any]) -> str:
    return (
        f"_BASE = {dict(s['base'])!r}\n"
        f"_SEEDS = {list(s['seeds'])!r}\n"
        f"_TASKS = [{{**_BASE, 'seed': seed}} for seed in _SEEDS]\n\n"
        f"def total() -> int: return len(_TASKS)\n"
        f"def resolve(i: int) -> dict: return _TASKS[i]\n"
    )


# Keep itertools imported so static analysers don't strip it from the
# module namespace if a future grid shape moves the import inline.
_ = itertools
