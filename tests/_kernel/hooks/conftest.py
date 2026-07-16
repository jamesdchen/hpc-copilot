"""Hook-test isolation: strip operator HPC_STOP_HOOK_* env overrides.

The demo/operator shell exports append-mode overrides (HPC_STOP_HOOK_APPEND,
HPC_STOP_HOOK_APPEND_ON_BLOCK) that change hook output SHAPE — with them
inherited, 28 hook tests fail locally while CI (clean env) stays green
(2026-07-16: a full night of "pre-existing KeyError 'decision'" adjudications
was exactly this leakage). Tests exercise the default contract; append-mode
tests set the vars explicitly via monkeypatch.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _strip_stop_hook_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HPC_STOP_HOOK_APPEND", raising=False)
    monkeypatch.delenv("HPC_STOP_HOOK_APPEND_ON_BLOCK", raising=False)
