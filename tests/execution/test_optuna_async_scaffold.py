"""RFC #362 §4 correctness tests for the async optuna campaign scaffold.

Unit C of the campaign-async-refill swarm verifies the *correctness half* of
continuous-async refill: the shipped
``execution/mapreduce/templates/scaffolds/optuna_async_strategy.py`` must
propose distinctly under K-in-flight concurrency and reconcile finished trials
out of order, all while staying load-idempotent (a crash mid-tick re-imports
the module and must replay, never re-ask).

The architecture is **one-trial-per-run** (``total()`` returns B=1): the
campaign resolver loops ``refill_count`` submits per tick, each re-importing
this module against the SAME persistent optuna store. So "B distinct asks" is
satisfied by ``constant_liar`` across K *separate* asks, and the load-bearing
knobs are (a) index the proposal by the SUBMITTED count (``len(_history())``),
not the completed count; (b) tell by ``trial_token`` (out-of-order safe),
``RUNNING``-guarded so a re-tell is a no-op; (c) the per-index proposal file is
the idempotency ledger — a re-ask after crash reads the persisted file and
never imports the optimizer.

The sibling ``tests/incorporation/test_scaffold_strategy.py`` covers the
scaffold-strategy *emit* path (``--async-refill`` → this asset, sync default,
pbt no-op) and the basic distinct-ask / in-process-idempotency / out-of-order
tell. This file pins the properties that suite does NOT: the ``RUNNING`` guard
firing on a re-tell, submitted-vs-completed index divergence, crash-replay
across a fresh module load with optuna *absent*, the tell-guard edge cases, and
the ``total()`` / ``resolve()`` contract under the async variant.

optuna is not a test dependency; a minimal in-memory stand-in
(``tests._optuna_fakes.make_fake_optuna``) records sampler kwargs, a per-trial
tell log, and persists studies by name so ``create_study(load_if_exists=True)``
returns the SAME study — the way the real sqlite store carries trial state
across re-imports.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

from hpc_agent import _PACKAGE_ROOT
from hpc_agent.incorporation.scaffold_strategy import scaffold_strategy
from tests._optuna_fakes import make_fake_optuna

_DEST_REL = Path(".hpc") / "tasks.py"
_ASYNC_ASSET = (
    Path(_PACKAGE_ROOT)
    / "execution"
    / "mapreduce"
    / "templates"
    / "scaffolds"
    / "optuna_async_strategy.py"
)


def _load(path: Path) -> Any:
    """Load a materialized tasks.py as a fresh module (a crash-restart re-import)."""
    spec = importlib.util.spec_from_file_location(
        f"_async_scaffold_{path.parent.parent.name}_{id(path):x}", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_async(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, optuna: Any | None) -> Any:
    """Materialize + load the async scaffold, wired to *optuna* (or none) and
    tmp dirs. ``_history`` is left for the test to control via monkeypatch."""
    scaffold_strategy(output_dir=tmp_path, name="optuna", async_refill=True)
    module = _load(tmp_path / _DEST_REL)
    if optuna is not None:
        monkeypatch.setitem(sys.modules, "optuna", optuna)
    monkeypatch.setattr(module, "_CID", "test")
    monkeypatch.setattr(module, "_CAMPAIGN_DIR", tmp_path)
    monkeypatch.setattr(module, "_PROPOSALS_DIR", tmp_path / "proposals")
    return module


def _rec(*, complete: bool, token: int | None, val: float | None = None) -> dict[str, Any]:
    metrics: dict[str, Any] = {} if val is None else {"val_loss": val}
    return {
        "complete": complete,
        "metrics": metrics,
        "trial_tokens": None if token is None else [token],
    }


# ─── submitted-count vs completed-count index (the async difference) ─────────


def test_current_proposal_indexes_by_submitted_not_completed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_current_proposal`` proposes at ``len(_history())`` (submitted), not
    ``_completed_count()`` — so refilled iterations don't collide on one index.

    Three iterations submitted, only one finished: the sync template would index
    the proposal by 1 (completed); the async template must index by 3 (submitted).
    """
    fake = make_fake_optuna()
    module = _load_async(tmp_path, monkeypatch, optuna=fake)
    history = [
        _rec(complete=True, token=0, val=0.5),
        _rec(complete=False, token=1),
        _rec(complete=False, token=2),
    ]
    monkeypatch.setattr(module, "_history", lambda: history)

    assert module._submitted_count() == 3
    assert module._completed_count() == 1

    module._current_proposal()

    # Indexed by submitted count → iter_00003, NOT the completed-count iter_00001.
    assert (tmp_path / "proposals" / "iter_00003.json").exists()
    assert not (tmp_path / "proposals" / "iter_00001.json").exists()


