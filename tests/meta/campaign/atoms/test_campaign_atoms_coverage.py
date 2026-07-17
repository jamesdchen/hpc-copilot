"""Behaviour-pinning mutation coverage for the campaign-loop atoms.

The campaign-advance ladder + its budget / converged / circuit-breaker /
resubmit-cap / refill inputs ARE the unattended-autonomy machinery: a silent
boundary/operator/precedence bug here loops a campaign forever, overspends, or
stops it wrongly. Two real bugs were found in this seam this session — the
``submitting`` orphan silently disarming the breaker (provenance-review F1) and
the status undercount that let a stop orphan a live array (F2) — proving it was
under-pinned.

The paired existing suites (``test_campaign_atoms.py``,
``test_budget_accounting.py``, ``test_circuit_breaker.py``,
``test_resubmit_cap.py``, ``test_advance_refill.py``,
``test_submitting_orphan_regression.py``, ``tests/meta/test_campaign_refill.py``)
already pin the happy paths and the F1/F2 status-layer readers. This file adds
the assertions those left as covered-but-UNASSERTED: the decision-boundary
comparisons (exactly-at-cap vs one-under), the *precedence* pairs of the advance
ladder that no single-guard test exercises, the ``n_iters`` completed-only count,
and the F2 fix surviving end-to-end at the advance layer. Each test names the
mutation it kills.

Mirrors the landed style of ``tests/state/test_journal_coverage.py``: one
assertion per surviving mutant, seeded over real journal + sidecar state.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent.meta.campaign.atoms.advance import campaign_advance
from hpc_agent.meta.campaign.atoms.budget import campaign_budget
from hpc_agent.meta.campaign.atoms.converged import campaign_converged
from hpc_agent.meta.campaign.atoms.resubmit_cap import max_task_resubmits
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

_PROFILE = "ml"
_CLUSTER = "hoffman2"


# ── seeding helpers ───────────────────────────────────────────────────────────


def _seed_sidecar(
    experiment_dir: Path, *, run_id: str, campaign_id: str, task_count: int = 1
) -> None:
    """Write a minimal v2 sidecar tagged with *campaign_id* (no journal record)."""
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha="0" * 12,
        hpc_agent_version="0.0.0+test",
        submitted_at="2026-01-01T00:00:00Z",
        executor="hpc_user_tasks",
        result_dir_template="results/{run_id}/{task_id}",
        task_count=task_count,
        tasks_py_sha="0" * 12,
        campaign_id=campaign_id,
        profile=_PROFILE,
        cluster=_CLUSTER,
        remote_path="/u/scratch/exp",
    )


def _seed_iteration(
    experiment_dir: Path,
    *,
    run_id: str,
    campaign_id: str,
    status: str = "complete",
    retries: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Seed BOTH a sidecar and a journal RunRecord (status + optional retries)."""
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    _seed_sidecar(experiment_dir, run_id=run_id, campaign_id=campaign_id)
    upsert_run(
        experiment_dir,
        RunRecord(
            run_id=run_id,
            profile=_PROFILE,
            cluster=_CLUSTER,
            ssh_target="user@host",
            remote_path="/scratch/exp",
            job_name="ml",
            job_ids=["1"] if status != "submitting" else [],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(experiment_dir.resolve()),
            campaign_id=campaign_id,
            status=status,
            retries=retries or {},
        ),
    )


def _seed_metrics(experiment_dir: Path, *, run_id: str, value: float) -> None:
    """Drop a metrics.json under the result dir so ``prior()`` picks it up."""
    metrics_dir = experiment_dir / "results" / run_id / "0"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "metrics.json").write_text(json.dumps({"loss": value}))


# ═══════════════════════════════════════════════════════════════════════════
# campaign-budget: the exhaustion boundary (spent >= cap), exactly at vs under
# ═══════════════════════════════════════════════════════════════════════════


def test_budget_exhausted_at_exact_cap(tmp_path: Path) -> None:
    # 3 sidecars, max_jobs == 3: spent EQUALS the cap → exhausted, remaining 0.
    # Kills ``spent_val >= cap_int`` → ``>`` (the exact-equality case would then
    # slip through and the campaign would over-run by one job).
    for i in range(3):
        _seed_sidecar(tmp_path, run_id=f"run_{i:04d}", campaign_id="A")
    out = campaign_budget(experiment_dir=tmp_path, campaign_id="A", max_jobs=3)
    assert out["spent"]["jobs"] == 3
    assert out["exhausted"] is True
    assert out["remaining"]["max_jobs"] == 0
    assert "max_jobs" in out["reason"]


