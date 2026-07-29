"""``queue-run`` — arrival is cheap, arrival is exactly once, arrival is honest.

What these tests pin beyond the happy path are the four rules the verb OWNS
(``docs/plans/run-queue-placement-2026-07-28.md``), each with the negative case
that shows its guard firing:

* **R2 (ungated)** — enqueueing spends nothing: no journal write, no cluster
  config read at all for an unpinned item, no artifact outside the ledger.
* **R8 / §10.S2 (replay dedup)** — a replayed ``request_id`` appends NO second
  line and returns the ORIGINAL item; the replay cannot rewrite it either.
* **R5 (a pin is supreme)** — a pin naming an unknown cluster is refused at the
  boundary, because nothing downstream would ever re-choose for that item; and
  a verified pin is stored as ``cluster_pin``, never as ``cluster``, so an
  operator's REQUEST stays distinguishable from placement's DECISION.
* **R1 (index, not journal)** — ``queued_count`` counts the ``queued``
  vocabulary only, and the ledger's tolerant read means a torn line degrades
  the count rather than wedging the queue.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from hpc_agent import errors
from hpc_agent._wire.actions.queue_run import QueueRunSpec
from hpc_agent.ops.queue import run as enqueue_mod
from hpc_agent.ops.queue.run import queue_run
from hpc_agent.state.queue_intake import (
    append_intake_placement,
    intake_path,
    intake_path_if_exists,
    read_intake_records,
)

if TYPE_CHECKING:
    from pathlib import Path

_CLUSTERS_YAML = """\
hoffman2:
  host: hoffman2.example.edu
  user: someone
  scheduler: sge
  scratch: /u/scratch/s/someone
carc:
  host: discovery.example.edu
  user: someone
  scheduler: slurm
  scratch: /scratch1/someone
"""


@pytest.fixture
def clusters_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the real loader at a two-cluster config (never the developer's own)."""
    path = tmp_path / "clusters.yaml"
    path.write_text(_CLUSTERS_YAML, encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(path))
    return path


def _spec(**over: Any) -> QueueRunSpec:
    payload: dict[str, Any] = {
        "request_id": "req-0001",
        "spec": {"run_name": "rv_sweep", "total": 4},
    }
    payload.update(over)
    return QueueRunSpec(**payload)


def _lines(experiment_dir: Path) -> list[str]:
    path = intake_path_if_exists(experiment_dir)
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# --------------------------------------------------------------------------
# arrival facts
# --------------------------------------------------------------------------


def test_enqueue_writes_one_record_and_echoes_the_ledger(tmp_path: Path) -> None:
    result = queue_run(
        experiment_dir=tmp_path,
        spec=_spec(run_name="rv_sweep", campaign_base="rv_sweep", resources={"gpu": True}),
    )

    assert result.path == str(intake_path(tmp_path))
    assert result.item_id == "req-0001"
    assert result.request_id == "req-0001"
    assert result.state == "queued"  # an item cannot ARRIVE placed
    assert result.replayed is False
    assert result.queued_count == 1
    assert result.enqueued_at  # stamped by the store at append time
    # run_id is COMPUTED from a resolved spec (§10.S2); Phase 1 does not resolve.
    assert result.run_id is None

    assert len(_lines(tmp_path)) == 1
    on_disk = read_intake_records(tmp_path)[0]
    assert on_disk["kind"] == "enqueue"
    assert on_disk["item_id"] == on_disk["request_id"] == "req-0001"
    assert on_disk["state"] == "queued"
    assert on_disk["spec"] == {"run_name": "rv_sweep", "total": 4}
    assert on_disk["run_name"] == "rv_sweep"
    assert on_disk["campaign_base"] == "rv_sweep"
    assert on_disk["resources"]["gpu"] is True
    # The echo IS what the ledger holds (plus the fold's own bookkeeping).
    assert result.record["item_id"] == "req-0001"
    assert result.record["record_count"] == 1


def test_spec_ref_item_is_recorded_verbatim_and_not_dereferenced(tmp_path: Path) -> None:
    """A pointer that goes stale is a fact for the projection, not a refusal."""
    result = queue_run(
        experiment_dir=tmp_path,
        spec=QueueRunSpec(request_id="req-ref", spec_ref="specs/does-not-exist.json"),
    )
    assert result.record["spec_ref"] == "specs/does-not-exist.json"
    assert result.record["spec"] is None
    assert result.state == "queued"


