"""Behaviour-pinning coverage for the RECORD substrate + the data-identity leg.

Companion to the landed ``test_journal_coverage.py`` / ``test_index_coverage.py``
(the journal/index READERS got batteries; the record layer + the sidecar
write/stamp discipline it sits under did not). The 2026-07-17 mutation triage
found covered-but-UNASSERTED logic on the record substrate: a boundary / default
/ operator / return mutation on :mod:`hpc_agent.state.run_record` (the RunRecord
round-trip, the ``_run_path``/``_lock_path`` layout, the forked-namespace probe)
and on the record-adjacent seams of :mod:`hpc_agent.state.runs` (the v1→v2
backfill's non-clobber discipline, the additive provenance stamp, the identity
reducers) would survive the suite.

The record IS the provenance a stranger reads to re-derive the citable table, so
a silent record bug is a silent reproducibility failure. Each test below adds an
assertion that KILLS a specific surviving mutant; the docstring names it. Stamp
paths already batteried elsewhere (``stamp_run_sidecar_env_lock`` in
``test_env_lock.py``, ``stamp_run_sidecar_hw_facts`` in ``test_hw_facts.py``,
``update_run_sidecar_job_ids`` in ``test_runs_sidecar_v2.py``) are NOT
duplicated here — this file covers the GAPS.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import (
    SCHEMA_VERSION,
    RunRecord,
    _lock_path,
    _namespace_run_count,
    _run_path,
    detect_forked_namespace,
)
from hpc_agent.state.runs import (
    DEFAULT_SUMMARY_ARTIFACT,
    backfill_run_sidecar_provenance,
    read_run_sidecar,
    resolved_summary_artifact,
    run_sidecar_path,
    sidecar_effective_identity,
    write_run_sidecar,
)

if TYPE_CHECKING:
    from pathlib import Path


def _record(run_id: str, experiment_dir: Path, **overrides: object) -> RunRecord:
    base: dict = {
        "run_id": run_id,
        "profile": "p",
        "cluster": "c",
        "ssh_target": "user@h",
        "remote_path": "/remote",
        "job_name": "j",
        "job_ids": ["100"],
        "total_tasks": 4,
        "submitted_at": "2026-01-01T00:00:00+00:00",
        "experiment_dir": str(experiment_dir),
    }
    base.update(overrides)
    return RunRecord(**base)  # type: ignore[arg-type]


def _sidecar_kwargs(run_id: str = "20260101-000000-deadbee") -> dict:
    return dict(
        run_id=run_id,
        cmd_sha="0" * 64,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 run.py",
        result_dir_template="results/{seed}",
        task_count=4,
        tasks_py_sha="1" * 64,
    )


# ── RunRecord.from_dict / to_dict: filtering + round-trip + backfill defaults ──


def test_from_dict_drops_unknown_fields(tmp_path: Path) -> None:
    """A payload carrying a key NOT on the dataclass is silently filtered, not fed
    to ``cls(**payload)`` (which would raise TypeError). Kills a mutant that drops
    the ``if k in known`` comprehension guard, and pins the forward-compat contract
    that lets a NEWER writer's extra field load on an OLDER reader."""
    payload = {
        **_record("r1", tmp_path).to_dict(),
        "a_field_from_the_future": {"nested": 1},
        "another_unknown": 42,
    }
    rec = RunRecord.from_dict(payload)
    assert rec.run_id == "r1"
    assert not hasattr(rec, "a_field_from_the_future")
    assert "a_field_from_the_future" not in rec.to_dict()
    assert "another_unknown" not in rec.to_dict()


def test_to_dict_from_dict_full_roundtrip_including_lineage(tmp_path: Path) -> None:
    """A record with the supersession/lineage + attempt fields populated survives
    ``to_dict`` → ``from_dict`` byte-for-byte. Kills any mutant that drops one of
    these fields from the asdict/round-trip (each is durable audit evidence)."""
    rec = _record(
        "r1",
        tmp_path,
        supersedes="20260101-000000-older00",
        superseded_by="20260101-000000-newer00",
        superseded_at="2026-01-02T00:00:00+00:00",
        pending_closure={"job_ids": ["9"], "reason": "no cancel affordance"},
        verdict_history=[{"decided_by": "code", "chosen": "resubmit"}],
        attempt=3,
        auto_resume_on_kill=True,
        max_auto_resumes=5,
    )
    round_tripped = RunRecord.from_dict(rec.to_dict())
    assert round_tripped == rec
    assert round_tripped.to_dict() == rec.to_dict()
    # And the lineage fields specifically survived (not silently reset to defaults).
    assert round_tripped.supersedes == "20260101-000000-older00"
    assert round_tripped.superseded_by == "20260101-000000-newer00"
    assert round_tripped.superseded_at == "2026-01-02T00:00:00+00:00"
    assert round_tripped.pending_closure == {"job_ids": ["9"], "reason": "no cancel affordance"}
    assert round_tripped.attempt == 3


