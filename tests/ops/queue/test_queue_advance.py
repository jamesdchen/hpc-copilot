"""``queue-advance`` — the placement decision, and the rules it can be shown to obey.

What these tests PIN beyond the happy path, one per binding rule. Each rule
gets its NEGATIVE case, because a guard you cannot show firing is a guard you
have not tested:

* **R5** (an explicit pin always wins) — a pin beats the least-loaded choice,
  survives a ``clusters`` restriction that excludes it, and — the case that
  matters — a pinned cluster failing a hard constraint HOLDS the item rather
  than silently re-placing it on the cluster that would have qualified.
* **R4** (nothing dropped, nothing guessed) — placements + holds always
  reconstruct the queued total, every hold carries a closed ``reason_code`` and
  a specific reason, an unknown pin carries its near-miss suggestions, and an
  item past ``max_placements`` is still EVALUATED so a bound is never reported
  as a constraint failure or vice versa.
* **R6 / §7 R3** (the scheduler is the capacity queue) — a cluster whose
  ``constraints.max_concurrent_jobs`` is already exceeded by live runs is still
  placed on, and no reason string anywhere cites ``max_concurrent_jobs`` or
  claims a capacity inference. The hold vocabulary has no ``no_capacity``.
* **R9** (one occupancy predicate) — the reported occupancy equals
  ``state/queue_occupancy.occupied_slots`` for the same cid, least-loaded is
  decided by it, and the module is pinned by source inspection to route through
  it instead of re-inlining a run scan.
* **R3** (pure) — a decision against a virgin experiment creates nothing, a
  decision against a populated one leaves the tree byte-identical, and two
  calls over the same state return byte-identical results.

Cluster-free: ``HPC_CLUSTERS_CONFIG`` points at a tmp yaml and
``HPC_JOURNAL_DIR`` redirects the journal tree. Nothing here touches a process,
a socket, or a cluster.
"""

from __future__ import annotations

import hashlib
import inspect
from typing import TYPE_CHECKING, Any

import pytest

import hpc_agent.ops.queue.advance as advance_mod
from hpc_agent import errors
from hpc_agent._wire.queries.queue_advance import QueueAdvanceSpec
from hpc_agent.ops.queue.advance import queue_advance
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.queue_intake import append_intake_item, append_intake_placement
from hpc_agent.state.queue_occupancy import occupied_slots
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-07-29T12:00:00+00:00"

#: Two clusters that differ only where a hard constraint can see it: ``alpha``
#: has GPUs and a 24h ceiling, ``beta`` has no GPUs and a 48h ceiling. So every
#: constraint test can eliminate exactly one of them and the tie-break tests can
#: leave both standing.
_CLUSTERS = """
alpha:
  scheduler: slurm
  host: alpha.edu
  user: me
  gpu_types: [A100, V100]
  max_walltime_sec: 86400
  constraints:
    max_walltime: "3:00:00"
    max_concurrent_jobs: 2
beta:
  scheduler: sge
  host: beta.edu
  user: me
  max_walltime_sec: 172800
  constraints:
    max_concurrent_jobs: 2
"""

#: Both clusters identical and both GPU-capable — the tie-break / least-loaded
#: fixture, where nothing but load and alphabetical order can decide.
_TWINS = """
alpha:
  scheduler: slurm
  host: alpha.edu
  user: me
  gpu_types: [A100]
  max_walltime_sec: 86400
beta:
  scheduler: slurm
  host: beta.edu
  user: me
  gpu_types: [A100]
  max_walltime_sec: 86400
"""


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the journal tree so RunRecords land under tmp, not the real home."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


def _clusters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, yaml_text: str = _CLUSTERS) -> None:
    path = tmp_path / "clusters.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(path))


def _exp(tmp_path: Path) -> Path:
    exp = tmp_path / "exp"
    exp.mkdir(exist_ok=True)
    return exp


def _spec(**kw: Any) -> QueueAdvanceSpec:
    kw.setdefault("now", _NOW)
    return QueueAdvanceSpec(**kw)


