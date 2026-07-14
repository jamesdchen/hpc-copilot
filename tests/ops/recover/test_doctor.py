"""Tests for the ``doctor`` driver-watchdog query (§5).

Detection only: doctor surfaces stalled runs as drafted proposals and never
restarts or re-arms anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.doctor import DoctorSpec
from hpc_agent.ops.recover.doctor import doctor
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.journal import mark_pending_decision, stamp_tick, upsert_run
from hpc_agent.state.run_record import RunRecord


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(run_id: str, *, status: str = "in_flight") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="hoffman2",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["100"],
        total_tasks=4,
        submitted_at="2026-07-03T00:00:00+00:00",
        experiment_dir="/exp",
        status=status,
    )


def test_doctor_surfaces_only_the_stalled_run(tmp_path: Path) -> None:
    now = "2026-07-03T01:00:00+00:00"
    upsert_run(tmp_path, _record("stalled"))
    stamp_tick(
        "stalled",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    upsert_run(tmp_path, _record("healthy"))
    stamp_tick(
        "healthy",
        last_tick_at="2026-07-03T00:59:00+00:00",
        next_tick_due="2026-07-03T02:00:00+00:00",
        experiment_dir=tmp_path,
    )

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now))
    assert out["now"] == now
    assert out["stalled_count"] == 1
    hit = out["stalled"][0]
    assert hit["run_id"] == "stalled"
    assert hit["status"] == "in_flight"
    assert hit["cluster"] == "hoffman2"
    assert hit["ssh_target"] == "u@h"
    # Drafted proposal + evidence, never an action.
    assert "stalled" in hit["proposal"].lower()
    assert "re-arm" in hit["proposal"].lower()
    assert hit["evidence"]["overdue_seconds"] == 3600
    assert hit["evidence"]["now"] == now


def test_doctor_empty_when_nothing_overdue(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("healthy"))
    stamp_tick(
        "healthy",
        last_tick_at="2026-07-03T00:59:00+00:00",
        next_tick_due="2026-07-03T02:00:00+00:00",
        experiment_dir=tmp_path,
    )
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))
    assert out["stalled_count"] == 0
    assert out["stalled"] == []


def test_doctor_rejects_malformed_now(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="not-a-timestamp"))


# ─── parked ≠ stalled (§5) ──────────────────────────────────────────────────


def _park(exp: Path, run_id: str) -> None:
    mark_pending_decision(
        run_id,
        block="s2",
        workflow="submit",
        brief={"proposal": "greenlight the canary?"},
        resume_cursor={"workflow": "submit", "run_id": run_id, "next_verb": "s3"},
        awaiting_since="2026-07-03T00:30:00+00:00",
        experiment_dir=exp,
    )


def test_doctor_reports_parked_run_not_stalled(tmp_path: Path) -> None:
    """A run past its tick deadline BUT carrying a pending_decision marker is
    parked (awaiting the human), never stalled — the §5 "parked ≠ stalled" read."""
    now = "2026-07-03T01:00:00+00:00"
    upsert_run(tmp_path, _record("parked"))
    stamp_tick(
        "parked",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",  # overdue: would be stalled...
        experiment_dir=tmp_path,
    )
    _park(tmp_path, "parked")  # ...but the marker flips the read

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now))

    # Never in the stalled list.
    assert out["stalled_count"] == 0
    assert out["stalled"] == []
    assert all(p["run_id"] != "parked" for p in out["stalled"])
    # Surfaced in parked with the awaiting read, not a re-arm proposal.
    assert out["parked_count"] == 1
    note = out["parked"][0]
    assert note["run_id"] == "parked"
    assert note["block"] == "s2"
    assert note["workflow"] == "submit"
    assert note["awaiting_since"] == "2026-07-03T00:30:00+00:00"
    assert "awaiting your decision" in note["note"].lower()
    assert "re-arm" not in note["note"].lower()


def test_doctor_separates_parked_from_stalled(tmp_path: Path) -> None:
    now = "2026-07-03T01:00:00+00:00"
    # A genuinely stalled run (overdue, no marker).
    upsert_run(tmp_path, _record("stalled"))
    stamp_tick(
        "stalled",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    # A parked run (overdue but awaiting a decision).
    upsert_run(tmp_path, _record("parked"))
    stamp_tick(
        "parked",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    _park(tmp_path, "parked")

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now))

    assert [p["run_id"] for p in out["stalled"]] == ["stalled"]
    assert [p["run_id"] for p in out["parked"]] == ["parked"]


def test_doctor_no_parked_when_none_awaiting(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("healthy"))
    stamp_tick(
        "healthy",
        last_tick_at="2026-07-03T00:59:00+00:00",
        next_tick_due="2026-07-03T02:00:00+00:00",
        experiment_dir=tmp_path,
    )
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))
    assert out["parked_count"] == 0
    assert out["parked"] == []


# ─── awaiting_advance: committed-but-unadvanced (§5 Phase-5) ─────────────────


def _commit_y(exp: Path, run_id: str) -> None:
    # A genuine greenlight's ``resolved`` carries the ``next_block`` routing
    # token naming the gated successor — the boundary the marker is parked at
    # (matching ``_park``'s resume_cursor.next_verb). The boundary-scoped
    # predicate (bug-sweep #1/#23, run-12 finding 21) keys on it.
    append_decision(
        exp,
        scope_kind="run",
        scope_id=run_id,
        block="s2",
        response="y",
        resolved={"approved": True, "next_block": "s3"},
    )


def test_doctor_stale_prior_boundary_y_stays_parked(tmp_path: Path) -> None:
    """A committed ``y`` that names a DIFFERENT boundary than the marker's
    (a prior boundary's already-consumed greenlight) must read as PARKED —
    awaiting the human — not as a stalled driver to re-arm (bug-sweep #1/#23,
    run-12 finding 21: the consumed-y livelock, doctor surface)."""
    now = "2026-07-03T01:00:00+00:00"
    upsert_run(tmp_path, _record("stale-y"))
    stamp_tick(
        "stale-y",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    _park(tmp_path, "stale-y")  # marker's resume_cursor.next_verb == "s3"
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="stale-y",
        block="s1",
        response="y",
        resolved={"approved": True, "next_block": "s2"},  # names the PRIOR boundary
    )

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now))

    assert out["awaiting_advance_count"] == 0
    assert out["parked_count"] == 1
    assert out["parked"][0]["run_id"] == "stale-y"


def _commit_nudge(exp: Path, run_id: str) -> None:
    append_decision(
        exp,
        scope_kind="run",
        scope_id=run_id,
        block="s2",
        response="cap the cost at 10",
    )


def test_doctor_surfaces_committed_y_as_awaiting_advance(tmp_path: Path) -> None:
    """A parked run whose latest committed decision is a `y` is a stalled driver
    (human decided, driver died before advancing) — surfaced in awaiting_advance
    with a re-arm proposal, NOT in parked and NOT in stalled."""
    now = "2026-07-03T01:00:00+00:00"
    upsert_run(tmp_path, _record("decided"))
    stamp_tick(
        "decided",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    _park(tmp_path, "decided")
    _commit_y(tmp_path, "decided")

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now))

    # Not stalled (has a marker), not merely parked (has a committed y).
    assert out["stalled_count"] == 0
    assert out["parked_count"] == 0
    assert out["parked"] == []
    # Surfaced as awaiting_advance with a re-arm proposal.
    assert out["awaiting_advance_count"] == 1
    prop = out["awaiting_advance"][0]
    assert prop["run_id"] == "decided"
    assert prop["block"] == "s2"
    assert prop["workflow"] == "submit"
    assert "re-arm" in prop["proposal"].lower()
    assert "block-drive" in prop["proposal"].lower()
    assert "decided" in prop["proposal"]
    assert prop["evidence"]["committed_response"] == "y"


def test_doctor_parked_with_only_nudge_stays_awaiting_human(tmp_path: Path) -> None:
    """A parked run whose latest decision is a nudge (not a `y`) is still
    genuinely awaiting the human → parked note, never awaiting_advance."""
    now = "2026-07-03T01:00:00+00:00"
    upsert_run(tmp_path, _record("nudged"))
    stamp_tick(
        "nudged",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    _park(tmp_path, "nudged")
    _commit_nudge(tmp_path, "nudged")

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now))

    assert out["awaiting_advance_count"] == 0
    assert out["awaiting_advance"] == []
    assert out["parked_count"] == 1
    assert out["parked"][0]["run_id"] == "nudged"
    assert out["stalled_count"] == 0


def test_doctor_y_then_unrelated_later_record_surfaces_as_awaiting_advance(
    tmp_path: Path,
) -> None:
    """F13 direction (b): a committed `y` followed by an UNRELATED later record (a different
    block — an overnight-consent) must still surface as awaiting_advance. Previously the
    doctor keyed on ``latest_decision`` only, so the trailing consent hid the genuine `y`
    and the out-of-session backstop stalled it — and disagreed with the now-fixed Stop guard."""
    now = "2026-07-03T01:00:00+00:00"
    upsert_run(tmp_path, _record("decided-then-consent"))
    stamp_tick(
        "decided-then-consent",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    _park(tmp_path, "decided-then-consent")
    _commit_y(tmp_path, "decided-then-consent")
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="decided-then-consent",
        block="overnight-consent",  # a DIFFERENT block — unrelated to the parked boundary
        response="let it run overnight",
    )

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now))

    assert out["awaiting_advance_count"] == 1
    assert out["awaiting_advance"][0]["run_id"] == "decided-then-consent"
    assert out["parked_count"] == 0


def test_doctor_y_then_nudge_latest_wins_stays_parked(tmp_path: Path) -> None:
    """A `y` followed by a later SAME-boundary nudge → the nudge supersedes the `y` → still
    awaiting the human (F13: matches the Stop guard + driver via the shared boundary scan)."""
    now = "2026-07-03T01:00:00+00:00"
    upsert_run(tmp_path, _record("reopened"))
    stamp_tick(
        "reopened",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    _park(tmp_path, "reopened")
    _commit_y(tmp_path, "reopened")
    _commit_nudge(tmp_path, "reopened")

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now))

    assert out["awaiting_advance_count"] == 0
    assert out["parked_count"] == 1
    assert out["parked"][0]["run_id"] == "reopened"


def test_doctor_parked_without_any_decision_stays_awaiting_human(tmp_path: Path) -> None:
    """Nothing committed yet → parked note, never awaiting_advance (the existing
    parked path is unchanged for a run with no decision journal)."""
    now = "2026-07-03T01:00:00+00:00"
    upsert_run(tmp_path, _record("waiting"))
    stamp_tick(
        "waiting",
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=tmp_path,
    )
    _park(tmp_path, "waiting")

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now=now))

    assert out["awaiting_advance_count"] == 0
    assert out["parked_count"] == 1
    assert out["parked"][0]["run_id"] == "waiting"


# ─── version_skew: content-keyed code identity ──────────────────────────────


def test_doctor_version_skew_fires_on_mismatched_shas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Synthetic skew: CLI embeds one sha, the source repo is at another →
    doctor emits the warning naming BOTH shas and the fix (reinstall)."""
    import hpc_agent.ops.recover.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "runtime_sha", lambda: "aaaa1111")
    monkeypatch.setattr(
        doctor_mod, "_resolve_source_repo", lambda _dir: (str(tmp_path), "bbbb2222")
    )

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))

    skew = out["version_skew"]
    assert skew is not None
    assert skew["cli_sha"] == "aaaa1111"
    assert skew["repo_sha"] == "bbbb2222"
    assert skew["repo_root"] == str(tmp_path)
    from hpc_agent import __version__

    assert skew["cli_version"].split("+", 1)[0] == __version__
    # The warning line names both shas and the fix.
    assert "aaaa1111" in skew["warning"]
    assert "bbbb2222" in skew["warning"]
    assert "reinstall" in skew["warning"].lower()


