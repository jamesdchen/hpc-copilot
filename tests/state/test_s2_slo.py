"""The S2 SLO reducer (s2-readiness pillar 6).

Every fixture below is written by the REAL writers — ``append_decision`` for the
decision journal, ``upsert_run`` for the run record — never hand-forged JSONL, so
a shape change in either writer fails these tests instead of silently drifting
past them.

No wall clock: every timestamp is injected, so the arithmetic is exact.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hpc_agent.state import readiness, s2_slo
from hpc_agent.state.decision_journal import append_decision, read_decisions
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

RUN_ID = "run-slo-1"
HOST = "slo.example.edu"
T0 = datetime(2026, 7, 30, 20, 0, 0, tzinfo=timezone.utc)


def _ts(offset_sec: float) -> str:
    return (T0 + timedelta(seconds=offset_sec)).isoformat(timespec="seconds")


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    (d / ".hpc").mkdir(parents=True)
    return d


def _decide(
    experiment: Path,
    *,
    block: str,
    next_block: str | None,
    response: str,
    offset_sec: float,
) -> None:
    """One real decision record on the run scope, at an injected instant."""
    append_decision(
        experiment,
        scope_kind="run",
        scope_id=RUN_ID,
        block=block,
        response=response,
        resolved={"next_block": next_block} if next_block else {},
        ts=_ts(offset_sec),
    )


def _tonights_shape(experiment: Path) -> None:
    """The 2026-07-30 shape: S1 nudge, S1 y → S2, S2 y → S3, then the array.

    This is the sequence the design's incident night actually produced — two
    human stops between the first commitment and the array landing — and it is
    what the SLO is supposed to make visible.
    """
    _decide(experiment, block="submit-s1", next_block=None, response="which cluster?", offset_sec=0)
    _decide(experiment, block="submit-s1", next_block="submit-s2", response="y", offset_sec=60)
    _decide(experiment, block="submit-s2", next_block="submit-s3", response="y", offset_sec=300)


class TestGreenlightBoundary:
    def test_the_earliest_s2_targeting_y_opens_the_window(self, experiment: Path) -> None:
        _tonights_shape(experiment)
        fire = s2_slo.greenlight_fire_record(read_decisions(experiment, "run", RUN_ID))
        assert fire is not None
        assert fire["ts"] == _ts(60)
        assert fire["resolved"]["next_block"] == "submit-s2"

    def test_a_nudge_never_opens_the_window(self, experiment: Path) -> None:
        _decide(experiment, block="submit-s1", next_block="submit-s2", response="no", offset_sec=0)
        assert s2_slo.greenlight_fire_record(read_decisions(experiment, "run", RUN_ID)) is None

    def test_a_y_targeting_an_earlier_block_never_opens_the_window(self, experiment: Path) -> None:
        _decide(experiment, block="submit-s1", next_block="submit-s1", response="y", offset_sec=0)
        assert s2_slo.greenlight_fire_record(read_decisions(experiment, "run", RUN_ID)) is None

    def test_fire_targets_track_the_block_chain(self) -> None:
        """Derived, not re-listed — a chain change cannot silently re-point the
        SLO's start boundary."""
        from hpc_agent.infra.block_chain import ORDER

        chain = ORDER["submit"]
        assert frozenset(chain[chain.index("submit-s2") :]) == s2_slo.FIRE_TARGETS

    def test_no_decisions_at_all_is_none(self, experiment: Path) -> None:
        assert s2_slo.greenlight_fire_record([]) is None

    @pytest.mark.parametrize(
        "resolved",
        [
            {"next_block": "submit-s2"},
            {"next_block": {"verb": "submit-s2"}},
            {"next_block": ""},
            {"next_block": {"verb": ""}},
            {"next_block": {}},
            {"next_block": 7},
            {},
            None,
            "not a dict",
        ],
    )
    def test_journaled_target_is_in_lockstep_with_the_block_gate(self, resolved: object) -> None:
        """The reducer replicates ``ops/block_gate._journaled_target`` (it cannot
        import a package-private symbol across packages). If the gate's reading
        of a greenlight ever diverged from this one, the SLO would silently
        measure from a boundary the driver never used."""
        from hpc_agent.ops.block_gate import _journaled_target as gate_target

        assert gate_target(resolved) == s2_slo._journaled_target(resolved)


