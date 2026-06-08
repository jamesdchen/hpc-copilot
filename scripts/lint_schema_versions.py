"""CI lint: the sidecar schema-version constants stay in sync.

``SUPPORTED_SCHEMA_VERSIONS`` is **necessarily duplicated** in the two
cluster-side dispatchers — ``execution/mapreduce/dispatch.py`` and
``execution/mapreduce/combiner.py`` — because both are deployed to compute
nodes *without* the rest of the package (stdlib-only, zero-dependency),
so they cannot import a shared constant. The sync is documented only by
a comment today; this lint makes it enforced.

Two invariants:

* The two ``SUPPORTED_SCHEMA_VERSIONS`` tuples must be identical. A
  dispatcher and a combiner that disagree on what they accept can
  silently diverge on a schema bump.
* The writer's current ``SIDECAR_SCHEMA_VERSION`` (``state/runs.py``)
  must be a member of that supported set — otherwise the orchestrator
  writes sidecars the cluster-side code rejects (``schema_incompat``).

Parsed via ``ast`` rather than imported: ``dispatch.py`` / ``combiner.py``
are stdlib-only modules meant to run detached on the cluster, and we
don't want this lint to depend on importing them (or the package) at all.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src" / "hpc_agent"

DISPATCH = SRC / "execution" / "mapreduce" / "dispatch.py"
COMBINER = SRC / "execution" / "mapreduce" / "combiner.py"
RUNS = SRC / "state" / "runs.py"


def _module_constant(path: Path, name: str) -> object:
    """Return the literal value of a module-level ``name = <literal>``.

    Handles both ``Assign`` (``X = (1, 2)``) and ``AnnAssign``
    (``X: int = 2``). Raises ``KeyError`` if the constant is absent and
    ``ValueError`` if its right-hand side isn't a literal.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == name for t in targets):
            continue
        if node.value is None:
            break
        try:
            return ast.literal_eval(node.value)
        except (ValueError, SyntaxError) as exc:
            raise ValueError(f"{path.name}: {name} is not a literal: {exc}") from exc
    raise KeyError(f"{path.name}: module constant {name!r} not found")


def main() -> int:
    violations: list[str] = []
    try:
        dispatch_versions = _module_constant(DISPATCH, "SUPPORTED_SCHEMA_VERSIONS")
        combiner_versions = _module_constant(COMBINER, "SUPPORTED_SCHEMA_VERSIONS")
        writer_version = _module_constant(RUNS, "SIDECAR_SCHEMA_VERSION")
    except (KeyError, ValueError) as exc:
        print(f"ERROR: could not read a schema-version constant: {exc}")
        return 1

    if dispatch_versions != combiner_versions:
        violations.append(
            "dispatch.py and combiner.py disagree on SUPPORTED_SCHEMA_VERSIONS: "
            f"{dispatch_versions!r} != {combiner_versions!r}"
        )

    supported = dispatch_versions if isinstance(dispatch_versions, tuple) else ()
    if writer_version not in supported:
        violations.append(
            f"state/runs.py SIDECAR_SCHEMA_VERSION={writer_version!r} is not in the "
            f"cluster-side supported set {dispatch_versions!r} — the orchestrator would "
            "write sidecars the dispatcher/combiner reject."
        )

    if violations:
        print("ERROR: cluster-side schema-version constants are out of sync:")
        for v in violations:
            print(f"  {v}")
        print(
            "\nWhy: dispatch.py and combiner.py are deployed stdlib-only to compute "
            "nodes and cannot import a shared constant, so SUPPORTED_SCHEMA_VERSIONS is "
            "duplicated by necessity. Keep both tuples equal and ensure they include "
            "state/runs.py:SIDECAR_SCHEMA_VERSION (the version the orchestrator writes)."
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
