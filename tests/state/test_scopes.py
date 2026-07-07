"""Tests for the scope substrate (``state/scopes.py``).

Pins: lock/unlock newest-wins precedence and unlocked-by-default; the
look-ledger (scope, run_id) dedup no-op; ``lineage_root`` collapsing a
supersedes chain and surviving a cycle; and the look counts distinguishing
looks from distinct lineages. Boundary: no tag vocabulary, shape-only tag
validation, plain-integer counts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent.state import scopes

if TYPE_CHECKING:
    from pathlib import Path


# ── lock state ───────────────────────────────────────────────────────────────


def test_unlocked_by_default(tmp_path: Path) -> None:
    assert scopes.is_scope_locked(tmp_path, "s1") is False


def test_lock_then_unlock_then_lock_newest_wins(tmp_path: Path) -> None:
    scopes.record_lock(tmp_path, "s1", reason="freeze after canary")
    assert scopes.is_scope_locked(tmp_path, "s1") is True

    # An unlock is an ordinary appended decision — newest record decides.
    from hpc_agent.state.decision_journal import append_decision

    append_decision(
        tmp_path,
        scope_kind="scope",
        scope_id="s1",
        block="scope-lock",
        response="reopen for one more sweep",
        resolved={"scope_action": "unlock"},
    )
    assert scopes.is_scope_locked(tmp_path, "s1") is False

    scopes.record_lock(tmp_path, "s1", reason="re-freeze")
    assert scopes.is_scope_locked(tmp_path, "s1") is True


def test_lock_history_is_append_only(tmp_path: Path) -> None:
    """Unlock never erases the lock record — both survive on disk."""
    from hpc_agent.state.decision_journal import append_decision, read_decisions

    scopes.record_lock(tmp_path, "s1", reason="lock")
    append_decision(
        tmp_path,
        scope_kind="scope",
        scope_id="s1",
        block="scope-lock",
        response="unlock",
        resolved={"scope_action": "unlock"},
    )
    records = read_decisions(tmp_path, "scope", "s1")
    actions = [r["resolved"].get("scope_action") for r in records]
    assert actions == ["lock", "unlock"]  # both durable, in order


def test_non_lock_decisions_do_not_change_state(tmp_path: Path) -> None:
    """An interleaved non-lock/unlock record is skipped by the scan."""
    from hpc_agent.state.decision_journal import append_decision

    scopes.record_lock(tmp_path, "s1", reason="lock")
    append_decision(tmp_path, scope_kind="scope", scope_id="s1", block="note", response="fyi")
    assert scopes.is_scope_locked(tmp_path, "s1") is True


def test_record_lock_stores_reason_and_action(tmp_path: Path) -> None:
    rec = scopes.record_lock(tmp_path, "s1", reason="freeze after canary")
    assert rec["response"] == "freeze after canary"
    assert rec["resolved"] == {"scope_action": "lock"}
    assert rec["scope_kind"] == "scope"


# ── look ledger ──────────────────────────────────────────────────────────────


def test_record_look_dedup_is_noop_on_second_same_pair(tmp_path: Path) -> None:
    first = scopes.record_look(
        tmp_path, "s1", run_id="r1", cmd_sha="sha1", lineage_root="r1", reducer_block="reduce"
    )
    second = scopes.record_look(
        tmp_path, "s1", run_id="r1", cmd_sha="sha1", lineage_root="r1", reducer_block="reduce"
    )
    assert first is not None
    assert second is None  # no-op
    assert scopes.count_prior_looks(tmp_path, "s1")["prior_looks"] == 1


def test_record_look_stores_identity_never_a_metric(tmp_path: Path) -> None:
    rec = scopes.record_look(
        tmp_path, "s1", run_id="r1", cmd_sha="sha1", lineage_root="root1", reducer_block="reduce"
    )
    assert rec is not None
    assert set(rec) == {
        "schema_version",
        "ts",
        "scope",
        "run_id",
        "cmd_sha",
        "lineage_root",
        "reducer_block",
    }
    # No metric-shaped key ever lands in the ledger.
    assert not ({"value", "metric", "mean", "result", "score"} & set(rec))


def test_counts_distinguish_looks_from_lineages(tmp_path: Path) -> None:
    # 3 looks across 2 distinct lineages.
    scopes.record_look(
        tmp_path, "s1", run_id="r1", cmd_sha="a", lineage_root="rootA", reducer_block="reduce"
    )
    scopes.record_look(
        tmp_path, "s1", run_id="r2", cmd_sha="a2", lineage_root="rootA", reducer_block="reduce"
    )
    scopes.record_look(
        tmp_path, "s1", run_id="r3", cmd_sha="b", lineage_root="rootB", reducer_block="reduce"
    )
    counts = scopes.count_prior_looks(tmp_path, "s1")
    assert counts == {"prior_looks": 3, "distinct_lineages": 2}
    assert all(isinstance(v, int) for v in counts.values())


def test_count_prior_looks_empty_ledger(tmp_path: Path) -> None:
    assert scopes.count_prior_looks(tmp_path, "never") == {
        "prior_looks": 0,
        "distinct_lineages": 0,
    }


# ── lineage_root ─────────────────────────────────────────────────────────────


class _FakeRecord:
    def __init__(self, supersedes: str = "") -> None:
        self.supersedes = supersedes


def _patch_records(monkeypatch: pytest.MonkeyPatch, records: dict[str, _FakeRecord]) -> None:
    monkeypatch.setattr(scopes, "_load_run", lambda _exp, rid: records.get(rid))


def test_lineage_root_collapses_a_supersedes_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C supersedes B supersedes A (A is the original root).
    _patch_records(
        monkeypatch,
        {
            "A": _FakeRecord(supersedes=""),
            "B": _FakeRecord(supersedes="A"),
            "C": _FakeRecord(supersedes="B"),
        },
    )
    assert scopes.lineage_root(tmp_path, "C") == "A"
    assert scopes.lineage_root(tmp_path, "B") == "A"
    assert scopes.lineage_root(tmp_path, "A") == "A"


def test_lineage_root_of_unknown_run_is_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_records(monkeypatch, {})
    assert scopes.lineage_root(tmp_path, "orphan") == "orphan"


def test_lineage_root_survives_a_cycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Corrupt loop: X supersedes Y supersedes X — must not spin, returns
    # the deterministic smallest id in the loop.
    _patch_records(
        monkeypatch,
        {
            "Y": _FakeRecord(supersedes="X"),
            "X": _FakeRecord(supersedes="Y"),
        },
    )
    assert scopes.lineage_root(tmp_path, "X") == "X"
    assert scopes.lineage_root(tmp_path, "Y") == "X"  # min({X, Y}) — entry-independent


# ── lineage_chain ────────────────────────────────────────────────────────────


def test_lineage_chain_orders_newest_to_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C supersedes B supersedes A — chain is newest→root.
    _patch_records(
        monkeypatch,
        {
            "A": _FakeRecord(supersedes=""),
            "B": _FakeRecord(supersedes="A"),
            "C": _FakeRecord(supersedes="B"),
        },
    )
    assert scopes.lineage_chain(tmp_path, "C") == ["C", "B", "A"]
    assert scopes.lineage_chain(tmp_path, "B") == ["B", "A"]
    assert scopes.lineage_chain(tmp_path, "A") == ["A"]
    # An unknown run is its own single-element chain.
    _patch_records(monkeypatch, {})
    assert scopes.lineage_chain(tmp_path, "orphan") == ["orphan"]


def test_chain_root_equals_lineage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Property: chain[-1] == lineage_root for non-cyclic chains (one walk)."""
    _patch_records(
        monkeypatch,
        {
            "A": _FakeRecord(supersedes=""),
            "B": _FakeRecord(supersedes="A"),
            "C": _FakeRecord(supersedes="B"),
        },
    )
    for rid in ("A", "B", "C"):
        assert scopes.lineage_chain(tmp_path, rid)[-1] == scopes.lineage_root(tmp_path, rid)


# ── tag validation (shape only, no vocabulary) ───────────────────────────────


@pytest.mark.parametrize("tag", ["", "../escape", "a/b", "has space", "tab\ttab"])
def test_bad_tag_shape_refused(tmp_path: Path, tag: str) -> None:
    with pytest.raises(errors.SpecInvalid):
        scopes.is_scope_locked(tmp_path, tag)


@pytest.mark.parametrize("tag", ["holdout", "test-set", "embargo.v2", "any_slug-1.0"])
def test_any_slug_tag_is_accepted_no_vocabulary(tmp_path: Path, tag: str) -> None:
    # The framework attaches no meaning to a tag — every slug is equally valid.
    assert scopes.is_scope_locked(tmp_path, tag) is False
    scopes.record_lock(tmp_path, tag, reason="lock")
    assert scopes.is_scope_locked(tmp_path, tag) is True