def test_budget_one_under_cap_not_exhausted(tmp_path: Path) -> None:
    # 3 sidecars, max_jobs == 4: one under the cap → NOT exhausted, remaining 1.
    # Kills ``>=`` → ``>=``-widened mutations and the ``max(0, cap-spent)``
    # remaining arithmetic (an off-by-one would report 0 or exhausted here).
    for i in range(3):
        _seed_sidecar(tmp_path, run_id=f"run_{i:04d}", campaign_id="A")
    out = campaign_budget(experiment_dir=tmp_path, campaign_id="A", max_jobs=4)
    assert out["exhausted"] is False
    assert out["remaining"]["max_jobs"] == 1
    assert out["reason"] == "within_budget"


def test_budget_none_cap_never_exhausts(tmp_path: Path) -> None:
    # No caps supplied at all → nothing can exhaust, every remaining is None.
    # Kills removal of the ``if cap is None: remaining=None; continue`` guard
    # (a dropped guard would compare spend against a None cap and crash / halt).
    for i in range(3):
        _seed_sidecar(tmp_path, run_id=f"run_{i:04d}", campaign_id="A")
    out = campaign_budget(experiment_dir=tmp_path, campaign_id="A")
    assert out["exhausted"] is False
    assert out["remaining"]["max_jobs"] is None
    assert out["remaining"]["max_core_hours"] is None


def test_budget_core_hours_float_boundary(tmp_path: Path) -> None:
    # One task: 3600s elapsed × 1 core (ceil(3600/3600)) = 1.0 core-hour.
    # cap == 1.0 exactly → exhausted; a hair above → not. Kills the FLOAT
    # ``spent_core_hours >= cap_ch`` boundary (distinct from the int ladder).
    from hpc_agent.state.runtime_prior import append_sample

    _seed_sidecar(tmp_path, run_id="run_0000", campaign_id="A")
    append_sample(
        tmp_path,
        profile=_PROFILE,
        cluster=_CLUSTER,
        run_id="run_0000",
        task_id=0,
        gpu_type="",
        node="n1",
        elapsed_sec=3600,
        exit_code=0,
        cpu_seconds_used=3600,
    )
    at_cap = campaign_budget(experiment_dir=tmp_path, campaign_id="A", max_core_hours=1.0)
    assert at_cap["spent"]["core_hours"] == 1.0
    assert at_cap["exhausted"] is True
    assert "max_core_hours" in at_cap["reason"]

    under = campaign_budget(experiment_dir=tmp_path, campaign_id="A", max_core_hours=1.0001)
    assert under["exhausted"] is False


# ═══════════════════════════════════════════════════════════════════════════
# campaign-converged: n_iters counts COMPLETED-only + the fire boundaries
# ═══════════════════════════════════════════════════════════════════════════


def test_converged_n_iters_counts_completed_only(tmp_path: Path) -> None:
    # 2 iterations produced metrics; a 3rd is an orphan (in-flight / no metrics.json
    # → prior() yields an empty {} entry). n_iters must count the 2 completed only:
    # the orphan does NOT burn a max_iters slot. With max_iters=3, completed-count
    # (2) < 3 → NOT converged. Kills ``sum(1 for entry in history if entry)`` →
    # dropping the ``if entry`` filter (which would count 3 and stop early).
    _seed_sidecar(tmp_path, run_id="run_0000", campaign_id="A")
    _seed_metrics(tmp_path, run_id="run_0000", value=0.5)
    _seed_sidecar(tmp_path, run_id="run_0001", campaign_id="A")
    _seed_metrics(tmp_path, run_id="run_0001", value=0.4)
    _seed_sidecar(tmp_path, run_id="run_0002", campaign_id="A")  # orphan: no metrics

    out = campaign_converged(experiment_dir=tmp_path, campaign_id="A", max_iters=3)
    assert out["iterations"] == 2
    assert out["converged"] is False


def test_converged_max_iters_exact_boundary(tmp_path: Path) -> None:
    # 2 completed iterations, max_iters == 2 → n_iters >= max_iters fires exactly
    # at equality. Kills ``n_iters >= int(max_iters)`` → ``>`` (which would run a
    # 3rd, unbudgeted iteration).
    _seed_sidecar(tmp_path, run_id="run_0000", campaign_id="A")
    _seed_metrics(tmp_path, run_id="run_0000", value=0.5)
    _seed_sidecar(tmp_path, run_id="run_0001", campaign_id="A")
    _seed_metrics(tmp_path, run_id="run_0001", value=0.4)

    out = campaign_converged(experiment_dir=tmp_path, campaign_id="A", max_iters=2)
    assert out["converged"] is True
    assert "max_iters_reached" in out["reason"]

    # One under the cap → still running.
    under = campaign_converged(experiment_dir=tmp_path, campaign_id="A", max_iters=3)
    assert under["converged"] is False


