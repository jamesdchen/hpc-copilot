"""T8 — the conclusion authorship gate (``ops/decision/journal.py``).

Fires each lock of ``_assert_conclusion_authorship`` on a synthetic violation and
drives the happy round-trip (plus supersession + revoke) end to end. The gate is
the E-shape three-lock structure (the ``_assert_registration_authorship`` sibling):

* Lock 1 — no affordance (organizational; pinned in the T11 contract suite).
* Lock 2 — recompute: every citation resolved server-side against the LIVE stores;
  an unresolvable / mismatched / empty citation set refuses; ``content_sha`` binds
  through the ONE attestation kernel.
* Lock 3 — authorship: bare ack refused, the response NAMES the ``conclusion_id``
  token-exact AND a cited sha by an 8+ hex prefix.

TOY VOCABULARY ONLY (the plan's fixture rule): widget lineage, never a real
domain's words.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.state.decision_journal import read_decisions
from hpc_agent.state.evidence import CURRENT, REVOKED, reduce_conclusion
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

# A run citation resolves against the sidecar's cmd_sha — the cheapest verifiable
# evidence for the fixture. The sha is >8 hex so the prefix bar has room.
_RUN_ID = "widget-run-1"
_CMD_SHA = "a3f2c9d1beef0011223344556677"


def _seed_run(experiment_dir: Path) -> None:
    """Write a run sidecar whose cmd_sha a ``run`` citation can verify against."""
    write_run_sidecar(
        experiment_dir,
        run_id=_RUN_ID,
        cmd_sha=_CMD_SHA,
        hpc_agent_version="0.0.0-test",
        submitted_at="2025-11-14T00:00:00Z",
        executor="widget_executor.py",
        result_dir_template="results/{run_id}",
        task_count=1,
        tasks_py_sha="tasks-sha-aaa",
    )


def _resolved(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "conclusion_id": "edge-x-2025h1",
        "tags": ["edge-x"],
        "concludes": [{"scope_kind": "run", "scope_id": _RUN_ID}],
        "citations": [{"kind": "run", "ref": _RUN_ID, "sha": _CMD_SHA}],
        "finding": "no alpha in 2025H1",
    }
    base.update(overrides)
    return base


def _append(experiment_dir: Path, *, response: str, **resolved_overrides: Any) -> Any:
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "conclusion",
            "scope_id": "edge-x-2025h1",
            "block": "conclusion",
            "response": response,
            "resolved": _resolved(**resolved_overrides),
        }
    )
    return append_decision(experiment_dir=experiment_dir, spec=spec)


# A response that satisfies Lock 3: names the id + an 8-hex prefix of the cited sha.
_GOOD_RESPONSE = "conclude edge-x-2025h1 — the run a3f2c9d1 shows no alpha"


# ── happy path ───────────────────────────────────────────────────────────────


def test_happy_round_trip_records_and_reduces_current(tmp_path: Path) -> None:
    _seed_run(tmp_path)
    out = _append(tmp_path, response=_GOOD_RESPONSE)
    assert out.count == 1
    # The gate hash-locked the verified citation set into resolved.content_sha.
    assert out.record.resolved["content_sha"]
    recs = read_decisions(tmp_path, "conclusion", "edge-x-2025h1")
    status = reduce_conclusion(recs, conclusion_id="edge-x-2025h1")
    assert status.status == CURRENT


def test_supersession_newest_wins(tmp_path: Path) -> None:
    _seed_run(tmp_path)
    _append(tmp_path, response=_GOOD_RESPONSE, finding="first take")
    _append(tmp_path, response=_GOOD_RESPONSE + " (revised)", finding="second take")
    recs = read_decisions(tmp_path, "conclusion", "edge-x-2025h1")
    status = reduce_conclusion(recs, conclusion_id="edge-x-2025h1")
    assert status.status == CURRENT
    assert status.winner is not None and status.winner["finding"] == "second take"
    assert len(status.superseded) == 1


def test_revoke_round_trip(tmp_path: Path) -> None:
    _seed_run(tmp_path)
    _append(tmp_path, response=_GOOD_RESPONSE)
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "conclusion",
            "scope_id": "edge-x-2025h1",
            "block": "conclusion-revoke",
            "response": "withdraw edge-x-2025h1 — superseded by fresh data",
            "resolved": {
                "conclusion_id": "edge-x-2025h1",
                "reason": "the 2025H1 window no longer holds after the vol regime shift",
            },
        }
    )
    append_decision(experiment_dir=tmp_path, spec=spec)
    recs = read_decisions(tmp_path, "conclusion", "edge-x-2025h1")
    status = reduce_conclusion(recs, conclusion_id="edge-x-2025h1")
    assert status.status == REVOKED


# ── Lock 2 fire tests (recompute / citation verification) ─────────────────────


def test_fabricated_citation_sha_refused(tmp_path: Path) -> None:
    _seed_run(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="MISMATCH"):
        _append(
            tmp_path,
            response=_GOOD_RESPONSE,
            citations=[{"kind": "run", "ref": _RUN_ID, "sha": "deadbeefdeadbeef"}],
        )


def test_unresolvable_citation_refused(tmp_path: Path) -> None:
    _seed_run(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="UNRESOLVABLE"):
        _append(
            tmp_path,
            response="conclude edge-x-2025h1 — the run a3f2c9d1 shows no alpha",
            citations=[{"kind": "run", "ref": "no-such-run", "sha": _CMD_SHA}],
        )


def test_empty_citations_refused(tmp_path: Path) -> None:
    _seed_run(tmp_path)
    # An empty citations list is refused at shape validation (the evidence-bound rule).
    with pytest.raises(errors.SpecInvalid, match="NON-EMPTY"):
        _append(tmp_path, response=_GOOD_RESPONSE, citations=[])


# ── Lock 3 fire tests (authorship) ────────────────────────────────────────────


def test_bare_ack_refused(tmp_path: Path) -> None:
    _seed_run(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="HUMAN act"):
        _append(tmp_path, response="y")


def test_response_missing_sha_prefix_refused(tmp_path: Path) -> None:
    _seed_run(tmp_path)
    # Names the id but no cited sha prefix.
    with pytest.raises(errors.SpecInvalid, match="cited sha"):
        _append(tmp_path, response="conclude edge-x-2025h1, looks negative to me")


def test_response_missing_conclusion_id_refused(tmp_path: Path) -> None:
    _seed_run(tmp_path)
    # Names the sha prefix but not the id.
    with pytest.raises(errors.SpecInvalid, match="NAME the conclusion_id"):
        _append(tmp_path, response="the run a3f2c9d1 shows no alpha")


# ── block/scope convention (both directions) ──────────────────────────────────


def test_conclusion_block_refused_for_non_conclusion_scope(tmp_path: Path) -> None:
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "run",
            "scope_id": "widget-run-1",
            "block": "conclusion",
            "response": "y",
        }
    )
    with pytest.raises(errors.SpecInvalid, match="conclusion-family block"):
        append_decision(experiment_dir=tmp_path, spec=spec)


def test_conclusion_scope_refuses_foreign_block(tmp_path: Path) -> None:
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "conclusion",
            "scope_id": "edge-x-2025h1",
            "block": "some-other-block",
            "response": "y",
        }
    )
    with pytest.raises(errors.SpecInvalid, match="accepts only its block family"):
        append_decision(experiment_dir=tmp_path, spec=spec)