class TestYToArrayAccepted:
    def test_the_interval_spans_from_the_first_y_to_the_accept_stamp(
        self, experiment: Path
    ) -> None:
        _tonights_shape(experiment)
        slo = s2_slo.compute_slo(read_decisions(experiment, "run", RUN_ID), accepted_at=_ts(412))
        # y at +60, accepted at +412 → the intermediate S2→S3 y is INSIDE the
        # window on purpose: it is latency the human paid.
        assert slo.y_to_array_accepted_seconds == 352

    def test_no_y_means_no_latency_but_the_other_fields_survive(self, experiment: Path) -> None:
        _decide(experiment, block="submit-s1", next_block=None, response="?", offset_sec=0)
        slo = s2_slo.compute_slo(read_decisions(experiment, "run", RUN_ID), accepted_at=_ts(100))
        assert slo.y_to_array_accepted_seconds is None
        assert slo.interventions_count == 1

    def test_no_accept_stamp_means_no_latency(self, experiment: Path) -> None:
        _tonights_shape(experiment)
        slo = s2_slo.compute_slo(read_decisions(experiment, "run", RUN_ID), accepted_at=None)
        assert slo.y_to_array_accepted_seconds is None

    def test_an_unparseable_accept_stamp_means_no_latency(self, experiment: Path) -> None:
        _tonights_shape(experiment)
        slo = s2_slo.compute_slo(read_decisions(experiment, "run", RUN_ID), accepted_at="tuesday")
        assert slo.y_to_array_accepted_seconds is None

    def test_an_accept_stamp_predating_the_y_is_none_not_negative(self, experiment: Path) -> None:
        """A resumed run's record was minted on an earlier attempt. An
        uninterpretable number is worse than an absent one."""
        _tonights_shape(experiment)
        slo = s2_slo.compute_slo(read_decisions(experiment, "run", RUN_ID), accepted_at=_ts(-99))
        assert slo.y_to_array_accepted_seconds is None

    def test_a_zero_second_interval_is_reported_not_dropped(self, experiment: Path) -> None:
        _decide(experiment, block="submit-s1", next_block="submit-s2", response="y", offset_sec=0)
        slo = s2_slo.compute_slo(read_decisions(experiment, "run", RUN_ID), accepted_at=_ts(0))
        assert slo.y_to_array_accepted_seconds == 0


class TestInterventionsCount:
    def test_counts_every_submit_chain_decision_including_nudges(self, experiment: Path) -> None:
        _tonights_shape(experiment)
        assert s2_slo.interventions_count(read_decisions(experiment, "run", RUN_ID)) == 3

    def test_decisions_from_other_workflows_are_not_submit_interventions(
        self, experiment: Path
    ) -> None:
        _tonights_shape(experiment)
        append_decision(
            experiment,
            scope_kind="run",
            scope_id=RUN_ID,
            block="aggregate-run",
            response="y",
            resolved={},
            ts=_ts(900),
        )
        assert s2_slo.interventions_count(read_decisions(experiment, "run", RUN_ID)) == 3

    def test_no_decisions_is_zero(self) -> None:
        assert s2_slo.interventions_count([]) == 0


