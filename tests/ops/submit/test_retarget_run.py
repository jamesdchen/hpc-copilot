"""Tests for ``retarget-run`` (proving-run #5 wave 5.2, the recovery arm).

The cluster-retarget recovery arm sequences supersede → re-resolve(new run_name)
→ re-canary in CODE. These assert:

* the LOAD-BEARING guards — a patch naming a DERIVED field, a patch that does NOT
  change the cluster (same or missing), and a scope with no resolved sidecar are
  all refused with ``SpecInvalid``;
* the 3-step composition — a ``{cluster: X}`` retarget re-resolves with X's
  activation/job_env RE-DERIVED, SUPERSEDES the old attempt (old→new link
  stamped), and re-canaries on X under a NEW run_name (distinct run_id);
* the SUCCESSORS wiring — a cluster delta at an anomaly terminator routes to
  ``retarget-run`` via ``block_chain.recovery_arm_verb``.

Idiom mirrors tests/ops/submit/test_revise_resolved.py: a REAL journal /
clusters.yaml via env vars, with compute-run-id + find-prior-run mocked at the
resolve seam (so build-submit-spec — the job_env derivation under test — runs for
real) and ``submit-and-verify`` mocked at the retarget seam (no real SSH).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
from hpc_agent._wire.workflows.retarget_run import RetargetRunInput
from hpc_agent._wire.workflows.submit_and_verify import SubmitAndVerifyResult
from hpc_agent.ops.retarget_run import retarget_run

if TYPE_CHECKING:
    from pathlib import Path

_RESOLVE_SEAM = "hpc_agent.ops.resolve_submit_inputs"
_RETARGET_SEAM = "hpc_agent.ops.retarget_run"
_OLD_RUN_ID = "exp-abcd1234"
_NEW_RUN_ID = "exp-h2new-a1b2c3d4"  # what the mocked compute-run-id mints
_BASE_RESOLVED = {"cluster": "h2old", "next_block": "submit-s2"}

_CLUSTERS_YAML = """\
h2old:
  scheduler: sge
  host: old.example.edu
  user: me
  scratch: /scratch/old
  conda_source: /opt/old/conda.sh
  conda_envs: [old_env]
h2new:
  scheduler: sge
  host: new.example.edu
  user: me
  scratch: /scratch/new
  conda_source: /opt/new/conda.sh
  conda_envs: [new_env]
"""


def _cr() -> dict[str, Any]:
    """compute-run-id's return (mocked at the resolve seam) — mints the NEW id."""
    return {
        "run_id": _NEW_RUN_ID,
        "cmd_sha": "a" * 64,
        "trial_tokens": None,
        "trial_params": [{"seed": 0}, {"seed": 1}],
        "total": 2,
    }


def _fp(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "found": False,
        "prior_run_id": None,
        "is_orphan": False,
        "status": None,
        "age_sec": None,
        "profile": None,
        "cluster": None,
        "job_ids": [],
        "campaign_id": None,
        "submitted_at": None,
    }
    base.update(over)
    return base


def _sv(*, verified: bool = True, failure_kind: Any = None) -> SubmitAndVerifyResult:
    """A stop_after_canary submit-and-verify result (mocked at the retarget seam)."""
    return SubmitAndVerifyResult(
        run_id=_NEW_RUN_ID,
        job_ids=[],  # stop_after_canary → main array not launched
        total_tasks=2,
        deduped=False,
        canary_run_id=f"{_NEW_RUN_ID}-canary",
        canary_job_ids=["55501"],
        verified=verified,
        failure_kind=failure_kind,
        verify_result=None,
    )


def _setup(tmp_path: Path, monkeypatch: Any, *, old_record_status: str | None = "failed") -> str:
    """Lay down clusters.yaml + journal + tasks.py + old sidecar + base greenlight.

    When *old_record_status* is set, also upsert an old-run journal RunRecord so
    supersession has a target to close (the old→new link to stamp). Returns the
    old run_id being retargeted.
    """
    from hpc_agent.ops.write_run_sidecar import write_run_sidecar
    from hpc_agent.state.decision_journal import append_decision

    clusters = tmp_path / "clusters.yaml"
    clusters.write_text(_CLUSTERS_YAML, encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(clusters))
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))

    hpc = tmp_path / ".hpc"
    hpc.mkdir(parents=True, exist_ok=True)
    (hpc / "tasks.py").write_text("# stub\n", encoding="utf-8")

    # The failed attempt's sidecar — the config snapshot retarget re-derives from,
    # written under the OLD cluster (h2old).
    write_run_sidecar(
        experiment_dir=tmp_path,
        spec=WriteRunSidecarInput(
            run_id=_OLD_RUN_ID,
            cmd_sha="a" * 64,
            executor="python -m src.exp --seed $seed",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=2,
            cluster="h2old",
            profile="exp",
            remote_path="/scratch/old/exp",
            resources={"walltime_sec": 3600, "cpus": 2},
            env={"conda_env": "old_env"},
        ),
    )

    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=_OLD_RUN_ID,
        block="s2",
        response="y",
        resolved=dict(_BASE_RESOLVED),
    )

    if old_record_status is not None:
        from hpc_agent.state.journal import upsert_run
        from hpc_agent.state.run_record import RunRecord

        upsert_run(
            tmp_path,
            RunRecord(
                run_id=_OLD_RUN_ID,
                profile="exp",
                cluster="h2old",
                ssh_target="me@old.example.edu",
                remote_path="/scratch/old/exp",
                job_name="exp",
                job_ids=[],  # terminal / no live jobs → supersede stamps, no kill
                total_tasks=2,
                submitted_at="2026-07-05T00:00:00+00:00",
                experiment_dir=str(tmp_path),
                status=old_record_status,
                backend="sge",
                job_env={"HPC_CMD_SHA": "a" * 64},
            ),
        )
    return _OLD_RUN_ID


