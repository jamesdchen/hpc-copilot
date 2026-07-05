"""Deadline discipline for driver-side block-verb subprocesses.

The proving-run-#3 wedge class (an unbounded parent-side wait on a child
subprocess — ``tests/contracts/test_src_subprocess_timeout_discipline.py``)
closed for the two drivers by giving every ``hpc-agent <verb>`` span a
per-verb deadline from the block registry
(:func:`hpc_agent.infra.block_chain.verb_deadline_seconds`):

* watch/wait-class verbs get their spec's own ``wall_clock_budget_seconds``
  plus slack (the block's internal timeout terminator fires first; the driver
  deadline is only the backstop);
* everything else gets a class ceiling (quick vs heavy);
* on expiry the child is KILLED and the span reports exit 124 (the
  ``timeout(1)`` convention), with the verb + deadline named in the reason.

The fire-path tests use a real synthetic hanging child (``python -c
"time.sleep(60)"``) under an injected sub-second deadline — no real long
sleeps, and the kill is exercised for real (including the Windows post-kill
bounded drain inside ``infra.remote._capture_via_select``).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from hpc_agent._kernel.lifecycle import block_drive as bd
from hpc_agent._kernel.lifecycle import drive
from hpc_agent.infra import block_chain

# A child that would outlive any test run unless the deadline kills it.
_HANGING_CHILD = [sys.executable, "-c", "import time; time.sleep(60)"]

# Generous wall bound on "the kill happened": deadline (0.5s) + the Windows
# post-kill drain (5s) + process spawn overhead, with margin.
_KILL_LATENCY_CEILING_SEC = 30.0


# ─── the deadline table ───────────────────────────────────────────────────────


class TestVerbDeadlineTable:
    def test_watch_verbs_get_spec_budget_plus_slack(self) -> None:
        """A watch-class verb's deadline is its own wall-clock budget + slack."""
        spec = {"monitor": {"wall_clock_budget_seconds": 1200}}
        for verb in ("status-watch", "submit-s3"):
            assert (
                block_chain.verb_deadline_seconds(verb, spec)
                == 1200 + block_chain.WATCH_BUDGET_SLACK_SEC
            ), verb

    def test_watch_verb_without_budget_gets_default_ceiling(self) -> None:
        """No budget on the spec → the 24 h default budget + slack (still finite)."""
        expected = 86400 + block_chain.WATCH_BUDGET_SLACK_SEC
        assert block_chain.verb_deadline_seconds("status-watch", {}) == expected
        # campaign-watch's spec carries no budget field at all.
        assert block_chain.verb_deadline_seconds("campaign-watch", {"campaign_id": "c"}) == expected

    def test_top_level_budget_is_honored(self) -> None:
        """A bare monitor-flow-shaped spec carries the budget at top level."""
        assert (
            block_chain.verb_deadline_seconds("monitor-flow", {"wall_clock_budget_seconds": 300})
            == 300 + block_chain.WATCH_BUDGET_SLACK_SEC
        )

    def test_unknown_verb_falls_back_to_bounded_ceiling(self) -> None:
        """The tick-loop's flow verbs (not block verbs) still get a finite bound."""
        assert (
            block_chain.verb_deadline_seconds("aggregate-flow", {"run_id": "r"})
            == 86400 + block_chain.WATCH_BUDGET_SLACK_SEC
        )

    def test_every_chain_verb_has_a_finite_positive_deadline(self) -> None:
        """The guarantee itself: no block verb is ever awaited unboundedly."""
        for verbs in block_chain.ORDER.values():
            for verb in verbs:
                deadline = block_chain.verb_deadline_seconds(verb, {})
                assert 0 < deadline < float("inf"), verb

    def test_quick_class_is_tighter_than_heavy_class(self) -> None:
        quick = block_chain.verb_deadline_seconds("submit-s1", {})
        heavy = block_chain.verb_deadline_seconds("submit-s2", {})
        assert quick < heavy


# ─── block_drive._run_block_verb: the deadline fires ─────────────────────────


class TestRunBlockVerbDeadline:
    def test_deadline_kills_hanging_child(self, monkeypatch: Any, tmp_path: Path) -> None:
        """A wedged block child is killed at the deadline; span reports exit 124."""
        monkeypatch.setattr(block_chain, "verb_deadline_seconds", lambda *_a, **_k: 0.5)
        monkeypatch.setattr(bd, "_block_verb_argv", lambda *_a, **_k: list(_HANGING_CHILD))
        start = time.monotonic()
        result, code = bd._run_block_verb("status-snapshot", {}, tmp_path)
        elapsed = time.monotonic() - start
        assert result == {}
        assert code == bd._TIMEOUT_EXIT_CODE
        assert elapsed < _KILL_LATENCY_CEILING_SEC, "child was awaited, not killed"

    def test_chain_reason_names_verb_and_deadline(self, monkeypatch: Any, tmp_path: Path) -> None:
        """The timeout-shaped skip result names the verb and the deadline."""
        monkeypatch.setattr(bd, "_run_block_verb", lambda *_a, **_k: ({}, bd._TIMEOUT_EXIT_CODE))
        result, code = bd._chain(
            tmp_path,
            run_id="",  # no run_id → no watchdog stamp I/O
            workflow="status",
            first_verb="status-snapshot",
            first_spec={},
            first_label="chained",
        )
        assert code == bd._TIMEOUT_EXIT_CODE
        assert result.action == "skip"
        assert "status-snapshot" in result.reason
        assert "600s driver deadline" in result.reason  # the quick-class deadline
        assert "killed" in result.reason


# ─── drive._run_cli_step: the deadline fires ─────────────────────────────────


class TestRunCliStepDeadline:
    def test_deadline_kills_hanging_child(self, monkeypatch: Any, tmp_path: Path) -> None:
        """A wedged CLI-step child is killed at the deadline; step reports exit 124."""
        monkeypatch.setattr(block_chain, "verb_deadline_seconds", lambda *_a, **_k: 0.5)
        monkeypatch.setattr(drive, "_cli_step_argv", lambda *_a, **_k: list(_HANGING_CHILD))
        start = time.monotonic()
        code = drive._run_cli_step("monitor-flow", "r1", tmp_path)
        elapsed = time.monotonic() - start
        assert code == drive._TIMEOUT_EXIT_CODE
        assert elapsed < _KILL_LATENCY_CEILING_SEC, "child was awaited, not killed"

    def test_watch_class_deadline_reaches_the_run(self, monkeypatch: Any, tmp_path: Path) -> None:
        """The step's timeout= is the registry deadline (budget-class, not a constant)."""
        seen: dict[str, Any] = {}

        def _spy(verb: str, spec: Any = None) -> float:
            seen["verb"], seen["spec"] = verb, spec
            return 0.5

        monkeypatch.setattr(block_chain, "verb_deadline_seconds", _spy)
        monkeypatch.setattr(drive, "_cli_step_argv", lambda *_a, **_k: list(_HANGING_CHILD))
        drive._run_cli_step("monitor-flow", "r1", tmp_path)
        assert seen["verb"] == "monitor-flow"
        assert seen["spec"] == {"run_id": "r1"}