def test_converged_target_boundary_minimize(tmp_path: Path) -> None:
    # best == target exactly (minimize) → crosses (``best <= target``). Kills
    # ``<=`` → ``<`` (a run that lands exactly on target would fail to stop).
    _seed_sidecar(tmp_path, run_id="run_0000", campaign_id="A")
    _seed_metrics(tmp_path, run_id="run_0000", value=0.30)

    at = campaign_converged(
        experiment_dir=tmp_path, campaign_id="A", metric="loss", target=0.30, direction="minimize"
    )
    assert at["converged"] is True
    assert "target_met" in at["reason"]

    # best just ABOVE the target → not met.
    above = campaign_converged(
        experiment_dir=tmp_path, campaign_id="A", metric="loss", target=0.29, direction="minimize"
    )
    assert above["converged"] is False


# ═══════════════════════════════════════════════════════════════════════════
# campaign-advance: the decision-ladder PRECEDENCE (the real order, pinned)
#
# Real order (sync): _over_budget > _wait_in_flight > _circuit_breaker >
#                    _resubmit_cap > _converged   (advance.py rules= tuple)
# The existing suite pins over_budget>converged and wait>breaker / wait>resubmit;
# these fill the UNPINNED adjacencies so a reordering mutant cannot survive.
# ═══════════════════════════════════════════════════════════════════════════


def test_over_budget_precedes_wait_in_flight(journal_home: Path, tmp_path: Path) -> None:
    # Budget is a RECOVERABLE, ack-gated halt that (unlike the terminal stops)
    # does NOT wait for in-flight runs — it is FIRST in the ladder. Seed a met
    # jobs cap WITH a run still in flight: the decision must be stop_over_budget,
    # not wait_in_flight. Kills swapping _over_budget below _wait_in_flight.
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="complete")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="in_flight")

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", max_jobs=2)
    assert out["status"]["in_flight"] == 1  # a run really is outstanding
    assert out["decision"] == "stop_over_budget"
    assert out["needs_acknowledgement"] is True


def test_circuit_breaker_precedes_resubmit_cap(journal_home: Path, tmp_path: Path) -> None:
    # Both loud-fail guards fire at once (3 consecutive failed iters, each with a
    # resubmit on slot "0" → breaker count 3 AND per-task total 3). The breaker is
    # ordered FIRST, so the decision is stop_circuit_breaker. Kills swapping the
    # _circuit_breaker / _resubmit_cap rule order.
    _seed_iteration(
        tmp_path, run_id="r0", campaign_id="A", status="failed", retries={"0": {"attempts": 1}}
    )
    _seed_iteration(
        tmp_path, run_id="r1", campaign_id="A", status="failed", retries={"0": {"attempts": 1}}
    )
    _seed_iteration(
        tmp_path, run_id="r2", campaign_id="A", status="failed", retries={"0": {"attempts": 1}}
    )

    out = campaign_advance(
        experiment_dir=tmp_path,
        campaign_id="A",
        circuit_breaker_failures=3,
        max_task_resubmits=3,
    )
    assert out["circuit_breaker"]["count"] == 3
    assert out["resubmit_cap"]["count"] == 3  # both guards genuinely tripped
    assert out["decision"] == "stop_circuit_breaker"


def test_resubmit_cap_precedes_converged(journal_home: Path, tmp_path: Path) -> None:
    # The resubmit cap is ordered ABOVE convergence. Seed COMPLETE iterations (so
    # the breaker streak is 0) that both meet the metric target AND carry a
    # resubmit total >= cap; the decision must be stop_resubmit_cap, not
    # stop_converged. Kills swapping the _resubmit_cap / _converged rule order.
    _seed_iteration(
        tmp_path, run_id="r0", campaign_id="A", status="complete", retries={"0": {"attempts": 2}}
    )
    _seed_metrics(tmp_path, run_id="r0", value=0.5)
    _seed_iteration(
        tmp_path, run_id="r1", campaign_id="A", status="complete", retries={"0": {"attempts": 2}}
    )
    _seed_metrics(tmp_path, run_id="r1", value=0.2)  # crosses target 0.3

    out = campaign_advance(
        experiment_dir=tmp_path,
        campaign_id="A",
        metric="loss",
        target=0.3,
        direction="minimize",
        max_task_resubmits=3,
    )
    assert out["resubmit_cap"]["count"] == 4  # 2+2 across the two runs
    assert out["converged"]["converged"] is True  # convergence also fired
    assert out["decision"] == "stop_resubmit_cap"


