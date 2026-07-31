"""The readiness ledger + S2 SLO, on the four surfaces humans actually read.

Pillar 1 of ``docs/design/s2-readiness.md`` carries a RENDER MANDATE — the
standing ledger is "rendered with age in the S1 brief and ``suggest-*``
surfaces" — and pillar 6 says the SLO "rides run telemetry and the morning
brief". A ledger nobody reads before the y changes nothing. These are the wires:

* ``suggest-prelude-action`` — the rung that hands off to a cluster,
* ``status-snapshot`` — the brief the human reads first,
* the overnight morning brief — the one they read half asleep,
* ``doctor`` — the one they read when something is already wrong.

Every surface is exercised against the five ledger states the design names —
``{ready, degraded, stale, unknown, corrupt}`` — where ``unknown`` is the ABSENT
ledger, its own fact: ``read_ledger`` reports a missing file as
``corrupt=False`` because "nothing has ever looked" and "we cannot read what
looked" are different things, and a surface that blurred them would let an
unreachable cluster hide behind a missing file.

**The consult-only claim is CHECKED, not asserted in prose.** Every test in this
module runs under ``tests/_no_network``'s autouse tripwire, whose
``BaseException`` passes straight through the fail-open ``except Exception``
walls these surfaces are built from — the 2026-07-30 adversarial review proved
an ``assert``-based guard is silently eaten by exactly this code. If any surface
ever grows a dial, a name resolution, or a subprocess on the render path, every
test here fails loudly. (``doctor``'s pre-existing bounded ``git rev-parse``
version-skew probe is neutralized per-test rather than exempted, so the tripwire
keeps covering everything else that verb does.)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from hpc_agent._wire.queries.doctor import DoctorSpec
from hpc_agent._wire.workflows.status_blocks import StatusSnapshotSpec
from hpc_agent.cli.prelude_actions import suggest_prelude_action
from hpc_agent.infra.time import utcnow
from hpc_agent.ops.overnight import overnight_morning_brief
from hpc_agent.ops.readiness_digest import (
    CONSULT_NOTE,
    EMPTY_LEDGER_NOTE,
    NO_LEDGER_NOTE,
    digest_for_host,
    digests_for,
    known_host_digests,
)
from hpc_agent.ops.recover.doctor import doctor
from hpc_agent.ops.status_blocks import status_snapshot
from hpc_agent.state import readiness
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from tests._no_network import no_network  # noqa: F401 — autouse zero-network tripwire

HOST = "hoffman2.example.edu"
CLUSTER = "hoffman2"
#: The read instant every injectable surface is evaluated at. Atoms are stamped
#: RELATIVE to it, so no assertion here depends on a wall clock.
T0 = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
_STALE_HORIZON = readiness.stale_after_sec(readiness.CONNECT)

_CLUSTERS_YAML = f"""\
{CLUSTER}:
  host: {HOST}
  scheduler: slurm
  scratch: /scratch
"""


def _iso(at: datetime) -> str:
    return at.isoformat(timespec="seconds")


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Journal home + a one-cluster clusters.yaml, both redirected into tmp."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    config = tmp_path / "clusters.yaml"
    config.write_text(_CLUSTERS_YAML, encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(config))
    return tmp_path


@pytest.fixture
def exp(tmp_path: Path) -> Path:
    directory = tmp_path / "exp"
    directory.mkdir(exist_ok=True)
    return directory


@pytest.fixture
def no_version_skew(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize ``doctor``'s bounded ``git rev-parse`` version-skew probe.

    That subprocess predates this wave and has nothing to do with readiness, but
    the no-network tripwire (correctly) counts a child process as a dial. Rather
    than exempt the whole verb — which would stop the tripwire policing the
    readiness section it exists to police — the probe is short-circuited at its
    own first gate: ``runtime_sha() == ""`` makes ``_detect_version_skew``
    return before it ever shells out. Anything else in ``doctor`` that spawns
    still trips the wire.
    """
    import hpc_agent.ops.recover.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "runtime_sha", lambda: "")


