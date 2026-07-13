"""Direct-atom tests for the ``scope-lock`` / ``scope-status`` primitives.

Constructs the wire spec, calls the primitive, asserts on the result (the
primitive-recipe atom-test minimum). Covers: lock → locked state; re-lock is
idempotent-in-effect (``already_locked``); the pure-read scope-status over one
tag and over every tag; and that a status read writes nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.scope_lock import (
    ScopeLockInput,
    ScopeLockResult,
    ScopeStatusInput,
    ScopeStatusResult,
)
from hpc_agent.ops.decision.journal.scope_lock import scope_lock, scope_status
from hpc_agent.state import scopes

if TYPE_CHECKING:
    from pathlib import Path


def _lock(tmp_path: Path, scope: str, reason: str = "freeze after canary") -> ScopeLockResult:
    return scope_lock(
        experiment_dir=tmp_path,
        spec=ScopeLockInput.model_validate({"scope": scope, "reason": reason}),
    )


def _status(tmp_path: Path, scope: str | None = None) -> ScopeStatusResult:
    payload = {"scope": scope} if scope is not None else {}
    return scope_status(experiment_dir=tmp_path, spec=ScopeStatusInput.model_validate(payload))


# ── scope-lock ───────────────────────────────────────────────────────────────


def test_lock_sets_locked_state(tmp_path: Path) -> None:
    out = _lock(tmp_path, "holdout")
    assert out.scope == "holdout"
    assert out.locked is True
    assert out.already_locked is False
    assert out.path.endswith("holdout.decisions.jsonl")
    # State visible through the substrate.
    assert scopes.is_scope_locked(tmp_path, "holdout") is True


def test_relock_reports_already_locked(tmp_path: Path) -> None:
    _lock(tmp_path, "holdout")
    again = _lock(tmp_path, "holdout", reason="re-freeze")
    assert again.locked is True
    assert again.already_locked is True  # idempotent-in-effect
    # Both lock records are durable (append-only audit trail).
    from hpc_agent.state.decision_journal import read_decisions

    records = read_decisions(tmp_path, "scope", "holdout")
    actions = [r["resolved"].get("scope_action") for r in records]
    assert actions == ["lock", "lock"]


def test_lock_rejects_bad_tag(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        scope_lock(
            experiment_dir=tmp_path,
            spec=ScopeLockInput.model_validate({"scope": "ok", "reason": "r"}).model_copy(
                update={"scope": "../escape"}
            ),
        )


# ── scope-status (pure read) ─────────────────────────────────────────────────


def test_status_reads_locked_and_looks(tmp_path: Path) -> None:
    _lock(tmp_path, "holdout")
    scopes.record_look(
        tmp_path, "holdout", run_id="r1", cmd_sha="a", lineage_root="rootA", reducer_block="reduce"
    )
    out = _status(tmp_path, "holdout")
    entry = out.scopes["holdout"]
    assert entry.locked is True
    assert entry.looks.prior_looks == 1
    assert entry.looks.distinct_lineages == 1
    assert entry.lock_history_len == 1


def test_status_unlocked_scope_default(tmp_path: Path) -> None:
    _lock(tmp_path, "holdout")
    # An unlock via append-decision flips the state; status reflects it.
    from hpc_agent.state.decision_journal import append_decision

    append_decision(
        tmp_path,
        scope_kind="scope",
        scope_id="holdout",
        block="scope-unlock",
        response="reopen for a confirmatory sweep",
        resolved={"scope_action": "unlock"},
    )
    entry = _status(tmp_path, "holdout").scopes["holdout"]
    assert entry.locked is False
    assert entry.lock_history_len == 2  # lock + unlock, append-only


def test_status_all_tags_when_scope_omitted(tmp_path: Path) -> None:
    _lock(tmp_path, "holdout")
    _lock(tmp_path, "embargo")
    out = _status(tmp_path)
    assert set(out.scopes) == {"holdout", "embargo"}


def test_status_missing_tree_is_empty(tmp_path: Path) -> None:
    out = _status(tmp_path)
    assert out.scopes == {}


def test_status_writes_nothing(tmp_path: Path) -> None:
    """A pure read never scaffolds the scopes tree."""
    _status(tmp_path, "never-looked")
    from hpc_agent._kernel.contract.layout import RepoLayout

    assert not (RepoLayout(tmp_path).hpc / "scopes").exists()


def test_status_rejects_bad_tag(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid):
        scope_status(
            experiment_dir=tmp_path,
            spec=ScopeStatusInput.model_validate({"scope": "ok"}).model_copy(
                update={"scope": "a/b"}
            ),
        )
