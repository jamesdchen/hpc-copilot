"""U1 — the sandbox experiment generator (rung 2 of the proving ladder).

Builds a scratch experiment dir and onboards it through the REAL intake
primitives — nothing mocked, nothing hand-written where a primitive owns the
write — so the sandbox block-loop driver (U3) and the anomaly arms (U6)
exercise the same code path a live proving run does. Plan:
``docs/plans/sandbox-proving-run-2026-07-18.md`` §4-U1.

The layout mirrors the live-proven ``demo-hpc`` pi experiment byte-shape for
byte-shape where it counts:

* ``train.py`` — a ``@register_run``-decorated ``run(seed, n_samples)``
  Monte-Carlo pi executor (the shape runs #13–#15 shipped), plus a
  ``"failing"`` variant whose identical signature onboards cleanly and whose
  body raises on every task — the U6(a) ``canary_failed`` anomaly arm.
* ``.hpc/tasks.py`` + ``interview.json`` — materialized by the REAL
  ``interview`` primitive (``record_interview``) from a typed
  ``items_x_seeds`` recipe. ``produced_by`` stamps
  ``{kind: agent, operator: "sandbox-proving"}`` per the U1 spec; the wire
  model additionally REQUIRES ``session_sha`` for ``kind="agent"``, so the
  stamp carries both fields (``_Provenance._check_kind_fields``) — the plan's
  letter, schema-valid.
* ``.hpc/axes.yaml`` — written by the REAL ``axes-init`` primitive (the
  scheduling axes) and the REAL ``classify-axis`` recorder (the
  ``executors.run`` block, ``classified_by="interview"`` — the same recorder
  ``classify-axis-auto``'s caller-supplied branch A drives, minus its
  subprocess preflight so this module stays hermetic). ``data_axis`` is
  ``sequential``: the conservative fail-safe the live demo shipped with — the
  sandbox proves the harness contract, never an elision.

Parameterized sweep (the 2026-07-18 determinism lesson): ``cmd_sha`` is
parameter identity, and ``run_id = <run_name>-<cmd_sha[:8]>`` — an identical
sweep mints an identical ``run_id`` and dedups against the prior sandbox run.
Callers MUST vary ``n_samples`` and/or ``seeds`` across successive sandbox
runs (the live drills bumped ``n_samples`` per attempt for the same reason).
There is deliberately no hardcoded sweep hidden from the caller: the defaults
below are the base point, not a fixture secret.

§3 trust doctrine (binding, never bends): everything lands in a
caller-supplied scratch dir, and the journal home MUST be an ephemeral
``HPC_JOURNAL_DIR``. :func:`require_sandbox_journal_home` REFUSES to run when
the var is unset/empty or resolves inside the production home
``~/.claude/hpc`` — the fixture is structurally incapable of touching a
production namespace. The env var is the one audited channel (CI:
``$RUNNER_TEMP``; local: a tmpdir); the ``HPC_HOMEDIR`` attribute fallback
``current_homedir`` honors is intentionally NOT honored here — an env var is
what the CI lane sets and what a reviewer greps for.

The containment test itself delegates to the ONE shared guard,
:mod:`sandbox_guard` — the alias-proof canonicalization (Defect 1: the
``\\?\\`` / admin-share spellings of the SAME directory bypassed a plain
``resolve()`` comparison) plus the samefile backstop — which U2's
``sandbox_seed.py`` also binds, so both public guards enforce one invariant.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

__all__ = [
    "SANDBOX_OPERATOR",
    "SandboxExperiment",
    "SandboxTrustError",
    "build_sandbox_experiment",
    "require_sandbox_journal_home",
]

# The authorship stamp every sandbox-minted interview carries (plan §4-U1:
# ``produced_by: {kind: agent, operator: sandbox-proving}``). Also the
# ``session_sha`` the wire model requires for ``kind="agent"``.
SANDBOX_OPERATOR = "sandbox-proving"

# Base-point sweep — a STARTING point callers vary, never a hidden constant
# (see the module docstring's determinism note). Eight seeds keeps
# ``total_tasks`` above the canary-skip threshold (4, #263) so the S2 canary
# gate stays on the sandbox path (U6(a)'s failing-canary arm needs it too).
_DEFAULT_SEEDS: tuple[int, ...] = tuple(range(8))
# Fast on the container (<<1s per task) while still a real computation. The
# live drills ran ~10M; the sandbox adjudicates the harness contract, not
# cluster throughput, so small-and-fast wins.
_DEFAULT_N_SAMPLES = 100_000

_DEFAULT_GOAL = (
    "sandbox proving run (rung 2): exercise the full harness contract "
    "end-to-end — block loop, gates, submit-once, kill drill, harvest"
)


class SandboxTrustError(RuntimeError):
    """§3 trust-doctrine refusal: the sandbox would touch (or might be) the
    production journal namespace. Raised BEFORE any file is written."""


# MIRROR: sandbox_seed.py::_load_shared_guard — identical loaders so every
#   consumer binds the ONE guard module object
#   pinned-by tests/integration/scheduler/test_sandbox_guard.py::test_one_guard_module_object
def _load_shared_guard() -> ModuleType:
    """Import the ONE §3 guard module as the same object in every load context.

    This file is loaded three ways: pytest's sys.path-prepend (bare module
    name), the ``tests.integration.scheduler`` package path (a
    ``tests.contracts`` consumer), and by-path exec from
    ``scripts/run_sandbox_proving.py`` (neither import root on sys.path). The
    sys.modules probe + the guard's own exec-time alias registration make the
    first load the ONLY load, so every consumer binds one guard object.
    """
    cached = sys.modules.get("sandbox_guard")
    if cached is not None:
        return cached
    try:
        return importlib.import_module("sandbox_guard")
    except ImportError:
        pass
    guard_path = Path(__file__).resolve().with_name("sandbox_guard.py")
    spec = importlib.util.spec_from_file_location("sandbox_guard", guard_path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load the shared sandbox guard at {guard_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["sandbox_guard"] = module
    spec.loader.exec_module(module)
    return module


_GUARD = _load_shared_guard()


def require_sandbox_journal_home() -> Path:
    """The §3 guard: return the sandbox journal home, or refuse LOUDLY.

    Refuses (:class:`SandboxTrustError`) when ``HPC_JOURNAL_DIR`` is
    unset/empty, or when it resolves to — or anywhere inside — the production
    journal home ``~/.claude/hpc`` under ANY spelling: the shared guard
    de-aliases the ``\\?\\`` / admin-share forms BEFORE comparing (Defect 1 —
    ``resolve()`` alone does not canonicalize them) and backstops the string
    legs with a samefile probe. A SIBLING of the production home (e.g.
    ``~/.claude/hpc-sandbox``) passes: the doctrine names the production
    namespace exactly, and journal writes land under
    ``<journal_home>/<repo_hash>/`` — a sibling never reaches
    ``~/.claude/hpc/<repo_hash>``. The returned path is the canonical
    (de-aliased, resolved) form.
    """
    env_val = os.environ.get("HPC_JOURNAL_DIR")
    if env_val is None or not env_val.strip():
        raise SandboxTrustError(
            "sandbox fixture refuses to run: HPC_JOURNAL_DIR is unset. The "
            "sandbox journal home is ALWAYS an ephemeral dir (plan §3): set "
            "HPC_JOURNAL_DIR to a tmpdir (CI: $RUNNER_TEMP) so no sandbox "
            "write can land in the production journal namespace."
        )
    if _GUARD.is_within_production_home(env_val):
        raise SandboxTrustError(
            f"sandbox fixture refuses to run: HPC_JOURNAL_DIR="
            f"{_GUARD.canonical_journal_path(env_val)} resolves inside the "
            f"production journal home {_GUARD.production_journal_home()}. "
            "Point it at an ephemeral dir (plan §3) — the sandbox must be "
            "structurally incapable of touching a production namespace."
        )
    home: Path = _GUARD.canonical_journal_path(env_val)
    return home


@dataclasses.dataclass(frozen=True)
class SandboxExperiment:
    """The materialized scratch experiment, with the identities U3+ consume.

    ``run_id`` / ``cmd_sha`` come from the REAL ``compute-run-id`` primitive
    over the materialized ``.hpc/tasks.py`` — the same identity the submit
    chain will mint, so the driver asserts against what the framework will
    actually use, never a fixture-side recomputation.
    """

    experiment_dir: Path
    run_name: str
    run_id: str
    cmd_sha: str
    total_tasks: int
    seeds: tuple[int, ...]
    n_samples: int
    executor_variant: str
    goal: str
    train_py: Path
    tasks_py: Path
    interview_json: Path
    axes_yaml: Path


# ── the executor variants ────────────────────────────────────────────────────
#
# The pi shape mirrors the live-proven demo executor (demo-hpc/train.py, runs
# #13–#15): stdlib + the installed hpc_agent only, so it imports unchanged in
# the scheduler container's python3. The failing variant keeps the IDENTICAL
# signature so the same interview materializes and the same swept-flag
# cross-check passes — the failure is a cluster-side runtime property (every
# task raises), which is exactly the U6(a) canary_failed arm.

_TRAIN_PY_PI = '''\
"""Sandbox proving executor — Monte-Carlo pi estimation.