# ── the five ledger states ───────────────────────────────────────────────────
#
# One seeder per state, all keyed on HOST, every atom stamped RELATIVE to the
# instant the surface will be read at. Two kinds of surface exist here, and the
# relative stamping serves both without a second fixture set:
#
#   * ``status-snapshot`` / the morning brief / ``doctor`` take an injected
#     ``now`` — read at ``T0``, exact arithmetic, no wall clock.
#   * ``suggest-prelude-action`` takes NO now (it is a pure suggestion over local
#     substrates and invents no clock parameter for a test's benefit), so its
#     digest dates against the real clock — seeded at ``utcnow()`` instead. Same
#     seeder, same states, ages asserted loosely.
#
# ``AGE_AT_READ`` is how old each state's freshest atom is at the read instant —
# the number the digest must print.

AGE_AT_READ: dict[str, float] = {
    "ready": 30.0,
    "degraded": 10.0,
    "stale": _STALE_HORIZON + 60.0,
    "unknown": 0.0,
    "corrupt": 10.0,
}

STATES: tuple[str, ...] = ("ready", "degraded", "stale", "unknown", "corrupt")

#: The verdict each state must produce at its read instant.
VERDICT: dict[str, str] = {
    "ready": "ready",
    "degraded": "degraded",
    "stale": "stale",
    "unknown": "unknown",
    "corrupt": "unknown",
}

#: The ``age_seconds`` each state must print at its read instant — the mandate's
#: OWN number, asserted on every surface rather than inferred from the presence
#: of a suffix. ``None`` where there is no dateable atom (no ledger at all, or a
#: file we could not read): honest, never a zero that reads as "just checked".
EXPECTED_AGE: dict[str, int | None] = {
    "ready": 30,
    "degraded": 10,
    "stale": int(_STALE_HORIZON + 60),
    "unknown": None,
    "corrupt": None,
}

#: Tolerance for the ONE surface with no injectable clock
#: (``suggest-prelude-action``): its digest dates against the real wall clock, so
#: the age is the seeded one plus however long the call took.
_WALL_CLOCK_SLACK_SEC = 30


def seed(state: str, *, read_at: datetime = T0) -> None:
    """Put HOST's ledger into *state* as seen from *read_at*.

    ``unknown`` writes nothing at all — an ABSENT ledger, which is its own fact.
    """
    if state == "unknown":
        return
    stamp = read_at - timedelta(seconds=AGE_AT_READ[state])
    if state == "corrupt":
        # Seed a real ledger first (so the host is DISCOVERABLE), then tear it:
        # a host whose ledger is unreadable must stay visible, because vanishing
        # from a readiness report reads as "nothing to worry about here".
        readiness.record_observation(HOST, readiness.CONNECT, "ok", source="ssh-circuit", now=stamp)
        readiness.readiness_path(HOST).write_text("{torn", encoding="utf-8")
        return
    readiness.record_observation(
        HOST,
        readiness.CONNECT,
        "ok",
        source="ssh-circuit",
        route="effective",
        latency_ms=41,
        now=stamp,
    )
    if state == "degraded":
        readiness.record_observation(
            HOST,
            readiness.PREAMBLE,
            "timeout",
            source="ssh-circuit",
            route="effective",
            now=stamp,
        )


def _mk_run(exp: Path, run_id: str, *, status: str = "in_flight", **kw: Any) -> RunRecord:
    record = RunRecord(
        run_id=run_id,
        profile="prof",
        cluster=CLUSTER,
        ssh_target=f"user@{HOST}",
        remote_path="/scratch/run",
        job_name="job",
        job_ids=["1"],
        total_tasks=10,
        submitted_at=_iso(T0),
        experiment_dir=str(exp),
        status=status,
        **kw,
    )
    upsert_run(exp, record)
    return record


