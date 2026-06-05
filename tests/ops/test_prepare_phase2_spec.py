"""Tests for the ``prepare-phase2-spec`` primitive (#279).

Pins the two-phase canary gate's Phase-1 → Phase-2 transform: the spec
the worker hands to the Phase-2 main-array ``submit-flow`` is the Phase-1
spec with EXACTLY three deterministic flips and nothing else changed —

* ``canary: false``        (the canary already ran in Phase 1)
* ``canary_only: false``   (Phase 2 IS the main-array launch)
* ``skip_rsync_deploy: true`` (Phase 1 already deployed the tree)

— validated locally against ``SubmitFlowSpec`` so a malformed Phase-1
spec surfaces a typed ``SpecInvalid`` instead of a cluster round-trip.

Calls :func:`prepare_phase2_spec` directly with a dict, mirroring
``test_resolve_resources.py``; no SSH, no journal, no registry needed.
"""

from __future__ import annotations

from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent.ops.prepare_phase2_spec import prepare_phase2_spec


def _phase1_spec(**overrides: Any) -> dict[str, Any]:
    """A minimal VALID Phase-1 (``canary_only``) SubmitFlowSpec dict.

    Covers every required field of ``SubmitFlowSpec`` (profile, cluster,
    ssh_target, remote_path, job_name, run_id, total_tasks, backend,
    script, job_env) plus the two-phase-canary flags set the Phase-1 way
    (``canary=true``, ``canary_only=true``, ``skip_rsync_deploy=false``).
    Overrides are merged on top so a test can mutate a single field.
    """
    spec: dict[str, Any] = {
        "profile": "train",
        "cluster": "hoffman2",
        "ssh_target": "user@login.hoffman2.edu",
        "remote_path": "/u/scratch/u/user/run",
        "job_name": "train_array",
        "run_id": "train_2026-06-05",
        "total_tasks": 64,
        "backend": "sge",
        "script": ".hpc/templates/cpu_array.sh",
        "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py", "HPC_RUN_ID": "train_2026-06-05"},
        # Phase-1 two-phase-canary flags.
        "canary": True,
        "canary_only": True,
        "skip_rsync_deploy": False,
        # A non-flipped optional field, to prove it is preserved verbatim.
        "partial_ok": True,
    }
    spec.update(overrides)
    return spec


class TestFlips:
    def test_three_flips_applied(self) -> None:
        out = prepare_phase2_spec(spec=_phase1_spec())
        phase2 = out["phase2_spec"]
        assert phase2["canary"] is False
        assert phase2["canary_only"] is False
        assert phase2["skip_rsync_deploy"] is True

    def test_every_other_field_preserved(self) -> None:
        spec = _phase1_spec()
        out = prepare_phase2_spec(spec=spec)
        phase2 = out["phase2_spec"]
        flipped = {"canary", "canary_only", "skip_rsync_deploy"}
        # Every non-flipped key is byte-identical to the Phase-1 spec...
        for key, value in spec.items():
            if key not in flipped:
                assert phase2[key] == value, key
        # ...and the transform introduces no new keys.
        assert set(phase2) == set(spec)
        # The non-flipped optional field rode through untouched.
        assert phase2["partial_ok"] is True

    def test_does_not_mutate_input(self) -> None:
        spec = _phase1_spec()
        prepare_phase2_spec(spec=spec)
        # The Phase-1 dict the caller passed is left intact.
        assert spec["canary"] is True
        assert spec["canary_only"] is True
        assert spec["skip_rsync_deploy"] is False


class TestFlipsApplied:
    def test_reports_the_three_flips(self) -> None:
        out = prepare_phase2_spec(spec=_phase1_spec())
        assert out["flips_applied"] == {
            "canary": False,
            "canary_only": False,
            "skip_rsync_deploy": True,
        }

    def test_output_shape(self) -> None:
        out = prepare_phase2_spec(spec=_phase1_spec())
        assert set(out) == {"phase2_spec", "flips_applied"}


class TestPhase2Validates:
    def test_phase2_is_a_valid_submit_flow_spec(self) -> None:
        # The whole point of #279: the derived spec validates against
        # SubmitFlowSpec, so it can be handed straight to submit-flow.
        from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

        out = prepare_phase2_spec(spec=_phase1_spec())
        model = SubmitFlowSpec.model_validate(out["phase2_spec"])
        assert model.canary is False
        assert model.canary_only is False
        assert model.skip_rsync_deploy is True


class TestInvalidPhase1:
    def test_zero_total_tasks_raises_spec_invalid(self) -> None:
        # total_tasks has ge=1; the flips can't fix a structurally bad spec.
        with pytest.raises(errors.SpecInvalid):
            prepare_phase2_spec(spec=_phase1_spec(total_tasks=0))

    def test_missing_required_field_raises_spec_invalid(self) -> None:
        spec = _phase1_spec()
        del spec["backend"]
        with pytest.raises(errors.SpecInvalid):
            prepare_phase2_spec(spec=spec)
