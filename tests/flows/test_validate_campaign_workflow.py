"""End-to-end tests for the ``validate-campaign`` workflow primitive.

Pattern: real ``tmp_path`` filesystem, real atom invocations (no
mocks of the function under test). Tests verify the composer's
contract: which atoms run when, how findings aggregate, how
``overall`` is derived.
"""

from __future__ import annotations

import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from hpc_agent._schema_models.workflows.validate_campaign import ValidateCampaignSpec
from hpc_agent.flows.validate_campaign import validate_campaign
from hpc_agent.state import runtime_prior as rp

if TYPE_CHECKING:
    from pathlib import Path

_PROFILE = "ml_ridge"
_CLUSTER = "discovery"


@pytest.fixture(autouse=True)
def _cleanup_sys_path():
    snapshot = list(sys.path)
    snapshot_modules = set(sys.modules)
    yield
    sys.path[:] = snapshot
    for mod in set(sys.modules) - snapshot_modules:
        sys.modules.pop(mod, None)


def _write_executor(tmp_path: Path, body: str) -> str:
    pkg = tmp_path / "exec_pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "exec_mod.py").write_text(body)
    sys.path.insert(0, str(tmp_path))
    return "exec_pkg.exec_mod"


def _write_tasks_py(tmp_path: Path, tasks: list[dict]) -> None:
    target = tmp_path / ".hpc" / "tasks.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        f"_TASKS = {tasks!r}\ndef total(): return len(_TASKS)\ndef resolve(i): return _TASKS[i]\n"
    )


# ─── verdict aggregation ───────────────────────────────────────────────


def test_no_validators_runs_returns_pass(tmp_path: Path) -> None:
    """Spec with no validator inputs → composer skips everything,
    returns pass with empty findings + empty validators_run."""
    report = validate_campaign(
        tmp_path,
        spec=ValidateCampaignSpec(profile=_PROFILE, cluster=_CLUSTER),
    )
    assert report.overall == "pass"
    assert report.findings == []
    assert report.validators_run == []


def test_only_walltime_validator_runs_when_only_walltime_supplied(tmp_path: Path) -> None:
    """When the spec only supplies the walltime params, only that atom
    runs; ``validators_run`` reflects exactly which atoms fired."""
    report = validate_campaign(
        tmp_path,
        spec=ValidateCampaignSpec(
            profile=_PROFILE,
            cluster=_CLUSTER,
            requested_walltime_sec=3600,
            gpu_type="a100",
        ),
    )
    assert report.validators_run == ["validate-walltime-against-history"]


def test_overall_fail_when_any_error_finding(tmp_path: Path) -> None:
    """Any error-severity finding pulls ``overall`` to fail. Pinning
    so the aggregator can't silently downgrade an error to warn."""
    modname = _write_executor(
        tmp_path,
        textwrap.dedent("""
            from typing import Literal
            def main(s: Literal["a", "b"]) -> None:
                pass
        """).strip(),
    )
    _write_tasks_py(tmp_path, [{"s": "fabricated"}])

    report = validate_campaign(
        tmp_path,
        spec=ValidateCampaignSpec(
            profile=_PROFILE,
            cluster=_CLUSTER,
            executor_module=modname,
            executor_function="main",
        ),
    )
    assert report.overall == "fail"
    assert any(f.severity == "error" for f in report.findings)


def test_overall_warn_when_only_warnings(tmp_path: Path) -> None:
    """Walltime below p95 default rule emits a warning. With no
    error-severity findings elsewhere, overall must be warn."""
    for tid, elapsed in enumerate([3000, 4000, 5000, 6000, 7000]):
        rp.append_sample(
            tmp_path,
            profile=_PROFILE,
            cluster=_CLUSTER,
            run_id=f"r{tid}",
            task_id=tid,
            gpu_type="a100",
            node="d11-07",
            elapsed_sec=elapsed,
        )
    report = validate_campaign(
        tmp_path,
        spec=ValidateCampaignSpec(
            profile=_PROFILE,
            cluster=_CLUSTER,
            requested_walltime_sec=3500,
            gpu_type="a100",
        ),
    )
    assert report.overall == "warn"


def test_overall_pass_when_only_info_findings(tmp_path: Path) -> None:
    """Cold-start emits an info finding; that's the only output. Info
    must NOT escalate the verdict."""
    report = validate_campaign(
        tmp_path,
        spec=ValidateCampaignSpec(
            profile=_PROFILE,
            cluster=_CLUSTER,
            requested_walltime_sec=3600,
            gpu_type="a100",
        ),
    )
    assert report.overall == "pass"
    assert any(f.severity == "info" for f in report.findings)


# ─── findings carry validator name (provenance) ────────────────────────


def test_each_finding_carries_validator_name(tmp_path: Path) -> None:
    """Findings must record which validator emitted them so the agent
    can branch / format per source."""
    modname = _write_executor(
        tmp_path,
        textwrap.dedent("""
            def main(x: int) -> None:
                pass
        """).strip(),
    )
    _write_tasks_py(tmp_path, [{"x": 1, "fabricated": "value"}])

    report = validate_campaign(
        tmp_path,
        spec=ValidateCampaignSpec(
            profile=_PROFILE,
            cluster=_CLUSTER,
            executor_module=modname,
            executor_function="main",
        ),
    )
    for f in report.findings:
        assert f.validator == "validate-executor-signatures"
