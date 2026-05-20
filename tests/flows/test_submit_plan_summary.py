"""Tests for ``hpc_agent.atoms.submit_plan_summary``."""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.atoms.submit_plan_summary import summarize_submit_plan


def _spec(**overrides) -> dict:
    base = dict(
        profile="ml_ridge",
        cluster="hoffman2",
        ssh_target="alice@h2.idre.ucla.edu",
        remote_path="/u/scratch/alice",
        run_id="r1",
        total_tasks=42,
        backend="sge",
        job_name="ml_ridge",
        script=".hpc/templates/cpu_array.sh",
        job_env={},
    )
    base.update(overrides)
    return base


def test_basic_summary_has_headline_body_confirm() -> None:
    out = summarize_submit_plan(_spec())
    assert "Ready to submit" in out["headline"]
    assert "ml_ridge" in out["headline"]
    assert "hoffman2" in out["headline"]
    assert "42 tasks" in out["headline"]
    assert "profile:      ml_ridge" in out["body"]
    assert "cluster:      hoffman2" in out["body"]
    assert "total_tasks:  42" in out["body"]
    assert out["confirm_prompt"] == "Confirm? [y/N]"


def test_large_task_count_warns_in_confirm_prompt() -> None:
    out = summarize_submit_plan(_spec(total_tasks=5000))
    assert "5000 tasks" in out["confirm_prompt"]
    assert ">1000" in out["confirm_prompt"]


def test_resources_block_renders_when_present() -> None:
    out = summarize_submit_plan(_spec(resources={"cpus": 8, "mem": "64G", "walltime": "06:00:00"}))
    assert "resources:    cpus=8, mem=64G, walltime=06:00:00" in out["body"]


def test_runtime_only_rendered_when_present() -> None:
    out = summarize_submit_plan(_spec())
    assert "runtime:" not in out["body"]
    out_uv = summarize_submit_plan(_spec(runtime="uv"))
    assert "runtime:      uv" in out_uv["body"]


def test_campaign_id_only_rendered_when_present() -> None:
    out = summarize_submit_plan(_spec())
    assert "campaign_id:" not in out["body"]
    out_c = summarize_submit_plan(_spec(campaign_id="ml_q1"))
    assert "campaign_id:  ml_q1" in out_c["body"]


def test_canary_off_rendered_explicitly() -> None:
    out = summarize_submit_plan(_spec(canary=False))
    assert "canary:       off" in out["body"]


def test_partial_ok_only_rendered_when_true() -> None:
    out = summarize_submit_plan(_spec())
    assert "partial_ok:" not in out["body"]
    out_p = summarize_submit_plan(_spec(partial_ok=True))
    assert "partial_ok:   on" in out_p["body"]


def test_byte_stable_for_same_input() -> None:
    a = summarize_submit_plan(_spec(runtime="uv", campaign_id="c1"))
    b = summarize_submit_plan(_spec(runtime="uv", campaign_id="c1"))
    assert a == b


def test_missing_required_key_raises() -> None:
    bad = _spec()
    del bad["cluster"]
    with pytest.raises(errors.SpecInvalid, match="cluster"):
        summarize_submit_plan(bad)


def test_non_dict_input_raises() -> None:
    with pytest.raises(errors.SpecInvalid, match="must be a dict"):
        summarize_submit_plan("not a dict")  # type: ignore[arg-type]
