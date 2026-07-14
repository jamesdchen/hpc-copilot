"""Tests for the plugin->core API-surface lint (W3 / P5b, "declare + pin").

Pins these invariants:

1. The real tree passes — the shipped notebook-render plugin imports only
   the declared surface, and every declared entry still resolves in core.
2. Stay-inside can FIRE: a synthetic plugin import of an *undeclared* core
   module is reported (a whole module absent from the allowlist).
3. Symbol-level granularity: importing an undeclared *symbol* from an
   allowlisted module fires even though the module itself is allowed.
4. The allowlist covers the real plugin EXACTLY — zero within-allowlist
   violations AND no dead (unused) allowlist entry.
5. Anti-drift can FIRE: an allowlisted symbol that no longer resolves in
   core (a reorg rename) is reported.
6. The contract doc lists every allowlisted module path verbatim, so the
   doc cannot silently drift from the enforced allowlist.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_plugin_api_surface", REPO_ROOT / "scripts" / "lint_plugin_api_surface.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_plugin_api_surface"] = lint
_SPEC.loader.exec_module(lint)

_DOC = REPO_ROOT / "docs" / "reference" / "plugin-api-contract.md"


def _scan_source(tmp_path: Path, body: str) -> set[tuple[str, str]]:
    """Write *body* into a temp plugin tree and return the scanned import set."""
    root = tmp_path / "src" / "hpc_agent_fake_plugin"
    root.mkdir(parents=True, exist_ok=True)
    (root / "mod.py").write_text(body, encoding="utf-8")
    return lint.scan_plugin_imports(tmp_path / "src")


def test_real_tree_is_clean() -> None:
    """The shipped plugin stays inside the allowlist and every entry resolves."""
    assert lint.main([]) == 0


def test_stay_inside_fires_on_undeclared_import(tmp_path: Path) -> None:
    """An import of an undeclared core module is a stay-inside violation."""
    scanned = _scan_source(tmp_path, "from hpc_agent.ops.jobs.submit_flow import run\n")
    violations = lint.check_within_allowlist(scanned, lint.ALLOWED_PLUGIN_IMPORTS)
    assert len(violations) == 1
    assert "hpc_agent.ops.jobs.submit_flow" in violations[0]


def test_symbol_level_fires(tmp_path: Path) -> None:
    """An undeclared symbol from an ALLOWLISTED module still fires."""
    scanned = _scan_source(
        tmp_path, "from hpc_agent.ops.notebook.canonical import not_a_real_symbol\n"
    )
    violations = lint.check_within_allowlist(scanned, lint.ALLOWED_PLUGIN_IMPORTS)
    assert len(violations) == 1
    assert "not_a_real_symbol" in violations[0]


def test_allowlist_covers_the_real_plugin_exactly() -> None:
    """Zero within-allowlist violations AND no dead allowlist entries."""
    scanned = lint.scan_plugin_imports(lint.PLUGIN_SRC_ROOT)
    assert lint.check_within_allowlist(scanned, lint.ALLOWED_PLUGIN_IMPORTS) == []
    assert lint.unused_allowlist_entries(scanned, lint.ALLOWED_PLUGIN_IMPORTS) == set()


def test_anti_drift_fires_on_core_rename() -> None:
    """A renamed/removed allowlisted symbol fails the anti-drift resolve check."""
    bogus = {"hpc_agent.ops.notebook.canonical": ("gone_symbol",)}
    violations = lint.check_allowlist_resolves(bogus)
    assert len(violations) == 1
    assert "gone_symbol" in violations[0]


def test_doc_lists_every_allowlisted_module() -> None:
    """Every allowlisted module path appears verbatim in the contract doc."""
    doc_text = _DOC.read_text(encoding="utf-8")
    missing = [m for m in lint.ALLOWED_PLUGIN_IMPORTS if m not in doc_text]
    assert missing == [], f"contract doc is missing module rows: {missing}"
