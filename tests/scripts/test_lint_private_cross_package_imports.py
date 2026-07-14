"""Tests for the private-cross-package-import lint (W2, "promotions can't regress").

Pins these invariants (mirrors ``test_lint_backend_boundary.py``):

1. The real tree passes — every remaining cross-package private import is either
   promoted away or listed in the seeded shrink-only ledger. This is the
   coupling test: it fails if a new un-allowlisted cross-package private import
   lands (the debt cannot grow silently).
2. The lint can actually FIRE: an ops file importing ``state._secret`` across the
   package boundary is reported — including the relative-import evasion
   (``from ...state.run_record import _secret``).
3. A SAME-package private import does NOT fire (package-private is fine within
   its own package), a PUBLIC import does not fire, and a private SUBMODULE
   (``pkg/_mod.py`` on disk) is module privacy, not symbol privacy — skipped.
4. An allowlisted triple passes (the fire is non-tautological).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_private_cross_package_imports",
    REPO_ROOT / "scripts" / "lint_private_cross_package_imports.py",
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_private_cross_package_imports"] = lint
_SPEC.loader.exec_module(lint)


def _hpc_file(tmp_path: Path, rel: str, body: str) -> Path:
    """Write *body* to ``<scan_root>/<rel>`` and return the scan root."""
    root = tmp_path / "src" / "hpc_agent"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return root


def test_real_tree_is_clean() -> None:
    """No un-allowlisted cross-package private import on the current tree (W2)."""
    assert lint.main() == 0


def test_cross_package_private_import_fires(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(lint, "ALLOWLIST", set())
    root = _hpc_file(
        tmp_path,
        "ops/rogue.py",
        "from hpc_agent.state.run_record import _secret\n",
    )
    assert lint.main(root) == 1
    err = capsys.readouterr().err
    assert "rogue.py" in err and "private-cross-package import" in err
    assert "_secret" in err
    assert "private_cross_import_allowlist.txt" in err  # remediation names the ledger


def test_same_package_private_does_not_fire(tmp_path: Path, monkeypatch) -> None:
    """A private symbol imported WITHIN its own package is fine."""
    monkeypatch.setattr(lint, "ALLOWLIST", set())
    root = _hpc_file(
        tmp_path,
        "state/index.py",
        "from hpc_agent.state.run_record import _secret\n",
    )
    assert lint.main(root) == 0


def test_public_import_does_not_fire(tmp_path: Path, monkeypatch) -> None:
    """A public (no leading underscore) cross-package import is fine."""
    monkeypatch.setattr(lint, "ALLOWLIST", set())
    root = _hpc_file(
        tmp_path,
        "ops/uses_public.py",
        "from hpc_agent.state.run_record import current_homedir\n",
    )
    assert lint.main(root) == 0


def test_private_submodule_does_not_fire(tmp_path: Path, monkeypatch) -> None:
    """``from pkg import _mod`` where ``pkg/_mod.py`` exists is module privacy —
    a different thing from the symbol privacy this lint governs. Skipped."""
    monkeypatch.setattr(lint, "ALLOWLIST", set())
    root = _hpc_file(
        tmp_path,
        "ops/importer.py",
        "from hpc_agent.state.cachepkg import _secretmod\n",
    )
    # The private SUBMODULE on disk (would otherwise fire: ops is not inside state).
    (root / "state" / "cachepkg").mkdir(parents=True, exist_ok=True)
    (root / "state" / "cachepkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "state" / "cachepkg" / "_secretmod.py").write_text("", encoding="utf-8")
    assert lint.main(root) == 0


def test_relative_import_evasion_fires(tmp_path: Path, monkeypatch) -> None:
    """A relative import can climb parents and reach a private symbol in another
    package — resolved, not skipped."""
    monkeypatch.setattr(lint, "ALLOWLIST", set())
    root = _hpc_file(
        tmp_path,
        "ops/monitor/sneaky.py",
        "from ...state.run_record import _secret\n",
    )
    assert lint.main(root) == 1


def test_allowlisted_entry_passes(tmp_path: Path, monkeypatch) -> None:
    """A triple in the ledger is sanctioned — the same import that fires above
    passes once allowlisted (so the fire is non-tautological)."""
    monkeypatch.setattr(
        lint,
        "ALLOWLIST",
        {("ops/rogue.py", "hpc_agent.state.run_record", "_secret")},
    )
    root = _hpc_file(
        tmp_path,
        "ops/rogue.py",
        "from hpc_agent.state.run_record import _secret\n",
    )
    assert lint.main(root) == 0
