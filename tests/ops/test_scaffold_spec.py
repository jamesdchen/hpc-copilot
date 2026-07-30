"""Tests for the ``scaffold-spec`` primitive (#287).

Pins the context-populated skeleton: each supported verb's emitted spec
VALIDATES against that verb's own input model (the "refuses to emit a spec
the verb would reject" guarantee), non-derivable required fields come back
as schema-valid placeholders flagged in ``unresolved_fields``, and the
clusters.yaml population emits only the COHERENT conda pair (#281).

The warm-path correctness tests drive the scaffolder helpers directly with
a hand-built :class:`_Context` (no clusters.yaml / tasks.py fixtures); the
cold + error paths drive the public :func:`scaffold_spec` on a bare dir.
"""

from __future__ import annotations

from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
from hpc_agent._wire.actions.classify_axis import ClassifyAxisInput
from hpc_agent._wire.actions.interview import InterviewSpec
from hpc_agent._wire.workflows.campaign_run import CampaignRunSpec
from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec
from hpc_agent._wire.workflows.validate_campaign import ValidateCampaignSpec
from hpc_agent.infra.clusters import ClusterConfig
from hpc_agent.ops.scaffold_spec import (
    _Acc,
    _build_submit_block,
    _Context,
    _scaffold_interview,
    _scaffold_resolve_submit_inputs,
    scaffold_spec,
)

_SUPPORTED = [
    "build-submit-spec",
    "campaign-run",
    "classify-axis",
    "interview",
    "resolve-submit-inputs",
    "validate-campaign",
]

# A minimal ``@register_run`` the AST scan (``discover-runs``) finds, so the
# classify-axis scaffolder can DERIVE ``run_signature_sha`` rather than
# placeholder it.
_REGISTER_RUN_SRC = """\
from __future__ import annotations
from hpc_agent.experiment_kit import register_run


@register_run
def train(seed: int, lr: float) -> dict:
    return {"seed": seed, "lr": lr}
"""


def _warm_ctx(**overrides: Any) -> _Context:
    """A fully-resolved context: a real ClusterConfig + run_id/cmd_sha + latest_run."""
    cfg = ClusterConfig(
        host="login.test.edu",
        user="jdoe",
        scheduler="slurm",
        scratch="/scratch/jdoe",
        conda_source="/opt/conda/etc/profile.d/conda.sh",
        conda_envs=["hpc-test"],
    )
    base = {
        "cluster_name": "test",
        "cluster_cfg": cfg,
        "run_name": "train",
        "latest_run": {
            "task_count": 64,
            "remote_path": "/scratch/jdoe/train",
            "runtime": "uv",
            "result_dir_template": "results/{run_id}/task_{task_id}",
            "campaign_id": None,
        },
        "run_id": "train-abcd1234",
        "cmd_sha": "a" * 64,
    }
    base.update(overrides)
    return _Context(**base)  # type: ignore[arg-type]


class TestErrorPaths:
    def test_unsupported_verb_raises_with_supported_list(self) -> None:
        with pytest.raises(errors.SpecInvalid) as exc:
            scaffold_spec(experiment_dir=__import__("pathlib").Path("."), verb="not-a-verb")
        assert "build-submit-spec" in str(exc.value)


