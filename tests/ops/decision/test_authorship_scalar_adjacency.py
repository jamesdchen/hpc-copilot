"""The off-by-one derivation is scoped to RANGE-SHAPED claims (run-15 gate finding 2).

Proving run 15 (``docs/plans/proving-run-15-runsheet.md``, gate finding 2): the
human-authorship gate ACCEPTED ``n_samples=10000004`` although the number was
absent from the experiment namespace's utterance log — the integral off-by-one
leg (:func:`_derivation_rule`) matched it against the prior drill's stated
``10000003``. That leg exists for RANGE endpoints of a stated count
(``seeds=[0..19]`` from "20 seeds"; length 20 from "0 through 19"); a standalone
scalar asserts exactly itself, so its adjacency to a stated number is a
coincidence, never a derivation.

Pins: a bare scalar adjacent (±1) to a stated-but-unrelated number is REFUSED
(the ``10000004`` incident, both adjacency directions); verbatim / zero scalars
still pass; the range-shaped derivations the rule exists for still pass; and the
accept-side disclosure attributes ``off_by_one`` ONLY to range-shaped tokens.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput, AppendDecisionResult
from hpc_agent.ops.decision.journal import append_decision

if TYPE_CHECKING:
    from pathlib import Path


def _append(tmp_path: Path, **overrides: object) -> AppendDecisionResult:
    base: dict[str, object] = {
        "scope_kind": "run",
        "scope_id": "run-1",
        "block": "s1",
        "response": "y",
    }
    base.update(overrides)
    return append_decision(experiment_dir=tmp_path, spec=AppendDecisionInput.model_validate(base))


def _log_utterance(tmp_path: Path, text: str) -> None:
    """Simulate the harness-side UserPromptSubmit capture for *tmp_path*."""
    from hpc_agent.state.run_record import journal_dir
    from hpc_agent.state.utterances import append_utterance

    journal_dir(tmp_path)  # the namespace a real state write would have created
    assert append_utterance(tmp_path, text) is not None


def test_scalar_adjacent_above_a_stated_number_is_refused(tmp_path: Path) -> None:
    """THE run-15 incident pin: the log states ``10000003`` (the prior drill's
    n) and "20 seeds"; a standalone ``n_samples=10000004`` must NOT derive by
    adjacency — the gate refuses and names the token."""
    _log_utterance(tmp_path, "rerun the drill at n=10000003, 20 seeds")
    with pytest.raises(errors.SpecInvalid) as ei:
        _append(
            tmp_path,
            resolved={
                "task_generator": {
                    "kind": "items_x_seeds",
                    "params": {"items": [{"n_samples": 10000004}], "seeds": list(range(20))},
                }
            },
        )
    msg = str(ei.value)
    assert "task_generator is human-authored" in msg
    assert "10000004" in msg  # the underivable token is named


def test_scalar_adjacent_below_a_stated_count_is_also_refused(tmp_path: Path) -> None:
    """The exclusion is on STANDALONE SCALARS, not on the incident value: a
    bare scalar 19 with "20 seeds" stated rides the same coincidence
    adjacency (19 == 20 - 1) and must be refused too."""
    _log_utterance(tmp_path, "20 seeds, n_samples=1000000")
    with pytest.raises(errors.SpecInvalid) as ei:
        _append(
            tmp_path,
            resolved={
                "task_generator": {
                    "kind": "items_x_seeds",
                    "params": {"items": [{"n_samples": 19}], "seeds": [20]},
                }
            },
        )
    assert "19" in str(ei.value)


def test_scalar_stated_verbatim_is_accepted(tmp_path: Path) -> None:
    """The tightening gates ADJACENCY, never statement: the same scalar
    shape passes when the human actually uttered the number."""
    _log_utterance(tmp_path, "apex drill at n=10000004, 20 seeds")
    out = _append(
        tmp_path,
        resolved={
            "task_generator": {
                "kind": "items_x_seeds",
                "params": {"items": [{"n_samples": 10000004}], "seeds": list(range(20))},
            }
        },
    )
    assert out.record.resolved["task_generator"]["params"]["items"] == [{"n_samples": 10000004}]


def test_contiguous_run_endpoint_derivation_still_works(tmp_path: Path) -> None:
    """Boundary guard against over-tightening: the range form the off-by-one
    rule EXISTS for — ``seeds=[0..19]`` derived from the stated count "20
    seeds" — still passes (endpoint 19 and length 20 stay derivable)."""
    _log_utterance(tmp_path, "20 seeds, n_samples=1000000")
    out = _append(
        tmp_path,
        resolved={
            "task_generator": {
                "kind": "items_x_seeds",
                "params": {"items": [{"n_samples": 1_000_000}], "seeds": list(range(20))},
            }
        },
    )
    assert out.record.resolved["task_generator"]["params"]["seeds"] == list(range(20))


def test_string_range_literal_derivation_still_works(tmp_path: Path) -> None:
    """A string-embedded range literal (``"0-49"``) is range-shaped: its
    endpoint 49 still derives from the stated count "50 seeds"."""
    _log_utterance(tmp_path, "50 seeds at 1M samples")
    out = _append(
        tmp_path,
        resolved={
            "task_generator": {
                "kind": "items_x_seeds",
                "seeds": 50,
                "seed_range": "0-49",
                "samples": 1_000_000,
            }
        },
    )
    assert out.record.resolved["task_generator"]["seeds"] == 50


def test_disclosure_attributes_off_by_one_only_to_range_shaped_tokens(tmp_path: Path) -> None:
    """The accept-side disclosure (docket #1 part 2) reports the rule per
    token: a contiguous run's endpoint keeps ``off_by_one``; the scalars
    around it are ``verbatim`` / ``zero``."""
    _log_utterance(tmp_path, "20 seeds, n_samples=1000000")
    out = _append(
        tmp_path,
        resolved={
            "task_generator": {
                "kind": "items_x_seeds",
                "params": {"items": [{"n_samples": 1_000_000}], "seeds": list(range(20))},
            }
        },
    )
    disclosure = out.record.provenance["human_authorship"]
    assert disclosure["evidence_source"] == "harness_captured"
    assert disclosure["fields"]["task_generator"]["numbers"] == {
        "0": "zero",
        "19": "off_by_one",
        "20": "verbatim",
        "1000000": "verbatim",
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
