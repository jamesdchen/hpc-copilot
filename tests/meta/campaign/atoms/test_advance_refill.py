"""``campaign-advance`` async-refill decision tests (#362, plan §1.2).

The refill rule is a pure function over ``campaign_status.in_flight`` and
``campaign_budget.remaining.max_jobs`` — so we drive it over **synthetic
evidence** by monkeypatching those two atoms, rather than standing up real
in-flight journal records. Pins:

* default-off is byte-identical (never refills; wait_in_flight intact);
* async-on + free slots + headroom → ``refill`` with the exact count;
* ``refill_count = max(0, min(K, remaining_max_jobs) - in_flight)``, incl.
  the unbounded (``remaining = None``) and budget-capped cases;
* a full pool falls back to ``wait_in_flight`` (never over-submits);
* over_budget / stop_converged still win over refill (stops outrank refill).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hpc_agent.meta.campaign.atoms.advance import campaign_advance

_CID = "tune_async"


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    return tmp_path


def _patch_evidence(
    monkeypatch: pytest.MonkeyPatch,
    *,
    in_flight: int,
    remaining_max_jobs: int | None,
    exhausted: bool = False,
    iterations: int = 3,
) -> None:
    """Inject synthetic ``campaign_status`` / ``campaign_budget`` evidence."""

    def fake_status(*, experiment_dir: Path, campaign_id: str) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "iterations": iterations,
            "in_flight": in_flight,
            "history": [],
            "run_ids": [],
        }

    def fake_budget(*, experiment_dir: Path, campaign_id: str, **_caps: Any) -> dict[str, Any]:
        return {
            "campaign_id": campaign_id,
            "spent": {"jobs": iterations, "tasks": iterations},
            "budget": {},
            "remaining": {"max_jobs": remaining_max_jobs},
            "projected": {},
            "coverage": {},
            "exhausted": exhausted,
            "reason": "max_jobs (cap met)" if exhausted else "within_budget",
        }

    monkeypatch.setattr("hpc_agent.meta.campaign.atoms.status.campaign_status", fake_status)
    monkeypatch.setattr("hpc_agent.meta.campaign.atoms.budget.campaign_budget", fake_budget)


# ─── default-off: byte-identical synchronous ladder ─────────────────────────


def test_default_off_continue_when_idle(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_evidence(monkeypatch, in_flight=0, remaining_max_jobs=10)
    out = campaign_advance(experiment_dir=experiment, campaign_id=_CID)
    assert out["decision"] == "continue"
    assert out["refill_count"] is None


def test_default_off_waits_in_flight(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With async off, in-flight runs → wait_in_flight, never refill."""
    _patch_evidence(monkeypatch, in_flight=2, remaining_max_jobs=10)
    out = campaign_advance(experiment_dir=experiment, campaign_id=_CID)
    assert out["decision"] == "wait_in_flight"
    assert out["refill_count"] is None


# ─── async on: the refill decision ──────────────────────────────────────────


def test_async_refills_with_headroom(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """async + in_flight < K + budget headroom → refill with exact count."""
    _patch_evidence(monkeypatch, in_flight=1, remaining_max_jobs=10)
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=4
    )
    assert out["decision"] == "refill"
    # min(K=4, remaining=10) - in_flight=1 = 3
    assert out["refill_count"] == 3


def test_async_refill_unbounded_budget(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """remaining_max_jobs=None (no jobs cap) → refill up to K - in_flight."""
    _patch_evidence(monkeypatch, in_flight=1, remaining_max_jobs=None)
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=4
    )
    assert out["decision"] == "refill"
    assert out["refill_count"] == 3


def test_async_refill_budget_capped(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tight jobs budget caps the refill below K - in_flight."""
    _patch_evidence(monkeypatch, in_flight=1, remaining_max_jobs=3)
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=10
    )
    assert out["decision"] == "refill"
    # min(K=10, remaining=3) - in_flight=1 = 2
    assert out["refill_count"] == 2


def test_async_pool_full_waits(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At/over the target pool, refill falls back to wait_in_flight (no over-submit)."""
    _patch_evidence(monkeypatch, in_flight=4, remaining_max_jobs=10)
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=4
    )
    assert out["decision"] == "wait_in_flight"
    assert out["refill_count"] is None


def test_async_default_k_when_unset(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """async on with no max_in_flight uses the framework default K (4)."""
    _patch_evidence(monkeypatch, in_flight=0, remaining_max_jobs=None)
    out = campaign_advance(experiment_dir=experiment, campaign_id=_CID, async_refill=True)
    assert out["decision"] == "refill"
    assert out["refill_count"] == 4


# ─── stops outrank refill ───────────────────────────────────────────────────


def test_over_budget_beats_refill(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A met budget cap halts even with free slots — over_budget wins."""
    _patch_evidence(monkeypatch, in_flight=1, remaining_max_jobs=0, exhausted=True)
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=4
    )
    assert out["decision"] == "stop_over_budget"
    assert out["needs_acknowledgement"] is True
    assert out["refill_count"] is None


def test_converged_beats_refill(experiment: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fired stop criterion halts refilling — stop_converged outranks refill."""
    _patch_evidence(monkeypatch, in_flight=1, remaining_max_jobs=10)

    def fake_converged(**_kw: Any) -> dict[str, Any]:
        return {"converged": True, "reason": "max_iters_reached(5)"}

    monkeypatch.setattr(
        "hpc_agent.meta.campaign.atoms.converged.campaign_converged", fake_converged
    )
    out = campaign_advance(
        experiment_dir=experiment, campaign_id=_CID, async_refill=True, max_in_flight=4
    )
    assert out["decision"] == "stop_converged"
    assert out["refill_count"] is None