def _enqueue(exp: Path, item_id: str, **record: Any) -> None:
    record.setdefault("spec", {"entry": "tasks.py"})
    append_intake_item(exp, record=record, request_id=item_id)


def _run(exp: Path, run_id: str, *, campaign_id: str, status: str = "in_flight") -> None:
    upsert_run(
        exp,
        RunRecord(
            run_id=run_id,
            profile="prof",
            cluster="alpha",
            ssh_target="me@alpha.edu",
            remote_path="/scratch/exp",
            job_name="job",
            job_ids=["1"],
            total_tasks=4,
            submitted_at="2026-07-29T00:00:00+00:00",
            experiment_dir=str(exp),
            status=status,
            campaign_id=campaign_id,
        ),
    )


def _tree(root: Path) -> dict[str, str]:
    """Every file under *root*, path -> content digest (the no-writes witness)."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _held(result: Any) -> dict[str, Any]:
    return {hold.item_id: hold for hold in result.held}


def _placed(result: Any) -> dict[str, Any]:
    return {placement.item_id: placement for placement in result.placements}


# ── empty / boundary input ───────────────────────────────────────────────────


def test_empty_queue_decides_empty_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing queued and nothing placeable are opposite situations (three states, not two)."""
    _clusters(tmp_path, monkeypatch)
    result = queue_advance(experiment_dir=_exp(tmp_path), spec=_spec())

    assert result.decision == "empty"
    assert result.placements == []
    assert result.held == []
    assert result.queued_total == 0
    assert "nothing queued" in result.brief


def test_bad_now_override_is_refused_at_the_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A primitive owns its invariants: an unparseable instant is a loud refusal."""
    _clusters(tmp_path, monkeypatch)
    with pytest.raises(errors.SpecInvalid):
        queue_advance(experiment_dir=_exp(tmp_path), spec=QueueAdvanceSpec(now="yesterday"))


def test_placed_items_are_not_candidates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only state 'queued' is a candidate — a placed item has already been decided."""
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    _enqueue(exp, "i1")
    append_intake_placement(
        exp,
        item_id="i1",
        request_id="i1-place",
        cluster="alpha",
        campaign_id="sweep_alpha",
        reason="decided earlier",
    )

    result = queue_advance(experiment_dir=exp, spec=_spec())

    assert result.queued_total == 0
    assert result.decision == "empty"


# ── R5: the explicit pin always wins ─────────────────────────────────────────


def test_pin_beats_the_least_loaded_choice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R5: policy would have picked the emptier cluster; the operator's pin overrides it."""
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    # Load alpha up so least-loaded would choose beta.
    _run(exp, "r1", campaign_id="sweep_alpha")
    _run(exp, "r2", campaign_id="sweep_alpha")
    _enqueue(exp, "i1", campaign_base="sweep", cluster_pin="alpha")

    result = queue_advance(experiment_dir=exp, spec=_spec())

    placement = _placed(result)["i1"]
    assert placement.cluster == "alpha"
    assert placement.pinned is True
    assert "pin" in placement.reason
    assert placement.campaign_id == "sweep_alpha"
    # The pin is evaluated against ITSELF: no alternative is offered back.
    assert [v.cluster for v in placement.considered] == ["alpha"]