def _greenlight(exp: Path, run_id: str) -> None:
    """One real ``y`` into S2, 41 s before the array — enough to MEASURE an SLO.

    Written by the real writer (``append_decision``), so a shape change in the
    decision journal fails this instead of drifting past it.
    """
    append_decision(
        exp,
        scope_kind="run",
        scope_id=run_id,
        block="submit-s1",
        response="y",
        resolved={"next_block": "submit-s2"},
        ts=_iso(T0 - timedelta(seconds=41)),
    )


# ── the digest itself ────────────────────────────────────────────────────────


class TestTheDigest:
    """The one-liner every surface renders — verdict, AGE, the sensor at fault."""

    @pytest.mark.parametrize("state", STATES)
    def test_verdict_matches_the_one_definition(self, state: str) -> None:
        """The digest never computes a verdict — it renders ``overall_verdict``'s."""
        seed(state)
        entry = digest_for_host(HOST, cluster=CLUSTER, now=T0)
        assert entry["verdict"] == VERDICT[state]
        assert entry["verdict"] == readiness.overall_verdict(readiness.read_ledger(HOST), now=T0)

    def test_ready_line_carries_the_age_and_no_fault(self) -> None:
        seed("ready")
        entry = digest_for_host(HOST, cluster=CLUSTER, now=T0)
        assert entry["age_seconds"] == 30
        assert entry["note"] is None
        assert entry["line"] == f"{CLUSTER} ({HOST}): ready · 30s ago ({CONSULT_NOTE})"

    def test_degraded_line_names_the_sensor_that_is_not_green(self) -> None:
        """'degraded' alone sends a human to the logs; the sensor sends them to the fix."""
        seed("degraded")
        entry = digest_for_host(HOST, cluster=CLUSTER, now=T0)
        assert entry["note"] == f"preamble/effective → {HOST}: timeout"
        assert entry["line"] == (
            f"{CLUSTER} ({HOST}): degraded · 10s ago · "
            f"preamble/effective → {HOST}: timeout ({CONSULT_NOTE})"
        )

    def test_stale_line_says_stale_with_its_horizon_never_degraded(self) -> None:
        """A stale failure is 'we do not know', not 'it is broken' — the host may
        have healed and nothing has looked since."""
        seed("stale")
        entry = digest_for_host(HOST, cluster=CLUSTER, now=T0)
        assert entry["verdict"] == "stale"
        assert entry["note"] == (
            f"connect/effective → {HOST}: ok, STALE (horizon {int(_STALE_HORIZON)}s)"
        )
        assert "16m ago" in entry["line"]

    def test_absent_ledger_is_honest_never_silent_and_never_green(self) -> None:
        entry = digest_for_host(HOST, cluster=CLUSTER, now=T0)
        assert entry["verdict"] == "unknown"
        assert entry["age_seconds"] is None
        assert entry["ledger_corrupt"] is False  # absent ≠ corrupt
        assert entry["note"] == NO_LEDGER_NOTE
        assert entry["line"] == (
            f"{CLUSTER} ({HOST}): unknown · age unknown · {NO_LEDGER_NOTE} ({CONSULT_NOTE})"
        )

    def test_a_ledger_of_only_foreign_atoms_is_not_reported_as_no_ledger(self) -> None:
        """An atom naming a sensor outside the vocabulary is DROPPED on read (so
        a foreign or future writer can never move a host's verdict). Reporting
        the result as 'no ledger' would hide that something IS writing here in a
        shape we refuse."""
        path = readiness.readiness_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": readiness.SCHEMA_VERSION,
                    "host": HOST,
                    "atoms": [{"sensor": "quantum-tunnel", "verdict": "ok", "at": _iso(T0)}],
                }
            ),
            encoding="utf-8",
        )
        entry = digest_for_host(HOST, cluster=CLUSTER, now=T0)
        assert entry["verdict"] == "unknown"
        assert entry["ledger_corrupt"] is False
        assert entry["note"] == EMPTY_LEDGER_NOTE

    def test_corrupt_ledger_is_disclosed_and_does_not_claim_nothing_was_observed(self) -> None:
        """The reassuring default note would contradict the disclosure beside it."""
        seed("corrupt")
        entry = digest_for_host(HOST, cluster=CLUSTER, now=T0)
        assert entry["ledger_corrupt"] is True
        assert entry["ledger_corruption_reason"]
        assert NO_LEDGER_NOTE not in entry["line"]
        assert "ledger file could not be read" in entry["line"]

    @pytest.mark.parametrize("state", STATES)
    def test_every_line_says_it_is_a_reading_not_a_check(self, state: str) -> None:
        """A line gets quoted out of its surface; it must carry its own caveat."""
        seed(state)
        entry = digest_for_host(HOST, cluster=CLUSTER, now=T0)
        assert entry["line"].endswith(f"({CONSULT_NOTE})")

    def test_user_at_host_targets_key_the_same_ledger(self) -> None:
        """A run's ssh_target can be handed in verbatim — one host identity."""
        seed("ready")
        bare = digest_for_host(HOST, now=T0)
        prefixed = digest_for_host(f"user@{HOST}", now=T0)
        assert prefixed["host"] == bare["host"] == HOST
        assert prefixed["line"] == bare["line"]

    def test_digests_for_dedupes_and_sorts(self) -> None:
        seed("ready")
        rows = digests_for(
            [(CLUSTER, f"user@{HOST}"), (CLUSTER, HOST), (None, "zeta.example"), (None, "")],
            now=T0,
        )
        assert [r["host"] for r in rows] == ["zeta.example", HOST]
        assert [r["cluster"] for r in rows] == [None, CLUSTER]

    def test_known_host_digests_covers_only_observed_hosts(self) -> None:
        seed("ready")
        readiness.record_observation("other.example", readiness.CONNECT, "down", source="s", now=T0)
        rows = known_host_digests(now=T0)
        # The configured cluster name is attached; a ledger-only host has none,
        # and is NOT hidden for lacking one.
        assert {r["host"]: r["cluster"] for r in rows} == {HOST: CLUSTER, "other.example": None}


