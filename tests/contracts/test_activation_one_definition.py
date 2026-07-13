"""G6 — activation is owned by ONE definition, not assembled per consumer.

Two mechanized pins that turn the run-#7/#8 ``rc=127`` broken-env class from a
belt (per-symptom seeds + per-consumer tests) into a line CI holds:

1. **Consumer enumeration.** Every control-plane consumer of the login-node
   status reporter (``ssh_status_report`` / ``_ssh_status_report``) must seed the
   run's env activation (``remote_activation=…``). Historically each new
   consumer re-armed the class by forgetting the seed — ``logs --all-failed``
   (#13), ``fetch_failures`` and the aggregate Check-1 reporter were three such
   unseeded consumers found by exactly this lens. This AST walk makes a NEW
   unseeded consumer fail CI instead of exiting 127 live.

2. **One reachability definition.** ``remote_activation_prefix`` (activate at the
   control plane) and the ``Activation`` invariant (accept at submit) must agree
   on WHEN a ``conda activate`` is emittable — the same finding-24 predicate
   (an explicit ``conda_source`` OR a conda-naming module). Gating the two on
   different rules is precisely the #33 drift.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.infra.clusters import Activation, remote_activation_prefix

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "hpc_agent"
_REPORTER_NAMES = {"ssh_status_report", "_ssh_status_report"}

# The row-251 reporter-consumer enumeration, as a set of module paths relative to
# the package root. A seventh consumer either seeds activation (and is added here
# deliberately) or trips this test — it cannot ship unseeded silently.
_KNOWN_CONSUMER_MODULES = {
    "ops/aggregate_flow.py",
    "ops/verify_canary.py",
    "ops/monitor/status.py",
    "ops/monitor/logs_atom.py",
    "ops/monitor/reconcile.py",
    "ops/recover/failures_atom.py",
}


def _name_of(node: ast.expr) -> str | None:
    """The bound name of a ``Name``/``Attribute`` callable reference, else None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _reporter_call_sites() -> list[tuple[str, ast.Call]]:
    """Every ``Call`` that INVOKES the reporter or PASSES it as a callable arg.

    Returns ``(module_relpath, call_node)`` pairs. The reporter-as-arg form
    covers ``reconcile``'s indirection (it hands ``_ssh_status_report`` to a poll
    helper), where the ``remote_activation`` seed rides the same call.
    """
    sites: list[tuple[str, ast.Call]] = []
    for path in _SRC_ROOT.rglob("*.py"):
        rel = path.relative_to(_SRC_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            direct = _name_of(node.func) in _REPORTER_NAMES
            as_arg = any(_name_of(a) in _REPORTER_NAMES for a in node.args)
            if direct or as_arg:
                sites.append((rel, node))
    return sites


def test_every_reporter_consumer_seeds_remote_activation() -> None:
    """No reporter call site may omit ``remote_activation`` — the seed that keeps
    the login-node reporter off the bare python that lacks hpc_agent (rc=127)."""
    unseeded = [
        f"{rel}:{node.lineno}"
        for rel, node in _reporter_call_sites()
        if not any(kw.arg == "remote_activation" for kw in node.keywords)
    ]
    assert not unseeded, (
        "reporter consumer(s) call ssh_status_report without seeding "
        f"remote_activation (rc=127 on conda clusters): {unseeded}. Seed it via "
        "remote_activation_for_sidecar(sidecar, fallback_cluster=record.cluster) "
        "— see ops/monitor/logs_atom.py."
    )


def test_reporter_consumer_set_is_the_enumerated_list() -> None:
    """The discovered consumer set equals the enumerated one — a new consumer
    forces a DELIBERATE edit here (and to the engineering-principles row), so it
    cannot ship without someone confirming it seeds activation."""
    discovered = {rel for rel, _ in _reporter_call_sites()}
    assert discovered == _KNOWN_CONSUMER_MODULES, (
        "reporter-consumer set drifted from the enumeration. Added: "
        f"{sorted(discovered - _KNOWN_CONSUMER_MODULES)}; removed: "
        f"{sorted(_KNOWN_CONSUMER_MODULES - discovered)}. A new consumer must "
        "seed remote_activation (above) and be added to _KNOWN_CONSUMER_MODULES "
        "and the row-251 enumeration in docs/internals/engineering-principles.md."
    )


@pytest.mark.parametrize(
    ("modules", "conda_source", "expect_activate"),
    [
        (["anaconda3/2024.06"], "", True),  # conda-naming module (finding 24)
        (["miniforge3"], "", True),
        (["mamba"], "", True),
        (["gcc/11"], "", False),  # non-conda module — refused at submit
        ([], "/c/conda.sh", True),  # explicit source
        (["gcc/11"], "/c/conda.sh", True),  # source present alongside a plain module
        ([], "", False),  # nothing reaches conda
    ],
)
def test_control_plane_activate_matches_submit_acceptance(
    modules: list[str], conda_source: str, expect_activate: bool
) -> None:
    """``remote_activation_prefix`` emits ``conda activate <env>`` for a
    configured env EXACTLY when the ``Activation`` invariant would ACCEPT that
    env — one definition of "conda reachable" across accept-at-submit and
    activate-at-control-plane (#33 drift closed)."""
    env = "myenv"
    prefix = remote_activation_prefix(
        {"modules": modules, "conda_source": conda_source or None, "conda_envs": [env]}
    )
    control_plane_activates = f"conda activate {env}" in prefix
    assert control_plane_activates is expect_activate

    submit_accepts = True
    try:
        Activation(modules=" ".join(modules), conda_source=conda_source, conda_env=env)
    except errors.SpecInvalid:
        submit_accepts = False
    assert submit_accepts is expect_activate
    # The two definitions must never disagree — that is the pin.
    assert control_plane_activates is submit_accepts
