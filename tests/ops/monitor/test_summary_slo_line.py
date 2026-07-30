"""The S2 SLO line in ``monitor-summary``'s body (s2-readiness pillar 6).

The reducer is tested in ``tests/state/test_s2_slo.py``; this pins how it REACHES
a human — the one place the scorecard is actually read. Two of the assertions
here are 2026-07-30 review findings: the retroactive-reconstruction marker (F8)
and the re-drive field (F5).

Every timestamp is injected; the SLO is a difference between two recorded
instants, so nothing here reads a wall clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from hpc_agent.ops.monitor.summary import FIELD_KIND, monitor_summary
from hpc_agent.state import readiness, s2_slo
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

RUN_ID = "r-slo"
HOST = "slo-line.example.edu"
T0 = datetime(2026, 7, 30, 20, 0, 0, tzinfo=timezone.utc)


def _dt(offset_sec: float) -> datetime:
    return T0 + timedelta(seconds=offset_sec)


def _ts(offset_sec: float) -> str:
    return _dt(offset_sec).isoformat(timespec="seconds")


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    (d / ".hpc").mkdir(parents=True)
    return d


def _seed(experiment: Path, *, submitted_at: str, cluster: str = "") -> RunRecord:
    record = RunRecord(
        run_id=RUN_ID,
        profile="p",
        cluster=cluster,
        ssh_target="u@h",
        remote_path="/x",
        job_name="j",
        job_ids=["1"],
        total_tasks=4,
        submitted_at=submitted_at,
        experiment_dir=str(experiment.resolve()),
    )
    upsert_run(experiment, record)
    return record


def _decide(
    experiment: Path, *, block: str, next_block: str | None, response: str, at: float
) -> None:
    append_decision(
        experiment,
        scope_kind="run",
        scope_id=RUN_ID,
        block=block,
        response=response,
        resolved={"next_block": next_block} if next_block else {},
        ts=_ts(at),
    )


def _body(experiment: Path) -> str:
    out: dict[str, Any] = monitor_summary(experiment, run_id=RUN_ID)
    return str(out["body"])


def _slo_line(experiment: Path) -> str | None:
    for line in _body(experiment).splitlines():
        if line.startswith("slo: "):
            return line
    return None


class TestTheLineAppears:
    def test_a_simple_attended_submit_renders_latency_and_count(self, experiment: Path) -> None:
        _decide(experiment, block="submit-s1", next_block="submit-s2", response="y", at=60)
        _decide(experiment, block="submit-s2", next_block="submit-s3", response="y", at=300)
        _seed(experiment, submitted_at=_ts(412))
        line = _slo_line(experiment)
        assert line is not None
        assert "y_to_array_accepted_seconds=352" in line
        assert "interventions_count=2" in line

    def test_no_measurable_slo_renders_no_line_rather_than_zeros(self, experiment: Path) -> None:
        """An unmeasured latency and a zero latency are different facts."""
        _seed(experiment, submitted_at=_ts(0))
        assert _slo_line(experiment) is None

    def test_a_zero_count_is_still_reported_when_the_line_renders(self, experiment: Path) -> None:
        """A count of zero is a real measurement ("no human stops")."""
        append_decision(
            experiment,
            scope_kind="run",
            scope_id=RUN_ID,
            block="aggregate-run",
            response="y",
            resolved={"next_block": "submit-s2"},
            ts=_ts(10),
        )
        _seed(experiment, submitted_at=_ts(70))
        line = _slo_line(experiment)
        assert line is not None
        assert "interventions_count=0" in line


class TestReviewFindings:
    def test_the_readiness_age_is_marked_reconstructed(
        self, experiment: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """F8: it is inferred after the fact from surviving atom stamps, not a
        stamp taken at fire time — a number that looks measured but was inferred
        is worse than one that says so."""
        # Point the run's cluster at our ledger host without a clusters.yaml.
        monkeypatch.setattr(s2_slo, "_host_for_cluster", lambda _c: HOST)
        _decide(experiment, block="submit-s1", next_block="submit-s2", response="y", at=60)
        readiness.record_observation(
            HOST, readiness.CONNECT, "ok", source="s", route="effective", now=_dt(0)
        )
        _seed(experiment, submitted_at=_ts(412), cluster="somewhere")
        line = _slo_line(experiment)
        assert line is not None
        assert "readiness_age_at_fire_seconds=60" in line
        assert "(reconstructed)" in line

    def test_a_redriven_run_shows_both_latencies(self, experiment: Path) -> None:
        """F5: the primary is last-attempt scoped; the day-scale span rides
        alongside, labelled, instead of silently replacing it."""
        _decide(experiment, block="submit-s1", next_block="submit-s2", response="y", at=0)
        _decide(experiment, block="submit-s1", next_block="submit-s2", response="y", at=36010)
        _seed(experiment, submitted_at=_ts(39174))
        line = _slo_line(experiment)
        assert line is not None
        assert "y_to_array_accepted_seconds=3164" in line
        assert "first_y_to_array_accepted_seconds=39174 (incl. re-drives)" in line

    def test_a_run_never_redriven_does_not_show_the_day_scale_field(self, experiment: Path) -> None:
        """Printing both when they are equal would imply a distinction that is
        not there."""
        _decide(experiment, block="submit-s1", next_block="submit-s2", response="y", at=60)
        _seed(experiment, submitted_at=_ts(412))
        line = _slo_line(experiment)
        assert line is not None
        assert "first_y_to_array_accepted_seconds" not in line


class TestTelemetryLabelSeam:
    def test_every_slo_field_is_declared_cumulative(self) -> None:
        """None is a per-tick delta, so none may acquire the ``+`` marker: a
        "+41 seconds" reading of a total elapsed time is the ``told 0`` class."""
        for name in s2_slo.SLO_FIELDS:
            assert FIELD_KIND[name] == "cumulative"

    def test_the_rendered_names_are_exactly_the_reducer_fields(self) -> None:
        """One list, two consumers — a field added to the reducer without a
        FIELD_KIND entry raises at render time rather than shipping unlabelled."""
        assert set(s2_slo.SLO_FIELDS) <= set(FIELD_KIND)
