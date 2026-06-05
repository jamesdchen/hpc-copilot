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


def _record(run_id: str, *, status: str = "in_flight", job_ids=("1",)) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/remote",
        job_name="j",
        job_ids=list(job_ids),
        total_tasks=4,
        submitted_at="2026-06-04T00:00:00Z",
        experiment_dir="/exp",
        status=status,
    )


def _stub_cluster(monkeypatch, *, alive: set[str] | None, raise_alive: bool = False):
    """Stub the three SSH calls reconcile fans out."""
    monkeypatch.setattr(
        recon, "_ssh_status_report", lambda **_kw: {"summary": {"complete": 0}, "waves": {}}
    )
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
    crashed with ``No module named hpc_agent.models.mapreduce.reduce``, and
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
            "No module named hpc_agent.models.mapreduce.reduce"
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