def test_from_dict_backfills_optional_defaults_for_old_record(tmp_path: Path) -> None:
    """An OLD record (only the required fields present) loads with every optional
    field at its DEFAULT — backfill-only: an absent field reads its default, a new
    field never leaks a non-default onto an old record. Kills mutants that flip a
    default literal (e.g. ``max_auto_resumes``/``max_auto_recovers`` 2→other,
    ``auto_resume_on_kill`` False→True, ``attempt`` 0→other, ``stage``/``status``
    literals) — the zero-blast-radius #294 safety posture."""
    minimal = {
        "run_id": "old1",
        "profile": "p",
        "cluster": "c",
        "ssh_target": "u@h",
        "remote_path": "/r",
        "job_name": "j",
        "job_ids": ["1"],
        "total_tasks": 4,
        "submitted_at": "2024-01-01T00:00:00+00:00",
        "experiment_dir": str(tmp_path),
    }
    rec = RunRecord.from_dict(minimal)
    assert rec.stage == "monitor"
    assert rec.status == "in_flight"
    assert rec.auto_resume_on_kill is False
    assert rec.auto_recover_on_failure is False
    assert rec.max_auto_resumes == 2
    assert rec.max_auto_recovers == 2
    assert rec.auto_resume_count == 0
    assert rec.attempt == 0
    assert rec.supersedes == ""
    assert rec.superseded_by == ""
    assert rec.superseded_at is None
    assert rec.pending_closure == {}
    assert rec.verdict_history == []
    assert rec.schema_version == SCHEMA_VERSION


def test_from_dict_uses_current_schema_version_when_absent(tmp_path: Path) -> None:
    """A payload without ``schema_version`` gets the module's SCHEMA_VERSION default
    (from the dataclass field), not None/0. Kills a mutant that changes the field's
    default away from ``SCHEMA_VERSION``."""
    payload = {k: v for k, v in _record("r1", tmp_path).to_dict().items() if k != "schema_version"}
    assert "schema_version" not in payload
    assert RunRecord.from_dict(payload).schema_version == SCHEMA_VERSION


# ── _run_path / _lock_path derivation ─────────────────────────────────────────


def test_lock_path_appends_lock_to_full_suffix(tmp_path: Path) -> None:
    """``_lock_path`` appends ``.lock`` to the FULL existing suffix — a sidecar
    ``r.json`` locks on ``r.json.lock``, never ``r.lock`` (which would collide two
    distinct targets sharing a stem onto one lock). Kills a mutant that swaps the
    suffix instead of appending."""
    assert _lock_path(tmp_path / "r.json").name == "r.json.lock"
    assert _lock_path(tmp_path / "index.json").name == "index.json.lock"
    # No-suffix target still gets a distinct lock sibling.
    assert _lock_path(tmp_path / "plain").name == "plain.lock"


def test_run_path_is_run_id_json_under_journal_runs(journal_home: Path, tmp_path: Path) -> None:
    """A run record lands at ``<journal namespace>/runs/<run_id>.json`` — the
    ``.json`` extension and the ``runs`` leaf are load-bearing (readers key on
    both). Kills a mutant that drops the extension or the runs-dir component."""
    exp = tmp_path / "exp"
    exp.mkdir()
    p = _run_path(exp, "20260101-000000-deadbee")
    assert p.name == "20260101-000000-deadbee.json"
    assert p.parent.name == "runs"


# ── detect_forked_namespace: the two guards the existing tests don't isolate ───


def test_forked_namespace_none_when_current_populated(journal_home: Path, tmp_path: Path) -> None:
    """A fork is reported ONLY when THIS dir has no live journal (renamed-away
    signature). A populated current namespace is the normal case — even with a
    genuinely orphaned sibling on disk, the probe must return None. Kills the
    ``_namespace_run_count(current_root) > 0 -> return None`` early-out (dropping
    it would surface a false fork for every busy experiment)."""
    # Orphaned sibling: a run under old_dir, then old_dir renamed away.
    old_dir = tmp_path / "exp1"
    old_dir.mkdir()
    upsert_run(old_dir, _record("run_old00001", old_dir))
    new_dir = tmp_path / "exp1-v2"
    old_dir.rename(new_dir)
    # Now ALSO populate the CURRENT (new_dir) namespace — it is no longer empty.
    upsert_run(new_dir, _record("run_new00001", new_dir))
    assert detect_forked_namespace(new_dir) is None


