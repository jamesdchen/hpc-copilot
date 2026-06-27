"""End-to-end proof for the #220 Phase-1 ``DeterministicCampaignResolver``.

The neutral tick-loop (``_kernel/lifecycle/drive.py``) resolves a judgement
(``kind == "agent"``) step through an injected :data:`JudgementResolver`. The
default spawns an LLM worker; this test drives the **deterministic** resolver
that executes the same ``decide`` step in code by chaining the existing
primitives — and proves three things the issue requires:

1. A ``decide`` step over a seeded strategy-driven campaign resolves to a
   ``continue`` decision and produces the next submit, with **zero worker /
   LLM spawn**. Only the cluster-I/O seam (``submit_fn``) is stubbed — no SSH,
   no qsub.
2. A residue case (an unclassifiable ``tasks.py``) **halts-and-parks**:
   non-zero exit, the escalation surfaced as data, and **no blind submit**.
3. The synthesized :class:`WorkerReport` round-trips through
   ``parse_worker_report`` / ``WorkerReport.model_validate`` — it is a
   contract-valid report, the same validation the LLM path's reports pass.

Fixture style mirrors ``tests/ops/test_resolve_and_recover_opt_in_e2e.py``:
``journal_home`` monkeypatches ``run_record.HPC_HOMEDIR`` so the journal lands
in tmp, and ``experiment`` is a tmp experiment dir.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hpc_agent._kernel.extension.spawn_prompt import (
    WorkerDecision,
    WorkerReport,
    parse_worker_report,
)
from hpc_agent._wire.actions.submit import SubmitSpec as _WireSubmitSpec
from hpc_agent.meta.campaign.cursor import read_cursor
from hpc_agent.meta.campaign.deterministic_resolver import (
    DeterministicCampaignResolver,
    deterministic_campaign_config,
)
from hpc_agent.ops.submit.runner import submit_and_record
from hpc_agent.state import run_record
from hpc_agent.state.runs import write_run_sidecar

_CAMPAIGN_ID = "tune_lr"
_PROFILE = "ml_tune"
_CLUSTER = "c"
_SSH = "user@host"
_REMOTE = "/remote/exp"

# A strategy-driven tasks.py: the guarded ``import optuna`` makes
# classify-campaign-path's AST scan emit a ``strategy`` (decided_by=code)
# verdict, while the module still imports cleanly (so compute-run-id can
# materialize the task list) even when optuna is not installed.
_STRATEGY_TASKS_PY = """\
try:
    import optuna  # noqa: F401
except ImportError:
    optuna = None


def total():
    return 2


def resolve(i):
    return {"seed": i, "lr": 0.1 + 0.01 * i, "trial_token": i}
"""

# A tasks.py that does NOT parse — classify-campaign-path escalates
# (decided_by=judgement, path=unclassifiable). The residue case.
_UNPARSEABLE_TASKS_PY = "def total(:\n    this is not python\n"


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _write_tasks_py(experiment_dir: Path, body: str) -> None:
    hpc = experiment_dir / ".hpc"
    hpc.mkdir(parents=True, exist_ok=True)
    (hpc / "tasks.py").write_text(body, encoding="utf-8")


def _seed_prior_iteration(experiment_dir: Path, *, run_id: str, loss: float) -> None:
    """Seed one completed campaign iteration: a sidecar + a journal record + metrics.

    The sidecar carries the v2 config snapshot the resolver rebuilds the next
    submit from; the journal record carries the submit-target identity
    (ssh_target / cluster / profile / remote_path) — written through the SAME
    ``submit_and_record`` sink a real submit uses.
    """
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="0" * 12,
        hpc_agent_version="0.0.0+test",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 run.py --seed $SEED",
        result_dir_template="results/{run_id}/{task_id}",
        task_count=2,
        tasks_py_sha="0" * 12,
        campaign_id=_CAMPAIGN_ID,
        profile=_PROFILE,
        cluster=_CLUSTER,
        remote_path=_REMOTE,
        runtime="uv",
        # The env-activation snapshot the run used; the resolver reuses it to
        # rebuild the next iteration's activation (build-submit-spec requires
        # at least one of modules / conda_source / conda_env).
        env={"conda_source": "/opt/conda/etc/profile.d/conda.sh", "conda_env": "ml"},
    )
    submit_and_record(
        experiment_dir,
        spec=_WireSubmitSpec(
            profile=_PROFILE,
            cluster=_CLUSTER,
            ssh_target=_SSH,
            remote_path=_REMOTE,
            job_name="tunejob",
            run_id=run_id,
            job_ids=["9000"],
            total_tasks=2,
            campaign_id=_CAMPAIGN_ID,
        ),
        script=".hpc/templates/cpu_array.sh",
        backend="slurm",
    )
    # Mark the run complete so campaign-advance sees no in-flight run and a
    # finished iteration (otherwise it returns wait_in_flight, not continue).
    from hpc_agent.state.journal import mark_run

    mark_run(experiment_dir, run_id, status="complete", stage="done")

    metrics_dir = experiment_dir / "results" / run_id / "0"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "metrics.json").write_text(json.dumps({"loss": loss}))


class _SubmitStub:
    """Stand-in for the cluster I/O seam — records the spec, fires no SSH/qsub."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, experiment_dir: Path, submit_spec: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(submit_spec)
        return {
            "run_id": submit_spec.get("run_id", "next-run"),
            "job_ids": ["9100"],
            "total_tasks": submit_spec.get("total_tasks", 2),
            "deduped": False,
            "canary_done": False,
            "main_launched": True,
        }