def test_doctor_version_skew_silent_when_shas_agree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hpc_agent.ops.recover.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "runtime_sha", lambda: "aaaa1111")
    monkeypatch.setattr(
        doctor_mod, "_resolve_source_repo", lambda _dir: (str(tmp_path), "aaaa1111")
    )
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))
    assert out["version_skew"] is None


def test_doctor_version_skew_tolerates_short_sha_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git may widen a short sha on collision — prefix agreement is agreement."""
    import hpc_agent.ops.recover.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "runtime_sha", lambda: "aaaa1111")
    monkeypatch.setattr(
        doctor_mod, "_resolve_source_repo", lambda _dir: (str(tmp_path), "aaaa1111ffff")
    )
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))
    assert out["version_skew"] is None


def test_doctor_version_skew_fails_open_without_cli_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No embedded/resolvable CLI sha (old wheel, no git) → silently skipped."""
    import hpc_agent.ops.recover.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "runtime_sha", lambda: None)
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))
    assert out["version_skew"] is None


def test_doctor_version_skew_fails_open_without_git_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No git binary at all → _resolve_source_repo yields None → field is null."""
    import hpc_agent._build_info as bi
    import hpc_agent.ops.recover.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "runtime_sha", lambda: "aaaa1111")

    def _missing(*a: object, **k: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(bi.subprocess, "run", _missing)
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))
    assert out["version_skew"] is None


def test_doctor_version_skew_skipped_for_non_hpc_agent_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """experiment_dir in SOME git repo that isn't hpc-agent's source → skip."""
    import hpc_agent.ops.recover.doctor as doctor_mod

    monkeypatch.setattr(doctor_mod, "runtime_sha", lambda: "aaaa1111")
    # git resolves a toplevel, but that root lacks src/hpc_agent/__init__.py —
    # a repo that merely USES hpc-agent must never trigger the comparison.
    monkeypatch.setattr(doctor_mod, "git_output", lambda *a, **k: str(tmp_path))
    assert doctor_mod._resolve_source_repo(tmp_path) is None
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))
    assert out["version_skew"] is None


# ─── alert delivery + needs-attention shape (proving run #3) ────────────────


def _write_alert(exp: Path, ts: str, message: str) -> Path:
    from hpc_agent.state.run_record import journal_dir

    log = journal_dir(exp) / "doctor.alerts.log"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(f"{ts} {message}\n")
    return log


def _stall(exp: Path, run_id: str) -> None:
    upsert_run(exp, _record(run_id))
    stamp_tick(
        run_id,
        last_tick_at="2026-07-03T00:00:00+00:00",
        next_tick_due="2026-07-03T00:00:00+00:00",
        experiment_dir=exp,
    )


def test_doctor_envelope_carries_unacknowledged_alerts(tmp_path: Path) -> None:
    """Regression (proving run #3): the alerts the watchdog wrote to
    doctor.alerts.log ride the doctor envelope itself — read-only, never
    acknowledged by doctor (the status snapshot's watermark owns that)."""
    ts = "2026-07-04T23:25:05+00:00"
    msg = "hpc-agent doctor: driver stalled since 23:01, run pi-estimation-canary — re-arm?"
    _write_alert(tmp_path, ts, msg)

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-05T01:00:00+00:00"))
    assert out["alerts"] == [{"ts": ts, "message": msg}]
    # A second doctor run still carries them — doctor never moves the watermark.
    again = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-05T01:00:00+00:00"))
    assert again["alerts"] == [{"ts": ts, "message": msg}]
    # The one-line digest names the pending alert(s) even with nothing stalled NOW.
    assert "1 unacknowledged alert(s)" in again["attention_summary"]
    assert again["needs_attention"] is False


def test_doctor_stalled_driver_is_unmistakable_at_top_level(tmp_path: Path) -> None:
    """A stalled driver flips needs_attention and leads the attention_summary —
    never buried under the per-run lists."""
    _stall(tmp_path, "stalled")
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))
    assert out["needs_attention"] is True
    assert out["attention_summary"].startswith("NEEDS ATTENTION")
    assert "1 stalled driver(s)" in out["attention_summary"]