def test_pinned_but_unqualified_cluster_holds_and_is_never_re_placed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5's negative case: the pin fails a hard constraint and alpha WOULD have qualified.

    Silently re-placing here is the defect: it would be the tool overruling an
    operator on a decision the operator already made. The item stays queued and
    the reason names the failed constraint.
    """
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    # beta has no GPUs; alpha does. The item pins beta anyway.
    _enqueue(exp, "i1", cluster_pin="beta", resources={"gpu": True})

    result = queue_advance(experiment_dir=exp, spec=_spec())

    assert result.decision == "hold"
    assert result.placements == []
    hold = _held(result)["i1"]
    assert hold.reason_code == "no_cluster_matches_constraints"
    assert "beta" in hold.reason
    assert hold.detail["pinned"] is True
    # The cluster that WOULD have qualified is never named as a placement.
    assert [v.cluster for v in hold.considered] == ["beta"]
    assert "alpha" not in hold.reason


def test_unknown_pin_holds_with_its_near_miss_suggestions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4/R5: a typo'd pin is disclosed as a typo, never re-chosen for."""
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", cluster_pin="alfa")

    result = queue_advance(experiment_dir=exp, spec=_spec())

    hold = _held(result)["i1"]
    assert hold.reason_code == "cluster_pin_unknown"
    assert hold.detail["pin"] == "alfa"
    assert "alpha" in hold.detail["suggestions"]
    assert hold.detail["configured"] == ["alpha", "beta"]
    assert result.placements == []


def test_clusters_restriction_narrows_policy_but_never_overrides_a_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The spec's candidate restriction bounds POLICY's choices, not the operator's."""
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    _enqueue(exp, "pinned", cluster_pin="beta")
    _enqueue(exp, "free")

    result = queue_advance(experiment_dir=exp, spec=_spec(max_placements=2, clusters=["alpha"]))

    assert result.considered_clusters == ["alpha"]
    assert _placed(result)["pinned"].cluster == "beta"
    assert _placed(result)["free"].cluster == "alpha"


def test_restriction_matching_no_configured_cluster_holds_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty candidate set is the likeliest cause of a surprising hold, so it is stated."""
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    _enqueue(exp, "i1")

    result = queue_advance(experiment_dir=exp, spec=_spec(clusters=["gamma"]))

    hold = _held(result)["i1"]
    assert hold.reason_code == "no_cluster_matches_constraints"
    assert hold.detail["requested_clusters"] == ["gamma"]
    assert hold.detail["configured"] == ["alpha", "beta"]


# ── hard constraints, and which ceiling was compared ─────────────────────────


def test_gpu_ask_eliminates_the_gpu_less_cluster_and_names_the_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", resources={"gpu": True})

    placement = _placed(queue_advance(experiment_dir=exp, spec=_spec()))["i1"]

    assert placement.cluster == "alpha"
    rejected = next(v for v in placement.considered if v.cluster == "beta")
    assert rejected.eligible is False
    assert "gpu_types" in rejected.reason


def test_gpu_type_is_matched_case_insensitively_against_gpu_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A specific label is a hard filter, and promoting an 'informational' field is disclosed."""
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    _enqueue(exp, "match", resources={"gpu": True, "gpu_type": "a100"})
    _enqueue(exp, "miss", resources={"gpu": True, "gpu_type": "H100"})

    result = queue_advance(experiment_dir=exp, spec=_spec(max_placements=2))

    assert _placed(result)["match"].cluster == "alpha"
    hold = _held(result)["miss"]
    assert hold.reason_code == "no_cluster_matches_constraints"
    alpha = next(v for v in hold.considered if v.cluster == "alpha")
    assert "informational" in alpha.reason


def test_walltime_verdict_names_which_of_the_two_ceilings_it_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S11: clusters.yaml carries two walltime ceilings that disagree 8-16x.

    Placement compares the top-level hard ceiling and says so, quoting the
    per-array planning ceiling it did NOT compare — alpha declares
    ``constraints.max_walltime: 3:00:00`` while accepting a 24h job.
    """
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", resources={"walltime_sec": 100_000})

    placement = _placed(queue_advance(experiment_dir=exp, spec=_spec()))["i1"]

    assert placement.cluster == "beta"
    alpha = next(v for v in placement.considered if v.cluster == "alpha")
    assert "max_walltime_sec 86400" in alpha.reason
    assert "the top-level hard ceiling" in alpha.reason
    assert "constraints.max_walltime '3:00:00'" in alpha.reason


def test_an_undeclared_walltime_ceiling_says_the_number_is_a_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ClusterConfig.max_walltime_sec`` DEFAULTS to 86400.

    An entry declaring no ceiling still holds items against 86400, and calling
    that "the top-level hard ceiling" sends the operator to grep a file the
    number does not appear in — no way to reconcile or fix the hold. The
    cost-gate leg already discloses its own absent-declaration case; this one
    now does too.
    """
    _clusters(
        tmp_path,
        monkeypatch,
        """
bigmem:
  scheduler: slurm
  host: bigmem.edu
  user: me
""",
    )
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", resources={"walltime_sec": 172_800})

    hold = _held(queue_advance(experiment_dir=exp, spec=_spec()))["i1"]

    reason = hold.considered[0].reason
    assert "max_walltime_sec 86400" in reason
    assert "the framework default" in reason
    assert "declares no max_walltime_sec" in reason


