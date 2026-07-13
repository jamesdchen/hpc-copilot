"""Contract: baked ``operations.json`` declares honest verb metadata.

``src/hpc_agent/operations.json`` is the diff-able snapshot of the live
``@primitive`` registry (baked by ``scripts/bake_operations_json.py`` from
:func:`hpc_agent._kernel.registry.operations.operations_catalog`; the registry
is the runtime SoT). Each row carries ``name``, ``verb``, ``side_effects``,
``summary``, and the schema/CLI pointers.

Two metadata fields silently rot when a new primitive lands without them:

* ``summary`` — projected from the primitive's ``cli.help`` string. An
  agent-facing verb with a blank summary is invisible to the ``find``
  discovery tier and to the capabilities catalog table: the operator sees a
  verb name with no gloss.
* ``side_effects`` — the declared-effect set. A ``mutate`` verb that claims
  NO side effect is the bug class this test pins: the whole point of the
  ``mutate`` tier is that it writes somewhere (cluster, sidecar, journal),
  and an empty ``side_effects`` list lies about that.

The concrete defect that motivated this test: ``update-run-constraints`` (a
``mutate`` verb that SSHes to run ``scontrol update ... Features=...`` and
mirrors the new feature set onto the run sidecar) shipped with ``summary=""``
and a bare ``side_effects=["ssh"]``. It had no CLI declaration, so the catalog
carried it with no human-readable gloss at all.

The asserts below encode rules DERIVED from the actual catalog shape so they
are green on HEAD, while still failing loudly for the pre-fix state:

1. Every verb that declares a CLI surface (``cli`` is not null) must carry a
   non-empty ``summary`` — ``summary`` IS ``cli.help``, so a CLI verb with a
   blank help string is the documentation defect.
2. Every ``verb == "mutate"`` must declare a non-empty ``side_effects`` list
   (a mutate that claims no effect is the bug class).
3. Every ``mutate`` verb that reaches the cluster (``"ssh"`` in
   ``side_effects``) must carry a non-empty ``summary``. This is the row
   ``update-run-constraints`` violated pre-fix: a cluster-mutating verb with
   no gloss. Sibling cluster-mutating verbs (``kill``, ``reconcile-journal``,
   ``reconcile-stale``, ``watcher-install``, ``combine-wave``,
   ``cluster-reduce``) all satisfy it, so the rule is green on HEAD once
   ``update-run-constraints`` is fixed-and-regened and would have been RED on
   its ``summary=""`` pre-fix state.

Pure query/validate verbs legitimately carry ``side_effects=[]`` and (when
they expose no CLI) an empty ``summary``; the rules above intentionally do not
touch them, so the test tracks the real contract rather than an aspirational
one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ``src/hpc_agent/operations.json`` — the baked snapshot under test.
_OPERATIONS_JSON = Path(__file__).resolve().parents[2] / "src" / "hpc_agent" / "operations.json"

# Verb tiers that, by definition, change state somewhere. Kept narrow (only
# ``mutate`` today) because the concrete contract this test pins is the
# mutate-tier one; widen deliberately with its own assert if a new
# state-changing tier needs the same guarantee.
_MUTATING_VERBS = frozenset({"mutate"})


def _catalog() -> list[dict[str, Any]]:
    """Load the baked operations catalog (list of op dicts)."""
    data: list[dict[str, Any]] = json.loads(_OPERATIONS_JSON.read_text(encoding="utf-8"))
    return data


def _summary(op: dict[str, Any]) -> str:
    return str(op.get("summary") or "")


def _side_effects(op: dict[str, Any]) -> list[str]:
    return list(op.get("side_effects") or [])


def test_operations_json_present_and_nonempty() -> None:
    """The baked snapshot exists and carries the expected row shape."""
    catalog = _catalog()
    assert catalog, f"{_OPERATIONS_JSON} is empty — the bake produced no rows."
    required = {"name", "verb", "side_effects", "summary"}
    missing = {op.get("name", "?") for op in catalog if not required <= set(op)}
    assert not missing, (
        f"operations.json rows missing one of {sorted(required)}: {sorted(missing)}. "
        "Re-bake with scripts/bake_operations_json.py --write."
    )


def test_cli_backed_verbs_carry_a_summary() -> None:
    """Any verb that declares a CLI surface must carry a non-empty summary.

    ``summary`` is projected from the primitive's ``cli.help`` string, so a
    CLI verb with a blank help string ships an undocumented subcommand. Verbs
    with ``cli == null`` (composed atoms, Tier-3 verbs) are exempt — they have
    no help string to surface.
    """
    offenders = sorted(
        op["name"] for op in _catalog() if op.get("cli") and not _summary(op).strip()
    )
    assert not offenders, (
        "These CLI-backed verbs carry an empty `summary` (== their `cli.help`): "
        f"{offenders}. Add a `help=` to the primitive's `cli=CliShape(...)` in "
        "its source module, then re-bake operations.json."
    )


def test_mutate_verbs_declare_side_effects() -> None:
    """A mutate verb that claims NO side effect is the bug class.

    The mutate tier exists precisely because these verbs write somewhere; an
    empty ``side_effects`` list misreports the verb as inert.
    """
    offenders = sorted(
        op["name"]
        for op in _catalog()
        if op.get("verb") in _MUTATING_VERBS and not _side_effects(op)
    )
    assert not offenders, (
        "These mutate-tier verbs declare an empty `side_effects` list — a "
        f"mutate that claims no effect is a lie about what it writes: {offenders}. "
        "Add the SideEffect(...) entries to the @primitive decorator."
    )


def test_cluster_mutating_verbs_carry_a_summary() -> None:
    """Every mutate verb that reaches the cluster (ssh) must be glossed.

    This is the row ``update-run-constraints`` violated pre-fix: a
    cluster-mutating verb (`scontrol update ... Features=...`) with an empty
    ``summary`` and no CLI gloss. Its sibling cluster-mutating verbs all carry
    a summary, so the invariant is green once this one is fixed-and-regened.
    """
    offenders = sorted(
        op["name"]
        for op in _catalog()
        if op.get("verb") in _MUTATING_VERBS
        and "ssh" in _side_effects(op)
        and not _summary(op).strip()
    )
    assert not offenders, (
        "These cluster-mutating (mutate + ssh) verbs carry no `summary`, so the "
        f"operator sees a cluster-writing verb with no gloss: {offenders}. Give "
        "each a `cli=CliShape(help=...)` in its source and re-bake operations.json."
    )