# ── the load-bearing guards ───────────────────────────────────────────────────


@pytest.mark.parametrize("derived_field", ["job_env", "executor", "ssh_target", "backend"])
def test_patch_naming_derived_field_is_refused(tmp_path: Path, derived_field: str) -> None:
    """A patch key naming a CODE-DERIVED field is refused (revise-resolved's guard,
    re-pointed) — fires before any I/O."""
    with pytest.raises(errors.SpecInvalid) as excinfo:
        retarget_run(
            tmp_path,
            spec=RetargetRunInput(old_run_id=_OLD_RUN_ID, patch={derived_field: "x"}),
        )
    assert derived_field in str(excinfo.value)
    assert "DERIVED" in str(excinfo.value)


def test_patch_without_cluster_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """A retarget whose delta names no cluster is refused — routed to revise-resolved."""
    old = _setup(tmp_path, monkeypatch)
    with pytest.raises(errors.SpecInvalid) as excinfo:
        retarget_run(tmp_path, spec=RetargetRunInput(old_run_id=old, patch={"walltime_sec": 7200}))
    assert "names no `cluster`" in str(excinfo.value)
    assert "revise-resolved" in str(excinfo.value)


def test_same_cluster_patch_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """A retarget to the SAME cluster is refused — it would self-collide the run_id
    (a self-supersession); the message routes to revise-resolved."""
    old = _setup(tmp_path, monkeypatch)
    with pytest.raises(errors.SpecInvalid) as excinfo:
        retarget_run(tmp_path, spec=RetargetRunInput(old_run_id=old, patch={"cluster": "h2old"}))
    assert "SAME cluster" in str(excinfo.value)
    assert "revise-resolved" in str(excinfo.value)


def test_no_sidecar_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """A scope with no resolved-run sidecar is refused — retarget amends a RESOLVED prior."""
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(tmp_path / "clusters.yaml"))
    (tmp_path / "clusters.yaml").write_text(_CLUSTERS_YAML, encoding="utf-8")
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    with pytest.raises(errors.SpecInvalid) as excinfo:
        retarget_run(
            tmp_path, spec=RetargetRunInput(old_run_id=_OLD_RUN_ID, patch={"cluster": "h2new"})
        )
    assert "no resolved-run sidecar" in str(excinfo.value)


# ── the 3-step composition ────────────────────────────────────────────────────