def test_declared_cost_gate_eliminates_a_cluster_that_would_refuse_the_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``constraints.max_estimated_core_hours`` is a DECLARED local gate, not a capacity guess."""
    _clusters(
        tmp_path,
        monkeypatch,
        """
alpha:
  scheduler: slurm
  host: alpha.edu
  user: me
  constraints:
    max_estimated_core_hours: 10
beta:
  scheduler: slurm
  host: beta.edu
  user: me
""",
    )
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", resources={"est_core_hours": 500})

    placement = _placed(queue_advance(experiment_dir=exp, spec=_spec()))["i1"]

    assert placement.cluster == "beta"
    alpha = next(v for v in placement.considered if v.cluster == "alpha")
    assert "max_estimated_core_hours" in alpha.reason


def test_a_cores_ask_is_stated_as_not_compared_rather_than_invented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """clusters.yaml declares no per-cluster core ceiling, so none is invented — it is disclosed."""
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", resources={"cores": 64})

    placement = _placed(queue_advance(experiment_dir=exp, spec=_spec()))["i1"]

    assert "cores 64 not compared" in placement.reason


def test_an_unreadable_cluster_entry_is_ineligible_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One broken stanza must not wedge the whole decision — it is one disclosed verdict."""
    _clusters(
        tmp_path,
        monkeypatch,
        """
alpha:
  scheduler: slurm
  host: alpha.edu
  user: me
beta: "not a mapping"
""",
    )
    exp = _exp(tmp_path)
    _enqueue(exp, "i1")

    placement = _placed(queue_advance(experiment_dir=exp, spec=_spec()))["i1"]

    assert placement.cluster == "alpha"
    beta = next(v for v in placement.considered if v.cluster == "beta")
    assert beta.eligible is False
    assert "not a mapping" in beta.reason


def test_unreadable_config_holds_every_item_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: 'nothing could be evaluated' is a disclosed hold on each item, not an exception."""
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(tmp_path / "absent.yaml"))
    exp = _exp(tmp_path)
    _enqueue(exp, "i1")
    _enqueue(exp, "i2")

    result = queue_advance(experiment_dir=exp, spec=_spec(max_placements=2))

    assert result.decision == "hold"
    assert result.held_counts == {"no_clusters_configured": 2}
    assert all("clusters.yaml" in hold.reason for hold in result.held)


def test_unparseable_resources_hold_the_item_rather_than_placing_it_ask_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable ask is NOT 'no asks' — so treated, it puts gpu work on a gpu-less host."""
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", resources="gpu please")

    result = queue_advance(experiment_dir=exp, spec=_spec())

    assert result.placements == []
    hold = _held(result)["i1"]
    assert hold.reason_code == "item_unresolved"
    assert "resources" in hold.reason


def test_a_scheduler_the_registry_cannot_resolve_is_one_ineligible_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ClusterConfig`` raises ``SpecInvalid`` — an ``HpcError``, NOT a ``ValueError``.

    Pydantic re-raises it untouched, so a ``ValidationError``-only guard let one
    bad stanza abort the whole pass: the placeable item on the good cluster was
    never placed and the queued item vanished from the decision with no hold and
    no reason. ``_evaluate`` is TOTAL — the failure is that entry's verdict.
    """
    _clusters(
        tmp_path,
        monkeypatch,
        """
