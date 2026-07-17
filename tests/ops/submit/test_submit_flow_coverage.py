"""Behaviour-pinning mutation coverage for ``ops/submit_flow.py``'s decision
boundaries — the seams the curated harness never got verdicts on.

Every assertion below names the mutation it kills. The file deliberately does
NOT re-pin what the landed suites already cover:

* ``_resolve_layer1`` in isolation (``test_layer1_dedup.py``);
* the U3 submit-once live flip (``test_submit_once_flip.py``);
* ``_canary_decision`` order + skip reasons (``test_submit_flow_canary_skip.py``);
* ``_smoke_one_executor`` / ``_module_shipped_in_repo`` / ``_missing_top_level_module``
  (``test_submit_flow_pre_stage_smoke.py``);
* ``_is_runnable_executor`` (``test_executor_env_guards.py``);
* ``_enforce_array_cap`` / the cap helpers (``test_array_cap_guard.py``);
* the F47/F48 *integration* through ``submit_flow_batch`` (``test_flow.py``);
* the canary crash-window guard (``test_canary_crash_window_guard.py``).

The GAPS covered here:

* ``_dedup_existing`` — the *consumption* of ``_resolve_layer1``: the front-door
  routing table (submitting → RECONCILE-raise, cross-cluster in_flight →
  REFUSE-raise, complete/in_flight → deduped envelope, failed/abandoned →
  proceed, no-journal → F47) asserted by calling the function DIRECTLY, not
  through the batch wrapper.
* ``_refuse_prestamped_without_journal`` — the F47 MAIN-array guard in isolation
  (only integration-covered before), mirroring the canary variant's unit file:
  the landed-vs-empty-ids discrimination.
* ``_pre_stage_smoke_gate`` — the gate-level discrimination (distinct-executor
  dedup, non-runnable skip, template-less skip, unreadable-sidecar skip) — the
  logic AROUND ``_smoke_one_executor``, which the pre-stage-smoke suite pins on
  its own.
* ``_submit_one_spec`` canary ladder — the existing-canary REPLAY vs FRESH-fire
  boundary (#276: a terminal-failure canary corpse is NOT reused; a live
  in_flight canary IS).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.ops import submit_flow as sf
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import (
    run_sidecar_path,
    update_run_sidecar_job_ids,
    write_run_sidecar,
)

if TYPE_CHECKING:
    from pathlib import Path


# --------------------------------------------------------------------------- #
# Shared builders.
# --------------------------------------------------------------------------- #


def _spec(run_id: str = "r0", *, cluster: str = "c", **over: Any) -> SubmitFlowSpec:
    base: dict[str, Any] = dict(
        profile="p",
        cluster=cluster,
        ssh_target="u@h",
        remote_path="/r",
        job_name=run_id,
        run_id=run_id,
        total_tasks=100,
        backend="sge",
        script="run.sh",
        job_env={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py", "HPC_CMD_SHA": "sha-abc"},
        result_dir_template="results/{run_id}/task_{task_id}",
    )
    base.update(over)
    return SubmitFlowSpec(**base)


def _seed_record(
    exp: Path,
    run_id: str,
    *,
    status: str,
    cluster: str = "c",
    job_ids: list[str] | None = None,
    total_tasks: int = 100,
) -> RunRecord:
    rec = RunRecord(
        run_id=run_id,
        profile="p",
        cluster=cluster,
        ssh_target="u@h",
        remote_path="/r",
        job_name=run_id,
        job_ids=list(job_ids) if job_ids is not None else ["J1", "J2"],
        total_tasks=total_tasks,
        submitted_at="2026-01-01T00:00:00+00:00",
        experiment_dir=str(exp.resolve()),
        status=status,
    )
    upsert_run(exp, rec)
    return rec


def _write_sidecar(exp: Path, run_id: str, *, executor: str, template: str | None) -> None:
    """Write a minimal per-run sidecar directly (the gate/guards read it back)."""
    path = run_sidecar_path(exp, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {"executor": executor}
    if template is not None:
        doc["result_dir_template"] = template
    path.write_text(json.dumps(doc), encoding="utf-8")


# =========================================================================== #
# Group A — _dedup_existing: the front-door routing table (consumption of
# _resolve_layer1). Called DIRECTLY so each branch is pinned without the batch
# prelude. The layer-1 predicate is pinned elsewhere; here it is the ROUTING of
# each verdict onto a raise / deduped-envelope / proceed(None).
# =========================================================================== #


class TestDedupExistingRouting:
    def test_submitting_record_raises_route_to_reconcile(
        self, tmp_path: Path, journal_home: Path
    ) -> None:
        """RECONCILE branch: a live ``submitting`` record (a prior submit orphaned
        in its dispatch->id window) must RAISE and route to reconcile-recovery, not
        blind-dedup. Kills a mutation dropping the ``decision.action == _RECONCILE``
        arm (which would fall through to a bogus deduped result with empty ids)."""
        _seed_record(tmp_path, "r0", status="submitting", job_ids=[])
        with pytest.raises(errors.SpecInvalid) as ei:
            sf._dedup_existing(tmp_path, _spec("r0"))
        msg = str(ei.value)
        assert "submitting" in msg
        assert "dispatch" in msg  # names the dispatch->id window / reconcile route

    def test_complete_record_returns_deduped_envelope(
        self, tmp_path: Path, journal_home: Path
    ) -> None:
        """DEDUP branch: a ``complete`` record replays as ``deduped=True`` carrying
        the RECORDED job_ids + total_tasks and ``canary_done=False``. Kills a
        mutation that returns None (would re-submit a finished run) or mis-copies
        the envelope fields."""
        _seed_record(tmp_path, "r0", status="complete", job_ids=["777", "778"], total_tasks=42)
        res = sf._dedup_existing(tmp_path, _spec("r0"))
        assert res is not None
        assert res.deduped is True
        assert res.job_ids == ["777", "778"]  # from the existing record, not the spec
        assert res.total_tasks == 42
        assert res.canary_done is False
        assert res.canary_run_id is None

    def test_in_flight_same_cluster_returns_deduped_envelope(
        self, tmp_path: Path, journal_home: Path
    ) -> None:
        """DEDUP branch (the live-run arm): an ``in_flight`` record on the SAME
        cluster dedups — never a second concurrent array. Kills a mutation that
        would let a live run fall through to a duplicate submit."""
        _seed_record(tmp_path, "r0", status="in_flight", cluster="c", job_ids=["55"])
        res = sf._dedup_existing(tmp_path, _spec("r0", cluster="c"))
        assert res is not None and res.deduped is True
        assert res.job_ids == ["55"]

    def test_failed_terminal_record_proceeds(self, tmp_path: Path, journal_home: Path) -> None:
        """PROCEED branch (#276): a ``failed`` corpse is resubmittable-terminal — it
        must NOT wedge future submits, so _dedup_existing returns None (fall through
        to a fresh submit). Kills a mutation that dedups against a dead run."""
        _seed_record(tmp_path, "r0", status="failed", job_ids=["dead"])
        assert sf._dedup_existing(tmp_path, _spec("r0")) is None

    def test_abandoned_terminal_record_proceeds(self, tmp_path: Path, journal_home: Path) -> None:
        """PROCEED branch (#276): ``abandoned`` is likewise resubmittable-terminal —
        a single status-probe flake must not permanently block the run_id."""
        _seed_record(tmp_path, "r0", status="abandoned", job_ids=["dead"])
        assert sf._dedup_existing(tmp_path, _spec("r0")) is None

    def test_in_flight_cross_cluster_refuses(self, tmp_path: Path, journal_home: Path) -> None:
        """REFUSE branch (proving run #5 / F48): a live run on cluster A re-submitted
        at cluster B must RAISE naming both clusters — deduping would silently
        re-attach to A's live run while nothing runs on B (run_id keys on params
        only). Kills a mutation dropping the ``decision.action == _REFUSE`` arm."""
        _seed_record(tmp_path, "r0", status="in_flight", cluster="discovery")
        with pytest.raises(errors.SpecInvalid) as ei:
            sf._dedup_existing(tmp_path, _spec("r0", cluster="hoffman2"))
        msg = str(ei.value)
        assert "discovery" in msg and "hoffman2" in msg
        assert "two clusters" in msg

    def test_no_journal_jobless_sidecar_proceeds(self, tmp_path: Path, journal_home: Path) -> None:
        """No journal record + a JOBLESS sidecar (the ordinary pre-qsub /
        two-phase-canary S3 window) is not a crash orphan → return None (proceed).
        Kills a mutation that makes the F47 guard fire on empty ids."""
        write_run_sidecar(
            tmp_path,
            run_id="r0",
            cmd_sha="",
            hpc_agent_version="",
            submitted_at="2026-01-01T00:00:00+00:00",
            executor="python run.py",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=4,
            tasks_py_sha="",
        )
        assert sf._dedup_existing(tmp_path, _spec("r0")) is None

    def test_no_journal_landed_ids_sidecar_refuses(
        self, tmp_path: Path, journal_home: Path
    ) -> None:
        """No journal record + a sidecar carrying LANDED job_ids (the F47 crash
        window) must RAISE through _dedup_existing → _refuse_prestamped_without_
        journal. Kills a mutation that skips the guard on the ``existing is None``
        arm (a re-qsub over live arrays)."""
        write_run_sidecar(
            tmp_path,
            run_id="r0",
            cmd_sha="",
            hpc_agent_version="",
            submitted_at="2026-01-01T00:00:00+00:00",
            executor="python run.py",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=4,
            tasks_py_sha="",
        )
        update_run_sidecar_job_ids(tmp_path, "r0", ["13610902"])
        with pytest.raises(errors.SpecInvalid) as ei:
            sf._dedup_existing(tmp_path, _spec("r0"))
        assert "13610902" in str(ei.value)


# =========================================================================== #
# Group B — _refuse_prestamped_without_journal: the F47 MAIN-array guard in
# ISOLATION (only integration-covered before). Mirrors the canary variant's
# unit file. The load-bearing discrimination: landed ids vs empty/absent.
# =========================================================================== #


class TestRefusePrestampedWithoutJournal:
    def test_landed_ids_refuse_and_name_them(self, tmp_path: Path, journal_home: Path) -> None:
        """Sidecar carries landed job_ids → refuse, naming EVERY id + the recovery
        path. Kills a mutation that inverts the ``if not landed: return`` gate or
        drops the ids from the message."""
        write_run_sidecar(
            tmp_path,
            run_id="r0",
            cmd_sha="",
            hpc_agent_version="",
            submitted_at="2026-01-01T00:00:00+00:00",
            executor="python run.py",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=4,
            tasks_py_sha="",
        )
        update_run_sidecar_job_ids(tmp_path, "r0", ["A1", "A2"])
        with pytest.raises(errors.SpecInvalid) as ei:
            sf._refuse_prestamped_without_journal(tmp_path, _spec("r0"))
        msg = str(ei.value)
        assert "A1" in msg and "A2" in msg
        assert "Reconcile" in msg  # the named recovery path

    def test_empty_ids_sidecar_is_silent(self, tmp_path: Path, journal_home: Path) -> None:
        """A jobless sidecar is NOT the crash window → the guard is a clean no-op.
        Kills a mutation that refuses on an empty ids list (would break every
        ordinary pre-qsub / S3 submit)."""
        write_run_sidecar(
            tmp_path,
            run_id="r0",
            cmd_sha="",
            hpc_agent_version="",
            submitted_at="2026-01-01T00:00:00+00:00",
            executor="python run.py",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=4,
            tasks_py_sha="",
        )
        # No update_run_sidecar_job_ids → job_ids empty. Must not raise.
        sf._refuse_prestamped_without_journal(tmp_path, _spec("r0"))

    def test_absent_sidecar_is_silent(self, tmp_path: Path, journal_home: Path) -> None:
        """No sidecar at all → read_run_sidecar_or_empty yields {} → no-op. Kills a
        mutation that treats an absent sidecar as landed."""
        sf._refuse_prestamped_without_journal(tmp_path, _spec("r0"))


# =========================================================================== #
# Group C — _pre_stage_smoke_gate: the gate-level discrimination AROUND
# _smoke_one_executor (which the pre-stage-smoke suite pins on its own). Here:
# the distinct-executor dedup, the non-runnable skip, the template-less skip,
# and the unreadable-sidecar skip. _smoke_one_executor is mocked and counted.
# =========================================================================== #


class TestPreStageSmokeGateDiscrimination:
    def test_same_executor_across_specs_smokes_once(self, tmp_path: Path) -> None:
        """The gate dedups on the executor STRING: two fresh specs sharing one
        executor smoke it ONCE (a campaign fan-out stays bounded). Kills a mutation
        dropping the ``if str(executor) in seen: continue`` dedup."""
        for rid in ("r0", "r1"):
            _write_sidecar(tmp_path, rid, executor="python shared.py", template="results/{seed}")
        specs = [_spec("r0"), _spec("r1")]
        with mock.patch.object(sf, "_smoke_one_executor") as smoke:
            sf._pre_stage_smoke_gate(tmp_path, specs, [0, 1])
        assert smoke.call_count == 1  # one DISTINCT executor → one smoke

    def test_distinct_executors_smoke_each(self, tmp_path: Path) -> None:
        """Contrast/boundary: two DIFFERENT executor strings smoke TWICE — the dedup
        keys on the string, it does not collapse everything to one. Kills a mutation
        that smokes only the first spec unconditionally."""
        _write_sidecar(tmp_path, "r0", executor="python a.py", template="results/{seed}")
        _write_sidecar(tmp_path, "r1", executor="python b.py", template="results/{seed}")
        specs = [_spec("r0"), _spec("r1")]
        with mock.patch.object(sf, "_smoke_one_executor") as smoke:
            sf._pre_stage_smoke_gate(tmp_path, specs, [0, 1])
        assert smoke.call_count == 2

    def test_non_runnable_executor_is_skipped(self, tmp_path: Path) -> None:
        """A sidecar whose executor is non-runnable (a bare script token — the
        sidecar guards own that refusal) contributes NO smoke; the gate is
        best-effort defense-in-depth. Kills a mutation dropping the
        ``not _is_runnable_executor(executor)`` skip."""
        _write_sidecar(tmp_path, "r0", executor="train.py", template="results/{seed}")
        with mock.patch.object(sf, "_smoke_one_executor") as smoke:
            sf._pre_stage_smoke_gate(tmp_path, [_spec("r0")], [0])
        smoke.assert_not_called()

    def test_template_less_sidecar_is_skipped(self, tmp_path: Path) -> None:
        """A runnable executor but NO result_dir_template → no smoke (the template
        is a required smoke input). Kills a mutation dropping the
        ``not result_dir_template`` skip."""
        _write_sidecar(tmp_path, "r0", executor="python ok.py", template=None)
        with mock.patch.object(sf, "_smoke_one_executor") as smoke:
            sf._pre_stage_smoke_gate(tmp_path, [_spec("r0")], [0])
        smoke.assert_not_called()

    def test_missing_sidecar_is_skipped(self, tmp_path: Path) -> None:
        """No sidecar on disk for a fresh spec → the best-effort read fails soft and
        contributes no smoke (never a hard precondition). Kills a mutation that
        turns the unreadable-sidecar path into a smoke call / crash."""
        with mock.patch.object(sf, "_smoke_one_executor") as smoke:
            sf._pre_stage_smoke_gate(tmp_path, [_spec("r0")], [0])
        smoke.assert_not_called()

    def test_opt_out_spec_is_skipped_in_a_mixed_batch(self, tmp_path: Path) -> None:
        """``pre_stage_smoke=false`` skips ONLY that spec; a co-batched opted-in spec
        still smokes. Kills a mutation that inverts the per-spec opt-out check."""
        _write_sidecar(tmp_path, "r0", executor="python optout.py", template="results/{seed}")
        _write_sidecar(tmp_path, "r1", executor="python optin.py", template="results/{seed}")
        specs = [_spec("r0", pre_stage_smoke=False), _spec("r1")]
        with mock.patch.object(sf, "_smoke_one_executor") as smoke:
            sf._pre_stage_smoke_gate(tmp_path, specs, [0, 1])
        # Only the opted-in r1 executor smoked.
        assert smoke.call_count == 1
        assert smoke.call_args.kwargs["executor"] == "python optin.py"


# =========================================================================== #
# Group D — _submit_one_spec canary ladder: the existing-canary REPLAY vs
# FRESH-fire boundary (#276). test_canary_gate pins the afterok-gate aspect of a
# live replay; here the boundary itself is pinned: a terminal-failure canary
# corpse is NOT reused (fresh fire), a live in_flight canary IS reused (no fire).
# =========================================================================== #


def _submit_one_with_mocks(tmp_path: Path, spec: SubmitFlowSpec, fire_return: list[str]):
    """Drive _submit_one_spec with the cluster arms mocked; return (result, fire)."""
    with (
        mock.patch.object(
            sf, "_augment_job_env", return_value={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"}
        ),
        mock.patch.object(sf, "build_remote_backend", return_value=mock.MagicMock()),
        mock.patch.object(sf, "_fire_canary", return_value=list(fire_return)) as fire,
        mock.patch.object(sf, "_submit_main_array", return_value=(["300"], None)),
        mock.patch.object(sf, "submit_and_record"),
    ):
        res = sf._submit_one_spec(experiment_dir=tmp_path, spec=spec, canary_decision=(True, None))
    return res, fire


class TestCanaryReplayVsFreshFire:
    def test_failed_canary_corpse_fires_fresh_not_reused(
        self, tmp_path: Path, journal_home: Path
    ) -> None:
        """#276: a terminal-failure canary record is NOT a live canary to reuse —
        the ladder falls through and fires a FRESH probe. Kills a mutation that
        drops the ``not is_resubmittable_terminal(existing_canary)`` predicate
        (which would gate main on a dead canary's forensic job_ids)."""
        _seed_record(tmp_path, "r0-canary", status="failed", job_ids=["CORPSE"], total_tasks=1)
        res, fire = _submit_one_with_mocks(tmp_path, _spec("r0", canary=True), ["FRESH"])
        fire.assert_called_once()  # fell through to a fresh fire, not a replay
        assert res.canary_job_ids == ["FRESH"]  # the corpse's id was NOT reused
        assert res.canary_done is True

    def test_live_inflight_canary_is_replayed_not_refired(
        self, tmp_path: Path, journal_home: Path
    ) -> None:
        """Boundary complement: a live ``in_flight`` canary IS reused — _fire_canary
        is NOT called and its recorded ids ride the result. Kills a mutation that
        re-fires a duplicate canary over a live one."""
        _seed_record(tmp_path, "r0-canary", status="in_flight", job_ids=["LIVE"], total_tasks=1)
        res, fire = _submit_one_with_mocks(tmp_path, _spec("r0", canary=True), ["FRESH"])
        fire.assert_not_called()  # replayed, not re-fired
        assert res.canary_job_ids == ["LIVE"]
        assert res.canary_done is True