class TestColdStartValidatesWithPlaceholders:
    def test_build_submit_spec_skeleton_validates(self, tmp_path: Any) -> None:
        res = scaffold_spec(experiment_dir=tmp_path, verb="build-submit-spec")
        assert res.verb == "build-submit-spec"
        assert sorted(res.supported_verbs) == _SUPPORTED
        # The whole point of #287: the skeleton validates, so the target
        # verb won't reject its shape.
        BuildSubmitSpecInput.model_validate(res.spec)
        # No tasks.py on a bare dir → run_id/cmd_sha are placeholders.
        assert "run_id" in res.unresolved_fields
        assert "cmd_sha" in res.unresolved_fields
        # Every unresolved field carries a placeholder marker in sources.
        for path in res.unresolved_fields:
            assert "placeholder" in res.sources[path]

    def test_resolve_submit_inputs_nested_skeleton_validates(self, tmp_path: Any) -> None:
        res = scaffold_spec(experiment_dir=tmp_path, verb="resolve-submit-inputs")
        ResolveSubmitInputsSpec.model_validate(res.spec)
        assert set(res.spec) >= {"run_name", "submit", "sidecar"}
        # Nested placeholders use dotted paths.
        assert "submit.run_id" in res.unresolved_fields
        assert "sidecar.executor" in res.unresolved_fields  # never derivable from context

    def test_validate_campaign_skeleton_validates(self, tmp_path: Any) -> None:
        res = scaffold_spec(experiment_dir=tmp_path, verb="validate-campaign")
        ValidateCampaignSpec.model_validate(res.spec)

    def test_campaign_run_scaffolds_nested_skeleton(self, tmp_path: Any) -> None:
        # #287's worst offender: the 3-level submit-pipeline → submit-and-verify
        # → submit-flow nesting, plus the monitor/aggregate run_ids.
        res = scaffold_spec(experiment_dir=tmp_path, verb="campaign-run")
        CampaignRunSpec.model_validate(res.spec)
        assert set(res.spec) >= {"submit", "status", "aggregate"}
        assert "submit.submit.submit.run_id" in res.unresolved_fields
        assert "submit.submit.submit.job_env" in res.unresolved_fields
        assert "status.monitor.run_id" in res.unresolved_fields
        assert "aggregate.run_id" in res.unresolved_fields


class TestWarmPopulation:
    def test_build_submit_block_fully_resolved(self) -> None:
        acc = _Acc()
        d = _build_submit_block(_warm_ctx(), acc, "")
        assert d["ssh_target"] == "jdoe@login.test.edu"  # from ClusterConfig.ssh_target
        assert d["backend"] == "slurm"  # from clusters.yaml#scheduler
        assert d["run_id"] == "train-abcd1234"  # real, from compute-run-id
        assert d["cmd_sha"] == "a" * 64
        assert d["total_tasks"] == 64  # from latest_run
        assert d["remote_path"] == "/scratch/jdoe/train"
        assert d["runtime"] == "uv"
        # Nothing left unresolved on the warm path.
        assert acc.unresolved == []
        BuildSubmitSpecInput.model_validate(d)

    def test_remote_path_derived_from_scratch_when_no_latest(self) -> None:
        acc = _Acc()
        d = _build_submit_block(_warm_ctx(latest_run={}), acc, "")
        assert d["remote_path"] == "/scratch/jdoe/train"  # scratch + run_name

    def test_no_cluster_cfg_uses_placeholders(self) -> None:
        # No clusters.yaml match → cluster-derived required fields are placeholders.
        acc = _Acc()
        d = _build_submit_block(_warm_ctx(cluster_cfg=None, cluster_name="ghost"), acc, "")
        assert d["backend"] == "slurm"  # placeholder — no scheduler to read
        assert d["ssh_target"] == "USER@HOST"  # placeholder — no ClusterConfig
        assert "backend" in acc.unresolved
        assert "ssh_target" in acc.unresolved


class TestCondaCoherence281:
    def test_emits_both_conda_fields_when_coherent(self) -> None:
        acc = _Acc()
        d = _build_submit_block(_warm_ctx(), acc, "")
        assert d["conda_source"] == "/opt/conda/etc/profile.d/conda.sh"
        assert d["conda_env"] == "hpc-test"

    def test_omits_conda_env_without_conda_source(self) -> None:
        # The #281 incoherent state (conda_env set, conda_source empty) must
        # never be emitted — it crashes the cluster preamble. Drop both.
        cfg = ClusterConfig(host="h", user="u", scheduler="sge", conda_envs=["env1"])
        acc = _Acc()
        d = _build_submit_block(_warm_ctx(cluster_cfg=cfg), acc, "")
        assert "conda_env" not in d
        assert "conda_source" not in d


