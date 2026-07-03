"""reconcile cascades to the canary sibling + surfaces unable_to_verify (#258).

A single ``reconcile --run-id <main>`` must settle BOTH paired journal entries
(the main run and its ``<main>-canary`` sibling) — a bare main reconcile used
to leave the canary ``in_flight`` and block the next submit. And when the
cluster alive-check itself fails, the envelope must report ``unable_to_verify``
(not a stale ``in_flight``) so callers distinguish "cluster says running" from
"we couldn't ask."
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.ops.monitor import reconcile as recon
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _record(
    run_id: str, *, status: str = "in_flight", job_ids=("1",), total_tasks: int = 4
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=list(job_ids),
        total_tasks=total_tasks,
        submitted_at="2026-06-04T00:00:00Z",
        experiment_dir="/exp",
        status=status,
    )


def _stub_cluster(
    monkeypatch,
    *,
    alive: set[str] | None,
    raise_alive: bool = False,
    summary: dict | None = None,
):
    """Stub the three SSH calls reconcile fans out."""
    report = {"summary": summary if summary is not None else {"complete": 0}, "waves": {}}
    monkeypatch.setattr(recon, "_ssh_status_report", lambda **_kw: report)
    monkeypatch.setattr(recon, "_ssh_list_combined_waves", lambda **_kw: [])

    def _alive(**_kw):
        if raise_alive:
            raise errors.RemoteCommandFailed("ssh auth failed (Duo cache expired)")
        return alive if alive is not None else set()

    monkeypatch.setattr(recon, "_ssh_alive_job_ids", _alive)


def test_reconcile_main_cascades_to_canary(tmp_path, monkeypatch):
    upsert_run(tmp_path, _record("mc_pi-bdae", job_ids=["13548839"]))
    upsert_run(tmp_path, _record("mc_pi-bdae-canary", job_ids=["13548838"]))
    # Cluster says nothing is alive → both should go abandoned.
    _stub_cluster(monkeypatch, alive=set())

    result = recon.reconcile(tmp_path, "mc_pi-bdae", scheduler="sge")

    assert result.status == "abandoned"
    # The KEY fix: the canary sibling is settled by the SAME call.
    canary = load_run(tmp_path, "mc_pi-bdae-canary")
    assert canary is not None and canary.status == "abandoned"
    # And the cascade is recorded for visibility.
    siblings = (result.last_status or {}).get("reconciled_siblings")
    assert siblings and siblings[0]["run_id"] == "mc_pi-bdae-canary"
    assert siblings[0]["lifecycle_state"] == "abandoned"


def test_reconcile_from_canary_id_cascades_to_main(tmp_path, monkeypatch):
    upsert_run(tmp_path, _record("mc_pi-bdae", job_ids=["13548839"]))
    upsert_run(tmp_path, _record("mc_pi-bdae-canary", job_ids=["13548838"]))
    _stub_cluster(monkeypatch, alive=set())

    recon.reconcile(tmp_path, "mc_pi-bdae-canary", scheduler="sge")

    assert load_run(tmp_path, "mc_pi-bdae").status == "abandoned"
    assert load_run(tmp_path, "mc_pi-bdae-canary").status == "abandoned"


def test_missing_sibling_is_a_noop(tmp_path, monkeypatch):
    # Only the main exists — no canary entry. Reconcile must not raise.
    upsert_run(tmp_path, _record("solo", job_ids=["999"]))
    _stub_cluster(monkeypatch, alive=set())
    result = recon.reconcile(tmp_path, "solo", scheduler="sge")
    assert result.status == "abandoned"
    assert "reconciled_siblings" not in (result.last_status or {})


def test_alive_check_failure_surfaces_unable_to_verify(tmp_path, monkeypatch):
    upsert_run(tmp_path, _record("stuck", job_ids=["555"]))
    # The alive-check SSH itself fails — we couldn't ask the cluster.
    _stub_cluster(monkeypatch, alive=None, raise_alive=True)

    result = recon.reconcile(tmp_path, "stuck", scheduler="sge")

    # Journal status is NOT flipped to abandoned (we couldn't verify).
    assert result.status == "in_flight"
    # The marker is set, and the envelope surfaces unable_to_verify — distinct
    # from a confirmed in_flight.
    assert (result.last_status or {}).get("verify_state") == "unable_to_verify"
    envelope = recon._reconcile_envelope(result)
    assert envelope["lifecycle_state"] == "unable_to_verify"


def test_confirmed_in_flight_is_not_unable_to_verify(tmp_path, monkeypatch):
    upsert_run(tmp_path, _record("running", job_ids=["777"]))
    # Cluster ANSWERS and the job is alive → genuinely in_flight, not unverifiable.
    _stub_cluster(monkeypatch, alive={"777"})
    result = recon.reconcile(tmp_path, "running", scheduler="sge")
    assert result.status == "in_flight"
    assert recon._reconcile_envelope(result)["lifecycle_state"] == "in_flight"


def test_reporter_failure_routes_through_unable_to_verify(tmp_path, monkeypatch):
    """Empirical 2026-06-05 demo failure: reconcile's reporter probe ran
    under bare ``/usr/bin/python`` (no activation prefix threaded through),
    crashed with ``No module named hpc_agent.execution.mapreduce.reduce``, and
    the pre-0.10.12 verdict logic gated unable_to_verify only on the
    alive-check failure — so an alive-check that successfully reported "no
    jobs alive" + a reporter that died still routed through ``abandoned``.
    But "no jobs alive" + "can't confirm results exist" is not a provable
    abandon: the run may have completed and the reporter just couldn't talk
    back. Route through ``unable_to_verify`` instead.
    """
    upsert_run(tmp_path, _record("reporter_dead", job_ids=["888"]))

    # Alive-check succeeds and finds nothing alive (job is gone from scheduler).
    # But the status reporter raises — the empirical bare-python failure shape.
    def _alive(**_kw):
        return set()  # zero alive

    def _status(**_kw):
        raise errors.RemoteCommandFailed(
            "status reporter failed (rc=1): /usr/bin/python: "
            "No module named hpc_agent.execution.mapreduce.reduce"
        )

    monkeypatch.setattr(recon, "_ssh_alive_job_ids", _alive)
    monkeypatch.setattr(recon, "_ssh_status_report", _status)
    monkeypatch.setattr(recon, "_ssh_list_combined_waves", lambda **_kw: [])

    result = recon.reconcile(tmp_path, "reporter_dead", scheduler="sge")

    # Journal status NOT flipped to abandoned: we couldn't independently
    # verify the on-disk results state.
    assert result.status == "in_flight"
    # The marker is set; the envelope surfaces unable_to_verify.
    assert (result.last_status or {}).get("verify_state") == "unable_to_verify"
    assert recon._reconcile_envelope(result)["lifecycle_state"] == "unable_to_verify"
    # Warning carries the reporter's actual error for the caller's debugging.
    warnings = (result.last_status or {}).get("warnings") or []
    assert any("status reporter" in w for w in warnings)


def test_reconcile_threads_remote_activation_to_reporter(tmp_path, monkeypatch):
    """Tier 1 of the 0.10.12 fix: the reporter call must receive
    ``remote_activation=<sidecar-derived prefix>`` so it runs under the run's
    conda/modules env on the cluster, not the bare login-node python. The
    monitor-side ``record_status`` already does this; reconcile didn't until
    now.
    """
    upsert_run(tmp_path, _record("activation_check", job_ids=["999"]))

    # Stub the sidecar shape (cluster key + env) and bypass the clusters.yaml
    # load by mocking the activation-prefix helper to return a sentinel string.
    # We only care that reconcile threaded *something* into the reporter call
    # — the exact prefix is the activation helper's contract, tested elsewhere.
    monkeypatch.setattr(
        "hpc_agent.state.runs.read_run_sidecar",
        lambda _exp, _rid: {"cluster": "test_cluster", "env": {"conda_env": "hpc-pi"}},
    )
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.remote_activation_for_sidecar",
        lambda _sidecar: "source /path/to/conda.sh && conda activate hpc-pi && ",
    )

    captured: dict = {}

    def _capture_status(**kw):
        captured.update(kw)
        return {"summary": {}, "waves": {}}

    monkeypatch.setattr(recon, "_ssh_status_report", _capture_status)
    monkeypatch.setattr(recon, "_ssh_list_combined_waves", lambda **_kw: [])
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", lambda **_kw: {"999"})

    recon.reconcile(tmp_path, "activation_check", scheduler="sge")

    # The activation prefix must have been threaded through (non-empty).
    # The exact string is computed by remote_activation_for_sidecar; we
    # only assert it's not the empty-string default that caused the bug.
    assert captured.get("remote_activation"), (
        "reporter was called without an activation prefix — bare-python path will fail"
    )


# ---------------------------------------------------------------------------
# 2026-06-11 — no-run-record remediation names the sidecar's pre-stamped ids
# ---------------------------------------------------------------------------


def test_no_record_hint_names_sidecar_job_ids(tmp_path, monkeypatch):
    """When the journal has no record but the sidecar carries pre-stamped
    job_ids (submit-flow's post-qsub crash-safety stamp), the JournalCorrupt
    remediation must name the REAL ids + the submit-spec mint path — the
    2026-06-11 demo's dead-end here is what pushed the orchestrator into
    fabricating ``["purged-completed"]``."""
    from hpc_agent.state.runs import update_run_sidecar_job_ids, write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id="rLost",
        cmd_sha="0" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-06-11T00:00:00Z",
        executor="python3 run.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=100,
        tasks_py_sha="1" * 64,
    )
    update_run_sidecar_job_ids(tmp_path, "rLost", ["13610902"])

    with pytest.raises(errors.JournalCorrupt) as exc:
        recon._reconcile_one(tmp_path, "rLost", scheduler="sge")
    msg = str(exc.value)
    assert "13610902" in msg
    assert "submit-spec" in msg


# ---------------------------------------------------------------------------
# Post-completion record purge must NOT read as abandoned (demo bug).
# A FINISHED run (all tasks complete, results on disk) whose scheduler dropped
# its job records post-completion has no alive jobs — but "no alive jobs" alone
# is not abandon. "abandoned" requires evidence of NON-completion. This is the
# same verdict-routing class as the alive_check_failed / reporter_failed guards.
# ---------------------------------------------------------------------------


def test_all_tasks_complete_with_purged_records_is_complete(tmp_path, monkeypatch):
    """All 10 array tasks complete + 0 failed/pending/running, but the scheduler
    purged the finished job's records (none alive). The verdict must be terminal
    ``complete``, NOT ``abandoned`` — the live-demo bug."""
    upsert_run(tmp_path, _record("done_purged", job_ids=["13548839"], total_tasks=10))
    # Reporter ran cleanly: every task complete, nothing outstanding.
    _stub_cluster(
        monkeypatch,
        alive=set(),  # records purged → nothing alive on the scheduler
        summary={"complete": 10, "running": 0, "pending": 0, "failed": 0, "unknown": 0},
    )

    result = recon.reconcile(tmp_path, "done_purged", scheduler="sge")

    assert result.status == "complete"
    envelope = recon._reconcile_envelope(result)
    assert envelope["lifecycle_state"] == "complete"
    # NOT abandoned, NOT unable_to_verify (both probes ran cleanly).
    assert (result.last_status or {}).get("verify_state") != "unable_to_verify"
    # Provenance is recorded AND reaches the envelope (Phase 1.5), not just
    # computed in isolation.
    assert envelope["last_status"]["verdict_reason"] == "all_tasks_complete"


def test_incomplete_with_purged_records_is_abandoned(tmp_path, monkeypatch):
    """Genuinely incomplete: some tasks never produced results AND nothing is
    alive AND the alive-check ran cleanly → ``abandoned``. The complete-guard
    must NOT fire when there is evidence of non-completion."""
    upsert_run(tmp_path, _record("half_done", job_ids=["13548840"], total_tasks=10))
    # 6 complete, 4 missing (unknown) — incomplete.
    _stub_cluster(
        monkeypatch,
        alive=set(),
        summary={"complete": 6, "running": 0, "pending": 0, "failed": 0, "unknown": 4},
    )

    result = recon.reconcile(tmp_path, "half_done", scheduler="sge")

    assert result.status == "abandoned"
    assert recon._reconcile_envelope(result)["lifecycle_state"] == "abandoned"
    # The new ``_run_failed`` arm must NOT steal this verdict: failed == 0, so
    # there is no positive failure evidence — pure absence stays ``abandoned``.
    assert result.status != "failed"
    assert "failure_features" not in (result.last_status or {})
    # Provenance distinguishes "no evidence" from a verified failure.
    assert (result.last_status or {})["verdict_reason"] == "no_on_disk_evidence"


# ---------------------------------------------------------------------------
# #351 sub-bug #4 — a run that RAN and FAILED (reporter failed>=1, readable
# exit_code/traceback on disk) must route to ``failed`` carrying the classified
# error, NOT collapse into ``abandoned`` ("scratch purged, no recovery"). Same
# verdict-routing class as the 0.10.12 reconcile fix: absence and failure must
# not share one bucket.
# ---------------------------------------------------------------------------


def test_ran_and_failed_with_purged_records_is_failed_not_abandoned(tmp_path, monkeypatch):
    """The canary ran and FAILED: reporter shows failed>=1 with a readable
    TypeError on disk, and no job is alive. The verdict must be terminal
    ``failed`` carrying ``failure_features`` (the classified error), NOT
    ``abandoned`` — the #351 bug routed this through abandoned and told the
    operator 'scratch purged; re-submit' for a fixable, on-disk failure."""
    upsert_run(tmp_path, _record("canary_failed", job_ids=["13548842"], total_tasks=1))
    # 1 task, it FAILED (failed=1, complete=0) — positive failure evidence.
    _stub_cluster(
        monkeypatch,
        alive=set(),  # job left the queue (purged) but it FAILED, didn't vanish
        summary={"complete": 0, "running": 0, "pending": 0, "failed": 1, "unknown": 0},
    )
    # Stub the cluster-side log fetch (lazily imported by _gather_failure_features)
    # so the classifier has a real traceback to bite on — the structured evidence
    # the ``failed`` verdict must carry.
    stderr_tail = (
        'Traceback (most recent call last):\n  File "run.py", line 12, in <module>\n'
        '    main()\n  File "run.py", line 8, in main\n    return x + None\n'
        "TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'\n"
        "[dispatch] FAILED task 0 exit_code: 1"
    )
    monkeypatch.setattr(
        "hpc_agent.infra.cluster_logs.fetch_task_logs",
        lambda **_kw: [{"task_id": 0, "content": stderr_tail, "path": "/remote/logs/j.o1.1"}],
    )

    result = recon.reconcile(tmp_path, "canary_failed", scheduler="sge")

    # NOT abandoned — the run failed, with evidence on disk.
    assert result.status == "failed"
    assert recon._reconcile_envelope(result)["lifecycle_state"] == "failed"
    # Provenance: positive failure evidence (alongside failure_features below).
    assert (result.last_status or {})["verdict_reason"] == "positive_failure_evidence"
    # The readable cluster log tail is carried out for the skill's ``failed``
    # branch — the load-bearing evidence (the TypeError that proves FAILURE, not
    # a purge). The signature classifier now lives in ``infra.failure_signatures``
    # (shared substrate the cross-subject boundary lint allows), so reconcile
    # classifies the tail inline — same enrichment ``verify_canary`` attaches.
    features = (result.last_status or {}).get("failure_features")
    assert isinstance(features, dict)
    assert features["cluster_log_tail"] == stderr_tail
    assert "TypeError" in features["cluster_log_tail"]
    assert features["log_path"] == "/remote/logs/j.o1.1"
    # The Traceback tail classifies (python_traceback) — a populated triple, not
    # a bare None: reconcile can classify now that the catalog is in infra.
    classified = features["classified_error"]
    assert isinstance(classified, dict)
    assert classified["error_class"] == "python_traceback"
    assert classified["suggested_fix"] == {"action": "user-debug"}
    assert classified["matched_pattern"]
    # The envelope's last_status carries the same evidence.
    env = recon._reconcile_envelope(result)
    assert "TypeError" in env["last_status"]["failure_features"]["cluster_log_tail"]
    assert env["last_status"]["failure_features"]["classified_error"]["error_class"] == (
        "python_traceback"
    )


def test_failed_verdict_survives_a_log_fetch_blip(tmp_path, monkeypatch):
    """The ``failed`` verdict stands on the reporter's positive ``failed`` count
    alone — if the best-effort log fetch raises (SSH blip), the run is STILL
    ``failed`` (never silently demoted to ``abandoned``); the evidence just
    degrades to an empty tail."""
    upsert_run(tmp_path, _record("failed_noLog", job_ids=["13548843"], total_tasks=1))
    _stub_cluster(
        monkeypatch,
        alive=set(),
        summary={"complete": 0, "running": 0, "pending": 0, "failed": 1, "unknown": 0},
    )

    def _boom(**_kw):
        raise errors.RemoteCommandFailed("ssh blip fetching the stderr tail")

    monkeypatch.setattr("hpc_agent.infra.cluster_logs.fetch_task_logs", _boom)

    result = recon.reconcile(tmp_path, "failed_noLog", scheduler="sge")

    assert result.status == "failed"
    assert recon._reconcile_envelope(result)["lifecycle_state"] == "failed"
    # Evidence degraded but present (empty tail, no classification).
    features = (result.last_status or {}).get("failure_features")
    assert isinstance(features, dict)
    assert features["cluster_log_tail"] == ""
    assert features["classified_error"] is None


def test_connectivity_blip_is_unable_to_verify_even_with_complete_results(tmp_path, monkeypatch):
    """The alive-check SSH itself fails. Even though the (stubbed) reporter says
    all complete, an alive-check we couldn't run routes through
    ``unable_to_verify`` (unchanged behavior) — we don't mint a terminal verdict
    on a probe that didn't run."""
    upsert_run(tmp_path, _record("blip", job_ids=["13548841"], total_tasks=10))
    _stub_cluster(
        monkeypatch,
        alive=None,
        raise_alive=True,  # connectivity blip on the alive-check
        summary={"complete": 10, "running": 0, "pending": 0, "failed": 0, "unknown": 0},
    )

    result = recon.reconcile(tmp_path, "blip", scheduler="sge")

    # Journal status untouched; envelope surfaces unable_to_verify.
    assert result.status == "in_flight"
    assert (result.last_status or {}).get("verify_state") == "unable_to_verify"
    assert recon._reconcile_envelope(result)["lifecycle_state"] == "unable_to_verify"


def test_no_record_no_sidecar_keeps_bare_message(tmp_path, monkeypatch):
    with pytest.raises(errors.JournalCorrupt) as exc:
        recon._reconcile_one(tmp_path, "rGone", scheduler="sge")
    msg = str(exc.value)
    assert "no run record" in msg
    assert "submit-spec" not in msg


def test_no_record_unreadable_sidecar_keeps_bare_message(tmp_path, monkeypatch):
    """The hint read is best-effort: a sidecar that fails to decode — here a
    too-new ``sidecar_schema_version`` (SchemaIncompat, which is NOT an
    OSError/JSONDecodeError) — must degrade to the bare JournalCorrupt, never
    leak the read error in place of the actionable 'no run record' message."""
    import json

    from hpc_agent.state.runs import run_sidecar_path

    target = run_sidecar_path(tmp_path, "rFuture")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"sidecar_schema_version": 999, "job_ids": ["13610902"]}))

    with pytest.raises(errors.JournalCorrupt) as exc:
        recon._reconcile_one(tmp_path, "rFuture", scheduler="sge")
    msg = str(exc.value)
    assert "no run record" in msg
    # The unreadable sidecar yields no hint, and the SchemaIncompat never surfaces.
    assert "submit-spec" not in msg
    assert "schema_version" not in msg