alpha:
  scheduler: slurm
  host: alpha.edu
  user: me
  max_walltime_sec: 86400
beta:
  scheduler: mystery_backend
  host: beta.edu
  user: me
""",
    )
    exp = _exp(tmp_path)
    _enqueue(exp, "i1")

    placement = _placed(queue_advance(experiment_dir=exp, spec=_spec()))["i1"]

    assert placement.cluster == "alpha"
    beta = next(v for v in placement.considered if v.cluster == "beta")
    assert beta.eligible is False
    assert "SpecInvalid" in beta.reason
    assert "mystery_backend" in beta.reason


def test_a_non_numeric_cost_gate_is_a_verdict_not_a_typeerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ClusterConstraints`` is a plain dataclass and coerces nothing.

    A quoted number in ``constraints.max_estimated_core_hours`` is an ordinary
    hand edit (``ClusterConfig`` types ``constraints`` as ``dict[str, Any]``, so
    it validates), and comparing a float against it raised ``TypeError`` out of
    the verb — every queued item in the call losing its decision. An unevaluable
    cost gate is not an absent one: the cluster is ineligible and says why.
    """
    _clusters(
        tmp_path,
        monkeypatch,
        """
alpha:
  scheduler: slurm
  host: alpha.edu
  user: me
  max_walltime_sec: 86400
  constraints:
    max_estimated_core_hours: "500"
""",
    )
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", resources={"est_core_hours": 900.0})

    hold = _held(queue_advance(experiment_dir=exp, spec=_spec()))["i1"]

    assert hold.reason_code == "no_cluster_matches_constraints"
    verdict = hold.considered[0]
    assert verdict.eligible is False
    assert "max_estimated_core_hours '500'" in verdict.reason
    assert "not a number" in verdict.reason


def test_a_non_string_cluster_key_holds_instead_of_raising_keyerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """YAML 1.1 turns a bare ``1234:`` into an int key; every lookup elsewhere keys on str.

    The verb used to build candidates with ``str(key)`` and then index the raw
    mapping with the stringified key — the very ``str()`` that anticipated the
    case broke the lookup it guarded (``KeyError``). Such an entry is dropped
    at load: placing on it would name a cluster nothing downstream can resolve.
    """
    _clusters(
        tmp_path,
        monkeypatch,
        """
1234:
  scheduler: slurm
  host: a.edu
  user: u
  max_walltime_sec: 86400
""",
    )
    exp = _exp(tmp_path)
    _enqueue(exp, "i1")

    result = queue_advance(experiment_dir=exp, spec=_spec())

    assert result.decision == "hold"
    hold = _held(result)["i1"]
    assert hold.reason_code == "no_clusters_configured"
    assert "non-empty string" in hold.reason


def test_a_malformed_pin_holds_rather_than_being_demoted_to_unpinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R5, the negative case ``_text`` used to swallow.

    A hand-edited or foreign ledger line carrying ``cluster_pin: ""`` (or a
    non-string) read as ``None`` — indistinguishable from an unpinned item — so
    the item was PLACED by policy with no disclosure that a pin was present and
    discarded. That is policy substituting itself for an operator's own choice,
    which is exactly what ``queue-run`` refuses loudly at the boundary.
    """
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    _enqueue(exp, "blank", cluster_pin="")
    _enqueue(exp, "weird", cluster_pin={"nope": 1})

    result = queue_advance(experiment_dir=exp, spec=_spec(max_placements=5))

    assert result.placements == []
    for item_id in ("blank", "weird"):
        hold = _held(result)[item_id]
        assert hold.reason_code == "item_unresolved"
        assert "cluster_pin" in hold.reason
        assert "R5" in hold.reason


def test_an_item_with_no_spec_and_no_spec_ref_is_unresolvable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign/hand-edited ledger line surfaces as a stated hold, never a silent skip."""
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    append_intake_item(exp, record={"run_name": "orphan"}, request_id="i1")

    hold = _held(queue_advance(experiment_dir=exp, spec=_spec()))["i1"]

    assert hold.reason_code == "item_unresolved"
    assert "spec_ref" in hold.reason


