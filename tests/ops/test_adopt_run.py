"""Tests for ``adopt-run`` — the foreign-run ingest primitive.

``adopt-run`` composes existing machinery: the sidecar goes through the
``write-run-sidecar`` primitive, the journal record through the existing
record path with NO scheduler call, and a terminal adoption settles through
the settle-run mechanism on directed evidence. These assert:

* fresh IN-FLIGHT adopt — sidecar AND journal record both exist after, with
  the given job_ids, status ``in_flight``, next_block = status-watch;
* fresh TERMINAL adopt — settled ``complete`` through the settle mechanism
  (decision journal sign-off with directed provenance, harvest fired),
  next_block = aggregate-check;
* the refuse-not-clobber guard — a pre-existing sidecar OR journal record is
  refused, pointing at the existing record;
* the terminal-evidence doctrine — job_ids absent + empty evidence refused;
* layout inference — a local sample tree infers result_dir_template +
  task_count (max+1, gap-safe); an unresolvable anchor yields the
  needs_elicitation envelope and writes NOTHING;
* cmd_sha derivation — sha256 of the stripped command, stable across
  whitespace, never caller-suppliable;
* job_ids wire shape — fabricated prose ids are refused at the wire.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from pydantic import ValidationError

from hpc_agent import errors
from hpc_agent._wire.actions.adopt_run import AdoptRunInput
from hpc_agent.ops.adopt_run import adopt_run

if TYPE_CHECKING:
    from pathlib import Path

_RUN_ID = "foreign-exp-1"
_COMMAND = "python train.py --seed $SEED --output-file $RESULT_DIR/metrics.json"
# adopt_run imports harvest_on_terminal lazily via ``from … import``, so the
# patch seam is the source module (same seam as the settle-run tests).
_HARVEST_SEAM = "hpc_agent.ops.monitor.harvest_guard.harvest_on_terminal"


def _spec(**overrides: Any) -> AdoptRunInput:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "command": _COMMAND,
        "cluster": "hoffman2",
        "ssh_target": "me@hoffman2.idre.ucla.edu",
        "remote_path": "/scratch/me/exp",
        "result_dir_template": "results/{run_id}/task_{task_id}",
        "task_count": 4,
    }
    base.update(overrides)
    return AdoptRunInput(**base)


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


# ── fresh in-flight adopt ─────────────────────────────────────────────────────


def test_in_flight_adopt_writes_sidecar_and_journal(tmp_path: Path) -> None:
    """job_ids present ⇒ sidecar + journal record both exist after, the record
    is in_flight with the given job_ids, and next_block hands off to
    status-watch. No scheduler call is possible (nothing is mocked)."""
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import read_run_sidecar

    res = adopt_run(tmp_path, spec=_spec(job_ids=["4242"]))

    assert res.stage_reached == "adopted_in_flight"
    assert res.needs_decision is False
    assert res.status == "in_flight"
    assert res.job_ids == ["4242"]

    sidecar = read_run_sidecar(tmp_path, _RUN_ID)
    assert sidecar["cmd_sha"] == res.cmd_sha
    assert sidecar["executor"] == _COMMAND
    assert sidecar["task_count"] == 4
    assert sidecar["job_ids"] == ["4242"]
    assert sidecar["cluster"] == "hoffman2"
    assert sidecar["extra"]["adopted"]["by"] == "adopt-run"

    rec = load_run(tmp_path, _RUN_ID)
    assert rec is not None
    assert rec.status == "in_flight"
    assert rec.job_ids == ["4242"]
    assert rec.total_tasks == 4
    assert rec.cluster == "hoffman2"

    assert res.next_block is not None
    assert res.next_block["verb"] == "status-watch"


# ── fresh terminal adopt ──────────────────────────────────────────────────────


def test_terminal_adopt_settles_with_decision_and_harvest(tmp_path: Path) -> None:
    """job_ids absent + terminal_evidence ⇒ the run is settled ``complete``
    through the settle mechanism: decision sign-off (block adopt-run, directed
    provenance), mark_run to complete, harvest fired with the settled cause."""
    from hpc_agent.state.decision_journal import read_decisions
    from hpc_agent.state.journal import load_run

    harvest_marker = {"harvested_at": "2026-08-14T01:00:00+00:00", "harvest_ok": True}
    with mock.patch(_HARVEST_SEAM, return_value=harvest_marker) as harvest:
        res = adopt_run(
            tmp_path,
            spec=_spec(terminal_evidence="reporter RC=0 all-4; result tree on disk"),
        )

    assert res.stage_reached == "adopted_terminal"
    assert res.status == "complete"
    assert res.job_ids == []

    rec = load_run(tmp_path, _RUN_ID)
    assert rec is not None
    assert rec.status == "complete"
    assert rec.job_ids == []
    assert rec.last_status["verdict_source"] == "human_directed"
    assert rec.last_status["verdict_reason"] == "adopt_run_directed_settle"

    harvest.assert_called_once()
    assert harvest.call_args.kwargs["terminal_cause"] == "complete"

    decisions = read_decisions(tmp_path, "run", _RUN_ID)
    assert len(decisions) == 1
    assert decisions[0]["block"] == "adopt-run"
    assert decisions[0]["response"] == "y"
    assert decisions[0]["proposal"].startswith("reporter RC=0")
    prov = decisions[0]["provenance"]
    assert prov["directed"] is True
    assert prov["kind"] == "adopt-run-directed-settle"

    assert res.next_block is not None
    assert res.next_block["verb"] == "aggregate-check"


def test_terminal_adopt_harvest_runs_for_real_via_seams(tmp_path: Path) -> None:
    """End-to-end (no cluster): the SAME harvest machinery runs via the
    injected aggregate seam — not just a mock assertion."""
    from hpc_agent.ops.monitor.harvest_guard import harvest_receipt_exists

    def _fake_aggregate(_exp: Path, _rid: str) -> Any:
        raise RuntimeError("no cluster in this test")  # harvest never raises

    def _fake_sweep(_remote: str, _rid: str) -> dict[int, list[str]]:
        return {}

    res = adopt_run(
        tmp_path,
        spec=_spec(terminal_evidence="proven on disk"),
        _aggregate=_fake_aggregate,
        _sweep=_fake_sweep,
    )
    assert res.stage_reached == "adopted_terminal"
    assert harvest_receipt_exists(tmp_path, _RUN_ID)


# ── refuse-not-clobber ────────────────────────────────────────────────────────


def test_existing_sidecar_is_refused(tmp_path: Path) -> None:
    """A pre-existing sidecar for the run_id refuses the adoption, pointing at
    the existing record — never clobbered."""
    from hpc_agent.state.runs import read_run_sidecar

    first = adopt_run(tmp_path, spec=_spec(job_ids=["4242"]))
    assert first.stage_reached == "adopted_in_flight"

    with pytest.raises(errors.SpecInvalid) as exc:
        adopt_run(tmp_path, spec=_spec(job_ids=["9999"]))
    assert "already has a sidecar" in str(exc.value)
    # The original record is untouched.
    assert read_run_sidecar(tmp_path, _RUN_ID)["job_ids"] == ["4242"]


def test_existing_journal_record_is_refused(tmp_path: Path) -> None:
    """A journal record alone (no sidecar — e.g. the sidecar was pruned) still
    refuses: BOTH stores are checked."""
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        tmp_path,
        RunRecord(
            run_id=_RUN_ID,
            profile="exp",
            cluster="hoffman2",
            ssh_target="me@hoffman2.idre.ucla.edu",
            remote_path="/scratch/me/exp",
            job_name=_RUN_ID,
            job_ids=["1"],
            total_tasks=4,
            submitted_at="2026-08-14T00:00:00+00:00",
            experiment_dir=str(tmp_path),
        ),
    )
    with pytest.raises(errors.SpecInvalid) as exc:
        adopt_run(tmp_path, spec=_spec(job_ids=["4242"]))
    assert "already has a journal record" in str(exc.value)


# ── terminal-evidence doctrine ────────────────────────────────────────────────


@pytest.mark.parametrize("evidence", [None, "", "   "])
def test_terminal_adopt_without_evidence_is_refused(tmp_path: Path, evidence: str | None) -> None:
    """job_ids absent + empty/missing terminal_evidence is refused (a settle
    with no evidence is a bare status flip) and NOTHING is written."""
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import run_sidecar_path

    with pytest.raises(errors.SpecInvalid) as exc:
        adopt_run(tmp_path, spec=_spec(terminal_evidence=evidence))
    assert "terminal_evidence is required" in str(exc.value)
    assert not run_sidecar_path(tmp_path, _RUN_ID).is_file()
    assert load_run(tmp_path, _RUN_ID) is None


# ── layout inference ──────────────────────────────────────────────────────────


def _lay_down_results(
    tmp_path: Path, *, indices: list[int], artifact: str = "metrics.json"
) -> Path:
    root = tmp_path / "results" / "foreign-exp"
    for i in indices:
        d = root / f"task_{i}"
        d.mkdir(parents=True)
        (d / artifact).write_text("{}", encoding="utf-8")
    return root


def test_layout_inference_from_local_sample(tmp_path: Path) -> None:
    """A local sample tree infers result_dir_template (per-task placeholder)
    and task_count = max(index)+1 — gap-safe, never len()."""
    root = _lay_down_results(tmp_path, indices=[0, 1, 3])  # gap at 2

    res = adopt_run(
        tmp_path,
        spec=_spec(
            result_dir_template=None,
            task_count=None,
            results_sample=str(root),
            job_ids=["4242"],
        ),
    )
    assert res.stage_reached == "adopted_in_flight"
    assert res.task_count == 4  # max(0,1,3)+1, NOT the count 3
    assert res.result_dir_template is not None
    assert "{task_id}" in res.result_dir_template


def test_layout_inference_failure_elicits_and_writes_nothing(tmp_path: Path) -> None:
    """A non-resolving anchor (e.g. a remote path) yields the needs_elicitation
    envelope naming the missing fields — no guess, no ssh probe, NOTHING
    written."""
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import run_sidecar_path

    res = adopt_run(
        tmp_path,
        spec=_spec(
            result_dir_template=None,
            task_count=None,
            results_sample="/scratch/me/exp/results",  # not local
            job_ids=["4242"],
        ),
    )
    assert res.stage_reached == "needs_elicitation"
    assert res.needs_decision is True
    assert "result_dir_template" in res.reason
    assert "task_count" in res.reason
    assert res.next_block is None
    assert not run_sidecar_path(tmp_path, _RUN_ID).is_file()
    assert load_run(tmp_path, _RUN_ID) is None


def test_missing_layout_without_sample_elicits(tmp_path: Path) -> None:
    """No layout fields and no results_sample at all ⇒ elicit, never guess."""
    res = adopt_run(
        tmp_path,
        spec=_spec(result_dir_template=None, task_count=None, job_ids=["4242"]),
    )
    assert res.stage_reached == "needs_elicitation"
    assert res.needs_decision is True


def test_inference_refuses_tree_without_summary_artifact(tmp_path: Path) -> None:
    """Task dirs missing the summary artifact prove the anchor is not the
    result tree — elicit."""
    root = _lay_down_results(tmp_path, indices=[0, 1], artifact="other.json")
    res = adopt_run(
        tmp_path,
        spec=_spec(
            result_dir_template=None,
            task_count=None,
            results_sample=str(root),
            job_ids=["4242"],
        ),
    )
    assert res.stage_reached == "needs_elicitation"
    assert "summary artifact" in res.reason


# ── cmd_sha derivation ────────────────────────────────────────────────────────


def test_cmd_sha_is_derived_and_whitespace_stable(tmp_path: Path) -> None:
    """cmd_sha is sha256 of the STRIPPED command (full 64 hex): the same
    command with surrounding whitespace derives the same identity."""
    expected = hashlib.sha256(_COMMAND.encode("utf-8")).hexdigest()

    res = adopt_run(tmp_path, spec=_spec(command=f"  {_COMMAND}\n", job_ids=["4242"]))
    assert res.cmd_sha == expected
    assert len(res.cmd_sha) == 64


def test_cmd_sha_cannot_be_caller_supplied() -> None:
    """The wire model forbids a smuggled cmd_sha field (extra='forbid')."""
    with pytest.raises(ValidationError):
        AdoptRunInput(
            run_id=_RUN_ID,
            command=_COMMAND,
            cluster="hoffman2",
            ssh_target="me@hoffman2.idre.ucla.edu",
            remote_path="/scratch/me/exp",
            cmd_sha="deadbeef" * 8,  # type: ignore[call-arg]
        )


# ── job_ids wire shape ────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", [["purged-completed"], ["job-42"], [""]])
def test_fabricated_job_ids_are_refused_at_the_wire(bad: list[str]) -> None:
    """SchedulerJobId (digit-leading) refuses prose placeholders — the same
    shape validator every journal-boundary spec uses."""
    with pytest.raises(ValidationError):
        _spec(job_ids=bad)


def test_empty_job_ids_list_is_refused_at_the_wire() -> None:
    """job_ids=[] is not a valid in-flight adoption — omit the field for the
    terminal branch instead (min_length=1)."""
    with pytest.raises(ValidationError):
        _spec(job_ids=[])
