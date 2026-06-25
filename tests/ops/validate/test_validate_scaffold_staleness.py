"""Tests for the ``validate-scaffold-staleness`` validator atom (#364).

Verifies the validator's finding contract: a stale scaffold yields a
single ``error``-severity ``stale_scaffold`` finding (which the
``validate-campaign`` composer escalates to ``overall="fail"``), and a
fresh/stamped scaffold yields none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.incorporation.build.scaffold_meta import stamp_scaffold_meta
from hpc_agent.ops.validate.scaffold_staleness import validate_scaffold_staleness

if TYPE_CHECKING:
    from pathlib import Path


def _hpc(tmp_path: Path) -> Path:
    d = tmp_path / ".hpc"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_fresh_stamped_scaffold_has_no_findings(tmp_path: Path) -> None:
    hpc = _hpc(tmp_path)
    (hpc / "tasks.py").write_text("from hpc_agent.executor_cli import flag\n")
    stamp_scaffold_meta(tmp_path, scaffold_files=["tasks.py"])
    result = validate_scaffold_staleness(tmp_path)
    assert result.findings == []
    assert result.status == "fresh"


def test_stale_scaffold_emits_error_finding(tmp_path: Path) -> None:
    hpc = _hpc(tmp_path)
    (hpc / "_build_tasks.py").write_text("from hpc_agent.template import register_run\n")
    result = validate_scaffold_staleness(tmp_path)
    assert result.status == "stale"
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.code == "stale_scaffold"
    assert finding.severity == "error"
    assert finding.validator == "validate-scaffold-staleness"
    # Evidence carries the broken imports + legacy artifacts for the caller.
    assert "_build_tasks.py" in finding.evidence["legacy_artifacts"]
    assert finding.suggested_fix and "regenerate" in finding.suggested_fix.lower()


def test_stale_finding_names_versions_in_evidence(tmp_path: Path) -> None:
    hpc = _hpc(tmp_path)
    (hpc / "tasks.py").write_text("from hpc_agent.template.axis import DataAxis\n")
    result = validate_scaffold_staleness(tmp_path)
    finding = result.findings[0]
    assert finding.evidence["installed_version"] == result.installed_version
    assert finding.evidence["unresolved_imports"]