# ── R9: one occupancy predicate, and least-loaded decided by it ──────────────


def test_least_loaded_uses_the_shared_occupancy_predicate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9: the reported numbers ARE ``occupied_slots``, and they decide the choice."""
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    _run(exp, "r1", campaign_id="sweep_alpha")
    _run(exp, "r2", campaign_id="sweep_alpha", status="submitting")
    _enqueue(exp, "i1", campaign_base="sweep")

    result = queue_advance(experiment_dir=exp, spec=_spec())

    assert result.occupancy["sweep_alpha"] == occupied_slots(exp, "sweep_alpha")
    assert result.occupancy["sweep_beta"] == occupied_slots(exp, "sweep_beta")
    assert result.occupancy["sweep_alpha"] == 2
    placement = _placed(result)["i1"]
    assert placement.cluster == "beta"
    assert "least-loaded" in placement.reason
    assert next(v for v in placement.considered if v.cluster == "alpha").occupied == 2


def test_a_queued_sibling_item_occupies_a_slot_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger half of the union is load-bearing: an undispatched item is not free capacity."""
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    _enqueue(exp, "already", campaign_base="sweep")
    append_intake_placement(
        exp,
        item_id="already",
        request_id="already-place",
        cluster="alpha",
        campaign_id="sweep_alpha",
        reason="decided earlier",
    )
    _enqueue(exp, "next", campaign_base="sweep")

    result = queue_advance(experiment_dir=exp, spec=_spec())

    assert result.occupancy["sweep_alpha"] == 1
    assert _placed(result)["next"].cluster == "beta"


def test_a_queued_pinned_item_occupies_its_cid_before_any_placement_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9's ``queued`` term, which used to be structurally dead.

    Only ``append_intake_placement`` stamps ``campaign_id``, and that same
    record sets ``state="placed"`` — so keying the ledger half on the stored
    field alone reduced ``{queued, placed}`` to ``{placed}`` and the whole
    enqueue→dispatch window S3 named read as free capacity. A PINNED item's cid
    is fully determined at enqueue (``<base>_<pin>``), so it is counted.
    """
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    for n in range(3):
        _enqueue(exp, f"pinned{n}", campaign_base="sweep", cluster_pin="alpha")

    assert occupied_slots(exp, "sweep_alpha") == 3
    assert occupied_slots(exp, "sweep_beta") == 0

    _enqueue(exp, "free", campaign_base="sweep")
    result = queue_advance(experiment_dir=exp, spec=_spec(max_placements=4))

    # The unpinned item is steered away from the three slots already spoken for.
    assert _placed(result)["free"].cluster == "beta"
    assert result.occupancy["sweep_alpha"] == 3


def test_an_unpinned_queued_item_occupies_no_cluster_yet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the derivation: an unpinned item's cluster is not yet decided.

    Counting it against a guessed cid would be R4's forbidden guess dressed as
    arithmetic, and it would make every unpinned item bias the choice away from
    whichever cluster the guess named.
    """
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", campaign_base="sweep")

    assert occupied_slots(exp, "sweep_alpha") == 0
    assert occupied_slots(exp, "sweep_beta") == 0


