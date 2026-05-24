"""Tests for the auto-retry policy resolver and hardcoded defaults.

The resolver combines a per-run sidecar override (populated at /submit
time when the user supplies a custom policy) with framework defaults
defined in ``hpc_agent.runner.DEFAULT_AUTO_RETRY_POLICY``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.ops.recover.failures_atom import _resolve_auto_retry
from hpc_agent.ops.recover.runner_failures import DEFAULT_AUTO_RETRY_POLICY
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


def _common_required_kwargs(run_id: str = "20260101-000000-resolve") -> dict:
    return dict(
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py",
        result_dir_template="results/{seed}",
        task_count=1,
        tasks_py_sha="1" * 64,
    )


# ---------------------------------------------------------------------------
# Hardcoded defaults
# ---------------------------------------------------------------------------


def test_default_policy_covers_documented_categories() -> None:
    """Defaults must cover every category that classify_failure can emit
    so the resolver always returns useful advice without user config.

    ``ssh_unreachable`` is bucketed by ``cluster_failures_by_fingerprint``
    (not the per-stderr classifier) for entries where the SSH transport
    failed; it's listed here because the policy applies to either path.
    """
    assert set(DEFAULT_AUTO_RETRY_POLICY) == {
        "gpu_oom",
        "system_oom",
        "walltime",
        "node_failure",
        "ssh_unreachable",
    }


def test_default_policy_caps_are_conservative() -> None:
    """Defaults must cap retries low so an auto-retry never compounds bugs."""
    for cat, policy in DEFAULT_AUTO_RETRY_POLICY.items():
        assert policy["max_attempts"] <= 2, f"{cat!r} cap too high"


# ---------------------------------------------------------------------------
# Resolver: sidecar absent -> defaults
# ---------------------------------------------------------------------------


def test_resolve_returns_defaults_when_sidecar_missing(tmp_path: Path) -> None:
    resolved = _resolve_auto_retry(tmp_path, "20260101-000000-nonex0000")
    assert resolved == DEFAULT_AUTO_RETRY_POLICY


def test_resolve_returns_defaults_when_sidecar_has_no_auto_retry(tmp_path: Path) -> None:
    write_run_sidecar(tmp_path, **_common_required_kwargs())
    resolved = _resolve_auto_retry(tmp_path, _common_required_kwargs()["run_id"])
    assert resolved == DEFAULT_AUTO_RETRY_POLICY


# ---------------------------------------------------------------------------
# Resolver: sidecar override
# ---------------------------------------------------------------------------


def test_resolve_returns_sidecar_override(tmp_path: Path) -> None:
    """A user-supplied auto_retry block in the sidecar fully replaces defaults."""
    custom = {
        "gpu_oom": {"max_attempts": 3, "mem_multiplier": 2.0},
        "walltime": {"max_attempts": 0},
    }
    write_run_sidecar(tmp_path, **_common_required_kwargs(), auto_retry=custom)
    resolved = _resolve_auto_retry(tmp_path, _common_required_kwargs()["run_id"])
    # Full replacement: only the keys the user supplied are present.
    assert set(resolved) == set(custom)
    assert resolved["gpu_oom"]["max_attempts"] == 3
    assert resolved["walltime"]["max_attempts"] == 0


def test_resolve_falls_back_to_defaults_when_sidecar_override_is_malformed(
    tmp_path: Path,
) -> None:
    """A non-dict-valued auto_retry entry is filtered out; if everything is
    filtered, the resolver falls back to defaults."""
    # All values are non-dict — every entry gets filtered.
    write_run_sidecar(
        tmp_path,
        **_common_required_kwargs(),
        auto_retry={"gpu_oom": "not a dict", "walltime": 0},  # type: ignore[dict-item]
    )
    resolved = _resolve_auto_retry(tmp_path, _common_required_kwargs()["run_id"])
    assert resolved == DEFAULT_AUTO_RETRY_POLICY
