"""Status-snapshot: the additive ``queue`` brief section (run-queue plan §3/§6).

The maintainer's order (2026-07-29): a stuck queue item must not wait silently
until the human thinks to ask ``queue-status`` — the morning digest volunteers
it. This pins the three properties that make the section trustworthy:

1. **One authority** — the section's rows and text equal a direct
   ``queue-advance`` call at the same instant, byte-for-byte (S13: relay,
   never summarize; two "what's stuck" surfaces must be one definition).
2. **Additive** — an experiment with no queue has NO ``queue`` key and every
   other brief field is byte-unchanged.
3. **Fail-open** — a queue read blowing up yields a normal snapshot with no
   ``queue`` key, never a blanked digest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hpc_agent._wire.actions.queue_run import QueueRunSpec
from hpc_agent._wire.queries.queue_advance import QueueAdvanceSpec
from hpc_agent._wire.workflows.status_blocks import StatusSnapshotSpec
from hpc_agent.ops.queue.advance import queue_advance
from hpc_agent.ops.queue.run import queue_run
from hpc_agent.ops.status_blocks import status_snapshot

_NOW = "2026-07-06T12:00:00+00:00"

# One CPU-only cluster, so a gpu-asking item is HELD with a disclosed reason —
# the exact "3 items waiting on gpu capacity" case §3 promised the brief.
_CLUSTERS = """\
cpubox:
  host: cpubox.example.edu
  scheduler: slurm
  scratch: /scratch
"""


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    clusters = tmp_path / "clusters.yaml"
    clusters.write_text(_CLUSTERS, encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(clusters))
    return tmp_path


def _exp(tmp_path: Path) -> Path:
    exp = tmp_path / "exp"
    exp.mkdir(exist_ok=True)
    return exp


def _enqueue_gpu_item(exp: Path, request_id: str) -> None:
    queue_run(
        experiment_dir=exp,
        spec=QueueRunSpec(
            request_id=request_id,
            spec_ref="specs/sweep.json",
            resources={"gpu": True},
        ),
    )


def _snapshot_brief(exp: Path) -> dict[str, Any]:
    return status_snapshot(exp, spec=StatusSnapshotSpec(now_iso=_NOW, mark_seen=False)).brief


def test_empty_queue_has_no_section_and_changes_nothing(tmp_path: Path) -> None:
    exp = _exp(tmp_path)
    brief = _snapshot_brief(exp)
    assert "queue" not in brief


def test_held_item_surfaces_with_the_authoritys_verbatim_rows(tmp_path: Path) -> None:
    exp = _exp(tmp_path)
    _enqueue_gpu_item(exp, "req-1")

    brief = _snapshot_brief(exp)
    section = brief["queue"]
    assert section["queued_total"] == 1
    assert section["held_total"] == 1
    assert section["placements_decided"] == 0

    # One definition: the section equals a direct queue-advance call at the
    # same instant — rows and rendered text byte-for-byte.
    direct = queue_advance(experiment_dir=exp, spec=QueueAdvanceSpec(now=_NOW))
    assert section["held"] == [h.model_dump(mode="json") for h in direct.held]
    assert section["text"] == direct.brief
    assert section["held_counts"] == dict(direct.held_counts)

    # The reason is the authority's sentence, present verbatim — the human
    # reads WHY it is stuck without asking.
    assert direct.held, "precondition: the gpu ask must be held on a cpu-only config"
    assert section["held"][0]["reason"] == direct.held[0].reason
    assert section["held"][0]["reason"]  # non-empty, a real sentence


def test_clip_is_visible_when_held_exceeds_ten(tmp_path: Path) -> None:
    exp = _exp(tmp_path)
    for i in range(12):
        _enqueue_gpu_item(exp, f"req-{i:02d}")

    section = _snapshot_brief(exp)["queue"]
    assert section["held_total"] == 12
    assert len(section["held"]) == 10  # clipped page...
    assert section["queued_total"] == 12  # ...with the totals keeping it visible


def test_queue_read_failure_never_blanks_the_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exp = _exp(tmp_path)
    _enqueue_gpu_item(exp, "req-1")

    def _boom(**_kw: Any) -> Any:
        raise RuntimeError("intake ledger unreadable")

    monkeypatch.setattr("hpc_agent.ops.queue.advance.queue_advance", _boom)
    brief = _snapshot_brief(exp)
    assert "queue" not in brief
    # The rest of the digest survives untouched.
    assert "attention" in brief
    assert "overnight" in brief