def test_placing_a_pinned_item_does_not_double_count_its_own_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The provisional counter must not re-add a slot the shared predicate already holds.

    A pinned item's cid does not change when it is placed, so the placement
    CONFIRMS a slot rather than taking a new one. Two pinned items on alpha plus
    one unpinned sibling: alpha reads 2, so the sibling goes to beta — not 3.
    """
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    _enqueue(exp, "p1", campaign_base="sweep", cluster_pin="alpha")
    _enqueue(exp, "p2", campaign_base="sweep", cluster_pin="alpha")
    _enqueue(exp, "free", campaign_base="sweep")

    result = queue_advance(experiment_dir=exp, spec=_spec(max_placements=3))

    assert result.occupancy["sweep_alpha"] == 2
    free = _placed(result)["free"]
    assert free.cluster == "beta"
    assert "decided earlier in this call" not in free.reason


def test_module_routes_occupancy_through_the_shared_predicate(tmp_path: Path) -> None:
    """Pinned by source: a second inline count is what ``queue_occupancy`` exists to prevent."""
    source = inspect.getsource(advance_mod)

    assert "occupied_slots(" in source
    assert "from hpc_agent.state.queue_occupancy import" in source
    assert "    occupied_slots,\n" in source
    for inlined in ("find_in_flight_runs", "find_runs_by_campaign", "find_submitting_runs"):
        assert inlined not in source


# ── R6: the scheduler is the capacity queue ──────────────────────────────────


def test_never_gates_on_a_scheduler_capacity_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R6/§7 R3, the negative case.

    alpha declares ``constraints.max_concurrent_jobs: 2`` and already carries
    three live runs for the item's cid. If the verb had (wrongly) read that
    field as a cross-run account cap — S11 proved it is per-submission-plan
    wave grouping — the item would be held. It is placed, and no reason string
    anywhere cites the field or claims a capacity inference.
    """
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    for n in range(3):
        _run(exp, f"r{n}", campaign_id="sweep_alpha")
    _enqueue(exp, "i1", campaign_base="sweep", cluster_pin="alpha")

    result = queue_advance(experiment_dir=exp, spec=_spec())

    assert result.decision == "place"
    assert _placed(result)["i1"].cluster == "alpha"
    # 3 live runs + the queued item itself: a PINNED item's cid is determined at
    # enqueue, so the shared predicate counts it before it is dispatched (R9's
    # ledger term). Being over ``max_concurrent_jobs`` still places.
    assert result.occupancy["sweep_alpha"] == 4
    assert result.occupancy["sweep_alpha"] == occupied_slots(exp, "sweep_alpha")
    blob = result.model_dump_json()
    assert "max_concurrent_jobs" not in blob
    assert "no_capacity" not in blob


# ── determinism: tie-break, batching, repeatability ──────────────────────────


