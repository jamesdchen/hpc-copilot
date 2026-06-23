"""Tests for the backend-boundary lint (issue #337, "enforce the seam").

Pins these invariants:

1. The real tree passes — no orchestrator file imports a concrete backend
   module today (verified out-of-band before this lint landed).
2. The lint can actually FIRE: an orchestrator file importing a concrete
   backend module is reported with the seam remediation — including the
   evasive spellings (lazy function-level imports, the
   ``from hpc_agent.infra.backends import slurm`` alias form, the
   ``from ...infra.backends import slurm`` relative climb, and reaching a
   private internal like ``_engine``).
3. The allowed seam surfaces — the package root ``hpc_agent.infra.backends``
   (and re-exports off it), ``remote_factory``, and ``profile`` — do NOT
   fire, from any orchestrator package.
4. Non-orchestrator code (``infra`` itself) may import concrete modules
   freely — the scan is scoped to the orchestrator packages.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_backend_boundary", REPO_ROOT / "scripts" / "lint_backend_boundary.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_backend_boundary"] = lint
_SPEC.loader.exec_module(lint)


def test_real_tree_is_clean() -> None:
    """The orchestrator imports only the seam on the current tree (#337)."""
    assert lint.main() == 0


def _orchestrator_file(tmp_path: Path, rel: str, body: str) -> Path:
    """Write *body* to ``<scan_root>/<rel>`` and return the scan root."""
    root = tmp_path / "src" / "hpc_agent"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return root


def test_concrete_import_fires(tmp_path: Path, capsys) -> None:
    root = _orchestrator_file(
        tmp_path,
        "ops/rogue.py",
        "from hpc_agent.infra.backends.slurm import SGEBackend\n",
    )
    assert lint.main(root) == 1
    err = capsys.readouterr().err
    assert "rogue.py" in err and "backend-boundary import" in err
    assert "lint_backend_boundary.py" in err  # remediation names the allowlist


def test_alias_form_import_fires(tmp_path: Path) -> None:
    """``from hpc_agent.infra.backends import slurm`` binds the concrete module
    without its dotted name appearing in the ``from`` clause — must still fire."""
    root = _orchestrator_file(
        tmp_path,
        "ops/alias.py",
        "from hpc_agent.infra.backends import slurm\n",
    )
    assert lint.main(root) == 1


def test_lazy_function_level_import_fires(tmp_path: Path) -> None:
    """A lazy import crosses the seam just the same — the AST walk sees inside
    function bodies."""
    root = _orchestrator_file(
        tmp_path,
        "recovery/lazy.py",
        "def f():\n"
        "    from hpc_agent.infra.backends import slurm_remote\n"
        "    return slurm_remote\n",
    )
    assert lint.main(root) == 1


def test_relative_import_evasion_fires(tmp_path: Path) -> None:
    """A relative import can climb parents (``from ...infra.backends import sge``)
    and reach the concrete module — resolved, not skipped."""
    root = _orchestrator_file(
        tmp_path,
        "meta/campaign/sneaky.py",
        "from ...infra.backends import sge\n",
    )
    assert lint.main(root) == 1


def test_internal_module_import_fires(tmp_path: Path) -> None:
    """A private internal (``_engine`` / ``_remote_base`` / ``_scripts`` /
    ``query``) is forbidden too, reaching into a submember as well."""
    root = _orchestrator_file(
        tmp_path,
        "incorporation/peek.py",
        "from hpc_agent.infra.backends._engine import ProfileBackend\n",
    )
    assert lint.main(root) == 1


def test_seam_package_root_is_allowed(tmp_path: Path) -> None:
    """The HPCBackend interface + registry functions re-exported off the
    package root are the seam — importing them never fires."""
    root = _orchestrator_file(
        tmp_path,
        "ops/uses_seam.py",
        "from hpc_agent.infra.backends import HPCBackend, get_backend_class\n",
    )
    assert lint.main(root) == 0


def test_remote_factory_and_profile_are_allowed(tmp_path: Path) -> None:
    """The construction factory and scheduler-as-data are orchestrator-safe."""
    root = _orchestrator_file(
        tmp_path,
        "integration/uses_factory.py",
        "from hpc_agent.infra.backends.remote_factory import build_remote_backend\n"
        "from hpc_agent.infra.backends.profile import PBSPRO_PROFILE\n",
    )
    assert lint.main(root) == 0


def test_non_orchestrator_code_may_import_concrete(tmp_path: Path) -> None:
    """The scan is scoped to the orchestrator: ``infra`` itself (where the
    backends live) may bind concrete modules freely."""
    root = _orchestrator_file(
        tmp_path,
        "infra/backends/remote_factory.py",
        "from hpc_agent.infra.backends.slurm_remote import RemoteSlurmBackend\n",
    )
    assert lint.main(root) == 0
