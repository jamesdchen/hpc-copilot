"""Tests for the bounded auto-prune planner (data-manifest ruling 6).

Pins the three ruling-mandated properties as PURE-planner assertions:

* a manifest-known extra is prunable (and its old sha rides along for journaling);
* an extra that is NOT manifest-known is an ANOMALY — never in ``to_prune``,
  always surfaced;
* a manifest-known set past either cap is REFUSED wholesale (``to_prune`` empty),
  with a disclosed reason.
"""

from __future__ import annotations

from hpc_agent.ops.transfer.manifest import FileEntry
from hpc_agent.ops.transfer.prune import (
    DEFAULT_PRUNE_MAX_BYTES,
    DEFAULT_PRUNE_MAX_FILES,
    plan_prune,
)


def _e(path: str, size: int = 1, sha: str = "deadbeef") -> FileEntry:
    return FileEntry(path=path, size=size, sha256=sha)


def test_manifest_known_extra_is_prunable_with_old_sha() -> None:
    plan = plan_prune(
        [_e("ours/dropped.py", size=10, sha="abc123")],
        manifest_known={"ours/dropped.py"},
    )
    assert plan.refused is False
    assert plan.to_prune == ("ours/dropped.py",)
    assert plan.anomalies == ()
    # the old remote sha rides along for the journal
    assert plan.prunable[0].sha256 == "abc123"
    assert plan.prune_bytes == 10


def test_unknown_extra_is_anomaly_never_pruned() -> None:
    plan = plan_prune(
        [_e("results/stray_output.json")],
        manifest_known=set(),  # first deploy / never shipped by us
    )
    assert plan.to_prune == ()
    assert plan.anomalies == ("results/stray_output.json",)
    assert plan.refused is False


def test_mixed_known_and_unknown_splits_cleanly() -> None:
    plan = plan_prune(
        [_e("ours/a.py"), _e("foreign/b.dat"), _e("ours/c.py")],
        manifest_known={"ours/a.py", "ours/c.py"},
    )
    assert plan.to_prune == ("ours/a.py", "ours/c.py")  # sorted
    assert plan.anomalies == ("foreign/b.dat",)


def test_over_file_count_cap_refuses_with_disclosure() -> None:
    entries = [_e(f"ours/f{i}.py") for i in range(5)]
    known = {e.path for e in entries}
    plan = plan_prune(entries, known, max_files=3)
    assert plan.refused is True
    assert plan.to_prune == ()  # nothing pruned when over-bound
    assert plan.refuse_reason is not None
    assert "max-files" in plan.refuse_reason
    # the would-be delete count is still reported for disclosure
    assert len(plan.prunable) == 5


def test_over_byte_cap_refuses_with_disclosure() -> None:
    plan = plan_prune(
        [_e("ours/big.bin", size=500)],
        manifest_known={"ours/big.bin"},
        max_bytes=100,
    )
    assert plan.refused is True
    assert plan.to_prune == ()
    assert plan.refuse_reason is not None
    assert "max-bytes" in plan.refuse_reason
    assert plan.prune_bytes == 500


def test_at_cap_boundary_is_allowed() -> None:
    """Exactly at the cap is allowed; only STRICTLY over refuses."""
    entries = [_e(f"ours/f{i}.py", size=10) for i in range(3)]
    known = {e.path for e in entries}
    plan = plan_prune(entries, known, max_files=3, max_bytes=30)
    assert plan.refused is False
    assert len(plan.to_prune) == 3


def test_no_extras_is_empty_plan() -> None:
    plan = plan_prune([], manifest_known={"whatever"})
    assert plan.to_prune == ()
    assert plan.anomalies == ()
    assert plan.refused is False


def test_defaults_are_conservative() -> None:
    assert DEFAULT_PRUNE_MAX_FILES == 100
    assert DEFAULT_PRUNE_MAX_BYTES == 100 * 1024 * 1024
