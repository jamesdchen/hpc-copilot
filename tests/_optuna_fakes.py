"""A shared, in-memory ``optuna`` stand-in for the campaign-scaffold tests.

optuna is not a test dependency, so the async-scaffold and scaffold-strategy
suites each hand-built the same minimal fake module: a study store keyed by
name (so ``create_study(load_if_exists=True)`` returns the SAME study, the way
the real sqlite store carries trial state across ``_propose`` re-imports), a
``TPESampler`` that records its kwargs, and ask/tell trial bookkeeping. The two
copies differed only in the async one's ``Study.tell_log`` — the per-trial tell
record its ``RUNNING``-guard assertions read. This module hoists the SUPERSET
(``tell_log`` present) into one importable builder; the extra field is inert
for callers that ignore it.

ADDITIVE by design, mirroring the other root-level shared helpers
(``tests._ssh_fakes``, ``tests._subprocess``): import it explicitly beside them
rather than reaching for a fixture::

    from tests._optuna_fakes import make_fake_optuna

    fake = make_fake_optuna()
    monkeypatch.setitem(sys.modules, "optuna", fake)
"""

from __future__ import annotations

import types
from typing import Any

__all__ = ["make_fake_optuna"]


def make_fake_optuna() -> types.ModuleType:
    """A minimal in-memory optuna stand-in with a tell log + sampler capture.

    Records sampler kwargs and persists studies by name so successive
    ``create_study(load_if_exists=True)`` calls return the SAME study — the way
    the real sqlite store persists trial state across ``_propose`` re-imports.
    ``Study.tell_log`` keeps ``(trial_number, value)`` per tell so a test can
    assert a re-tell against an already-COMPLETE trial is a no-op (the RUNNING
    guard); callers that don't need it simply ignore it.
    """
    studies: dict[str, Any] = {}
    sampler_calls: list[dict[str, Any]] = []

    class TrialState:
        RUNNING = "running"
        COMPLETE = "complete"

    class Trial:
        def __init__(self, number: int) -> None:
            self.number = number
            self.state = TrialState.RUNNING

        def suggest_float(self, name: str, low: float, high: float, log: bool = False) -> float:
            # Deterministic + distinct per trial number so proposals differ.
            return low * (self.number + 1)

    class Study:
        def __init__(self) -> None:
            self.trials: list[Trial] = []
            # (trial_number, value) per tell — lets a test assert a re-tell
            # against an already-COMPLETE trial is a no-op (the RUNNING guard).
            self.tell_log: list[tuple[int, float]] = []

        def ask(self) -> Trial:
            t = Trial(len(self.trials))
            self.trials.append(t)
            return t

        def tell(self, trial: Trial, value: float) -> None:
            trial.state = TrialState.COMPLETE
            self.tell_log.append((trial.number, value))

    class TPESampler:
        def __init__(self, **kwargs: Any) -> None:
            sampler_calls.append(kwargs)

    def create_study(
        *,
        storage: str,
        study_name: str,
        direction: str,
        sampler: Any = None,
        load_if_exists: bool = False,
    ) -> Any:
        if load_if_exists and study_name in studies:
            return studies[study_name]
        s = Study()
        studies[study_name] = s
        return s

    mod = types.ModuleType("optuna")
    mod.create_study = create_study  # type: ignore[attr-defined]
    mod.trial = types.SimpleNamespace(TrialState=TrialState)  # type: ignore[attr-defined]
    mod.samplers = types.SimpleNamespace(TPESampler=TPESampler)  # type: ignore[attr-defined]
    mod._studies = studies  # type: ignore[attr-defined]
    mod._sampler_calls = sampler_calls  # type: ignore[attr-defined]
    return mod