# ── surface 1: suggest-prelude-action ────────────────────────────────────────


def _settle_prelude(exp: Path, *, cluster: str | None = CLUSTER) -> None:
    """Put the prelude in its terminal (rung 9 → ``submit-s1``) state.

    Materialized interview, a valid axes.yaml, no audit journal, no packs — the
    exact state in which the very next step touches a cluster.
    """
    doc: dict[str, Any] = {"goal": "measure the thing", "_materialized": {}}
    if cluster is not None:
        doc["cluster_target"] = {"cluster": cluster, "profile": "standard"}
    (exp / "interview.json").write_text(json.dumps(doc), encoding="utf-8")
    (exp / ".hpc").mkdir(exist_ok=True)
    (exp / ".hpc" / "axes.yaml").write_text("axes_schema_version: 1\n", encoding="utf-8")


class TestSuggestPreludeAction:
    @pytest.mark.parametrize("state", STATES)
    def test_the_cluster_rung_carries_the_line_for_every_ledger_state(
        self, exp: Path, state: str
    ) -> None:
        seed(state, read_at=utcnow())
        _settle_prelude(exp)
        result = suggest_prelude_action(exp)
        assert result.action == "submit-s1"
        assert len(result.readiness) == 1
        row = result.readiness[0]
        assert row.cluster == CLUSTER
        assert row.host == HOST
        assert row.verdict == VERDICT[state]
        assert row.line.endswith(f"({CONSULT_NOTE})")
        # Not ready ⇒ the line NAMES what is wrong; ready ⇒ nothing to name.
        assert (row.note is None) is (row.verdict == "ready")
        # RENDERED WITH AGE — the mandate's own number, not just a suffix. This
        # surface has no injectable clock, so the age is the seeded one plus the
        # call's own duration; ``None`` stays exactly ``None``.
        expected = EXPECTED_AGE[state]
        if expected is None:
            assert row.age_seconds is None
            assert "age unknown" in row.line
        else:
            assert row.age_seconds is not None
            assert expected <= row.age_seconds <= expected + _WALL_CLOCK_SLACK_SEC

    def test_a_non_cluster_rung_carries_no_readiness_line(self, exp: Path) -> None:
        """A line beside a rung that cannot use it is noise, and a usually-noisy
        field trains its readers to skip the one time it mattered."""
        seed("degraded", read_at=utcnow())
        result = suggest_prelude_action(exp)  # cold start → notebook-scaffold-template
        assert result.action == "notebook-scaffold-template"
        assert result.readiness == []

    def test_an_absent_ledger_renders_honestly_rather_than_vanishing(self, exp: Path) -> None:
        _settle_prelude(exp)
        result = suggest_prelude_action(exp)
        assert [r.verdict for r in result.readiness] == ["unknown"]
        assert NO_LEDGER_NOTE in result.readiness[0].line

    def test_an_unpinned_cluster_reports_the_configured_ones(self, exp: Path) -> None:
        seed("ready", read_at=utcnow())
        _settle_prelude(exp, cluster=None)
        result = suggest_prelude_action(exp)
        assert [r.host for r in result.readiness] == [HOST]
        # One configured cluster ⇒ no ambiguity to disclose.
        assert not any("pins no cluster_target" in d for d in result.disclosures)

    def test_a_pin_naming_no_configured_cluster_is_disclosed_not_invented(self, exp: Path) -> None:
        seed("ready", read_at=utcnow())
        _settle_prelude(exp, cluster="ghostbox")
        result = suggest_prelude_action(exp)
        assert result.readiness == []
        assert any("ghostbox" in d and "clusters.yaml" in d for d in result.disclosures)

    def test_a_broken_readiness_read_never_fails_the_suggestion(
        self, exp: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard is REAL, not decorative: an advisory line must never be the
        thing that breaks the verb a human runs to learn what to do next.

        Injected at the seam the surface actually calls (``digests_for``, imported
        inside the guarded block), so narrowing the ``except Exception`` to any
        specific class fails this test.
        """
        import hpc_agent.ops.readiness_digest as digest_mod

        def _boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("ledger exploded")

        monkeypatch.setattr(digest_mod, "digests_for", _boom)
        seed("ready", read_at=utcnow())
        _settle_prelude(exp)
        result = suggest_prelude_action(exp)
        # The suggestion still lands, whole — and says why the line is missing.
        assert result.action == "submit-s1"
        assert result.rung == 9
        assert result.readiness == []
        assert any("readiness ledger unavailable" in d for d in result.disclosures)

    def test_readiness_never_changes_the_rung(self, exp: Path) -> None:
        """The ladder is total; a degraded cluster must not make it weather-dependent."""
        _settle_prelude(exp)
        without = suggest_prelude_action(exp)
        seed("degraded", read_at=utcnow())
        with_degraded = suggest_prelude_action(exp)
        assert without.action == with_degraded.action == "submit-s1"
        assert without.rung == with_degraded.rung
        assert without.readiness[0].verdict == "unknown"
        assert with_degraded.readiness[0].verdict == "degraded"


# ── surface 2: status-snapshot ───────────────────────────────────────────────


class TestStatusSnapshot:
    @pytest.mark.parametrize("state", STATES)
    def test_one_line_per_involved_cluster_in_every_ledger_state(
        self, exp: Path, state: str
    ) -> None:
        seed(state)
        _mk_run(exp, "run-a")
        _mk_run(exp, "run-b")  # same cluster — one readiness FACT, not two
        result = status_snapshot(exp, spec=StatusSnapshotSpec(now_iso=_iso(T0), mark_seen=False))
        rows = result.brief["readiness"]
        assert [r["host"] for r in rows] == [HOST]
        assert rows[0]["cluster"] == CLUSTER
        assert rows[0]["verdict"] == VERDICT[state]
        assert rows[0]["line"].endswith(f"({CONSULT_NOTE})")
        # RENDERED WITH AGE — exact here, because ``now`` is injected.
        assert rows[0]["age_seconds"] == EXPECTED_AGE[state]
        if EXPECTED_AGE[state] is None:
            assert "age unknown" in rows[0]["line"]

    def test_no_involved_cluster_omits_the_keys_entirely(self, exp: Path) -> None:
        """Additive: a fleet with no runs is byte-unchanged."""
        seed("ready")
        result = status_snapshot(exp, spec=StatusSnapshotSpec(now_iso=_iso(T0), mark_seen=False))
        assert "readiness" not in result.brief
        assert "slo" not in result.brief

    def test_absent_ledger_still_renders_the_involved_cluster(self, exp: Path) -> None:
        _mk_run(exp, "run-a")
        result = status_snapshot(exp, spec=StatusSnapshotSpec(now_iso=_iso(T0), mark_seen=False))
        row = result.brief["readiness"][0]
        assert row["verdict"] == "unknown"
        assert row["note"] == NO_LEDGER_NOTE

    def test_the_slo_line_is_monitor_summarys_own_renderer(self, exp: Path) -> None:
        """One definition, two surfaces: the snapshot RELAYS the SLO line, it
        never re-composes it — two surfaces disagreeing about a scorecard is
        worse than one surface not carrying it."""
        from hpc_agent.ops.monitor.summary import format_slo
        from hpc_agent.state.s2_slo import slo_for_run

        seed("ready")
        record = _mk_run(exp, "run-done", status="complete")
        _greenlight(exp, "run-done")
        result = status_snapshot(
            exp, spec=StatusSnapshotSpec(run_id="run-done", now_iso=_iso(T0), mark_seen=False)
        )
        rows = result.brief["slo"]
        assert [r["run_id"] for r in rows] == ["run-done"]
        assert rows[0]["slo"] == format_slo(slo_for_run(exp, "run-done", record))
        assert "y_to_array_accepted_seconds=41" in rows[0]["slo"]
        assert "interventions_count=1" in rows[0]["slo"]

    def test_an_in_flight_run_carries_no_settled_scorecard(self, exp: Path) -> None:
        seed("ready")
        _mk_run(exp, "run-live")
        _greenlight(exp, "run-live")
        result = status_snapshot(
            exp, spec=StatusSnapshotSpec(run_id="run-live", now_iso=_iso(T0), mark_seen=False)
        )
        assert "slo" not in result.brief
        assert result.brief["readiness"][0]["verdict"] == "ready"

    def test_a_broken_readiness_read_never_blanks_the_digest(
        self, exp: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hpc_agent.ops.readiness_digest as digest_mod

        def _boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("ledger exploded")

        monkeypatch.setattr(digest_mod, "digests_for", _boom)
        _mk_run(exp, "run-a")
        result = status_snapshot(exp, spec=StatusSnapshotSpec(now_iso=_iso(T0), mark_seen=False))
        assert "readiness" not in result.brief
        assert result.brief["running_where"][0]["run_id"] == "run-a"

    def test_a_broken_scorecard_read_never_blanks_the_digest(
        self, exp: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SLO section's guard is REAL too: the morning read is what a human
        opens when something already went wrong, and a scorecard is the least
        load-bearing thing on it — it must fail out of the way.

        Injected at ``slo_for_run``, the reducer the section imports inside its
        guarded block, so narrowing the ``except Exception`` fails this test.
        """
        import hpc_agent.state.s2_slo as slo_mod

        def _boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("reducer exploded")

        monkeypatch.setattr(slo_mod, "slo_for_run", _boom)
        seed("ready")
        _mk_run(exp, "run-done", status="complete")
        _greenlight(exp, "run-done")
        result = status_snapshot(
            exp, spec=StatusSnapshotSpec(run_id="run-done", now_iso=_iso(T0), mark_seen=False)
        )
        assert "slo" not in result.brief
        # …and every other paragraph is untouched, readiness included.
        assert result.brief["running_where"][0]["run_id"] == "run-done"
        assert result.brief["readiness"][0]["verdict"] == "ready"


# ── surface 2b: the overnight morning brief ──────────────────────────────────


class TestMorningBrief:
    @pytest.mark.parametrize("state", STATES)
    def test_the_run_scope_brief_carries_its_clusters_line(self, exp: Path, state: str) -> None:
        seed(state)
        _mk_run(exp, "run-a")
        brief = overnight_morning_brief(exp, scope_kind="run", scope_id="run-a", now_iso=_iso(T0))
        row = brief["readiness"]
        assert row["host"] == HOST
        assert row["cluster"] == CLUSTER
        assert row["verdict"] == VERDICT[state]
        assert row["line"].endswith(f"({CONSULT_NOTE})")
        # RENDERED WITH AGE — exact here, because ``now_iso`` is injected. The
        # half-asleep read is precisely where "ready" without "how long ago"
        # would be believed.
        assert row["age_seconds"] == EXPECTED_AGE[state]
        if EXPECTED_AGE[state] is None:
            assert "age unknown" in row["line"]

    def test_absent_ledger_renders_honestly(self, exp: Path) -> None:
        _mk_run(exp, "run-a")
        brief = overnight_morning_brief(exp, scope_kind="run", scope_id="run-a", now_iso=_iso(T0))
        assert brief["readiness"]["verdict"] == "unknown"
        assert brief["readiness"]["note"] == NO_LEDGER_NOTE

    def test_the_slo_line_is_the_same_renderer_and_only_once_finished(self, exp: Path) -> None:
        from hpc_agent.ops.monitor.summary import format_slo
        from hpc_agent.state.s2_slo import slo_for_run

        seed("ready")
        _mk_run(exp, "run-live")
        _greenlight(exp, "run-live")
        live = overnight_morning_brief(exp, scope_kind="run", scope_id="run-live", now_iso=_iso(T0))
        assert live["slo"] is None

        record = _mk_run(exp, "run-done", status="complete")
        _greenlight(exp, "run-done")
        done = overnight_morning_brief(exp, scope_kind="run", scope_id="run-done", now_iso=_iso(T0))
        assert done["slo"] == format_slo(slo_for_run(exp, "run-done", record))
        assert "y_to_array_accepted_seconds=41" in done["slo"]

    def test_a_non_run_scope_composes_neither(self, exp: Path) -> None:
        """A campaign scope has no single run record to date a scorecard against,
        and inventing one would be a second definition of 'the run this is about'."""
        seed("ready")
        brief = overnight_morning_brief(
            exp, scope_kind="campaign", scope_id="camp-1", now_iso=_iso(T0)
        )
        assert brief["readiness"] is None
        assert brief["slo"] is None

    def test_an_unjournaled_run_composes_neither_rather_than_guessing(self, exp: Path) -> None:
        seed("ready")
        brief = overnight_morning_brief(exp, scope_kind="run", scope_id="ghost", now_iso=_iso(T0))
        assert brief["readiness"] is None
        assert brief["slo"] is None

    def test_a_broken_readiness_read_never_wedges_the_morning_read(
        self, exp: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard is REAL: the overnight disclosure — what was consumed, what
        died, the re-grant offer — is the whole point of this brief, and an
        advisory readiness line must never take it down with it.

        Injected at ``digest_for_host``, imported inside the guarded block, so
        narrowing the ``except Exception`` fails this test.
        """
        import hpc_agent.ops.readiness_digest as digest_mod

        def _boom(*_a: object, **_k: object) -> None:
            raise RuntimeError("ledger exploded")

        monkeypatch.setattr(digest_mod, "digest_for_host", _boom)
        seed("ready")
        _mk_run(exp, "run-done", status="complete")
        _greenlight(exp, "run-done")
        brief = overnight_morning_brief(
            exp, scope_kind="run", scope_id="run-done", now_iso=_iso(T0)
        )
        # Both s2-readiness fields fall out together (one guard covers the pair),
        # and every field the brief exists FOR is still there.
        assert brief["readiness"] is None
        assert brief["slo"] is None
        assert brief["scope_id"] == "run-done"
        assert brief["surfaced_at"] == _iso(T0)
        assert brief["consumed"] == []


# ── surface 3: doctor ────────────────────────────────────────────────────────


@pytest.mark.usefixtures("no_version_skew")
class TestDoctor:
    @pytest.mark.parametrize("state", ["ready", "degraded", "stale", "corrupt"])
    def test_every_known_host_is_listed_with_verdict_and_age(self, exp: Path, state: str) -> None:
        seed(state)
        result = doctor(experiment_dir=exp, spec=DoctorSpec(now=_iso(T0)))
        rows = result["readiness"]
        assert [r["host"] for r in rows] == [HOST]
        assert rows[0]["verdict"] == VERDICT[state]
        # A watchdog line is the one most likely to be read as a live check —
        # doctor is where an operator looks when something is already wrong.
        assert rows[0]["line"].endswith(f"({CONSULT_NOTE})")
        # AGE is the whole point: a verdict without one is the failure mode the
        # ledger exists to remove. (A corrupt ledger has no atom to date — and
        # says so rather than printing a zero.)
        assert rows[0]["age_seconds"] == EXPECTED_AGE[state]
        if state == "corrupt":
            assert "ledger file could not be read" in rows[0]["line"]

    def test_a_host_with_no_ledger_is_not_invented(self, exp: Path) -> None:
        """doctor reports what the machine KNOWS — a line here means something
        was observed. (cluster-readiness is the verb that unions the config in.)"""
        result = doctor(experiment_dir=exp, spec=DoctorSpec(now=_iso(T0)))
        assert result["readiness"] == []

    def test_last_corruption_is_disclosed_after_a_rebuild(self, exp: Path) -> None:
        """A rebuild that discards a corrupt file must not also discard the FACT
        — otherwise the operator's only signal vanishes with the file."""
        path = readiness.readiness_path(HOST)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{torn", encoding="utf-8")
        readiness.record_observation(
            HOST, readiness.CONNECT, "ok", source="ssh-circuit", now=T0 - timedelta(seconds=30)
        )
        result = doctor(experiment_dir=exp, spec=DoctorSpec(now=_iso(T0)))
        row = result["readiness"][0]
        assert row["ledger_corrupt"] is False  # the file reads fine NOW
        assert row["last_corruption"]["reason"]
        assert row["verdict"] == "ready"

    @pytest.mark.parametrize("state", STATES)
    def test_readiness_never_flips_needs_attention(self, exp: Path, state: str) -> None:
        """A stale ledger means nothing has looked lately — not a driver that
        died. Folding it in would make the watchdog's one load-bearing bit fire
        on weather.

        Parametrized over ALL FIVE states, including the two that could not
        plausibly flip the bit (``ready`` is green; ``unknown`` produces no row
        at all, since doctor's scope is hosts that HAVE a ledger). Vacuous cases
        cost one run each and they pin the shape: if a later edit ever made an
        empty or green readiness section fire ``needs_attention``, the vacuous
        rows are the ones that would catch it.
        """
        seed(state)
        result = doctor(experiment_dir=exp, spec=DoctorSpec(now=_iso(T0)))
        assert result["needs_attention"] is False
        assert result["attention_summary"].startswith("all clear")
        # ``unknown`` = no ledger file ⇒ nothing observed ⇒ no row to render.
        assert (result["readiness"] == []) is (state == "unknown")

    def test_a_broken_ledger_read_never_breaks_detection(
        self, exp: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hpc_agent.ops.readiness_digest as digest_mod

        def _boom(**_k: object) -> None:
            raise RuntimeError("ledger exploded")

        monkeypatch.setattr(digest_mod, "known_host_digests", _boom)
        result = doctor(experiment_dir=exp, spec=DoctorSpec(now=_iso(T0)))
        assert result["readiness"] == []
        assert result["needs_attention"] is False