Materialized by tests/integration/scheduler/sandbox_fixture.py (plan-unit U1,
docs/plans/sandbox-proving-run-2026-07-18.md). Mirrors the live proving-run
demo shape: a @register_run-decorated run(seed, n_samples) the framework
discovers by AST and dispatches via `hpc_agent.executor_cli run-registered`.
Stdlib + the installed hpc_agent only.
"""

from __future__ import annotations

import random

from hpc_agent.experiment_kit import register_run


@register_run
def run(seed: int = 0, n_samples: int = 100000) -> dict:
    """Estimate pi by throwing ``n_samples`` darts at the unit square."""
    rng = random.Random(seed)
    inside = sum(
        1 for _ in range(n_samples) if rng.random() ** 2 + rng.random() ** 2 <= 1.0
    )
    pi_est = 4.0 * inside / n_samples
    return {
        "seed": seed,
        "n_samples": n_samples,
        "pi_estimate": pi_est,
        "abs_error": abs(pi_est - 3.141592653589793),
    }
'''

_TRAIN_PY_FAILING = '''\
"""Sandbox proving executor — the FAILING variant (U6 anomaly arm).

Materialized by tests/integration/scheduler/sandbox_fixture.py (plan-unit U1).
Identical signature to the pi executor so the same interview materializes and
the same sweep cross-checks; the failure is a cluster-side runtime property —
every task raises, so the canary fails (the U6(a) canary_failed arm).
"""

from __future__ import annotations

from hpc_agent.experiment_kit import register_run


@register_run
def run(seed: int = 0, n_samples: int = 100000) -> dict:
    """Always fails — the U6 failing-executor canary arm."""
    raise RuntimeError(
        "sandbox failing-executor variant: every task fails by construction "
        "(U6 canary_failed arm)"
    )
'''

_TRAIN_PY_BY_VARIANT = {"pi": _TRAIN_PY_PI, "failing": _TRAIN_PY_FAILING}


def build_sandbox_experiment(
    experiment_dir: Path,
    *,
    run_name: str = "sandbox-pi",
    seeds: Sequence[int] = _DEFAULT_SEEDS,
    n_samples: int = _DEFAULT_N_SAMPLES,
    executor_variant: str = "pi",
    cluster: str | None = None,
    goal: str | None = None,
) -> SandboxExperiment:
    """Build a scratch experiment via the REAL intake primitives.

    Order: §3 guard FIRST (nothing is written before it clears) → ``train.py``
    → ``record_interview`` (materializes ``.hpc/tasks.py`` + ``interview.json``,
    and itself validates the ``@register_run`` is discoverable and the swept
    flags match its signature) → ``axes_init`` + ``classify_axis`` (the two
    axes.yaml blocks) → ``compute_run_id`` (the run identity).

    *seeds* / *n_samples* are the freshness knobs (module docstring): vary
    them across successive sandbox runs or the minted ``run_id`` dedups
    against the prior one. *executor_variant* is ``"pi"`` (default) or
    ``"failing"`` (the U6 canary-fail arm). *cluster*, when given, records a
    ``cluster_target`` ({cluster, profile "cpu"}) so ``meta.json`` lands for
    the driver's walk; omitted, the walk resolves the cluster itself.
    """
    # §3 trust doctrine — BEFORE any file is written.
    require_sandbox_journal_home()

    seed_list = [int(s) for s in seeds]
    if not seed_list:
        raise ValueError(
            "seeds must be non-empty: task_count == len(seeds) and the "
            "items_x_seeds recipe requires >= 1 seed"
        )
    if executor_variant not in _TRAIN_PY_BY_VARIANT:
        raise ValueError(
            f"unknown executor_variant {executor_variant!r}; expected one of "
            f"{sorted(_TRAIN_PY_BY_VARIANT)}"
        )
    if n_samples < 1:
        raise ValueError(f"n_samples must be >= 1; got {n_samples}")

    experiment_dir = Path(experiment_dir)
    experiment_dir.mkdir(parents=True, exist_ok=True)
    train_py = experiment_dir / "train.py"
    train_py.write_text(_TRAIN_PY_BY_VARIANT[executor_variant], encoding="utf-8")

    intent: dict[str, Any] = {
        "goal": goal or _DEFAULT_GOAL,
        "task_count": len(seed_list),
        "task_kind": SANDBOX_OPERATOR,
        "produced_by": {
            "kind": "agent",
            # The wire model REQUIRES session_sha for kind="agent"; the plan
            # names operator. The stamp carries both (see module docstring).
            "session_sha": SANDBOX_OPERATOR,
            "operator": SANDBOX_OPERATOR,
        },
        "notes": (
            "Materialized by tests/integration/scheduler/sandbox_fixture.py "
            "(sandbox proving, rung 2). Seeded/journaled state carries "
            "sandbox-proving provenance; nothing here is a human approval."
        ),
        "task_generator": {
            "kind": "items_x_seeds",
            "params": {"items": [{"n_samples": int(n_samples)}], "seeds": seed_list},
        },
        "entry_point": {"kind": "register_run", "run_name": "run"},
    }
    if cluster is not None:
        intent["cluster_target"] = {"cluster": cluster, "profile": "cpu"}

    from hpc_agent._wire.actions.interview import InterviewSpec
    from hpc_agent.ops.memory.interview import record_interview

    interview_result = record_interview(
        InterviewSpec.model_validate(intent), campaign_dir=experiment_dir
    )

    # axes.yaml, both blocks, via the REAL primitives — axes-init owns the
    # scheduling axes (order mirrors the live demo: seed outer, n_samples the
    # degenerate size-1 axis), classify-axis records the DataAxis. The
    # recorder's upsert preserves the scheduling block.
    from hpc_agent.incorporation.axes_init import axes_init

    axes_init(
        experiment_dir=experiment_dir,
        axes=[
            {"name": "seed", "size": len(seed_list)},
            {"name": "n_samples", "size": 1},
        ],
        homogeneous_axes=[],
    )

    # The interview already proved exactly one @register_run named "run" is
    # discoverable (it refuses otherwise), so this re-discovery is the same
    # AST walk's result — read off the signature sha the recorder persists.
    from hpc_agent._wire.actions.classify_axis import ClassifyAxisInput
    from hpc_agent.experiment_kit.discover import discover_runs
    from hpc_agent.incorporation.classify_axis import classify_axis

    run_info = next(r for r in discover_runs(experiment_dir) if r.name == "run")
    classify_axis(
        experiment_dir,
        spec=ClassifyAxisInput.model_validate(
            {
                "run_name": "run",
                "run_signature_sha": run_info.run_signature_sha,
                # The conservative fail-safe the live demo shipped (see the
                # module docstring) — the sandbox proves the harness contract,
                # never an elision.
                "data_axis": {"kind": "sequential"},
                "classified_by": "interview",
            }
        ),
    )

    from hpc_agent.incorporation.build.compute_run_id import compute_run_id

    identity = compute_run_id(experiment_dir, run_name=run_name)

    return SandboxExperiment(
        experiment_dir=experiment_dir,
        run_name=run_name,
        run_id=str(identity["run_id"]),
        cmd_sha=str(identity["cmd_sha"]),
        total_tasks=int(interview_result["total_tasks"]),
        seeds=tuple(seed_list),
        n_samples=int(n_samples),
        executor_variant=executor_variant,
        goal=str(intent["goal"]),
        train_py=train_py,
        tasks_py=experiment_dir / ".hpc" / "tasks.py",
        interview_json=experiment_dir / "interview.json",
        axes_yaml=experiment_dir / ".hpc" / "axes.yaml",
    )