# ═══════════════════════════════════════════════════════════════════════════
# campaign-advance _wait_in_flight: blocks on ANY non-terminal (the F2 fix)
# ═══════════════════════════════════════════════════════════════════════════


def test_wait_in_flight_blocks_on_submitting_orphan(journal_home: Path, tmp_path: Path) -> None:
    # F2 (provenance-review): a ``submitting`` orphan can name a LIVE array whose
    # id-read was severed, so it is NON-TERMINAL and MUST hold the ladder at
    # wait_in_flight — a terminal stop would orphan the array. Seed a full breaker
    # streak (3 failed) PLUS a submitting orphan at the tail: the breaker would
    # otherwise fire, but wait_in_flight must win. Kills reverting campaign_status
    # to ``r.status == "in_flight"`` (which drops ``submitting`` → in_flight 0 →
    # breaker fires → the exact orphan-abandoning bug).
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r2", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r3", campaign_id="A", status="submitting")

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", circuit_breaker_failures=3)
    assert out["status"]["in_flight"] == 1  # the submitting orphan counts
    assert out["decision"] == "wait_in_flight"


def test_wait_in_flight_not_fired_when_all_terminal(journal_home: Path, tmp_path: Path) -> None:
    # The mirror: with NO outstanding run, the wait guard must NOT fire and the
    # breaker is free to halt. Kills ``if n > 0`` → ``n >= 0`` in _wait_in_flight
    # (a widened guard would wait forever on an idle, all-terminal campaign).
    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r1", campaign_id="A", status="failed")
    _seed_iteration(tmp_path, run_id="r2", campaign_id="A", status="failed")

    out = campaign_advance(experiment_dir=tmp_path, campaign_id="A", circuit_breaker_failures=3)
    assert out["status"]["in_flight"] == 0
    assert out["decision"] == "stop_circuit_breaker"


# ═══════════════════════════════════════════════════════════════════════════
# resubmit_cap atom: the per-slot zero guard (attempts <= 0 → not a slot)
# ═══════════════════════════════════════════════════════════════════════════


def test_resubmit_cap_ignores_zero_attempt_entries(journal_home: Path, tmp_path: Path) -> None:
    # A ``retries`` map carrying an explicit ``attempts: 0`` entry is NOT a
    # resubmitted slot: it must not appear in per_task and must not become the
    # worst task_id. Existing coverage only uses an EMPTY retries dict, leaving the
    # ``if attempts <= 0: continue`` guard unpinned. Kills ``<= 0`` → ``< 0``
    # (which would fold a 0-attempt slot in with task_id set and per_task={"0":0}).
    from hpc_agent.state.index import find_runs_by_campaign

    _seed_iteration(tmp_path, run_id="r0", campaign_id="A", retries={"0": {"attempts": 0}})
    out = max_task_resubmits(find_runs_by_campaign(tmp_path, "A"))
    assert out["count"] == 0
    assert out["task_id"] is None
    assert out["per_task"] == {}


# ═══════════════════════════════════════════════════════════════════════════
# campaign-refill mint discipline: never fabricate a spec from placeholders
# ═══════════════════════════════════════════════════════════════════════════


def test_refill_reconstruct_refuses_empty_journal(journal_home: Path, tmp_path: Path) -> None:
    # ``_build_iteration_resolve_spec`` reconstructs the NEXT iteration's submit
    # context from the newest prior run. If advance decided refill but the journal
    # has NO run, that is a loud SpecInvalid — never a spec silently built from the
    # _PH_RUN_ID / _PH_CMD_SHA placeholders (which would submit a garbage run,
    # violating the fresh-mint discipline). Existing tests cover a prior with
    # missing FIELDS; this pins the no-prior-at-all branch. Kills replacing the
    # ``if not records: raise`` guard with a silent fallthrough.
    from hpc_agent.ops.campaign_refill import _build_iteration_resolve_spec

    with pytest.raises(errors.SpecInvalid, match="no prior run"):
        _build_iteration_resolve_spec(tmp_path, "ghost")