@pytest.mark.parametrize(
    "over",
    [
        {"spec": None, "spec_ref": None},
        {"spec": {"a": 1}, "spec_ref": "specs/x.json"},
        {"spec": {}},
    ],
    ids=["neither", "both", "empty-spec"],
)
def test_spec_layer_refuses_ambiguous_or_empty_work(over: dict[str, Any]) -> None:
    """A ledger row must name work exactly once — refused before any append."""
    with pytest.raises(ValidationError):
        _spec(**over)


# --------------------------------------------------------------------------
# R2 — ungated: enqueueing spends nothing
# --------------------------------------------------------------------------


def test_enqueue_is_ungated_and_touches_nothing_but_the_ledger(
    tmp_path: Path, journal_home: Path
) -> None:
    """R2 (§1): no journal write, no decision journal, no consent artifact."""
    experiment_dir = tmp_path / "exp"
    experiment_dir.mkdir()

    queue_run(experiment_dir=experiment_dir, spec=_spec())

    # The run journal tree (~/.claude/hpc/<repo_hash>/) was never opened.
    assert not journal_home.exists() or not any(journal_home.rglob("*.json"))
    # Inside the repo sidecar, ONLY the queue exists — no runs/, no decisions,
    # no scopes: nothing that could constitute spending a gate.
    hpc = experiment_dir / ".hpc"
    assert sorted(p.name for p in hpc.iterdir()) == [".gitignore", "queue"]
    assert (hpc / "queue" / "intake.jsonl").is_file()


def test_unpinned_enqueue_never_reads_the_cluster_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An item with no pin has no cluster question to ask — so it asks none."""
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(tmp_path / "no-such-file.yaml"))
    result = queue_run(experiment_dir=tmp_path, spec=_spec())
    assert result.state == "queued"
    assert result.record["cluster_pin"] is None


# --------------------------------------------------------------------------
# R8 / §10.S2 — a replayed request_id appends nothing
# --------------------------------------------------------------------------


def test_replayed_request_id_does_not_append_twice(tmp_path: Path) -> None:
    first = queue_run(experiment_dir=tmp_path, spec=_spec())
    second = queue_run(experiment_dir=tmp_path, spec=_spec())

    assert first.replayed is False
    assert second.replayed is True
    assert len(_lines(tmp_path)) == 1
    assert second.enqueued_at == first.enqueued_at
    assert second.item_id == first.item_id
    assert second.queued_count == 1


def test_replay_returns_the_original_and_cannot_rewrite_it(tmp_path: Path) -> None:
    """The dedup probe matches on request_id ALONE: a changed payload is ignored."""
    queue_run(experiment_dir=tmp_path, spec=_spec(spec={"run_name": "first", "total": 1}))
    replay = queue_run(
        experiment_dir=tmp_path,
        spec=_spec(spec={"run_name": "SECOND", "total": 999}, campaign_base="sneaky"),
    )

    assert replay.replayed is True
    assert replay.record["spec"] == {"run_name": "first", "total": 1}
    assert replay.record["campaign_base"] is None
    assert len(_lines(tmp_path)) == 1


def test_distinct_request_ids_are_distinct_items(tmp_path: Path) -> None:
    queue_run(experiment_dir=tmp_path, spec=_spec(request_id="req-a"))
    result = queue_run(experiment_dir=tmp_path, spec=_spec(request_id="req-b"))
    assert result.replayed is False
    assert result.queued_count == 2
    assert len(_lines(tmp_path)) == 2


# --------------------------------------------------------------------------
# R5 — the explicit pin, and the guard that refuses an unplaceable one
# --------------------------------------------------------------------------


def test_verified_pin_is_stored_as_cluster_pin_not_cluster(
    tmp_path: Path, clusters_config: Path
) -> None:
    """The REQUEST and the DECISION must stay distinguishable after the fold."""
    result = queue_run(experiment_dir=tmp_path, spec=_spec(cluster="hoffman2"))
    assert result.record["cluster_pin"] == "hoffman2"
    assert "cluster" not in result.record
    assert read_intake_records(tmp_path)[0]["cluster_pin"] == "hoffman2"


def test_unknown_cluster_pin_is_refused_and_nothing_is_enqueued(
    tmp_path: Path, clusters_config: Path
) -> None:
    """R5's consequence: a pin nothing re-chooses must be right at arrival."""
    with pytest.raises(errors.ClusterUnknown) as excinfo:
        queue_run(experiment_dir=tmp_path, spec=_spec(cluster="hoffman3"))

    msg = str(excinfo.value)
    assert "hoffman3" in msg
    assert "hoffman2" in msg  # the near-miss suggestion
    assert _lines(tmp_path) == []  # the refusal is before the append


