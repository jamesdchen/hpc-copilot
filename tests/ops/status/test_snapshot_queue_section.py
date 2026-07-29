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
4. **Enqueue age** (§10.S3's accepted cost) — the window widens for items held
   pre-gate, so the section shows each queued item's ``enqueued_at`` and a
   CODE-computed ``age_sec``, oldest first, from the SAME derivation
   ``queue-status`` uses (one definition, and the LLM never does date math). A
   pre-stamp record surfaces age UNKNOWN (``null``), never a guess.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

import hpc_agent.ops.queue.status as queue_status_mod
import hpc_agent.ops.status_blocks as status_blocks_mod
from hpc_agent._wire.actions.queue_run import QueueRunSpec
from hpc_agent._wire.queries.queue_advance import QueueAdvanceSpec
from hpc_agent._wire.queries.queue_status import QueueStatusSpec
from hpc_agent._wire.workflows.status_blocks import StatusSnapshotSpec
from hpc_agent.ops.queue.advance import queue_advance
from hpc_agent.ops.queue.run import queue_run
from hpc_agent.ops.queue.status import queue_status
from hpc_agent.ops.status_blocks import status_snapshot
from hpc_agent.state.queue_intake import intake_path

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
    # queued_ages obeys the same bounded-projection posture: 10 rows, and
    # queued_total is what keeps the clip visible.
    assert len(section["queued_ages"]) == 10


# ── enqueue age (§10.S3: the pre-gate window widens; the brief shows how long) ─


def _enqueue_at(exp: Path, request_id: str, stamp: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Enqueue with the ledger's arrival clock pinned to *stamp*."""
    monkeypatch.setattr("hpc_agent.state.queue_intake.utcnow_iso", lambda: stamp)
    _enqueue_gpu_item(exp, request_id)


def test_queued_ages_are_code_computed_and_longest_wait_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The human sees which items have waited longest for a y — exact rows.

    Pins all three legs at once: the ledger's ``enqueued_at`` is relayed as
    stored, ``age_sec`` is CODE-computed against the snapshot's single instant
    (the renderer/LLM never does date math — the determinism boundary), and the
    ordering is oldest first.
    """
    exp = _exp(tmp_path)
    # Enqueued out of arrival-age order on purpose: the sort must be by age.
    _enqueue_at(exp, "req-newer", "2026-07-06T11:00:00+00:00", monkeypatch)
    _enqueue_at(exp, "req-older", "2026-07-06T10:00:00+00:00", monkeypatch)

    section = _snapshot_brief(exp)["queue"]
    assert section["queued_ages"] == [
        {"item_id": "req-older", "enqueued_at": "2026-07-06T10:00:00+00:00", "age_sec": 7200},
        {"item_id": "req-newer", "enqueued_at": "2026-07-06T11:00:00+00:00", "age_sec": 3600},
    ]


def test_age_is_the_one_derivation_queue_status_uses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One derivation, not two: digest age == queue-status age at the same instant.

    Behavioral equality plus the route-through pin (the
    ``test_layers_share_one_drift_predicate`` precedent): both surfaces must
    call ``state/queue_intake.enqueue_age_sec`` rather than re-inlining the
    date math.
    """
    exp = _exp(tmp_path)
    _enqueue_at(exp, "req-1", "2026-07-06T09:30:00+00:00", monkeypatch)

    (digest_row,) = _snapshot_brief(exp)["queue"]["queued_ages"]
    (status_item,) = queue_status(experiment_dir=exp, spec=QueueStatusSpec(now=_NOW)).items
    assert digest_row["age_sec"] == status_item.age_sec == 9000
    assert digest_row["enqueued_at"] == status_item.enqueued_at

    assert "enqueue_age_sec" in inspect.getsource(queue_status_mod._build_item)
    assert "enqueue_age_sec" in inspect.getsource(status_blocks_mod._queued_age_rows)


def test_pre_stamp_record_surfaces_age_unknown_never_guessed(tmp_path: Path) -> None:
    """A ledger written before ``enqueued_at`` existed reads age UNKNOWN.

    Additive contract: the old record itself is untouched (byte-identical
    ledger) and its row discloses ``enqueued_at: null, age_sec: null`` rather
    than inventing an age — and it sorts AFTER every known age so a guessless
    row never displaces a real longest-waiter.
    """
    exp = _exp(tmp_path)
    legacy = {
        "kind": "enqueue",
        "item_id": "legacy-1",
        "request_id": "legacy-1",
        "state": "queued",
        "spec_ref": "specs/sweep.json",
        "resources": {"gpu": True},
    }
    ledger = intake_path(exp)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    before = ledger.read_bytes()
    _enqueue_gpu_item(exp, "req-stamped")

    section = _snapshot_brief(exp)["queue"]
    rows = section["queued_ages"]
    assert [row["item_id"] for row in rows] == ["req-stamped", "legacy-1"]
    assert rows[1] == {"item_id": "legacy-1", "enqueued_at": None, "age_sec": None}
    assert rows[0]["age_sec"] is not None and rows[0]["age_sec"] >= 0
    # The snapshot rewrote nothing: the legacy line is still byte-identical.
    assert ledger.read_bytes().startswith(before)


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