# ---------------------------------------------------------------------------
# #356 — a crashed-submit orphan (valid jobless sidecar + no journal record)
# is a BENIGN ``no_run_record`` verdict, NOT a JournalCorrupt. Both halves are
# pinned here: (a) the benign path classifies + a fresh submit proceeds with no
# manual rm; (b) the three loud cases still fire JournalCorrupt and can never
# be masked by the benign branch (#328).
# ---------------------------------------------------------------------------


def _write_jobless_sidecar(tmp_path, run_id: str):
    """A valid v2 sidecar written at Step 6d, before any qsub — no job_ids.

    This is exactly what ``submit-flow`` leaves when the process dies before
    ``submit_and_record`` minted the journal record and stamped the ids.
    """
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id=run_id,
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-06-24T00:00:00Z",
        executor="python3 run.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=8,
        tasks_py_sha="b" * 64,
    )


def test_benign_orphan_classifies_no_run_record_not_corrupt(tmp_path, monkeypatch):
    """Valid jobless sidecar + no journal record → benign OrphanedReconcile,
    surfaced as a ``no_run_record`` envelope. NOT a JournalCorrupt (the #356
    core requirement): no hand-``rm`` before a fresh submit."""
    _write_jobless_sidecar(tmp_path, "rOrphan")

    # _reconcile_one returns the benign marker (no exception), alive_failed False.
    result, alive_failed = recon._reconcile_one(tmp_path, "rOrphan", scheduler="sge")
    assert isinstance(result, recon.OrphanedReconcile)
    assert result.run_id == "rOrphan"
    assert alive_failed is False

    # The envelope surfaces the benign terminal-ish state — discardable/overwritable.
    envelope = recon._reconcile_envelope(result)
    assert envelope["lifecycle_state"] == "no_run_record"
    assert envelope["run_id"] == "rOrphan"
    assert envelope["combined_waves"] == []
    assert envelope["failed_waves"] == []
    # last_status carries the orphaned verdict + an actionable next_step so the
    # agent reading the envelope knows to proceed with a fresh submit (no rm).
    assert envelope["last_status"]["verdict"] == "orphaned"
    assert "fresh submit" in envelope["last_status"]["next_step"]