def test_doctor_all_clear_summary_when_healthy(tmp_path: Path) -> None:
    upsert_run(tmp_path, _record("healthy"))
    stamp_tick(
        "healthy",
        last_tick_at="2026-07-03T00:59:00+00:00",
        next_tick_due="2026-07-03T02:00:00+00:00",
        experiment_dir=tmp_path,
    )
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))
    assert out["needs_attention"] is False
    assert "all clear" in out["attention_summary"]
    assert out["alerts"] == []


def test_doctor_alerts_fail_open_on_corrupt_log(tmp_path: Path) -> None:
    """A garbage alerts log yields no alerts and never an error."""
    _write_alert(tmp_path, "not-a-timestamp", "junk")
    log = _write_alert(tmp_path, "\x00\x01", "more junk")
    log.write_bytes(log.read_bytes() + b"\xff\xfe raw bytes no newline")

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))
    assert out["alerts"] == []
    assert out["needs_attention"] is False


# ─── env-echo disclosure (run-12 finding 24 addendum) ────────────────────────


def test_doctor_echoes_hpc_env_overrides(tmp_path: Path, monkeypatch) -> None:
    """Every HPC_* env var is echoed verbatim in the brief — the env-vs-record
    drift seat (HPC_SSH_ENGINE sat exported for days contradicting the session
    record, invisible to every surface). Disclosure only, never judged."""
    monkeypatch.setenv("HPC_SSH_ENGINE", "asyncssh")
    monkeypatch.setenv("HPC_SSH_TIMEOUT_SEC", "1800")

    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))

    echoed = out["active_env_overrides"]
    assert echoed["HPC_SSH_ENGINE"] == "asyncssh"
    assert echoed["HPC_SSH_TIMEOUT_SEC"] == "1800"
    assert all(k.startswith("HPC_") for k in echoed)


def test_doctor_env_echo_empty_when_unset(tmp_path: Path, monkeypatch) -> None:
    for key in [k for k in __import__("os").environ if k.startswith("HPC_")]:
        monkeypatch.delenv(key, raising=False)
    out = doctor(experiment_dir=tmp_path, spec=DoctorSpec(now="2026-07-03T01:00:00+00:00"))
    assert out["active_env_overrides"] == {}
