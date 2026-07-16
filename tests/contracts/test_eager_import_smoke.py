"""Contract: the lazy root ``__init__`` keeps its cold-start floor AND still
resolves every deferred symbol (B3 / PEP 562).

``import hpc_agent`` used to eagerly pull ``hpc_agent.infra`` (pydantic + yaml
+ transport) and the kernel registry, plus ``importlib.metadata`` for the
version — ~0.9-1.3s. B3 moved those symbols behind a module ``__getattr__``
(the ``_LAZY_PUBLIC`` table) so a bare import touches none of them.

Two obligations, both pinned here:

* **cold.init-eager-import-floor** — a bare ``import hpc_agent`` must NOT load
  the heavy submodules (subprocess-isolated so a sibling test that imported
  them earlier can't mask a regression).
* **eager smoke covers every deferred symbol** — the guard-can-fire principle
  applied to the smoke itself: :data:`EAGER_SYMBOLS` must resolve without error
  AND must be a superset of the runtime-deferred set. Add a name to
  ``_LAZY_PUBLIC`` without listing it here and this test reds (see the seeded
  case below), so the smoke can never pass vacuously.

Runs in the default tier (not a CI workflow step) so it also covers the
3.10 / 3.11 / Windows legs of the matrix (ARCHITECT-MEMO A2/A13).
"""

from __future__ import annotations

import ast
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

import hpc_agent

_INIT_PATH = Path(hpc_agent.__file__)

# Every root symbol whose resolution is deferred to ``__getattr__`` — the
# ``_LAZY_PUBLIC`` current-home names, the lazily-computed ``__version__``, and
# the eager-but-underscore ``_PACKAGE_ROOT`` (G4). The eager smoke must import
# ALL of them without error; the coverage guard below proves this list stays a
# superset of the runtime-deferred set as new names are added.
EAGER_SYMBOLS: tuple[str, ...] = (
    # _LAZY_PUBLIC (current home, no DeprecationWarning)
    "register_run",
    "JournalLayout",
    "RepoLayout",
    "PrimitiveMeta",
    "SideEffect",
    "get_meta",
    "get_registry",
    "primitive",
    "register_primitives",
    "load_clusters_config",
    # computed lazily, cached back
    "__version__",
    # eager underscore attr (G4) — served without import-time work
    "_PACKAGE_ROOT",
)

# Heavy submodules a bare ``import hpc_agent`` must NOT drag in. Each was an
# eager import before B3; loading any of them on bare import re-grows the
# cold-start tax and reds this contract.
_MUST_NOT_LOAD_ON_IMPORT: tuple[str, ...] = (
    "hpc_agent.infra",
    "hpc_agent.infra.clusters",
    "hpc_agent._kernel.registry.primitive",
    "importlib.metadata",
)


def test_eager_smoke_resolves_every_deferred_symbol() -> None:
    """Every deferred root symbol resolves through ``__getattr__`` cleanly."""
    for name in EAGER_SYMBOLS:
        # DeprecationWarning is only for _MOVED names (none listed here); a
        # current-home symbol resolving must be warning-free.
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            value = getattr(hpc_agent, name)
        assert value is not None, f"hpc_agent.{name} resolved to None"


def test_eager_smoke_covers_the_deferred_set() -> None:
    """Guard-can-fire on the smoke: EAGER_SYMBOLS must cover every deferred name.

    The runtime-deferred set is ``_LAZY_PUBLIC`` (heavy current-home imports)
    plus ``__version__``. If a future change defers another symbol but forgets
    to list it here, the smoke would silently stop covering it — this assertion
    fires instead.
    """
    deferred = set(hpc_agent._LAZY_PUBLIC) | {"__version__"}
    uncovered = deferred - set(EAGER_SYMBOLS)
    assert not uncovered, (
        "EAGER_SYMBOLS no longer covers every deferred root symbol; add "
        f"{sorted(uncovered)} to tests/contracts/test_eager_import_smoke.py."
    )


def test_seeded_removal_reds_the_coverage_guard() -> None:
    """Seed: dropping a deferred name from the eager list must fail coverage.

    Proves the coverage assertion in the test above actually fires (it is not
    vacuous) by re-running the exact check against a list with one deferred
    name removed.
    """
    deferred = set(hpc_agent._LAZY_PUBLIC) | {"__version__"}
    seeded = set(EAGER_SYMBOLS) - {"load_clusters_config"}
    uncovered = deferred - seeded
    assert uncovered == {"load_clusters_config"}, (
        "seeded removal did not expose a coverage gap — the guard is vacuous"
    )


def test_bare_import_does_not_load_heavy_submodules() -> None:
    """cold.init-eager-import-floor: a bare ``import hpc_agent`` stays lazy.

    Subprocess-isolated: another test in this process may have already imported
    ``hpc_agent.infra`` (or triggered ``load_clusters_config``), which would
    mask a regression if we checked ``sys.modules`` in-process.
    """
    checks = ", ".join(f"{m!r}: {m!r} in sys.modules" for m in _MUST_NOT_LOAD_ON_IMPORT)
    code = f"import sys; import hpc_agent; print({{{checks}}})"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    loaded = ast.literal_eval(proc.stdout.strip())
    offenders = sorted(name for name, present in loaded.items() if present)
    assert not offenders, (
        "bare `import hpc_agent` eagerly loaded heavy submodule(s) "
        f"{offenders} — B3 defers these behind __getattr__. stderr:\n{proc.stderr}"
    )


def test_package_root_resolves_without_getattr_import_work() -> None:
    """``hpc_agent._PACKAGE_ROOT`` is a real eager attr (G4), not a lazy import."""
    assert hpc_agent._PACKAGE_ROOT.is_dir()
    assert (hpc_agent._PACKAGE_ROOT / "__init__.py").is_file()


def test_unknown_attribute_raises_honest_attributeerror() -> None:
    """``__getattr__`` raises AttributeError for unknown names — never swallows.

    Covers both a plain name and an underscore name (G4): the shim must not
    treat ``_not_a_real_attr`` as an implicit lazy target.
    """
    for missing in ("definitely_not_an_export", "_not_a_real_attr"):
        with pytest.raises(AttributeError, match=missing):
            getattr(hpc_agent, missing)


def _type_checking_imported_names() -> set[str]:
    """Names imported inside the ``if TYPE_CHECKING:`` block of ``__init__.py``."""
    tree = ast.parse(_INIT_PATH.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        is_tc = (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )
        if not is_tc:
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.ImportFrom):
                for alias in stmt.names:
                    names.add(alias.asname or alias.name)
    return names


def test_type_checking_mirror() -> None:
    """The mypy TYPE_CHECKING mirror must cover every ``_LAZY_PUBLIC`` name.

    ``__getattr__`` returns ``Any``, so without the mirror mypy would silently
    type ``hpc_agent.JournalLayout`` as ``Any``. The MIRROR annotation in
    ``__init__.py`` names this test as the drift pin (lint_mirror_ledger); it
    must red if the twin loses a name that ``_LAZY_PUBLIC`` still defers.
    """
    mirror = _type_checking_imported_names()
    missing = set(hpc_agent._LAZY_PUBLIC) - mirror
    assert not missing, (
        "the `if TYPE_CHECKING:` mirror in src/hpc_agent/__init__.py is missing "
        f"{sorted(missing)} — mypy would type these as Any. Keep the block in "
        "step with _LAZY_PUBLIC."
    )
