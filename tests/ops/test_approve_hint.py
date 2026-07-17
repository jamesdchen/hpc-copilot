"""The scoped-consent hint (OFFERED-CONSENT ruling, 2026-07-16).

``compose_approve_hint`` is the code composer that generalizes the audit-view
"To sign: type ..." precedent to the block-drive decision boundaries: it renders
the ready-to-type consent utterance naming the exact scope tokens (successor, run,
the ``@<sha8>`` spec pin) plus, for a standing consent, its bounds. This battery
pins:

* the utterance grammar (short scoped form, sha pin included when materialized);
* DETERMINISM — same tokens always render the same utterance;
* the standing/overnight variant naming duration/caps/wake;
* the GATE-ACCEPTANCE contract — the composed utterance, typed back as the human's
  ``response``, commits through the SAME ``append_decision`` gates a bare ``y`` does
  (backward compat preserved; no gate weakened), while the load-bearing
  overnight-consent refusal of an agent-relayed ``y`` stays intact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.ops.relay_render import compose_approve_hint
from hpc_agent.state.decision_journal import read_decisions

if TYPE_CHECKING:
    from pathlib import Path

_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f90112233445566778899aabbccddeeff00"


# ── the utterance grammar ──────────────────────────────────────────────────────


def test_scoped_utterance_names_successor_run_and_sha_pin() -> None:
    hint = compose_approve_hint(
        workflow="submit",
        successor="submit-s2",
        run_id="pi-train-d363e2a3",
        cluster="hoffman2",
        next_spec_sha=_SHA,
    )
    assert hint is not None
    assert hint["utterance"] == "y submit-s2 pi-train-d363e2a3 @a1b2c3d4"
    assert hint["bare_ok"] is True
    assert hint["standing"] is False
    # The scope the y grants, made legible as named tokens.
    assert hint["scope_tokens"]["next_block"] == "submit-s2"
    assert hint["scope_tokens"]["run_id"] == "pi-train-d363e2a3"
    assert hint["scope_tokens"]["cluster"] == "hoffman2"
    assert hint["scope_tokens"]["next_spec_sha"] == _SHA
    assert "submit-s2" in hint["line"] and "a1b2c3d4" in hint["line"]


def test_absent_sha_omits_the_pin_but_still_scopes() -> None:
    """A Row-14 composition refusal (no materialized sha) drops the @pin, keeps scope."""
    hint = compose_approve_hint(
        workflow="submit",
        successor="submit-s3",
        run_id="r1",
        next_spec_sha=None,
    )
    assert hint is not None
    assert hint["utterance"] == "y submit-s3 r1"
    assert "@" not in hint["utterance"]
    assert "next_spec_sha" not in hint["scope_tokens"]


def test_no_successor_or_no_run_yields_no_hint() -> None:
    assert compose_approve_hint(workflow="submit", successor=None, run_id="r1") is None
    assert compose_approve_hint(workflow="submit", successor="submit-s2", run_id=None) is None
    assert compose_approve_hint(workflow="submit", successor="submit-s2", run_id="") is None


def test_composed_line_is_deterministic() -> None:
    """Same record -> same utterance (the determinism the ruling's token-exact bar needs)."""
    kw: dict[str, Any] = {
        "workflow": "aggregate",
        "successor": "aggregate-run",
        "run_id": "causal_tune_tree-de448128",
        "cluster": "carc",
        "next_spec_sha": _SHA,
    }
    first = compose_approve_hint(**kw)
    second = compose_approve_hint(**kw)
    assert first == second


# ── standing / overnight consent: bounds are named ─────────────────────────────


def test_standing_campaign_consent_names_bounds() -> None:
    hint = compose_approve_hint(
        workflow="campaign",
        successor="campaign-watch",
        run_id="widget-camp",
        next_spec_sha=_SHA,
        standing=True,
        bounds={
            "expires_at": "2026-07-17T08:00:00+00:00",
            "walltime_cap": 36000,
            "budget_cap": 12.5,
            "wake": {"kind": "watch"},
        },
    )
    assert hint is not None
    assert hint["standing"] is True
    line = hint["line"]
    assert "STANDING consent" in line
    assert "unattended async campaign" in line
    assert "2026-07-17T08:00:00+00:00" in line  # duration
    assert "36000 wall-seconds" in line  # cap
    assert "12.5 budget" in line  # cap
    assert "wake armed" in line  # wake condition
    # Bounds are also captured as named tokens.
    bounds_tokens = hint["scope_tokens"]["bounds"]
    assert bounds_tokens["expires_at"] == "2026-07-17T08:00:00+00:00"
    assert bounds_tokens["walltime_cap"] == 36000
    assert bounds_tokens["budget_cap"] == 12.5


def test_standing_without_bounds_still_flags_standing() -> None:
    hint = compose_approve_hint(
        workflow="campaign",
        successor="campaign-watch",
        run_id="widget-camp",
        standing=True,
    )
    assert hint is not None
    assert hint["standing"] is True
    assert "STANDING consent" in hint["line"]
    assert "bounds" not in hint["scope_tokens"]


# ── gate acceptance: the composed utterance commits, and so does a bare y ───────


def _commit(tmp_path: Path, response: str) -> Any:
    """Commit a plain run-advance greenlight with *response* as the human's text."""
    return append_decision(
        experiment_dir=tmp_path,
        spec=AppendDecisionInput.model_validate(
            {
                "scope_kind": "run",
                "scope_id": "r1",
                "block": "submit-s1",
                "response": response,
                "resolved": {"next_block": "submit-s2"},
            }
        ),
    )


def test_composed_utterance_commits_through_the_existing_gates(tmp_path: Path) -> None:
    """The scoped utterance, typed back, passes every append_decision gate — no change."""
    hint = compose_approve_hint(
        workflow="submit",
        successor="submit-s2",
        run_id="r1",
        next_spec_sha=_SHA,
    )
    assert hint is not None
    result = _commit(tmp_path, hint["utterance"])
    records = read_decisions(tmp_path, "run", "r1")
    assert len(records) == 1
    assert records[0]["response"] == hint["utterance"]
    assert result.record.resolved["next_block"] == "submit-s2"


def test_plain_bare_y_still_commits_backward_compat(tmp_path: Path) -> None:
    """The hint ADDS the scoped form; a bare ``y`` keeps working where it works today."""
    _commit(tmp_path, "y")
    records = read_decisions(tmp_path, "run", "r1")
    assert len(records) == 1
    assert records[0]["response"] == "y"


def test_overnight_consent_still_refuses_an_agent_relayed_y(tmp_path: Path) -> None:
    """The load-bearing refusal is NOT weakened: a standing consent needs bound-capture.

    Composing a scoped HINT for a standing boundary must never let an agent-relayed
    ``y`` (or the composed line echoed back without a binding surface) satisfy the
    overnight-consent authorship gate — it requires a BOUND consent record captured
    at a binding surface (USER RULING 3), which a plain append cannot forge.
    """
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=AppendDecisionInput.model_validate(
                {
                    "scope_kind": "run",
                    "scope_id": "r1",
                    "block": "overnight-consent",
                    "response": "y submit-s3 r1 @a1b2c3d4",
                    "resolved": {
                        "cmd_sha": "deadbeef",
                        "heal_classes": ["retry"],
                        "expires_at": "2099-01-01T08:00:00+00:00",
                        "walltime_cap": 3600,
                    },
                }
            ),
        )
    # The E2 authorship-missing marker rides the refusal (the bound-capture gate).
    assert "bound" in str(exc.value).lower()
