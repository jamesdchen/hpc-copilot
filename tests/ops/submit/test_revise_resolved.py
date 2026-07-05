"""Tests for ``revise-resolved`` (proving-run #5 wave 5.1, the ROOT fix).

The nudge becomes a FIELD DELTA the LLM names; the verb re-resolves and
re-derives every field the delta invalidates. These assert:

* the LOAD-BEARING guard refuses a patch that names a DERIVED field
  (job_env / executor / run_id / ssh_target / …) with ``SpecInvalid`` — the
  thing that makes hand-authoring a derived value structurally impossible;
* a ``{cluster: X}`` patch re-resolves with X's activation / job_env RE-DERIVED
  from clusters.yaml (not the old cluster's) — closes findings 13/17 by
  construction;
* the verb does NOT bypass the gates: a ``goal`` / ``task_generator`` delta
  still faces the human-authorship gate at the re-commit (append-decision).

Idiom mirrors tests/ops/test_resolve_submit_inputs.py — a REAL journal /
clusters.yaml via env vars, with only compute-run-id + find-prior-run mocked at
the resolve seam so build-submit-spec (the job_env derivation under test) runs
for real.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
from hpc_agent._wire.workflows.revise_resolved import ReviseResolvedInput
from hpc_agent.ops.revise_resolved import revise_resolved

if TYPE_CHECKING:
    from pathlib import Path

_RESOLVE_SEAM = "hpc_agent.ops.resolve_submit_inputs"
_RUN_ID = "exp-abcd1234"
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
    """compute-run-id's return (mocked) — the authoritative task list."""
    return {
        "run_id": _RUN_ID,
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


def _setup(tmp_path: Path, monkeypatch: Any, *, base_resolved: dict[str, Any]) -> str:
    """Lay down clusters.yaml + journal + tasks.py + prior sidecar + base greenlight.

    Returns the run_id whose RESOLVED prior ``revise-resolved`` amends.
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

    # The prior RESOLVED run's sidecar — the config snapshot revise-resolved
    # re-derives from. Written under the OLD cluster (h2old).
    write_run_sidecar(
        experiment_dir=tmp_path,
        spec=WriteRunSidecarInput(
            run_id=_RUN_ID,
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

    # The base greenlight the patch amends (state fn = no gates, the _greenlight idiom).
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=_RUN_ID,
        block="s1",
        response="y",
        resolved=base_resolved,
    )
    return _RUN_ID


# ── the load-bearing guard: derived fields are refused ────────────────────────


@pytest.mark.parametrize(
    "derived_field",
    [
        "job_env",
        "executor",
        "run_id",
        "cmd_sha",
        "ssh_target",
        "backend",
        "remote_path",
        "total_tasks",
    ],
)
def test_patch_naming_derived_field_is_refused(tmp_path: Path, derived_field: str) -> None:
    """A patch key naming a CODE-DERIVED field is refused with SpecInvalid — the
    guard fires BEFORE any I/O (no journal/sidecar needed), and names the field."""
    with pytest.raises(errors.SpecInvalid) as excinfo:
        revise_resolved(
            tmp_path,
            spec=ReviseResolvedInput(
                scope_kind="run", scope_id=_RUN_ID, patch={derived_field: "whatever"}
            ),
        )
    msg = str(excinfo.value)
    assert derived_field in msg
    assert "DERIVED" in msg  # the targeted "you named a derived field" message


def test_patch_naming_unknown_field_is_refused(tmp_path: Path) -> None:
    """An unknown key (typo / outside the walk vocabulary) is refused too — the
    allowlist is exhaustive, so nothing is silently threaded."""
    with pytest.raises(errors.SpecInvalid) as excinfo:
        revise_resolved(
            tmp_path,
            spec=ReviseResolvedInput(scope_kind="run", scope_id=_RUN_ID, patch={"clsuter": "x"}),
        )
    assert "not resolver-owned input fields" in str(excinfo.value)


def test_empty_patch_is_refused(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        revise_resolved(
            tmp_path, spec=ReviseResolvedInput(scope_kind="run", scope_id=_RUN_ID, patch={})
        )


# ── the delta re-resolves: {cluster: X} re-derives job_env from X ─────────────


def test_cluster_patch_rederives_job_env_from_new_cluster(tmp_path: Path, monkeypatch: Any) -> None:
    """A ``{cluster: h2new}`` delta re-resolves with h2new's activation/job_env
    RE-DERIVED (CONDA_ENV=new_env), NOT the old cluster's — the finding-13 class
    (job_env dropped across a hand-carried retarget) closed by construction."""
    run_id = _setup(tmp_path, monkeypatch, base_resolved=dict(_BASE_RESOLVED))

    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=_fp(found=False)),
    ):
        res = revise_resolved(
            tmp_path,
            spec=ReviseResolvedInput(scope_kind="run", scope_id=run_id, patch={"cluster": "h2new"}),
        )

    assert res.stage_reached == "resolved"
    assert res.needs_decision is True
    assert res.applied_patch == {"cluster": "h2new"}
    # The patch is reflected in the amended resolved (for the audit + re-commit).
    assert res.brief["resolved"]["cluster"] == "h2new"

    # job_env is RE-DERIVED from h2new — the whole point.
    submit_spec = res.brief["resolve"]["submit_spec"]
    job_env = submit_spec["job_env"]
    assert job_env["CONDA_ENV"] == "new_env"  # h2new's env, re-derived
    assert job_env["CONDA_ENV"] != "old_env"  # NOT the stale hand-carried value
    assert job_env["CONDA_SOURCE"] == "/opt/new/conda.sh"
    # ssh_target + backend re-derived from clusters.yaml (else the finding-18/19
    # cross-check would have refused a stale value).
    assert submit_spec["ssh_target"] == "me@new.example.edu"
    assert submit_spec["cluster"] == "h2new"
    # remote_path re-anchored under the new cluster's scratch (leaf kept).
    assert submit_spec["remote_path"] == "/scratch/new/exp"


def test_walltime_patch_rederives_under_same_cluster(tmp_path: Path, monkeypatch: Any) -> None:
    """A resource delta (walltime_sec) re-resolves cleanly under the unchanged
    cluster — job_env still derives from h2old (no accidental retarget)."""
    run_id = _setup(tmp_path, monkeypatch, base_resolved=dict(_BASE_RESOLVED))

    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=_fp(found=False)),
    ):
        res = revise_resolved(
            tmp_path,
            spec=ReviseResolvedInput(
                scope_kind="run", scope_id=run_id, patch={"walltime_sec": 7200}
            ),
        )

    submit_spec = res.brief["resolve"]["submit_spec"]
    assert submit_spec["cluster"] == "h2old"
    assert submit_spec["job_env"]["CONDA_ENV"] == "old_env"  # unchanged cluster
    assert submit_spec["job_env"]["HPC_WALLTIME_SEC"] == "7200"  # the delta applied


def test_no_sidecar_is_refused(tmp_path: Path, monkeypatch: Any) -> None:
    """A scope with no resolved-run sidecar (the pre-resolve S1 boundary) is
    refused — revise-resolved amends a RESOLVED prior."""
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(tmp_path / "clusters.yaml"))
    (tmp_path / "clusters.yaml").write_text(_CLUSTERS_YAML, encoding="utf-8")
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    with pytest.raises(errors.SpecInvalid) as excinfo:
        revise_resolved(
            tmp_path,
            spec=ReviseResolvedInput(
                scope_kind="run", scope_id=_RUN_ID, patch={"cluster": "h2new"}
            ),
        )
    assert "no resolved-run sidecar" in str(excinfo.value)


# ── does NOT bypass the gates: a REQUIRED_CALLER delta faces the authorship gate ──


def test_goal_delta_is_allowed_but_faces_authorship_gate_at_recommit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A ``goal`` delta is ACCEPTED by revise-resolved (goal is caller-authored,
    not derived) and reflected in the amended resolved — but the re-commit via
    append-decision still faces the human-authorship gate, which refuses a goal
    that no human utterance backs. revise-resolved produces the brief; it does
    NOT bypass the gate (design §4)."""
    from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
    from hpc_agent.ops.decision.journal import append_decision as append_decision_gated

    # base has cluster (not goal) → goal is a FIRST commit at re-commit time.
    run_id = _setup(tmp_path, monkeypatch, base_resolved=dict(_BASE_RESOLVED))

    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=_cr()),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=_fp(found=False)),
    ):
        res = revise_resolved(
            tmp_path,
            spec=ReviseResolvedInput(
                scope_kind="run",
                scope_id=run_id,
                patch={"goal": "reduce variance across seeds"},
            ),
        )

    # Accepted + reflected — the guard does NOT refuse a caller-authored field.
    assert res.brief["resolved"]["goal"] == "reduce variance across seeds"

    # The re-commit faces the human-authorship gate (no utterance log backs the
    # new goal, response is a bare y) → refused. revise-resolved did not launder it.
    with pytest.raises(errors.SpecInvalid) as excinfo:
        append_decision_gated(
            experiment_dir=tmp_path,
            spec=AppendDecisionInput(
                scope_kind="run",
                scope_id=run_id,
                block="s1",
                response="y",
                resolved={**res.brief["resolved"], "next_block": "submit-s2"},
            ),
        )
    assert "human-authorship gate" in str(excinfo.value)