def _decide_spawn_request(experiment_dir: Path) -> dict[str, Any]:
    """The ``decide`` spawn_request load-context's delegate emits for this step."""
    return {
        "workflow": "campaign",
        "experiment_dir": str(experiment_dir),
        "fields": {"campaign_id": _CAMPAIGN_ID, "step": "decide"},
    }


def test_decide_continue_produces_next_submit_with_no_llm_spawn(
    journal_home: Path, experiment: Path
) -> None:
    """A ``decide`` over a seeded strategy campaign resolves continue → submit.

    The resolver chains classify-campaign-path (code: strategy) →
    campaign-advance (continue) → resolve-submit-inputs → the injected submit
    seam → advance_cursor. No LLM is spawned (the resolver never calls
    run_workflow); the only side effect is the stubbed submit.
    """
    _write_tasks_py(experiment, _STRATEGY_TASKS_PY)
    _seed_prior_iteration(experiment, run_id="iter0", loss=0.9)

    submit = _SubmitStub()
    resolver = DeterministicCampaignResolver(submit_fn=submit)

    report, exit_code = resolver(_decide_spawn_request(experiment), experiment)

    assert exit_code == 0
    # The next iteration was actually submitted through the (stubbed) seam.
    assert len(submit.calls) == 1
    submitted_spec = submit.calls[0]
    assert submitted_spec["campaign_id"] == _CAMPAIGN_ID
    # The resolver reported the deterministic code decisions it walked.
    points = {d.point: d.outcome for d in report.decisions}
    assert points["path"] == "strategy"
    assert points["decide"] == "continue"
    # The result names the submitted run + advanced cursor.
    assert report.result["submitted"] is True
    assert report.result["job_ids"] == ["9100"]

    # The campaign cursor advanced by one iteration.
    cursor = read_cursor(experiment, _CAMPAIGN_ID)
    assert cursor is not None
    assert cursor["iteration"] == 1


def test_decide_report_round_trips_through_parse_worker_report(
    journal_home: Path, experiment: Path
) -> None:
    """The synthesized report is contract-valid — it passes the SAME
    parse_worker_report validation the LLM path's reports must pass (decision
    points enumerated for `campaign`; judgement points carry a non-empty why)."""
    _write_tasks_py(experiment, _STRATEGY_TASKS_PY)
    _seed_prior_iteration(experiment, run_id="iter0", loss=0.9)

    submit = _SubmitStub()
    resolver = DeterministicCampaignResolver(submit_fn=submit)
    report, _ = resolver(_decide_spawn_request(experiment), experiment)

    reparsed = parse_worker_report(json.dumps(report.model_dump(mode="json")), workflow="campaign")
    assert isinstance(reparsed, WorkerReport)
    # model_validate accepts it too.
    assert WorkerReport.model_validate(report.model_dump(mode="json")).decisions


