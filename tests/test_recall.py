"""Tests for the ``recall`` primitive and its CLI adapter.

Recall walks a directory tree for ``interview.json`` files and projects
each into a recency-sorted, filterable summary. The tests pin:

- Discovery (rglob over arbitrary nesting)
- Filter semantics (exact-match on task_kind / operator; ISO-8601 since)
- Recency sort (latest first)
- Limit truncation + total_matching reporting
- Resilience to malformed / non-interview JSON
- CLI envelope shape
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any

from claude_hpc.atoms.recall import recall_campaigns

if TYPE_CHECKING:
    from pathlib import Path


def _write_interview(
    campaign_dir: Path,
    *,
    goal: str = "test campaign",
    task_kind: str | None = None,
    task_count: int = 3,
    operator: str | None = None,
    materialized_at: str = "2026-05-04T12:00:00+00:00",
    cmd_sha: str = "deadbeef" * 8,
    extra: dict[str, Any] | None = None,
) -> None:
    campaign_dir.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {
        "goal": goal,
        "task_count": task_count,
        "produced_by": {"kind": "human", "operator": operator},
        "_materialized": {
            "at": materialized_at,
            "cmd_sha": cmd_sha,
            "total_tasks": task_count,
        },
    }
    if task_kind is not None:
        doc["task_kind"] = task_kind
    if extra:
        doc.update(extra)
    (campaign_dir / "interview.json").write_text(json.dumps(doc, indent=2))


# ─── discovery + sort ─────────────────────────────────────────────────────


def test_empty_root_returns_empty(tmp_path: Path) -> None:
    data = recall_campaigns(tmp_path)
    assert data["campaigns"] == []
    assert data["total_matching"] == 0
    assert data["showing"] == 0


def test_walks_nested_directories(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a" / "exp1")
    _write_interview(tmp_path / "b" / "deeper" / "exp2")
    _write_interview(tmp_path / "exp3")
    data = recall_campaigns(tmp_path)
    assert data["total_matching"] == 3


def test_recency_sort_descending(tmp_path: Path) -> None:
    _write_interview(tmp_path / "old", materialized_at="2026-01-01T00:00:00+00:00")
    _write_interview(tmp_path / "new", materialized_at="2026-05-01T00:00:00+00:00")
    _write_interview(tmp_path / "mid", materialized_at="2026-03-01T00:00:00+00:00")
    data = recall_campaigns(tmp_path)
    ats = [c["materialized_at"] for c in data["campaigns"]]
    assert ats == sorted(ats, reverse=True)


# ─── filter semantics ─────────────────────────────────────────────────────


def test_filter_task_kind_exact_match(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a", task_kind="ml-hparam-sweep")
    _write_interview(tmp_path / "b", task_kind="rl-rollout")
    _write_interview(tmp_path / "c", task_kind="ml-hparam-sweep")
    data = recall_campaigns(tmp_path, task_kind="ml-hparam-sweep")
    assert data["total_matching"] == 2
    assert all(c["task_kind"] == "ml-hparam-sweep" for c in data["campaigns"])


def test_filter_operator_exact_match(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a", operator="james")
    _write_interview(tmp_path / "b", operator="alex")
    data = recall_campaigns(tmp_path, operator="james")
    assert data["total_matching"] == 1
    assert data["campaigns"][0]["operator"] == "james"


def test_filter_since_uses_iso_string_compare(tmp_path: Path) -> None:
    """ISO-8601 lexicographic ordering matches chronological — no datetime parse needed."""
    _write_interview(tmp_path / "a", materialized_at="2026-01-15T10:00:00+00:00")
    _write_interview(tmp_path / "b", materialized_at="2026-04-15T10:00:00+00:00")
    _write_interview(tmp_path / "c", materialized_at="2026-06-15T10:00:00+00:00")
    data = recall_campaigns(tmp_path, since="2026-04-01T00:00:00+00:00")
    assert data["total_matching"] == 2


def test_combined_filters_are_anded(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a", task_kind="ml", operator="james")
    _write_interview(tmp_path / "b", task_kind="rl", operator="james")
    _write_interview(tmp_path / "c", task_kind="ml", operator="alex")
    data = recall_campaigns(tmp_path, task_kind="ml", operator="james")
    assert data["total_matching"] == 1


# ─── limit + reporting ────────────────────────────────────────────────────


def test_limit_truncates_and_reports_total(tmp_path: Path) -> None:
    for i in range(5):
        _write_interview(tmp_path / f"c{i}", materialized_at=f"2026-05-0{i + 1}T00:00:00+00:00")
    data = recall_campaigns(tmp_path, limit=3)
    assert data["total_matching"] == 5
    assert data["showing"] == 3
    assert len(data["campaigns"]) == 3


# ─── resilience ───────────────────────────────────────────────────────────


def test_malformed_interview_json_is_skipped(tmp_path: Path) -> None:
    """A corrupt or non-interview file shouldn't break the recall."""
    _write_interview(tmp_path / "good")
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    (bad_dir / "interview.json").write_text("{not json")
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    # Missing _materialized — pre-spike interview format or unrelated file
    (legacy_dir / "interview.json").write_text(json.dumps({"goal": "old"}))

    data = recall_campaigns(tmp_path)
    assert data["total_matching"] == 1
    assert data["campaigns"][0]["campaign_dir"].endswith("/good")


def test_invalid_root_raises(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="not a directory"):
        recall_campaigns(tmp_path / "does-not-exist")


# ─── CLI surface ──────────────────────────────────────────────────────────


def _run_cli(*args: str) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "claude_hpc", *args],
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_cli_help_lists_recall() -> None:
    rc, out, _ = _run_cli("--help")
    assert rc == 0
    assert "recall" in out


def test_cli_emits_envelope(tmp_path: Path) -> None:
    _write_interview(tmp_path / "a", task_kind="ml-hparam-sweep")
    _write_interview(tmp_path / "b", task_kind="rl-rollout")
    rc, out, err = _run_cli("recall", "--root", str(tmp_path), "--task-kind", "ml-hparam-sweep")
    assert rc == 0, err
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["data"]["total_matching"] == 1
    assert payload["data"]["campaigns"][0]["task_kind"] == "ml-hparam-sweep"


def test_cli_invalid_root_maps_to_user_error(tmp_path: Path) -> None:
    rc, out, _ = _run_cli("recall", "--root", str(tmp_path / "nonexistent"))
    assert rc == 1
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error_code"] == "spec_invalid"