def test_equal_load_falls_back_to_the_stable_alphabetical_tie_break(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", campaign_base="sweep")

    placement = _placed(queue_advance(experiment_dir=exp, spec=_spec()))["i1"]

    assert placement.cluster == "alpha"
    assert "tie-break" in placement.reason


def test_an_open_loop_item_discloses_that_load_is_not_comparable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No campaign base: the cid-keyed predicate cannot compare clusters — say so, don't guess."""
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    _run(exp, "r1", campaign_id="sweep_alpha")
    _enqueue(exp, "i1")

    placement = _placed(queue_advance(experiment_dir=exp, spec=_spec()))["i1"]

    assert placement.campaign_id is None
    assert "no campaign base" in placement.reason
    assert "tie-break" in placement.reason


def test_a_batch_spreads_instead_of_stacking_on_one_cluster(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A placement decided earlier in the SAME call provisionally occupies its slot."""
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", campaign_base="sweep")
    _enqueue(exp, "i2", campaign_base="sweep")

    result = queue_advance(experiment_dir=exp, spec=_spec(max_placements=2))

    assert [p.cluster for p in result.placements] == ["alpha", "beta"]
    assert "decided earlier in this call" in _placed(result)["i2"].reason
    # The per-cluster verdict stays exactly the shared predicate's number (R9).
    assert all(v.occupied == 0 for v in _placed(result)["i2"].considered)


def test_items_past_the_bound_are_evaluated_not_lumped_into_the_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R4: a caller-imposed bound and a constraint failure are different facts about an item."""
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    _enqueue(exp, "first")
    _enqueue(exp, "second")
    _enqueue(exp, "impossible", resources={"gpu": True, "gpu_type": "H100"})

    result = queue_advance(experiment_dir=exp, spec=_spec(max_placements=1))

    assert list(_placed(result)) == ["first"]
    bounded = _held(result)["second"]
    assert bounded.reason_code == "batch_limit_reached"
    assert bounded.detail["would_place_on"] in {"alpha", "beta"}
    # The unplaceable item keeps its REAL reason even though the bound was hit.
    assert _held(result)["impossible"].reason_code == "no_cluster_matches_constraints"
    assert result.held_counts == {"batch_limit_reached": 1, "no_cluster_matches_constraints": 1}


def test_a_pinned_item_past_the_bound_is_also_reported_as_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    _enqueue(exp, "first")
    _enqueue(exp, "second", cluster_pin="beta")

    result = queue_advance(experiment_dir=exp, spec=_spec(max_placements=1))

    hold = _held(result)["second"]
    assert hold.reason_code == "batch_limit_reached"
    assert hold.detail["would_place_on"] == "beta"


def test_nothing_is_ever_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R4 as arithmetic: placements + holds reconstruct the in-scope queued total."""
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    _enqueue(exp, "ok")
    _enqueue(exp, "gpu", resources={"gpu": True})
    _enqueue(exp, "huge", resources={"walltime_sec": 999_999})
    _enqueue(exp, "typo", cluster_pin="alfa")
    _enqueue(exp, "junk", resources=[1, 2, 3])

    result = queue_advance(experiment_dir=exp, spec=_spec(max_placements=2))

    assert result.queued_total == 5
    assert len(result.placements) + len(result.held) == 5
    assert sum(result.held_counts.values()) == len(result.held)
    assert all(hold.reason.strip() for hold in result.held)
    assert all(placement.reason.strip() for placement in result.placements)


def test_campaign_base_filter_scopes_the_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Out-of-scope items are not held — they were never this call's business."""
    _clusters(tmp_path, monkeypatch, _TWINS)
    exp = _exp(tmp_path)
    _enqueue(exp, "mine", campaign_base="sweep")
    _enqueue(exp, "theirs", campaign_base="other")

    result = queue_advance(experiment_dir=exp, spec=_spec(campaign_base="sweep"))

    assert result.queued_total == 1
    assert list(_placed(result)) == ["mine"]
    assert result.held == []


def test_the_decision_is_reproducible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R3's determinism: the same ledger + config always yields the same decision."""
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", campaign_base="sweep", resources={"gpu": True})
    _enqueue(exp, "i2", campaign_base="sweep")

    spec = _spec(max_placements=2)
    first = queue_advance(experiment_dir=exp, spec=spec)
    second = queue_advance(experiment_dir=exp, spec=spec)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_the_brief_is_code_computed_from_the_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§3: the placement has to be in front of the human in the same breath as the question."""
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", resources={"gpu": True})
    _enqueue(exp, "i2", cluster_pin="alfa")

    result = queue_advance(experiment_dir=exp, spec=_spec())

    assert result.brief.startswith("Run queue: 2 queued, 1 placement(s) decided, 1 held")
    assert "next: i1 -> alpha" in result.brief
    assert "held: 1 cluster_pin_unknown." in result.brief


# ── R3: writes nothing ───────────────────────────────────────────────────────


def test_a_decision_against_a_virgin_experiment_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F46: a query must never scaffold the tree it reports on."""
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)

    queue_advance(experiment_dir=exp, spec=_spec())

    assert not (exp / ".hpc").exists()
    assert list(exp.iterdir()) == []


def test_a_decision_leaves_a_populated_tree_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R3 mechanized: the empty ``side_effects`` list is a promise this asserts."""
    _clusters(tmp_path, monkeypatch)
    exp = _exp(tmp_path)
    _enqueue(exp, "i1", campaign_base="sweep")
    _run(exp, "r1", campaign_id="sweep_alpha")
    before = _tree(exp)

    queue_advance(experiment_dir=exp, spec=_spec())

    assert _tree(exp) == before