def test_unclassifiable_path_halts_and_parks(journal_home: Path, experiment: Path) -> None:
    """A residue case: an unparseable tasks.py escalates at classify-campaign-path.

    The resolver halts-and-parks — non-zero exit, the escalation surfaced as
    data in the report, and NO blind submit. It never guesses manual-vs-strategy.
    """
    _write_tasks_py(experiment, _UNPARSEABLE_TASKS_PY)
    _seed_prior_iteration(experiment, run_id="iter0", loss=0.9)

    submit = _SubmitStub()
    resolver = DeterministicCampaignResolver(submit_fn=submit)

    report, exit_code = resolver(_decide_spawn_request(experiment), experiment)

    assert exit_code != 0  # halt-and-park, not proceed
    assert submit.calls == []  # no blind submit
    assert report.result["residue"] is True
    assert report.result["point"] == "path"
    assert "ESCALATION" in report.anomalies
    # The parked report is still contract-valid.
    assert parse_worker_report(json.dumps(report.model_dump(mode="json")), workflow="campaign")
    # The cursor did NOT advance — nothing was submitted.
    assert read_cursor(experiment, _CAMPAIGN_ID) is None


def _escalating_classify(**_kwargs: Any) -> dict[str, Any]:
    """A classify-campaign-path verdict that escalates (the judgement tail)."""
    return {
        "decided_by": "judgement",
        "path": None,
        "reason": "ambiguous markers: both a manual grid and a strategy import found",
        "candidates": ["manual", "strategy"],
    }


