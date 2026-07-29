"""The dispatch batch tiers — one-per-y by default, batch under a DECLARED tier.

The doctrine both batch bounds carry ("a larger batch is for the
consented/unattended tier") is enforced at the ACTOR's wire model and disclosed
on its result; these tests pin each half with the case that fails without it:

* **the interactive bound cannot be raised silently** — ``max_dispatches > 1``
  with neither ``tier='unattended'`` nor ``item_ids`` is refused at the spec,
  so no agent or config can turn one signed y into N starts;
* **the unattended tier actually batches** — a declared tier lifts the
  one-per-tick throttle for real (N lifecycles started in one call) and the
  result SAYS why (``batch_allowed_by``), in the brief too;
* **naming the items is the other honest basis** — the stranded-recovery shape
  (``item_ids`` + ``max_dispatches=len(items)``) still validates and is
  disclosed as its own basis, not as the tier's;
* **the tier is a throttle, never a gate** — a batch of items the submit spine
  refuses comes back as N ``gate_refused`` rows with ``needs_decision`` set;
  declaring the tier bought nothing at any gate (D1);
* **default-tier semantics are intact** — overflow past ``max_dispatches=1`` is
  still held as ``batch_limit_reached`` and ``batch_allowed_by`` stays null;
* **the wake edge is the unattended seat** — ``chain_dispatch_on_retire``
  declares the tier and drains a batch, not one item per retirement.

Fixtures mirror ``test_queue_dispatch.py``: the one actuation seat is faked at
its source module and everything else is real.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest
from pydantic import ValidationError

from hpc_agent._wire.workflows.campaign_run import CampaignRunResult
from hpc_agent._wire.workflows.queue_dispatch import QueueDispatchSpec
from hpc_agent.ops.queue.chain import chain_dispatch_on_retire
from hpc_agent.ops.queue.dispatch import queue_dispatch
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.queue_intake import append_intake_item, read_intake_items
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-07-29T12:00:00+00:00"

#: The one actuation seat the queue composes (D1), patched at its source module.
_RUN = "hpc_agent.ops.campaign_run.campaign_run"

_CLUSTERS = """
alpha:
  scheduler: slurm
  host: alpha.edu
  user: me
  max_walltime_sec: 86400
"""


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


@pytest.fixture(autouse=True)
def _clusters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "clusters.yaml"
    path.write_text(_CLUSTERS, encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(path))


def _exp(tmp_path: Path) -> Path:
    exp = tmp_path / "exp"
    exp.mkdir(exist_ok=True)
    return exp


def _submit_spec(run_id: str) -> dict[str, Any]:
    return {
        "profile": "ml",
        "cluster": "alpha",
        "ssh_target": "me@alpha.edu",
        "remote_path": "/scratch/ml",
        "job_name": "ml_array",
        "run_id": run_id,
        "total_tasks": 1,
        "backend": "slurm",
        "script": ".hpc/templates/cpu_array.sh",
        "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
    }


def _enqueue(exp: Path, item_id: str, *, run_id: str, cmd_sha: str) -> None:
    append_intake_item(
        exp,
        record={
            "run_name": "ml",
            "run_id": run_id,
            "cmd_sha": cmd_sha,
            "cluster_pin": "alpha",
            "campaign_base": "study",
            "spec": _submit_spec(run_id),
        },
        request_id=item_id,
    )


def _enqueue_three(exp: Path) -> None:
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")
    _enqueue(exp, "item-2", run_id="ml-bbbb2222", cmd_sha="bbbb2222")
    _enqueue(exp, "item-3", run_id="ml-cccc3333", cmd_sha="cccc3333")


def _detached_for(experiment_dir: Path, *, spec: Any) -> CampaignRunResult:
    """The faked seat for a BATCH: each start detaches under its own run id."""
    return CampaignRunResult(
        stage_reached="detached",
        needs_decision=False,
        reason="detached",
        run_id=spec.aggregate.run_id,
        started=True,
        watch="journal",
        detached_pid=4242,
    )


def _states(exp: Path) -> dict[str, str]:
    return {str(i.get("item_id")): str(i.get("state")) for i in read_intake_items(exp)}


# ── the interactive bound cannot be raised silently ──────────────────────────


def test_interactive_batch_is_refused_at_the_wire() -> None:
    """No tier declared, no items named: max_dispatches > 1 must not validate.
    This is the whole 'do not let an agent or config silently raise the
    interactive default' rule — the refusal happens at the spec, before any
    ledger read, so a CLI/agent spec file hits the same wall as Python."""
    with pytest.raises(ValidationError, match="tier='unattended'"):
        QueueDispatchSpec(max_dispatches=2)


def test_explicitly_interactive_tier_cannot_batch_either(tmp_path: Path) -> None:
    """Saying 'interactive' out loud grants nothing the default did not."""
    with pytest.raises(ValidationError, match="one-decision-per-y"):
        QueueDispatchSpec(max_dispatches=3, tier="interactive")


# ── the unattended tier actually batches, and says so ────────────────────────


def test_unattended_tier_lifts_the_throttle_and_disclosures_the_basis(tmp_path: Path) -> None:
    """Three queued items, one declared-tier call: three lifecycles start, and
    the result names WHY the batch was allowed — on the field and in the brief."""
    exp = _exp(tmp_path)
    _enqueue_three(exp)

    with mock.patch(_RUN, side_effect=_detached_for) as m_run:
        res = queue_dispatch(
            experiment_dir=exp,
            spec=QueueDispatchSpec(tier="unattended", max_dispatches=3, now=_NOW),
        )

    assert res.stage_reached == "dispatched"
    assert [row.item_id for row in res.dispatched] == ["item-1", "item-2", "item-3"]
    assert m_run.call_count == 3
    assert _states(exp) == {"item-1": "placed", "item-2": "placed", "item-3": "placed"}
    assert res.batch_allowed_by == "unattended_tier"
    assert "unattended_tier" in res.brief  # §3: disclosed in the same breath
    assert res.held_counts == {}  # nothing was throttled behind the bound


def test_named_items_are_their_own_disclosed_basis(tmp_path: Path) -> None:
    """The stranded-recovery shape: item_ids + max_dispatches=len(items) still
    validates without any tier claim, and is disclosed as 'item_ids'."""
    exp = _exp(tmp_path)
    _enqueue_three(exp)

    with mock.patch(_RUN, side_effect=_detached_for):
        res = queue_dispatch(
            experiment_dir=exp,
            spec=QueueDispatchSpec(item_ids=["item-1", "item-2"], max_dispatches=2, now=_NOW),
        )

    assert [row.item_id for row in res.dispatched] == ["item-1", "item-2"]
    assert res.batch_allowed_by == "item_ids"


# ── the tier is a throttle, never a gate (D1) ────────────────────────────────


def test_a_declared_tier_bypasses_no_gate(tmp_path: Path) -> None:
    """Batching only removes the one-per-tick bound: every item still meets its
    own cluster-boundary gate, and a refused gate refuses the whole batch item
    by item, each disclosed, with the human flagged."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")
    _enqueue(exp, "item-2", run_id="ml-bbbb2222", cmd_sha="bbbb2222")
    refused = CampaignRunResult(
        stage_reached="submit_failed",
        needs_decision=True,
        reason="submit spine stopped at 'no_greenlight': no y and no live standing consent.",
        run_id="ml-aaaa1111",
    )

    with mock.patch(_RUN, return_value=refused):
        res = queue_dispatch(
            experiment_dir=exp,
            spec=QueueDispatchSpec(tier="unattended", max_dispatches=2, now=_NOW),
        )

    assert res.stage_reached == "dispatch_refused"
    assert res.needs_decision is True
    assert [row.reason_code for row in res.refused] == ["gate_refused", "gate_refused"]
    assert res.batch_allowed_by == "unattended_tier"