class TestReadinessAgeAtFire:
    def test_the_ledger_age_is_measured_at_the_fire_instant_not_now(self, experiment: Path) -> None:
        _tonights_shape(experiment)  # fire at +60
        readiness.record_observation(
            HOST,
            readiness.CONNECT,
            "ok",
            source="ssh-circuit",
            route="effective",
            now=T0 + timedelta(seconds=10),
        )
        # An atom refreshed LATER must not make the fire look fresher than it was.
        readiness.record_observation(
            HOST,
            readiness.PREAMBLE,
            "ok",
            source="ssh-circuit",
            route="effective",
            now=T0 + timedelta(seconds=5000),
        )
        slo = s2_slo.compute_slo(
            read_decisions(experiment, "run", RUN_ID), accepted_at=_ts(412), host=HOST
        )
        assert slo.readiness_age_at_fire_seconds == 50

    def test_no_ledger_is_none_never_zero(self, experiment: Path) -> None:
        _tonights_shape(experiment)
        slo = s2_slo.compute_slo(
            read_decisions(experiment, "run", RUN_ID), accepted_at=_ts(412), host=HOST
        )
        assert slo.readiness_age_at_fire_seconds is None

    def test_no_host_is_none(self, experiment: Path) -> None:
        _tonights_shape(experiment)
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        slo = s2_slo.compute_slo(
            read_decisions(experiment, "run", RUN_ID), accepted_at=_ts(412), host=""
        )
        assert slo.readiness_age_at_fire_seconds is None

    def test_no_fire_means_no_readiness_age(self, experiment: Path) -> None:
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="s", now=T0)
        slo = s2_slo.compute_slo([], accepted_at=_ts(412), host=HOST)
        assert slo.readiness_age_at_fire_seconds is None


class TestSloShape:
    def test_measured_is_false_only_when_nothing_at_all_was_observed(self) -> None:
        assert not s2_slo.S2Slo().measured
        assert s2_slo.S2Slo(interventions_count=1).measured
        assert s2_slo.S2Slo(y_to_array_accepted_seconds=0).measured
        assert s2_slo.S2Slo(readiness_age_at_fire_seconds=0).measured

    def test_as_dict_is_the_declared_field_order(self) -> None:
        assert list(s2_slo.S2Slo().as_dict()) == list(s2_slo.SLO_FIELDS)

    def test_every_slo_field_declares_a_telemetry_kind(self) -> None:
        """The label seam ``scripts/lint_telemetry_labels.py`` governs: an SLO
        field that reached a render without a declared kind is the ``told 0``
        confusion class."""
        from hpc_agent.ops.monitor.summary import FIELD_KIND

        for name in s2_slo.SLO_FIELDS:
            assert FIELD_KIND[name] == "cumulative"


class TestSloForRun:
    def test_reads_the_run_journals_it_already_has(self, experiment: Path) -> None:
        _tonights_shape(experiment)
        record = RunRecord(
            run_id=RUN_ID,
            experiment_dir=str(experiment),
            cluster="",
            profile="sge",
            ssh_target="u@h",
            remote_path="/scratch/e",
            job_name="j",
            job_ids=["1"],
            total_tasks=4,
            submitted_at=_ts(412),
        )
        upsert_run(experiment, record)
        slo = s2_slo.slo_for_run(experiment, RUN_ID, record)
        assert slo.y_to_array_accepted_seconds == 352
        assert slo.interventions_count == 3
        assert slo.readiness_age_at_fire_seconds is None

    def test_an_unreadable_decision_journal_yields_the_empty_slo(
        self, experiment: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*_a: object, **_k: object) -> list:
            raise RuntimeError("journal on fire")

        monkeypatch.setattr("hpc_agent.state.decision_journal.read_decisions", _boom)
        record = RunRecord(
            run_id=RUN_ID,
            experiment_dir=str(experiment),
            cluster="",
            profile="sge",
            ssh_target="u@h",
            remote_path="/scratch/e",
            job_name="j",
            job_ids=[],
            total_tasks=1,
            submitted_at=_ts(0),
        )
        assert not s2_slo.slo_for_run(experiment, RUN_ID, record).measured