def test_retarget_supersedes_reresolves_and_recanaries(tmp_path: Path, monkeypatch: Any) -> None:
    """A ``{cluster: h2new}`` retarget: re-resolves job_env from h2new, supersedes
    the old attempt (old→new link stamped), and re-canaries under a NEW run_id."""
    from hpc_agent.state.journal import load_run

    old = _setup(tmp_path, monkeypatch)

    captured: dict[str, Any] = {}

    def _fake_sv(experiment_dir: Any, *, spec: Any, stop_after_canary: bool = False) -> Any:
        captured["submit"] = spec.submit
        captured["stop_after_canary"] = stop_after_canary
        return _sv(verified=True)

    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=_fp(found=False)),
        mock.patch(f"{_RETARGET_SEAM}.submit_and_verify", side_effect=_fake_sv),
    ):
        res = retarget_run(
            tmp_path, spec=RetargetRunInput(old_run_id=old, patch={"cluster": "h2new"})
        )

    # Outcome + audit.
    assert res.stage_reached == "retargeted_canary_verified"
    assert res.needs_decision is True
    assert res.verified is True
    assert res.superseded_run_id == old
    assert res.run_id == _NEW_RUN_ID
    assert res.run_id != old  # a DISTINCT run identity (not a re-attach)
    assert res.applied_patch == {"cluster": "h2new"}

    # Step 1 (re-resolve): job_env RE-DERIVED from h2new, NOT the old cluster.
    submit_spec = res.brief["resolve"]["submit_spec"]
    assert submit_spec["job_env"]["CONDA_ENV"] == "new_env"
    assert submit_spec["job_env"]["CONDA_ENV"] != "old_env"
    assert submit_spec["ssh_target"] == "me@new.example.edu"
    assert submit_spec["remote_path"] == "/scratch/new/exp"
    assert res.brief["resolved"]["cluster"] == "h2new"
    assert res.brief["cluster"] == "h2new"
    assert res.brief["retargeted_from"] == {"run_id": old, "cluster": "h2old"}

    # Step 2 (supersede): the old run carries the backward superseded_by link.
    old_rec = load_run(tmp_path, old)
    assert old_rec is not None
    assert old_rec.superseded_by == _NEW_RUN_ID
    assert res.brief["supersession"]["superseded_run_id"] == old

    # Step 3 (re-canary): submit-and-verify got the h2new spec with canary on,
    # stopped after the canary.
    assert captured["submit"].cluster == "h2new"
    assert captured["submit"].canary is True
    assert captured["submit"].run_id == _NEW_RUN_ID
    assert captured["stop_after_canary"] is True


def test_retarget_canary_fails_again(tmp_path: Path, monkeypatch: Any) -> None:
    """A canary that fails on the new cluster too → retargeted_canary_failed; the
    old attempt is still superseded and the failure_kind rides the brief."""
    old = _setup(tmp_path, monkeypatch)
    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=_fp(found=False)),
        mock.patch(
            f"{_RETARGET_SEAM}.submit_and_verify",
            return_value=_sv(verified=False, failure_kind="nonzero_exit"),
        ),
    ):
        res = retarget_run(
            tmp_path, spec=RetargetRunInput(old_run_id=old, patch={"cluster": "h2new"})
        )
    assert res.stage_reached == "retargeted_canary_failed"
    assert res.verified is False
    assert res.failure_kind == "nonzero_exit"
    assert res.superseded_run_id == old


def test_explicit_new_run_name_is_honored(tmp_path: Path, monkeypatch: Any) -> None:
    """An explicit new_run_name is passed through to the re-resolve (the LLM never
    needs it, but a caller may override the code-derived default)."""
    old = _setup(tmp_path, monkeypatch)
    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=_cr()) as cri,
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=_fp(found=False)),
        mock.patch(f"{_RETARGET_SEAM}.submit_and_verify", return_value=_sv(verified=True)),
    ):
        retarget_run(
            tmp_path,
            spec=RetargetRunInput(
                old_run_id=old, patch={"cluster": "h2new"}, new_run_name="exp-fresh"
            ),
        )
    # resolve-submit-inputs is fed the explicit run_name via compute-run-id.
    assert cri.call_args.kwargs["run_name"] == "exp-fresh"


def test_resolve_blocked_short_circuits_before_recanary(tmp_path: Path, monkeypatch: Any) -> None:
    """If the fresh resolve surfaces its OWN decision (e.g. a live sibling of the
    NEW run_id from a prior retarget), retarget stops at resolve_blocked and does
    NOT re-canary."""
    old = _setup(tmp_path, monkeypatch)
    sv_mock = mock.MagicMock()
    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(
            f"{_RESOLVE_SEAM}.find_prior_run",
            return_value=_fp(
                found=True, is_orphan=False, status="in_flight", prior_run_id=_NEW_RUN_ID
            ),
        ),
        mock.patch(f"{_RETARGET_SEAM}.submit_and_verify", sv_mock),
    ):
        res = retarget_run(
            tmp_path, spec=RetargetRunInput(old_run_id=old, patch={"cluster": "h2new"})
        )
    assert res.stage_reached == "resolve_blocked"
    assert res.needs_decision is True
    sv_mock.assert_not_called()  # never re-canaried


# ── SUCCESSORS wiring (the recovery arm) ──────────────────────────────────────


def test_cluster_delta_at_anomaly_routes_to_retarget_run() -> None:
    """The block_chain SoT routes a cluster delta at an anomaly terminator to this
    verb — the route is a function of the spec, computed in code (§4.1)."""
    from hpc_agent.infra.block_chain import recovery_arm_verb

    assert recovery_arm_verb("submit-s2", "canary_failed", ["cluster"]) == "retarget-run"
    assert recovery_arm_verb("submit-s3", "watching_anomaly", ["cluster"]) == "retarget-run"
    # A non-cluster recovery stays a human branch.
    assert recovery_arm_verb("submit-s2", "canary_failed", ["walltime_sec"]) is None