# ── default-tier semantics are intact ────────────────────────────────────────


def test_default_tier_still_throttles_and_holds_batch_limit_reached(tmp_path: Path) -> None:
    """One start per default tick; the overflow is held with advance's own
    ``batch_limit_reached`` and the disclosure field stays null — the batch
    machinery is invisible to a call that never asked for a batch."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")
    _enqueue(exp, "item-2", run_id="ml-bbbb2222", cmd_sha="bbbb2222")

    with mock.patch(_RUN, side_effect=_detached_for) as m_run:
        res = queue_dispatch(experiment_dir=exp, spec=QueueDispatchSpec(now=_NOW))

    assert m_run.call_count == 1
    assert [row.item_id for row in res.dispatched] == ["item-1"]
    assert res.held_counts == {"batch_limit_reached": 1}
    (hold,) = res.held
    assert (hold.item_id, hold.reason_code) == ("item-2", "batch_limit_reached")
    assert res.batch_allowed_by is None
    assert "batch:" not in res.brief


# ── the wake edge is the unattended seat ─────────────────────────────────────


def test_wake_edge_declares_the_tier_and_drains_a_batch(tmp_path: Path) -> None:
    """A retirement is machine-fired — no y is being taken — so the chain hook
    dispatches under the declared unattended tier and drains MORE than one
    waiting item per retirement. Without the tier this chained tick could only
    ever start one."""
    exp = _exp(tmp_path)
    _enqueue_three(exp)
    upsert_run(
        exp,
        RunRecord(
            run_id="ml-zzzz9999",
            profile="ml",
            cluster="alpha",
            ssh_target="me@alpha.edu",
            remote_path="/scratch/ml",
            job_name="ml_array",
            job_ids=["1"],
            total_tasks=1,
            submitted_at="2026-07-29T00:00:00+00:00",
            experiment_dir=str(exp),
            status="complete",
            campaign_id="study_alpha",
        ),
    )

    with mock.patch(_RUN, side_effect=_detached_for) as m_run:
        chained = chain_dispatch_on_retire(exp, run_id="ml-zzzz9999", origin="campaign-run")

    assert chained is not None and chained["chained"] is True
    assert chained["stage_reached"] == "dispatched"
    assert chained["dispatched"] == 3
    assert m_run.call_count == 3
    assert _states(exp) == {"item-1": "placed", "item-2": "placed", "item-3": "placed"}