def test_forked_namespace_skips_sibling_whose_dir_still_exists(
    journal_home: Path, tmp_path: Path
) -> None:
    """A sibling namespace is a FORK only when its recorded ``experiment_dir`` is
    GONE (renamed). A sibling whose dir STILL EXISTS is a live, unrelated
    experiment — never reported as this dir's fork. Kills the
    ``if Path(prior_dir).exists(): continue`` guard (dropping it would claim every
    other experiment's namespace as a fork of an empty current dir)."""
    other = tmp_path / "other-live"
    other.mkdir()
    upsert_run(other, _record("run_other0001", other))  # populated, dir exists

    empty = tmp_path / "fresh"
    empty.mkdir()  # no journal records under this hash
    assert detect_forked_namespace(empty) is None


def test_namespace_run_count_excludes_last_status_snapshots(
    journal_home: Path, tmp_path: Path
) -> None:
    """The fork ``run_count`` counts run RECORDS, not the ``<id>.last_status.json``
    cache snapshots co-located in ``runs/``. Kills a mutant that drops the
    ``.last_status.json`` endswith exclusion (which would inflate the count and
    flip an empty-but-snapshotted namespace to 'populated')."""
    exp = tmp_path / "exp"
    exp.mkdir()
    upsert_run(exp, _record("20260101-000000-real0001", exp))
    # Drop a cache snapshot beside the real record (what the monitor tick writes).
    from hpc_agent.state.run_record import journal_dir

    runs = journal_dir(exp) / "runs"
    (runs / "20260101-000000-real0001.last_status.json").write_text("{}", encoding="utf-8")
    assert _namespace_run_count(journal_dir(exp)) == 1
    # A namespace with ONLY a snapshot (no record) counts as empty.
    (runs / "20260101-000000-ghost002.last_status.json").write_text("{}", encoding="utf-8")
    assert _namespace_run_count(journal_dir(exp)) == 1


# ── runs.py: v1→v2 backfill is setdefault (never clobbers a recorded value) ────


def test_read_backfill_does_not_overwrite_a_recorded_provenance_value(tmp_path: Path) -> None:
    """The v1→v2 read backfill is ``setdefault`` — a v1 sidecar that DID record a
    field keeps its value; only ABSENT keys are filled to None. Kills a mutant that
    turns the backfill into an unconditional assignment (which would erase a real
    ``data_sha`` recorded by an intermediate writer)."""
    run_id = "20240101-000000-legacy00"
    target = run_sidecar_path(tmp_path, run_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "sidecar_schema_version": 1,
                "run_id": run_id,
                "cmd_sha": "a" * 64,
                "hpc_agent_version": "0.2.0",
                "submitted_at": "2024-01-01T00:00:00Z",
                "executor": "python3 old.py",
                "result_dir_template": "out/{seed}",
                "task_count": 1,
                "tasks_py_sha": "b" * 64,
                "data_sha": "already-recorded-sha",  # present on the old sidecar
            }
        ),
        encoding="utf-8",
    )
    data = read_run_sidecar(tmp_path, run_id)
    assert data["data_sha"] == "already-recorded-sha"  # NOT clobbered to None
    # A sibling key that WAS absent is filled to None (the backfill still fires).
    assert data["env_hash"] is None
    assert data["hw_sha"] is None
    assert data["env_lock_status"] is None


# ── backfill_run_sidecar_provenance: additive, never-clobbering (untested unit) ─


def test_backfill_provenance_fills_null_fields(tmp_path: Path) -> None:
    """The #312 capture seam fills the null provenance legs on an existing sidecar."""
    write_run_sidecar(tmp_path, **_sidecar_kwargs())
    rid = _sidecar_kwargs()["run_id"]
    backfill_run_sidecar_provenance(
        tmp_path,
        rid,
        data_sha="d-sha",
        env_hash="e-sha",
        data_manifest_sha="m-sha",
    )
    data = read_run_sidecar(tmp_path, rid)
    assert data["data_sha"] == "d-sha"
    assert data["env_hash"] == "e-sha"
    assert data["data_manifest_sha"] == "m-sha"