class TestResolveComposesBlocks:
    def test_run_id_threads_into_both_blocks(self) -> None:
        acc = _Acc()
        spec = _scaffold_resolve_submit_inputs(_warm_ctx(), acc)
        assert spec["run_name"] == "train"
        assert spec["submit"]["run_id"] == "train-abcd1234"
        assert spec["sidecar"]["run_id"] == "train-abcd1234"
        # sidecar.executor is never derivable → still unresolved even on the warm path.
        assert "sidecar.executor" in acc.unresolved
        ResolveSubmitInputsSpec.model_validate(spec)


class TestInterviewScaffold:
    """Coverage for the ``interview`` verb — entry verb for hpc-wrap-entry-point.

    Closes the demo-session gap: the orchestrator hand-built an InterviewSpec
    after emit-skill-return and burned 7m+ on schema-divination. The
    scaffolder emits ``goal`` + ``task_generator`` + ``produced_by`` as
    typed placeholders so the caller mutates rather than synthesizes a
    discriminated-union node from scratch.
    """

    def test_cold_start_skeleton_validates(self, tmp_path: Any) -> None:
        res = scaffold_spec(experiment_dir=tmp_path, verb="interview")
        assert res.verb == "interview"
        assert "interview" in res.supported_verbs
        # The #287 guarantee: the emitted spec passes the target verb's own model.
        InterviewSpec.model_validate(res.spec)
        # The two fields the task spec calls out: caller MUST override goal +
        # task_generator (the wrap-entry-point skill always supplies a recipe).
        assert "goal" in res.unresolved_fields
        assert "task_generator" in res.unresolved_fields
        # Every unresolved field carries a placeholder marker in sources.
        for path in res.unresolved_fields:
            assert "placeholder" in res.sources[path]

    def test_register_run_default_omits_data_axis_hint(self, tmp_path: Any) -> None:
        # #260 — the register_run entry-point schema rejects data_axis_hint.
        # On a bare experiment dir (no detect-entry-point candidates) the
        # scaffolder must fall back to register_run and NEVER emit the field.
        acc = _Acc()
        spec = _scaffold_interview(
            _Context(
                cluster_name=None,
                cluster_cfg=None,
                run_name=None,
                latest_run={},
                run_id=None,
                cmd_sha=None,
                experiment_dir=tmp_path,
            ),
            acc,
        )
        ep = spec["entry_point"]
        assert ep["kind"] == "register_run"
        assert "data_axis_hint" not in ep
        # And the whole skeleton still validates.
        InterviewSpec.model_validate(spec)

    def test_shell_command_sanitizes_non_identifier_run_name(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A hyphen/dot-bearing run name must NOT hard-fail the shell_command
        skeleton (bug-sweep #49): the pattern-constrained run_name degrades to a
        placeholder (flagged unresolved) instead of raising SpecInvalid."""
        import hpc_agent.ops.scaffold_spec as ss

        monkeypatch.setattr(
            ss,
            "_detect_entry_point_candidates",
            lambda *_a, **_k: ("shell_command", [{"argv_kind": "shell", "path": "run.sh"}]),
        )
        acc = _Acc()
        spec = _scaffold_interview(
            _Context(
                cluster_name=None,
                cluster_cfg=None,
                run_name="causal-tune.v2",  # not ^[a-zA-Z_][a-zA-Z0-9_]*$
                latest_run={},
                run_id=None,
                cmd_sha=None,
                experiment_dir=tmp_path,
            ),
            acc,
        )
        ep = spec["entry_point"]
        assert ep["kind"] == "shell_command"
        # The invalid run name was replaced by the placeholder, not inserted verbatim.
        assert ep["run_name"] == "placeholder_run"
        assert "entry_point.run_name" in acc.unresolved
        assert any("not identifier-shaped" in w for w in acc.warnings)
        # The #287 guarantee holds: the whole skeleton still validates.
        InterviewSpec.model_validate(spec)

    def test_supported_verbs_list_includes_interview(self, tmp_path: Any) -> None:
        res = scaffold_spec(experiment_dir=tmp_path, verb="build-submit-spec")
        assert sorted(res.supported_verbs) == _SUPPORTED


class TestClassifyAxisScaffold:
    """Coverage for the ``classify-axis`` verb — the second PRELUDE spec (P1.b).

    ``suggest-prelude-action``'s rung 7 hands this spec back to the caller, so it
    must be composable from context. The sharp field is ``run_signature_sha``: it
    is the exact key ``axes.yaml`` uses to invalidate a stale classification, so a
    hand-typed one silently binds the wrong signature — the scaffolder derives it
    from the ``discover-runs`` AST scan instead.
    """

    def test_cold_start_skeleton_validates_with_placeholders(self, tmp_path: Any) -> None:
        res = scaffold_spec(experiment_dir=tmp_path, verb="classify-axis")
        assert res.verb == "classify-axis"
        assert "classify-axis" in res.supported_verbs
        # The #287 guarantee: the emitted spec passes the target verb's own model.
        ClassifyAxisInput.model_validate(res.spec)
        # Nothing is derivable on a bare dir — both identity fields are flagged.
        assert "run_name" in res.unresolved_fields
        assert "run_signature_sha" in res.unresolved_fields
        for path in res.unresolved_fields:
            assert "placeholder" in res.sources[path]

    def test_data_axis_is_always_unresolved_and_fail_safe(self, tmp_path: Any) -> None:
        """The classification is a JUDGMENT point — never presented as resolved."""
        (tmp_path / "train.py").write_text(_REGISTER_RUN_SRC, encoding="utf-8")
        res = scaffold_spec(experiment_dir=tmp_path, verb="classify-axis", run_name="train")
        assert "data_axis" in res.unresolved_fields
        # 'sequential' is the classifier's own fail-safe: a caller who invokes the
        # skeleton unedited under-parallelizes rather than returning wrong numbers.
        assert res.spec["data_axis"] == {"kind": "sequential"}
        assert "judgment point" in res.sources["data_axis"]

    def test_run_signature_sha_is_derived_from_the_ast_scan(self, tmp_path: Any) -> None:
        from hpc_agent.state.discover import discover_runs

        (tmp_path / "train.py").write_text(_REGISTER_RUN_SRC, encoding="utf-8")
        expected = next(i.run_signature_sha for i in discover_runs(tmp_path) if i.name == "train")
        res = scaffold_spec(experiment_dir=tmp_path, verb="classify-axis", run_name="train")
        assert res.spec["run_name"] == "train"
        assert res.spec["run_signature_sha"] == expected
        # Derived, not placeholdered.
        assert "run_signature_sha" not in res.unresolved_fields
        assert "discover-runs AST scan" in res.sources["run_signature_sha"]
        ClassifyAxisInput.model_validate(res.spec)

    def test_unknown_run_name_degrades_to_a_placeholder_with_a_warning(self, tmp_path: Any) -> None:
        (tmp_path / "train.py").write_text(_REGISTER_RUN_SRC, encoding="utf-8")
        res = scaffold_spec(experiment_dir=tmp_path, verb="classify-axis", run_name="absent_run")
        assert res.spec["run_name"] == "absent_run"
        assert "run_signature_sha" in res.unresolved_fields
        assert any("no @register_run named 'absent_run'" in w for w in res.warnings)
        ClassifyAxisInput.model_validate(res.spec)

    def test_classified_by_defaults_to_agent_and_is_disclosed(self, tmp_path: Any) -> None:
        res = scaffold_spec(experiment_dir=tmp_path, verb="classify-axis")
        assert res.spec["classified_by"] == "agent"
        assert "classified_by" not in res.unresolved_fields
        assert "override to 'interview'" in res.sources["classified_by"]