def test_refill_tick_asks_distinct_trials_as_history_grows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real refill mechanism: within one tick each submit writes a sidecar
    (history grows by one), so successive ``_current_proposal`` calls index a
    fresh iteration and ask a DISTINCT trial. Distinctness is per constant_liar."""
    fake = make_fake_optuna()
    module = _load_async(tmp_path, monkeypatch, optuna=fake)
    history: list[dict[str, Any]] = []
    monkeypatch.setattr(module, "_history", lambda: history)

    tokens: list[int] = []
    for _ in range(3):
        p = module._current_proposal()
        tokens.append(p["trial_token"])
        # Each submit writes its sidecar → the next submit sees one more record.
        history.append(_rec(complete=False, token=p["trial_token"]))

    assert tokens == [0, 1, 2]  # three distinct trials asked
    assert len(fake._studies["test"].trials) == 3
    for n in range(3):
        assert (tmp_path / "proposals" / f"iter_{n:05d}.json").exists()
    # The decorrelating sampler is wired on every create_study.
    assert fake._sampler_calls and all(c.get("constant_liar") is True for c in fake._sampler_calls)


# ─── tell-by-token: re-tell no-op (the RUNNING guard must fire) ──────────────


def test_retell_of_completed_trial_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A finished trial is told exactly once: the second tick's ``_tell_finished``
    sees state != RUNNING and skips it. Proves the idempotent-tell guard fires."""
    fake = make_fake_optuna()
    module = _load_async(tmp_path, monkeypatch, optuna=fake)
    history: list[dict[str, Any]] = []
    monkeypatch.setattr(module, "_history", lambda: history)

    # Submit trial 0, it finishes, submit trial 1 (which tells trial 0).
    module._propose(0)
    history.append(_rec(complete=True, token=0, val=0.3))
    module._propose(1)

    study = fake._studies["test"]
    assert study.trials[0].state == "complete"
    assert study.tell_log == [(0, 0.3)]  # told once

    # Next tick re-runs _tell_finished over the SAME (still-complete) record.
    module._propose(2)
    assert study.tell_log == [(0, 0.3)]  # NOT told again — RUNNING guard fired


def test_tell_finished_reconciles_out_of_order_by_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record order != trial order: a later-submitted trial that finishes first
    is told by its ``trial_token``, never by record position."""
    fake = make_fake_optuna()
    module = _load_async(tmp_path, monkeypatch, optuna=fake)
    history: list[dict[str, Any]] = []
    monkeypatch.setattr(module, "_history", lambda: history)

    for n in range(3):  # asks trials 0,1,2
        module._propose(n)
    study = fake._studies["test"]

    # Trial 2 finishes FIRST; 0 and 1 still in flight.
    history.extend(
        [
            _rec(complete=False, token=0),
            _rec(complete=False, token=1),
            _rec(complete=True, token=2, val=0.4),
        ]
    )
    module._propose(3)

    assert study.tell_log == [(2, 0.4)]  # told trial 2 by token
    assert study.trials[0].state == "running"
    assert study.trials[1].state == "running"


def test_tell_finished_skips_malformed_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tell guards hold: a record with no token, an out-of-range token, or a
    missing objective key is skipped without crashing; a valid one still tells."""
    fake = make_fake_optuna()
    module = _load_async(tmp_path, monkeypatch, optuna=fake)
    history: list[dict[str, Any]] = []
    monkeypatch.setattr(module, "_history", lambda: history)

    module._propose(0)  # only trial 0 exists (n_trials == 1)

    history.extend(
        [
            _rec(complete=True, token=None, val=0.1),  # no trial_tokens → skip
            _rec(complete=True, token=99, val=0.2),  # token out of range → skip
            {"complete": True, "metrics": {}, "trial_tokens": [0]},  # no objective → skip
        ]
    )
    module._propose(1)  # runs _tell_finished over the malformed history

    assert fake._studies["test"].tell_log == []  # nothing told, no exception