def test_benign_orphan_via_top_level_reconcile_no_ssh(tmp_path, monkeypatch):
    """The public ``reconcile`` short-circuits on a benign orphan — no sibling
    cascade (there is no record to merge into) and no cluster round-trip. If
    any of the three SSH probes were invoked this would raise."""

    def _boom(**_kw):
        raise AssertionError("benign orphan must not reach the cluster")

    monkeypatch.setattr(recon, "_ssh_status_report", _boom)
    monkeypatch.setattr(recon, "_ssh_list_combined_waves", _boom)
    monkeypatch.setattr(recon, "_ssh_alive_job_ids", _boom)
    _write_jobless_sidecar(tmp_path, "rOrphan2")

    result = recon.reconcile(tmp_path, "rOrphan2", scheduler="sge")
    assert isinstance(result, recon.OrphanedReconcile)
    assert recon._reconcile_envelope(result)["lifecycle_state"] == "no_run_record"


def test_benign_orphan_fresh_submit_proceeds_without_manual_rm(tmp_path, monkeypatch):
    """AC2: a fresh submit for the SAME run_id proceeds with no file deletion.

    The orphan sidecar (matching cmd_sha, empty job_ids, no journal record) must
    NOT be a dedup target — ``submit_and_record`` falls through to a real submit
    and the new record carries the freshly-minted job_ids."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    _write_jobless_sidecar(tmp_path, "rOrphan3")

    from hpc_agent._wire.actions.submit import SubmitSpec
    from hpc_agent.ops.submit.runner import submit_and_record

    spec = SubmitSpec(
        run_id="rOrphan3",
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=["55501"],  # the real ids the fresh qsub returned
        total_tasks=8,
    )
    record, deduped = submit_and_record(
        tmp_path, spec=spec, cmd_sha="a" * 64, tasks_py_sha="b" * 64
    )
    # The submit PROCEEDED (not a dedup replay against the orphan) and stamped
    # the real job_ids — no manual rm of the orphan sidecar was required.
    assert deduped is False
    assert record.job_ids == ["55501"]
    assert load_run(tmp_path, "rOrphan3").job_ids == ["55501"]


def test_stranded_ids_orphan_stays_journal_corrupt_not_benign(tmp_path, monkeypatch):
    """REGRESSION-PIN (#328/#356): a sidecar WITH job_ids but no journal record
    is the stranded-post-qsub case — it must STAY a JournalCorrupt-with-hint,
    NOT be reclassified as a benign orphan. The benign branch can never mask it."""
    from hpc_agent.state.runs import update_run_sidecar_job_ids

    _write_jobless_sidecar(tmp_path, "rStranded")
    update_run_sidecar_job_ids(tmp_path, "rStranded", ["13610902"])

    with pytest.raises(errors.JournalCorrupt) as exc:
        recon._reconcile_one(tmp_path, "rStranded", scheduler="sge")
    msg = str(exc.value)
    assert "13610902" in msg
    assert "submit-spec" in msg


def test_malformed_sidecar_stays_journal_corrupt_not_benign(tmp_path, monkeypatch):
    """REGRESSION-PIN (#356): a sidecar that is NOT valid JSON must stay a loud
    JournalCorrupt — a failed read can never read as a benign orphan."""
    from hpc_agent.state.runs import run_sidecar_path

    target = run_sidecar_path(tmp_path, "rGarbage")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{ this is not valid json")

    with pytest.raises(errors.JournalCorrupt) as exc:
        recon._reconcile_one(tmp_path, "rGarbage", scheduler="sge")
    assert "no run record" in str(exc.value)


# ---------------------------------------------------------------------------
# FIX-3 — the settle-arm harvest fires only on a verdict TRANSITION. An
# idempotent re-reconcile (same verdict) must NOT re-pay the rsync-pull +
# reduce + ledger append; a legit complete→failed downgrade still harvests
# (the verdict is revisable — engineering-principles).
# ---------------------------------------------------------------------------


def _count_harvests(monkeypatch) -> list[str]:
    calls: list[str] = []

    def _fake(experiment_dir, run_id, *, terminal_cause, record=None, **_kw):
        calls.append(terminal_cause)
        return {}

    monkeypatch.setattr(recon, "harvest_on_terminal", _fake)
    return calls


def test_idempotent_reconcile_does_not_reharvest(tmp_path, monkeypatch):
    harvests = _count_harvests(monkeypatch)
    upsert_run(tmp_path, _record("done_once", job_ids=["1"], total_tasks=10))
    _stub_cluster(
        monkeypatch,
        alive=set(),
        summary={"complete": 10, "running": 0, "pending": 0, "failed": 0, "unknown": 0},
    )

    # First reconcile: in_flight → complete is a TRANSITION → harvest fires once.
    recon.reconcile(tmp_path, "done_once", scheduler="sge")
    assert harvests == ["complete"]

    # Second reconcile: already complete, SAME verdict → NO second harvest.
    recon.reconcile(tmp_path, "done_once", scheduler="sge")
    assert harvests == ["complete"]


def test_verdict_downgrade_reharvests(tmp_path, monkeypatch):
    harvests = _count_harvests(monkeypatch)
    # A run the journal already marked complete (a premature verdict).
    upsert_run(tmp_path, _record("was_complete", status="complete", job_ids=["2"], total_tasks=1))
    # New evidence: the single task actually FAILED → complete→failed downgrade.
    monkeypatch.setattr("hpc_agent.infra.cluster_logs.fetch_task_logs", lambda **_kw: [])
    _stub_cluster(
        monkeypatch,
        alive=set(),
        summary={"complete": 0, "running": 0, "pending": 0, "failed": 1, "unknown": 0},
    )

    result = recon.reconcile(tmp_path, "was_complete", scheduler="sge")
    assert result.status == "failed"
    # complete → failed IS a transition (the verdict is revisable) → harvest fires.
    assert harvests == ["failed"]


def test_registered_inconsistent_run_still_abandoned_not_orphaned(tmp_path, monkeypatch):
    """A REGISTERED run with no alive jobs and an incomplete reporter summary is
    an ``abandoned`` verdict (existing path) — the benign-orphan branch only
    fires when there is NO journal record, so a registered-but-inconsistent run
    is untouched by #356."""
    upsert_run(tmp_path, _record("registered_bad", job_ids=["42"], total_tasks=10))
    _stub_cluster(
        monkeypatch,
        alive=set(),
        summary={"complete": 3, "running": 0, "pending": 0, "failed": 0, "unknown": 7},
    )
    result = recon.reconcile(tmp_path, "registered_bad", scheduler="sge")
    assert not isinstance(result, recon.OrphanedReconcile)
    assert result.status == "abandoned"