def test_unknown_pin_with_no_near_miss_still_refuses(tmp_path: Path, clusters_config: Path) -> None:
    with pytest.raises(errors.ClusterUnknown, match="clusters list"):
        queue_run(experiment_dir=tmp_path, spec=_spec(cluster="zzz-nowhere"))
    assert _lines(tmp_path) == []


def test_blank_cluster_pin_is_refused_not_demoted_to_unpinned(
    tmp_path: Path, clusters_config: Path
) -> None:
    """'' and absent mean opposite things; guessing which is what R4/R5 forbid."""
    with pytest.raises(errors.SpecInvalid, match="blank"):
        queue_run(experiment_dir=tmp_path, spec=_spec(cluster="   "))
    assert _lines(tmp_path) == []


def test_unverifiable_cluster_config_refuses_a_pinned_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unverified pin is not a verified pin — refuse rather than trust it."""
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(errors.ConfigInvalid, match="clusters.yaml"):
        queue_run(experiment_dir=tmp_path, spec=_spec(cluster="hoffman2"))
    assert _lines(tmp_path) == []


def test_non_mapping_cluster_config_refuses_a_pinned_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bad = tmp_path / "clusters.yaml"
    bad.write_text("- hoffman2\n- carc\n", encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(bad))
    with pytest.raises(errors.ConfigInvalid, match="mapping"):
        queue_run(experiment_dir=tmp_path, spec=_spec(cluster="hoffman2"))
    assert _lines(tmp_path) == []


# --------------------------------------------------------------------------
# R1 — the counted vocabulary, and the tolerant read
# --------------------------------------------------------------------------


def test_queued_count_excludes_placed_items(tmp_path: Path) -> None:
    """R1's vocabulary is {queued, placed}; 'queued' is the depth advance sees."""
    queue_run(experiment_dir=tmp_path, spec=_spec(request_id="req-placed"))
    append_intake_placement(
        tmp_path,
        item_id="req-placed",
        request_id="req-placed-move",
        cluster="hoffman2",
        campaign_id="rv_sweep_hoffman2",
        reason="only cluster satisfying gpu ask",
    )
    result = queue_run(experiment_dir=tmp_path, spec=_spec(request_id="req-new"))

    assert result.queued_count == 1  # the placed item no longer counts as queued
    assert result.state == "queued"


def test_torn_and_foreign_lines_degrade_the_read_without_wedging(tmp_path: Path) -> None:
    """A queue read is on the 'what needs me' path; one bad byte must not wedge it."""
    queue_run(experiment_dir=tmp_path, spec=_spec(request_id="req-good"))
    with intake_path(tmp_path).open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "enqueue", "item_id": "torn", "state": "queued"\n')
        fh.write('"a bare json string, not a record"\n')
        fh.write(json.dumps({"kind": "enqueue", "item_id": "no-state"}) + "\n")

    result = queue_run(experiment_dir=tmp_path, spec=_spec(request_id="req-after"))
    assert result.replayed is False
    assert result.queued_count == 2  # the two well-formed items, and only those


def test_ledger_that_does_not_read_back_is_a_loud_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Echoing a record no reader can see would report an enqueue that never was."""
    monkeypatch.setattr(enqueue_mod, "read_intake_items", lambda _dir: [])
    with pytest.raises(errors.JournalCorrupt, match="does not read it back"):
        queue_run(experiment_dir=tmp_path, spec=_spec())


# --------------------------------------------------------------------------
# the contract the registry publishes
# --------------------------------------------------------------------------


def test_decorator_declares_the_write_and_the_dedup_rule() -> None:
    meta = queue_run._primitive_meta
    assert meta.name == "queue-run"
    assert meta.verb == "mutate"
    assert meta.agent_facing is True
    assert [(se.kind, se.target) for se in meta.side_effects] == [
        ("file_write", "<experiment>/.hpc/queue/intake.jsonl")
    ]
    # R8: idempotent BECAUSE the client's request_id is the dedup key.
    assert meta.idempotent is True
    assert meta.idempotency_key == "request_id"
    assert "request_id" in QueueRunSpec.model_fields