def test_resolved_path_hint_unparks_classify_escalation(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fields.resolved.path`` breaks the tie classify-campaign-path couldn't.

    The pre-resolved-values channel an LlmJudgementResolver (or any
    orchestrator answering a park) writes into: with classify escalating,
    a resolved 'strategy' hint lets the decide chain continue all the way
    to the submit seam instead of parking. The recorded path decision
    carries the adjudication provenance in its why.
    """
    _write_tasks_py(experiment, _STRATEGY_TASKS_PY)
    _seed_prior_iteration(experiment, run_id="iter0", loss=0.9)
    monkeypatch.setattr(
        "hpc_agent.incorporation.classify_campaign_path.classify_campaign_path",
        _escalating_classify,
    )

    submit = _SubmitStub()
    resolver = DeterministicCampaignResolver(submit_fn=submit)
    request = _decide_spawn_request(experiment)
    request["fields"]["resolved"] = {"path": "strategy"}

    report, exit_code = resolver(request, experiment)

    assert exit_code == 0
    assert len(submit.calls) == 1
    points = {d.point: d for d in report.decisions}
    assert points["path"].outcome == "strategy"
    assert "fields.resolved" in points["path"].why
    assert read_cursor(experiment, _CAMPAIGN_ID)["iteration"] == 1  # type: ignore[index]


def test_resolved_hint_never_overrides_confident_classification(
    journal_home: Path, experiment: Path
) -> None:
    """The hint only breaks ties: a confident code classification wins even
    when the caller pre-resolved a contradictory value."""
    _write_tasks_py(experiment, _STRATEGY_TASKS_PY)  # classifies 'strategy' by code
    _seed_prior_iteration(experiment, run_id="iter0", loss=0.9)

    submit = _SubmitStub()
    resolver = DeterministicCampaignResolver(submit_fn=submit)
    request = _decide_spawn_request(experiment)
    request["fields"]["resolved"] = {"path": "manual"}  # contradicts the AST scan

    report, exit_code = resolver(request, experiment)

    assert exit_code == 0
    points = {d.point: d for d in report.decisions}
    assert points["path"].outcome == "strategy"  # code evidence won
    assert "fields.resolved" not in points["path"].why


def test_llm_resolver_bridge_continues_campaign_end_to_end(
    journal_home: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full bridge: code parks → one structured() adjudication → continue.

    LlmJudgementResolver wraps DeterministicCampaignResolver; classify
    escalates; the (fake) model picks 'strategy' from the closed menu; the
    decision feeds back through fields.resolved; the retried decide chain
    runs to the submit seam and advances the cursor. Exactly ONE model
    call, zero worker spawns — the granular-control consumption mode.
    """
    from hpc_agent._kernel.lifecycle.llm_resolver import LlmJudgementResolver

    _write_tasks_py(experiment, _STRATEGY_TASKS_PY)
    _seed_prior_iteration(experiment, run_id="iter0", loss=0.9)
    monkeypatch.setattr(
        "hpc_agent.incorporation.classify_campaign_path.classify_campaign_path",
        _escalating_classify,
    )

    class _Model:
        name = "fake"
        calls = 0

        def complete(self, messages: list[Any], *, schema: dict | None = None) -> str:
            type(self).calls += 1
            return json.dumps(
                {"chosen": "strategy", "why": "tasks.py consumes prior-iteration history"}
            )

    submit = _SubmitStub()
    resolver = LlmJudgementResolver(
        inner=DeterministicCampaignResolver(submit_fn=submit),
        model=_Model(),
        menu={"path": ["manual", "strategy"]},
    )

    report, exit_code = resolver(_decide_spawn_request(experiment), experiment)

    assert exit_code == 0
    assert _Model.calls == 1
    assert len(submit.calls) == 1
    assert report.result["submitted"] is True
    # Audit trail: the adjudication (model's why) AND the resolved path
    # decision (caller-resolved provenance) both ride the final report,
    # and it round-trips the worker-report contract.
    path_decisions = [d for d in report.decisions if d.point == "path"]
    whys = " | ".join(d.why for d in path_decisions)
    assert "consumes prior-iteration history" in whys
    assert "fields.resolved" in whys
    assert parse_worker_report(json.dumps(report.model_dump(mode="json")), workflow="campaign")
    assert read_cursor(experiment, _CAMPAIGN_ID)["iteration"] == 1  # type: ignore[index]


def test_decide_stop_decision_is_clean_terminal_not_residue(
    journal_home: Path, experiment: Path
) -> None:
    """A non-`continue` decision (here: max_iters convergence) is a DECIDED
    clean terminal — exit 0, no submit, no residue flag — not an escalation."""
    _write_tasks_py(experiment, _STRATEGY_TASKS_PY)
    _seed_prior_iteration(experiment, run_id="iter0", loss=0.9)
    # A manifest with max_iters=1 makes campaign-advance decide stop_converged
    # after the one seeded iteration.
    from hpc_agent.meta.campaign.manifest import write_manifest

    write_manifest(
        experiment,
        campaign_id=_CAMPAIGN_ID,
        stop_criteria={"max_iters": 1},
    )

    submit = _SubmitStub()
    resolver = DeterministicCampaignResolver(submit_fn=submit)
    report, exit_code = resolver(_decide_spawn_request(experiment), experiment)

    assert exit_code == 0
    assert submit.calls == []  # a stop decision submits nothing
    assert "residue" not in report.result  # a decided stop is not an escalation
    decide = next(d for d in report.decisions if d.point == "decide")
    assert decide.outcome == "stop_converged"
    assert read_cursor(experiment, _CAMPAIGN_ID) is None


def test_cold_submit_with_no_prior_run_halts_and_parks(
    journal_home: Path, experiment: Path
) -> None:
    """A cold ``submit`` with no prior run to rebuild the context from is the
    executor-discovery / axis interview — genuine judgement the headless
    resolver cannot run. It parks (non-zero exit), never a blind submit."""
    _write_tasks_py(experiment, _STRATEGY_TASKS_PY)  # tasks.py present, but no prior run

    submit = _SubmitStub()
    resolver = DeterministicCampaignResolver(submit_fn=submit)

    report, exit_code = resolver(
        {"workflow": "submit", "experiment_dir": str(experiment), "fields": {}}, experiment
    )

    assert exit_code != 0
    assert submit.calls == []
    assert report.result["residue"] is True
    # A submit-workflow report validates against the submit decision points.
    assert parse_worker_report(json.dumps(report.model_dump(mode="json")), workflow="submit")


def test_deterministic_config_injects_resolver_into_loop_config() -> None:
    """The opt-in entry point hands a CampaignLoopConfig whose resolver is the
    deterministic one, with the default monitor/aggregate step table intact."""
    cfg = deterministic_campaign_config()
    assert isinstance(cfg.resolver, DeterministicCampaignResolver)
    assert dict(cfg.step_table) == {"monitor": "monitor-flow", "aggregate": "aggregate-flow"}


# ─── async refill arm (#362, plan §1.4) ─────────────────────────────────────

# A strategy tasks.py that is BOTH classify-as-strategy (guarded optuna import)
# AND stateful across submits: ``resolve`` seeds params by the count of existing
# run sidecars, which each refill submit increments — so successive submits in
# one tick produce distinct cmd_shas / run_ids (the property the real async
# scaffold gets from constant_liar + the submitted-count proposal index).
_STATEFUL_STRATEGY_TASKS_PY = """\
try:
    import optuna  # noqa: F401
except ImportError:
    optuna = None

from pathlib import Path

from hpc_agent.state.runs import find_existing_runs

_EXP = Path(__file__).resolve().parent.parent


def total():
    return 1


def resolve(i):
    n = len(find_existing_runs(_EXP))
    return {"seed": n, "trial_token": n}
"""


def test_refill_submits_n_distinct_iterations_and_advances_cursor(
    journal_home: Path, experiment: Path
) -> None:
    """async_refill manifest → campaign-advance decides ``refill`` → the resolver
    submits refill_count distinct iterations and advances the cursor once each."""
    from hpc_agent.meta.campaign.manifest import write_manifest

    _write_tasks_py(experiment, _STATEFUL_STRATEGY_TASKS_PY)
    _seed_prior_iteration(experiment, run_id="iter0", loss=0.9)
    # Async on, K=3, no budget cap → in_flight 0, remaining unbounded →
    # refill_count = min(3, ∞) - 0 = 3.
    write_manifest(experiment, campaign_id=_CAMPAIGN_ID, async_refill=True, max_in_flight=3)

    submit = _SubmitStub()
    resolver = DeterministicCampaignResolver(submit_fn=submit)
    report, exit_code = resolver(_decide_spawn_request(experiment), experiment)

    assert exit_code == 0
    # Three distinct iterations submitted through the (stubbed) seam.
    assert len(submit.calls) == 3
    assert report.result["refilled"] is True
    assert report.result["submitted"] == 3
    run_ids = report.result["run_ids"]
    assert len(run_ids) == 3
    assert len(set(run_ids)) == 3  # distinct — no cmd_sha dedup collision

    # The decisions name the strategy path and the refill decision (deduped:
    # the shared path/decide entries appear once, not three times).
    points = {(d.point, d.outcome) for d in report.decisions}
    assert ("path", "strategy") in points
    assert ("decide", "refill") in points

    # The cursor advanced once per submit → +3.
    cursor = read_cursor(experiment, _CAMPAIGN_ID)
    assert cursor is not None
    assert cursor["iteration"] == 3


def test_refill_merge_surfaces_residue_and_worst_exit(journal_home: Path, experiment: Path) -> None:
    """A residue mid-refill is surfaced (anomalies + worst exit code), not
    swallowed — one parked submit parks the whole tick."""
    from hpc_agent.meta.campaign.deterministic_resolver import _EXIT_RESIDUE

    resolver = DeterministicCampaignResolver(submit_fn=_SubmitStub())

    ok = WorkerReport(
        result={"submitted": True, "run_id": "run_a", "job_ids": ["1"]},
        decisions=[],
        anomalies="",
    )
    residue = WorkerReport(
        result={"residue": True, "point": "decide", "outcome": "needs_interview"},
        decisions=[WorkerDecision(point="decide", outcome="needs_interview", why="no prior run")],
        anomalies="ESCALATION (parked, not guessed): no prior run sidecar",
    )
    shared = [WorkerDecision(point="decide", outcome="refill", why="async refill: submit 2 more")]

    merged, exit_code = resolver._aggregate_refill_reports(
        [(ok, 0), (residue, _EXIT_RESIDUE)],
        campaign_id=_CAMPAIGN_ID,
        refill_count=2,
        extra_decisions=shared,
    )

    # Worst-of-N exit: the parked submit dominates.
    assert exit_code == _EXIT_RESIDUE
    # The residue is surfaced, not swallowed.
    assert "ESCALATION" in merged.anomalies
    # Only the successful submit contributes a run_id.
    assert merged.result["run_ids"] == ["run_a"]
    assert merged.result["submitted"] == 1
    # Both the shared refill decision and the residue's decide entry survive.
    points = {(d.point, d.outcome) for d in merged.decisions}
    assert ("decide", "refill") in points
    assert ("decide", "needs_interview") in points
    # The merged report is still contract-valid.
    assert parse_worker_report(json.dumps(merged.model_dump(mode="json")), workflow="campaign")