# ─── crash-replay idempotency (fresh module load, optuna absent) ─────────────


def test_crash_replay_reuses_persisted_proposal_without_optuna(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submit that already wrote its proposal file replays it after a crash:
    a FRESH module load calls ``_propose`` at the same index and returns the
    persisted proposal WITHOUT importing optuna (optuna isn't even installed).

    This is the compute-node / crash-restart fast path — the proposal file is
    the idempotency ledger, so no phantom trial is minted on re-import.
    """
    fake = make_fake_optuna()
    module = _load_async(tmp_path, monkeypatch, optuna=fake)
    monkeypatch.setattr(module, "_history", list)

    original = module._propose(2)
    on_disk = tmp_path / "proposals" / "iter_00002.json"
    assert on_disk.exists()
    assert len(fake._studies["test"].trials) == 1  # exactly one ask

    # Simulate a crash + restart: drop the fake so a real ``import optuna`` inside
    # _propose would raise (optuna is not installed) — proving it is never reached.
    monkeypatch.delitem(sys.modules, "optuna", raising=False)
    assert "optuna" not in sys.modules
    reloaded = _load(tmp_path / _DEST_REL)
    monkeypatch.setattr(reloaded, "_CID", "test")
    monkeypatch.setattr(reloaded, "_CAMPAIGN_DIR", tmp_path)
    monkeypatch.setattr(reloaded, "_PROPOSALS_DIR", tmp_path / "proposals")
    monkeypatch.setattr(reloaded, "_history", list)

    replayed = reloaded._propose(2)  # must NOT import optuna

    assert replayed == original  # same persisted proposal
    assert "optuna" not in sys.modules  # the optimizer was never imported


def test_propose_needs_optuna_only_on_the_ask_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contrast to the replay test: a MISSING proposal file forces the ask path,
    which does import optuna — so the fast path above is genuinely what saves the
    re-import, not an unconditional optuna-free code path."""
    module = _load_async(tmp_path, monkeypatch, optuna=None)  # no fake wired
    monkeypatch.setattr(module, "_history", list)
    monkeypatch.delitem(sys.modules, "optuna", raising=False)

    with pytest.raises(ModuleNotFoundError):
        module._propose(0)  # no cached file → import optuna → fails (not installed)


# ─── total() / resolve() contract under the async variant ────────────────────


def test_total_is_b1_gated_on_completed_not_submitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``total()`` is B=1 while COMPLETED trials are below the cap (the resolver
    loops refill_count submits), and gates on completed — not submitted — count,
    so many in-flight trials don't prematurely halt the campaign."""
    module = _load_async(tmp_path, monkeypatch, optuna=None)

    # Many submitted, few completed → still B=1 (gate is on completed count).
    monkeypatch.setattr(
        module,
        "_history",
        lambda: [_rec(complete=(i < 2), token=i, val=0.1) for i in range(10)],
    )
    assert module._completed_count() == 2
    assert module._submitted_count() == 10
    assert module.total() == 1

    # At/over the completed-trial cap → 0 (no further asks).
    monkeypatch.setattr(module, "_MAX_TRIALS", 2)
    assert module.total() == 0


def test_resolve_roundtrips_reserved_trial_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resolve`` returns the swept params plus the reserved ``trial_token`` key
    (excluded from cmd_sha, round-tripped to the sidecar for out-of-order tell)."""
    fake = make_fake_optuna()
    module = _load_async(tmp_path, monkeypatch, optuna=fake)
    monkeypatch.setattr(module, "_history", list)

    resolved = module.resolve(0)

    assert "trial_token" in resolved
    assert resolved["trial_token"] == 0
    assert set(resolved) == {"lr", "weight_decay", "trial_token"}
    # The reserved key is the one compute_cmd_sha strips (identity is params-only).
    from hpc_agent.state.run_sha import RESERVED_TASK_KEYS

    assert "trial_token" in RESERVED_TASK_KEYS
