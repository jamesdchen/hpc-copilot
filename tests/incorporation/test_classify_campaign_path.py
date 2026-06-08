"""Tests for the classify-campaign-path AST matcher primitive.

Pins the migration of the campaign 'path' point out of the LLM: the
common manual/strategy cases resolve deterministically (decided_by=code);
only an unparseable tasks.py escalates (decided_by=judgement).
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent.incorporation.classify_campaign_path import (
    classify_campaign_path,
    scan_campaign_path,
)

_MANUAL = """
def total():
    return 9

def resolve(i):
    grid = [(a, b) for a in (1, 2, 3) for b in (0.1, 0.2, 0.3)]
    return {"alpha": grid[i][0], "lr": grid[i][1]}
"""

_STRATEGY_OPTUNA = """
import optuna
from hpc_agent.execution.mapreduce.reduce.history import prior

def resolve(i):
    study = optuna.create_study(direction="minimize")
    for past in prior(".", "camp"):
        study.tell(past.trial, past.value)
    t = study.ask()
    return {"lr": t.suggest_float("lr", 1e-4, 1e-1)}
"""


def _write(tmp_path: Path, body: str) -> str:
    p = tmp_path / "tasks.py"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_manual_grid_resolves_to_code(tmp_path: Path) -> None:
    out = classify_campaign_path(source_path=_write(tmp_path, _MANUAL))
    assert out["path"] == "manual"
    assert out["decided_by"] == "code"
    assert out["signals"] == []
    assert out["supports_async_concurrency"] is False


def test_strategy_optuna_resolves_to_code_with_signals(tmp_path: Path) -> None:
    out = classify_campaign_path(source_path=_write(tmp_path, _STRATEGY_OPTUNA))
    assert out["path"] == "strategy"
    assert out["decided_by"] == "code"
    assert any(s.startswith(("import:optuna", "from:")) for s in out["signals"])
    assert "call:create_study" in out["signals"]
    assert "call:tell" in out["signals"] and "call:ask" in out["signals"]
    # Optuna is built for parallel asks → the concurrency point may consider K.
    assert out["supports_async_concurrency"] is True


def test_unparseable_escalates_to_judgement(tmp_path: Path) -> None:
    out = classify_campaign_path(source_path=_write(tmp_path, "def total(:\n  this is not python"))
    assert out["path"] == "unclassifiable"
    assert out["decided_by"] == "judgement"
    assert set(out["candidates"]) == {"manual", "strategy"}


def test_missing_file_escalates(tmp_path: Path) -> None:
    out = classify_campaign_path(source_path=str(tmp_path / "nope.py"))
    assert out["path"] == "unclassifiable"
    assert out["decided_by"] == "judgement"


def test_scan_is_total_on_garbage() -> None:
    signals, parsed = scan_campaign_path("@@@ not python @@@")
    assert parsed is False
    assert signals == set()


_MANUAL_WITH_LOCAL_PRIOR = """
def prior(i):
    return i - 1

def total():
    return 5

def resolve(i):
    return {"x": prior(i)}
"""

_STRATEGY_PRIOR_ONLY = """
from hpc_agent.execution.mapreduce.reduce.history import prior

def resolve(i):
    past = prior(".", "camp")
    return {"x": len(past)}
"""

_MANUAL_WITH_LOCAL_ASK = """
def ask(i):
    return i

def total():
    return 3

def resolve(i):
    return {"x": ask(i)}
"""


def test_local_prior_is_not_a_strategy_signal(tmp_path: Path) -> None:
    # A manual grid that defines its own prior() must NOT be misread as strategy.
    out = classify_campaign_path(source_path=_write(tmp_path, _MANUAL_WITH_LOCAL_PRIOR))
    assert out["path"] == "manual"
    assert out["decided_by"] == "code"
    assert "call:prior" not in out["signals"]


def test_history_imported_prior_is_a_strategy_signal(tmp_path: Path) -> None:
    # prior() imported from the framework *history* module is a real Path-B tell,
    # even with no optimizer import present.
    out = classify_campaign_path(source_path=_write(tmp_path, _STRATEGY_PRIOR_ONLY))
    assert out["path"] == "strategy"
    assert out["decided_by"] == "code"
    assert "call:prior" in out["signals"]


def test_bare_local_ask_is_not_a_strategy_signal(tmp_path: Path) -> None:
    # ask/tell count only as method calls (study.ask()); a bare local ask() must not.
    out = classify_campaign_path(source_path=_write(tmp_path, _MANUAL_WITH_LOCAL_ASK))
    assert out["path"] == "manual"
    assert "call:ask" not in out["signals"]