def test_backfill_provenance_never_overwrites_a_recorded_value(tmp_path: Path) -> None:
    """Strictly additive: a field already recorded on the sidecar is NEVER
    overwritten (the write-first #148/#150 invariant — a pre-written provenance
    value is durable). Kills the ``existing.get(field) is None`` non-clobber guard;
    without it a later backfill would silently rewrite recorded provenance."""
    write_run_sidecar(tmp_path, **_sidecar_kwargs(), data_sha="original-data-sha")
    rid = _sidecar_kwargs()["run_id"]
    backfill_run_sidecar_provenance(
        tmp_path,
        rid,
        data_sha="attempted-overwrite",
        env_hash="fresh-env",
        data_manifest_sha=None,
    )
    data = read_run_sidecar(tmp_path, rid)
    assert data["data_sha"] == "original-data-sha"  # untouched
    assert data["env_hash"] == "fresh-env"  # the null leg WAS filled


def test_backfill_provenance_skips_none_supplied_values(tmp_path: Path) -> None:
    """A ``None`` supplied value writes NOTHING — a could-not-compute leg leaves the
    sidecar's null in place (no ``"env_hash": null`` key materialized as a real
    value flip). Kills the ``value is not None`` guard."""
    write_run_sidecar(tmp_path, **_sidecar_kwargs())
    rid = _sidecar_kwargs()["run_id"]
    backfill_run_sidecar_provenance(
        tmp_path, rid, data_sha="only-data", env_hash=None, data_manifest_sha=None
    )
    raw = json.loads(run_sidecar_path(tmp_path, rid).read_text(encoding="utf-8"))
    assert raw["data_sha"] == "only-data"
    # env_hash / data_manifest_sha were None → not written into the compact JSON.
    assert "env_hash" not in raw
    assert "data_manifest_sha" not in raw


def test_backfill_provenance_raises_when_sidecar_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        backfill_run_sidecar_provenance(
            tmp_path, "20260101-000000-nope0000", data_sha="d", env_hash="e"
        )


# ── resolved_summary_artifact: the pure reducer's absent/blank/strip edges ─────


def test_resolved_summary_artifact_defaults_and_strips(tmp_path: Path) -> None:
    """The ONE 'which file marks a task summary' reader defaults to ``metrics.json``
    for a None/non-dict/absent/blank value and STRIPS a declared name. Kills the
    ``isinstance(sidecar, dict)`` guard, the ``.strip()`` blank check, and the
    strip-on-return — any of which would mis-route the reducer's completion count."""
    assert resolved_summary_artifact(None) == DEFAULT_SUMMARY_ARTIFACT
    assert resolved_summary_artifact("not-a-dict") == DEFAULT_SUMMARY_ARTIFACT  # type: ignore[arg-type]
    assert resolved_summary_artifact({}) == DEFAULT_SUMMARY_ARTIFACT
    assert resolved_summary_artifact({"summary_artifact": None}) == DEFAULT_SUMMARY_ARTIFACT
    assert resolved_summary_artifact({"summary_artifact": ""}) == DEFAULT_SUMMARY_ARTIFACT
    assert resolved_summary_artifact({"summary_artifact": "   "}) == DEFAULT_SUMMARY_ARTIFACT
    assert resolved_summary_artifact({"summary_artifact": " results.json "}) == "results.json"


# ── sidecar_effective_identity: node_sha preferred, cmd_sha fallback, else None ─


def test_sidecar_effective_identity_prefers_node_sha_then_cmd_sha() -> None:
    """The ONE dedup-identity definition: ``node_sha`` when present (params +
    ancestry), else the bare ``cmd_sha`` (0-parent degeneracy), else None (identity
    unknown → never a match). Kills the ``or`` precedence flip and the None guard —
    a swap would dedup a DAG-parented run against a bare-cmd_sha stranger."""
    assert sidecar_effective_identity({"node_sha": "n", "cmd_sha": "c"}) == "n"
    assert sidecar_effective_identity({"cmd_sha": "c"}) == "c"
    # Empty node_sha falls through to cmd_sha (not '' asserted as an identity).
    assert sidecar_effective_identity({"node_sha": "", "cmd_sha": "c"}) == "c"
    assert sidecar_effective_identity({}) is None
    assert sidecar_effective_identity({"node_sha": "", "cmd_sha": ""}) is None
