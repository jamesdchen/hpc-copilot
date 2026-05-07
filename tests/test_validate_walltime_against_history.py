"""Tests for ``claude_hpc.atoms.validate_walltime_against_history``.

Pattern: seed runtime_prior with samples via the public
``append_sample`` API (real I/O via tmp_path), optionally write a
``.hpc/playbook.yaml``, then call the validator. The roll-up
machinery is exercised end-to-end.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from claude_hpc._schema_models.validate_walltime_against_history import (
    ValidateWalltimeAgainstHistorySpec,
)
from claude_hpc.atoms.validate_walltime_against_history import (
    validate_walltime_against_history,
)
from claude_hpc.state import runtime_prior as rp

if TYPE_CHECKING:
    from pathlib import Path

_PROFILE = "ml_ridge"
_CLUSTER = "discovery"


def _seed(tmp_path: Path, *, gpu_type: str = "a100", durations: list[int]) -> None:
    for tid, elapsed in enumerate(durations):
        rp.append_sample(
            tmp_path,
            profile=_PROFILE,
            cluster=_CLUSTER,
            run_id=f"r{tid}",
            task_id=tid,
            gpu_type=gpu_type,
            node="d11-07",
            elapsed_sec=elapsed,
        )


def _write_playbook(tmp_path: Path, body: str) -> None:
    target = tmp_path / ".hpc" / "playbook.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)


def _spec(**overrides) -> ValidateWalltimeAgainstHistorySpec:
    base = dict(
        profile=_PROFILE,
        cluster=_CLUSTER,
        requested_walltime_sec=3600,
        gpu_type="a100",
    )
    base.update(overrides)
    return ValidateWalltimeAgainstHistorySpec(**base)


# ─── cold-start ────────────────────────────────────────────────────────


def test_cold_start_emits_info_finding(tmp_path: Path) -> None:
    """No samples → info-level cold_start finding; no quantile check.
    The agent knows the lack of warning is "no data," not "all clear."""
    out = validate_walltime_against_history(tmp_path, spec=_spec())
    finding = next(f for f in out.findings if f.code == "cold_start_no_history")
    assert finding.severity == "info"
    assert all(f.code != "walltime_below_quantile" for f in out.findings)


# ─── walltime_below_quantile (default p95 rule) ────────────────────────


def test_walltime_below_p95_emits_warning(tmp_path: Path) -> None:
    """Default rule: warn when requested < historical p95. Five evenly-
    spaced samples make p95 predictable."""
    _seed(tmp_path, durations=[3000, 4000, 5000, 6000, 7000])
    out = validate_walltime_against_history(
        tmp_path, spec=_spec(requested_walltime_sec=3500)
    )
    finding = next(
        f for f in out.findings if f.code == "walltime_below_quantile"
    )
    assert finding.severity == "warning"
    assert finding.evidence["requested_walltime_sec"] == 3500
    assert finding.evidence["quantile_label"] == "p95"
    assert finding.evidence["gpu_type"] == "a100"
    assert finding.suggested_fix is not None


def test_walltime_at_or_above_p95_emits_no_quantile_finding(tmp_path: Path) -> None:
    _seed(tmp_path, durations=[3000, 4000, 5000])
    out = validate_walltime_against_history(
        tmp_path, spec=_spec(requested_walltime_sec=10000)
    )
    assert all(f.code != "walltime_below_quantile" for f in out.findings)


def test_walltime_check_skipped_when_gpu_type_unknown(tmp_path: Path) -> None:
    """When ``spec.gpu_type`` doesn't match any historical bucket,
    the quantile rule has nothing to compare against — no finding."""
    _seed(tmp_path, gpu_type="a100", durations=[4000])
    out = validate_walltime_against_history(
        tmp_path,
        spec=_spec(gpu_type="h100", requested_walltime_sec=1),
    )
    assert all(f.code != "walltime_below_quantile" for f in out.findings)


def test_playbook_walltime_rules_override_default(tmp_path: Path) -> None:
    """Custom walltime_rules in playbook.yaml replace the default p95
    rule (rather than stack on top)."""
    _seed(tmp_path, durations=[3000, 4000, 5000, 6000, 7000])
    _write_playbook(
        tmp_path,
        """
        walltime_rules:
          - below_quantile: 0.5
            severity: error
            message: requested below p50 — definitely too short
        """,
    )
    out = validate_walltime_against_history(
        tmp_path, spec=_spec(requested_walltime_sec=4500)
    )
    findings = [f for f in out.findings if f.code == "walltime_below_quantile"]
    assert len(findings) == 1
    assert findings[0].severity == "error"
    assert findings[0].evidence["quantile_label"] == "p50"


# ─── known_bad_combination (V100 + attn-fp32 etc.) ─────────────────────


def test_known_bad_combination_fires_when_match(tmp_path: Path) -> None:
    _write_playbook(
        tmp_path,
        """
        known_bad_combinations:
          - gpu: v100
            workload_tag: attn-fp32
            severity: error
            reason: V100 fp32 attention is numerically unstable
        """,
    )
    out = validate_walltime_against_history(
        tmp_path,
        spec=_spec(gpu_type="v100", workload_tags=["attn-fp32"]),
    )
    finding = next(
        f for f in out.findings if f.code == "known_bad_combination"
    )
    assert finding.severity == "error"
    assert "V100 fp32 attention" in finding.message
    assert finding.evidence == {"gpu_type": "v100", "workload_tag": "attn-fp32"}
    assert finding.suggested_fix is not None


def test_known_bad_combination_silent_when_gpu_or_tag_mismatches(tmp_path: Path) -> None:
    _write_playbook(
        tmp_path,
        """
        known_bad_combinations:
          - gpu: v100
            workload_tag: attn-fp32
            severity: error
            reason: x
        """,
    )
    # Different GPU.
    out_a = validate_walltime_against_history(
        tmp_path, spec=_spec(gpu_type="a100", workload_tags=["attn-fp32"])
    )
    assert all(f.code != "known_bad_combination" for f in out_a.findings)
    # Different tag.
    out_b = validate_walltime_against_history(
        tmp_path, spec=_spec(gpu_type="v100", workload_tags=["mixed-precision"])
    )
    assert all(f.code != "known_bad_combination" for f in out_b.findings)


def test_known_bad_silent_when_workload_tags_empty(tmp_path: Path) -> None:
    """No workload_tags supplied → playbook lookup is disabled. Lets
    callers opt out without editing playbook.yaml."""
    _write_playbook(
        tmp_path,
        """
        known_bad_combinations:
          - gpu: v100
            workload_tag: attn-fp32
            severity: error
            reason: x
        """,
    )
    out = validate_walltime_against_history(
        tmp_path, spec=_spec(gpu_type="v100", workload_tags=[])
    )
    assert all(f.code != "known_bad_combination" for f in out.findings)


# ─── playbook errors surface as one finding ────────────────────────────


def test_malformed_playbook_emits_single_error_finding(tmp_path: Path) -> None:
    _write_playbook(tmp_path, "{not yaml: at all: oops")
    out = validate_walltime_against_history(tmp_path, spec=_spec())
    findings = out.findings
    assert len(findings) == 1
    assert findings[0].code == "playbook_parse_error"
    assert findings[0].severity == "error"
