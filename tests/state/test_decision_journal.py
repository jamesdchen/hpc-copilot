"""Tests for the decision-journal state layer (``state/decision_journal.py``).

Covers the append→read round-trip (order preserved), append-only
discipline (a second append never clobbers the first), valid JSONL
line-per-record on disk, both run and campaign scopes, and the ``y`` vs
nudge-text ``response`` persistence.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.state.decision_journal import (
    append_decision,
    decisions_path,
    read_decisions,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_append_then_read_round_trip_preserves_order(tmp_path: Path) -> None:
    for block, response in [
        ("submit.S1", "no — hold walltime, halve the grid"),
        ("submit.S1", "y"),
        ("harvest", "y"),
    ]:
        append_decision(
            tmp_path,
            scope_kind="run",
            scope_id="20260101-000000-deadbee",
            block=block,
            response=response,
            evidence_digest={"status": "canary green"},
            proposal=["option A", "option B"],
        )

    records = read_decisions(tmp_path, "run", "20260101-000000-deadbee")
    assert [(r["block"], r["response"]) for r in records] == [
        ("submit.S1", "no — hold walltime, halve the grid"),
        ("submit.S1", "y"),
        ("harvest", "y"),
    ]
    # Every record carries the full design-§2 schema.
    for r in records:
        assert set(r) >= {
            "schema_version",
            "ts",
            "scope_kind",
            "scope_id",
            "block",
            "evidence_digest",
            "proposal",
            "response",
            "resolved",
            "provenance",
        }
        assert r["scope_kind"] == "run"


def test_append_is_append_only(tmp_path: Path) -> None:
    """A second append never clobbers the first."""
    first = append_decision(
        tmp_path, scope_kind="run", scope_id="run-x", block="submit.S1", response="y"
    )
    append_decision(tmp_path, scope_kind="run", scope_id="run-x", block="anomaly", response="stop")
    records = read_decisions(tmp_path, "run", "run-x")
    assert len(records) == 2
    # The first record is byte-preserved (ts + payload) after the second append.
    assert records[0] == first


def test_disk_format_is_one_json_object_per_line(tmp_path: Path) -> None:
    append_decision(tmp_path, scope_kind="run", scope_id="run-y", block="submit.S2", response="y")
    append_decision(tmp_path, scope_kind="run", scope_id="run-y", block="harvest", response="y")
    path = decisions_path(tmp_path, "run", "run-y")
    assert path == tmp_path / ".hpc" / "runs" / "run-y.decisions.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    for line in lines:
        obj = json.loads(line)  # each line is a standalone JSON object
        assert isinstance(obj, dict)


def test_campaign_scope_locality_and_round_trip(tmp_path: Path) -> None:
    append_decision(
        tmp_path,
        scope_kind="campaign",
        scope_id="camp-42",
        block="campaign.spec",
        response="y",
        resolved={"strategy": "tpe", "budget": 1000},
    )
    path = decisions_path(tmp_path, "campaign", "camp-42")
    assert path == tmp_path / ".hpc" / "campaigns" / "camp-42" / "decisions.jsonl"
    records = read_decisions(tmp_path, "campaign", "camp-42")
    assert len(records) == 1
    assert records[0]["scope_kind"] == "campaign"
    assert records[0]["resolved"] == {"strategy": "tpe", "budget": 1000}


def test_scope_kind_lands_under_hpc_scopes(tmp_path: Path) -> None:
    """A ``scope`` decision journals under ``.hpc/scopes/<tag>.decisions.jsonl``."""
    append_decision(
        tmp_path,
        scope_kind="scope",
        scope_id="my-scope",
        block="scope-lock",
        response="freeze",
        resolved={"scope_action": "lock"},
    )
    path = decisions_path(tmp_path, "scope", "my-scope")
    assert path == tmp_path / ".hpc" / "scopes" / "my-scope.decisions.jsonl"
    records = read_decisions(tmp_path, "scope", "my-scope")
    assert len(records) == 1
    assert records[0]["scope_kind"] == "scope"
    assert records[0]["resolved"] == {"scope_action": "lock"}


def test_notebook_kind_lands_under_hpc_notebooks(tmp_path: Path) -> None:
    """A ``notebook`` decision journals under ``.hpc/notebooks/<audit_id>.decisions.jsonl``.

    Design ``docs/design/notebook-audit.md`` D3: sign-offs are ordinary
    append-decision records under a caller-authored ``audit_id``.
    """
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id="my-audit",
        block="notebook-sign-off",
        response="sign construction",
        resolved={
            "audit_id": "my-audit",
            "section": "construction",
            "section_sha": "abc",
            "view_sha": "def",
        },
    )
    path = decisions_path(tmp_path, "notebook", "my-audit")
    assert path == tmp_path / ".hpc" / "notebooks" / "my-audit.decisions.jsonl"
    records = read_decisions(tmp_path, "notebook", "my-audit")
    assert len(records) == 1
    assert records[0]["scope_kind"] == "notebook"
    assert records[0]["resolved"]["section"] == "construction"


def test_registration_kind_lands_under_hpc_registrations(tmp_path: Path) -> None:
    """A ``registration`` decision journals under
    ``.hpc/registrations/<registration_id>.decisions.jsonl``.

    Design ``docs/design/registration-kernel.md`` R9: the deployment-boundary
    attestation rides ``append-decision`` under a caller-authored
    ``registration_id`` (a SIXTH scope kind, its own path branch).
    """
    append_decision(
        tmp_path,
        scope_kind="registration",
        scope_id="reg-widgets",
        block="registration",
        response="register reg-widgets at dossier abc123",
        resolved={
            "registration_id": "reg-widgets",
            "run_id": "widget-run-1",
            "dossier_sha": "abc123",
        },
    )
    path = decisions_path(tmp_path, "registration", "reg-widgets")
    assert path == tmp_path / ".hpc" / "registrations" / "reg-widgets.decisions.jsonl"
    records = read_decisions(tmp_path, "registration", "reg-widgets")
    assert len(records) == 1
    assert records[0]["scope_kind"] == "registration"
    assert records[0]["resolved"]["registration_id"] == "reg-widgets"


def test_pack_kind_lands_under_hpc_packs(tmp_path: Path) -> None:
    """A ``pack`` decision journals under ``.hpc/packs/<pack_name>.decisions.jsonl``.

    Design ``docs/design/domain-packs.md`` ("The bind event"): the mechanical
    ``pack-bind`` / ``pack-receipt`` CODE attestations ride ``append-decision``
    under a caller-authored pack ``name`` (a dedicated scope kind, its own path
    branch — the notebook/registration precedent).
    """
    append_decision(
        tmp_path,
        scope_kind="pack",
        scope_id="toy-widgets",
        block="pack-bind",
        response="bound",
        resolved={
            "pack": "toy-widgets",
            "version": "1.2.0",
            "manifest_sha": "abc123",
        },
    )
    path = decisions_path(tmp_path, "pack", "toy-widgets")
    assert path == tmp_path / ".hpc" / "packs" / "toy-widgets.decisions.jsonl"
    records = read_decisions(tmp_path, "pack", "toy-widgets")
    assert len(records) == 1
    assert records[0]["scope_kind"] == "pack"
    assert records[0]["resolved"]["pack"] == "toy-widgets"


def test_conclusion_kind_lands_under_hpc_conclusions(tmp_path: Path) -> None:
    """A ``conclusion`` decision journals under
    ``.hpc/conclusions/<conclusion_id>.decisions.jsonl``.

    Design ``docs/design/evidence-memory.md`` E-shape: the human-authored finding
    rides ``append-decision`` under a caller-authored ``conclusion_id`` (an EIGHTH
    scope kind, its own path branch — the notebook/registration/pack precedent). A
    conclusion typically spans several runs/campaigns and outlives any one of them,
    so it is never coupled to a run or campaign journal (the R9 rationale).
    """
    append_decision(
        tmp_path,
        scope_kind="conclusion",
        scope_id="edge-x-2025h1",
        block="conclusion",
        response="conclude edge-x-2025h1 — see a3f2c9d1",
        resolved={
            "conclusion_id": "edge-x-2025h1",
            "tags": ["edge-x"],
            "citations": [{"kind": "run", "ref": "widget-run-1", "sha": "a3f2c9d1"}],
            "finding": "no alpha in 2025H1",
        },
    )
    path = decisions_path(tmp_path, "conclusion", "edge-x-2025h1")
    assert path == tmp_path / ".hpc" / "conclusions" / "edge-x-2025h1.decisions.jsonl"
    records = read_decisions(tmp_path, "conclusion", "edge-x-2025h1")
    assert len(records) == 1
    assert records[0]["scope_kind"] == "conclusion"
    assert records[0]["resolved"]["conclusion_id"] == "edge-x-2025h1"


def test_scope_kinds_lockstep_with_wire_literal() -> None:
    """``SCOPE_KINDS`` and the wire ``ScopeKind`` literal stay byte-for-byte equal.

    The lockstep contract the notebook/registration/pack kinds established: the
    state frozenset and the ``_wire`` Pydantic literal are two spellings of ONE
    vocabulary; a kind added to one but not the other is the drift this pins.
    """
    from typing import get_args

    from hpc_agent._wire.actions.decision_journal import ScopeKind
    from hpc_agent.state.decision_journal import SCOPE_KINDS

    assert set(get_args(ScopeKind)) == set(SCOPE_KINDS)
    assert "conclusion" in SCOPE_KINDS


def test_scope_kind_is_a_separate_store_from_run_and_campaign(tmp_path: Path) -> None:
    """Existing run/campaign locality is unchanged by the new scope kind."""
    append_decision(tmp_path, scope_kind="run", scope_id="id", block="submit.S1", response="y")
    append_decision(
        tmp_path, scope_kind="campaign", scope_id="id", block="campaign.spec", response="y"
    )
    append_decision(tmp_path, scope_kind="scope", scope_id="id", block="scope-lock", response="y")
    append_decision(
        tmp_path, scope_kind="notebook", scope_id="id", block="notebook-sign-off", response="y"
    )
    append_decision(
        tmp_path, scope_kind="registration", scope_id="id", block="registration", response="y"
    )
    append_decision(tmp_path, scope_kind="pack", scope_id="id", block="pack-bind", response="bound")
    append_decision(
        tmp_path, scope_kind="conclusion", scope_id="id", block="conclusion", response="concluded"
    )
    assert (
        decisions_path(tmp_path, "run", "id") == tmp_path / ".hpc" / "runs" / "id.decisions.jsonl"
    )
    assert (
        decisions_path(tmp_path, "campaign", "id")
        == tmp_path / ".hpc" / "campaigns" / "id" / "decisions.jsonl"
    )
    assert (
        decisions_path(tmp_path, "registration", "id")
        == tmp_path / ".hpc" / "registrations" / "id.decisions.jsonl"
    )
    assert (
        decisions_path(tmp_path, "pack", "id") == tmp_path / ".hpc" / "packs" / "id.decisions.jsonl"
    )
    assert (
        decisions_path(tmp_path, "conclusion", "id")
        == tmp_path / ".hpc" / "conclusions" / "id.decisions.jsonl"
    )
    assert len(read_decisions(tmp_path, "run", "id")) == 1
    assert len(read_decisions(tmp_path, "campaign", "id")) == 1
    assert len(read_decisions(tmp_path, "scope", "id")) == 1
    assert len(read_decisions(tmp_path, "notebook", "id")) == 1
    assert len(read_decisions(tmp_path, "registration", "id")) == 1
    assert len(read_decisions(tmp_path, "pack", "id")) == 1
    assert len(read_decisions(tmp_path, "conclusion", "id")) == 1


def test_run_and_campaign_scopes_are_separate_stores(tmp_path: Path) -> None:
    append_decision(
        tmp_path, scope_kind="run", scope_id="shared-id", block="submit.S1", response="y"
    )
    append_decision(
        tmp_path, scope_kind="campaign", scope_id="shared-id", block="campaign.spec", response="y"
    )
    run_records = read_decisions(tmp_path, "run", "shared-id")
    camp_records = read_decisions(tmp_path, "campaign", "shared-id")
    assert len(run_records) == 1
    assert len(camp_records) == 1
    assert run_records[0]["block"] == "submit.S1"
    assert camp_records[0]["block"] == "campaign.spec"


def test_y_and_nudge_both_persist(tmp_path: Path) -> None:
    append_decision(tmp_path, scope_kind="run", scope_id="r", block="submit.S1", response="y")
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id="r",
        block="submit.S1",
        response="no — cut the batch to 8",
    )
    responses = [r["response"] for r in read_decisions(tmp_path, "run", "r")]
    assert responses == ["y", "no — cut the batch to 8"]


def test_read_missing_journal_returns_empty(tmp_path: Path) -> None:
    assert read_decisions(tmp_path, "run", "never-written") == []


def test_ts_is_auto_stamped_when_omitted(tmp_path: Path) -> None:
    rec = append_decision(tmp_path, scope_kind="run", scope_id="r", block="b", response="y")
    assert rec["ts"]  # non-empty ISO string
    assert rec["schema_version"] == 1


@pytest.mark.parametrize(
    ("scope_kind", "scope_id", "block", "response"),
    [
        ("bogus", "r", "b", "y"),  # unknown scope_kind
        ("run", "", "b", "y"),  # empty scope_id
        ("run", "../escape", "b", "y"),  # path-escaping scope_id
        ("run", "r", "", "y"),  # empty block
        ("run", "r", "b", ""),  # empty response
    ],
)
def test_append_rejects_invalid_input(
    tmp_path: Path, scope_kind: str, scope_id: str, block: str, response: str
) -> None:
    with pytest.raises(errors.SpecInvalid):
        append_decision(
            tmp_path,
            scope_kind=scope_kind,
            scope_id=scope_id,
            block=block,
            response=response,
        )
