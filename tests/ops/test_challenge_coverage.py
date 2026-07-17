"""Behavior-pinning mutation battery for the CHALLENGE substrate + gate.

Targets: ``src/hpc_agent/state/challenges.py`` (the T1 substrate — content-sha,
target dispatch, per-challenge reduction) and
``src/hpc_agent/ops/decision/journal/challenge.py`` (the T5 gate helpers). Both
are trust-core adjacent: a silent flip here corrupts standing dissent — a fake
challenge sha binds, a resolution is mis-recorded, or a superseded/withdrawn
headline is reported wrong wherever the challenged record is disclosed.

The sibling suites already cover a lot: ``tests/state/test_challenges.py`` pins
the validators, the target-existence scan, the base reduce paths, and the
collector; ``tests/ops/decision/test_challenge_authorship.py`` pins the three
filing locks + the verdict/withdraw floor; ``test_multi_human_gate.py`` pins MH7;
``test_b4_ts_anchor.py`` pins the verdict/withdraw ts>=anchor filter. This file
pins the SUBTLER seams those do NOT reach, consequence-ranked:

* ``challenge_content_sha`` determinism + target/citation sensitivity (the sha
  the bind recomputes — a constant here defeats the whole recompute lock);
* the ``attestation``-kind target dispatch keys on SCOPE, not subject_id;
* ``reduce_challenge`` resolution NEWEST-WINS ordering, the unknown-verdict floor,
  superseded-over-withdrawn headline, foreign-id isolation, newest-filing target;
* the verdict ``VERDICTS`` membership boundary (exact, case-sensitive);
* the gate's dismissal-without-a-filing refusal.

Each assertion's docstring / comment names the mutant it kills. TOY WIDGET
vocabulary only, never a real domain's words.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.ops.decision.journal.challenge import (
    _challenge_filing_attestor,
    _challenge_filing_citations,
)
from hpc_agent.state import challenges
from hpc_agent.state.decision_journal import decisions_path
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


# ── fixtures / builders (toy widget vocabulary) ───────────────────────────────


def _run_target(subject_id: str, content_sha: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "kind": "run",
        "subject_kind": "widget-run",
        "subject_id": subject_id,
        "content_sha": content_sha,
        "scope": {"scope_kind": "run", "scope_id": subject_id},
    }
    base.update(overrides)
    return base


_CITE = [{"kind": "run", "ref": "widget-run-1", "sha": "sha-widget-1"}]


# ── challenge_content_sha: the recompute foundation ───────────────────────────


def test_content_sha_is_deterministic() -> None:
    """The same {target, citations} yields the SAME sha on every call — the bind
    recomputes this at append, so a nondeterministic sha would refuse every honest
    re-sign.

    kills: injecting any per-call nondeterminism (a wall-clock / id into the
    canonical payload)."""
    tgt = challenges.validate_target(_run_target("widget-run-1", "cmd-1"))
    a = challenges.challenge_content_sha(tgt, _CITE)
    b = challenges.challenge_content_sha(tgt, _CITE)
    assert a == b
    assert isinstance(a, str) and len(a) == 64  # a sha256 hex digest


def test_content_sha_depends_on_target() -> None:
    """A DIFFERENT target content_sha yields a different challenge sha — the sha
    binds WHAT is attacked.

    kills: a mutation that drops ``target`` from the canonical payload (so two
    challenges against different shas would collide and be interchangeable)."""
    base = challenges.challenge_content_sha(
        challenges.validate_target(_run_target("widget-run-1", "cmd-1")), _CITE
    )
    moved = challenges.challenge_content_sha(
        challenges.validate_target(_run_target("widget-run-1", "cmd-2")), _CITE
    )
    assert base != moved


def test_content_sha_depends_on_citations() -> None:
    """A DIFFERENT cited sha yields a different challenge sha — the sha binds WHAT
    the dissent rests on.

    kills: a mutation that drops ``citations`` from the canonical payload."""
    tgt = challenges.validate_target(_run_target("widget-run-1", "cmd-1"))
    base = challenges.challenge_content_sha(tgt, _CITE)
    other = challenges.challenge_content_sha(
        tgt, [{"kind": "run", "ref": "widget-run-1", "sha": "sha-widget-2"}]
    )
    assert base != other


# ── attestation-kind target dispatch keys on SCOPE, not subject_id ────────────


def test_attestation_target_existence_keys_on_scope_not_subject_id(tmp_path: Path) -> None:
    """For the ``attestation`` kind, existence SCANS the journal named by
    ``scope`` (scope_kind/scope_id), NOT by ``subject_id``. A target whose
    subject_id is UNRELATED to the journal still resolves when the scope names the
    journal that carries the sha.

    kills: a mutation that scans the attestation journal by ``subject_id`` instead
    of the scope address (the existing state suite uses subject_id == scope_id, so
    it cannot catch this swap)."""
    journal = tmp_path / ".hpc" / "notebooks" / "widget-nb.decisions.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps({"block": "sign-off", "resolved": {"content_sha": "sha-scoped"}}) + "\n",
        encoding="utf-8",
    )
    target = challenges.validate_target(
        {
            "kind": "attestation",
            "subject_kind": "widget-signoff",
            "subject_id": "TOTALLY-UNRELATED-subject",  # deliberately not the journal name
            "content_sha": "sha-scoped",
            "scope": {"scope_kind": "notebook", "scope_id": "widget-nb"},
        }
    )
    res = challenges.resolve_target_existence(tmp_path, target)
    assert res.resolved and res.matches


# ── reduce_challenge: resolution NEWEST-WINS + floors ─────────────────────────


def _filing(cid: str, *, ts: str, target: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": ts,
        "block": "challenge",
        "resolved": {
            "challenge_id": cid,
            "target": target,
            "citations": _CITE,
            "grounds": "widget grounds",
        },
    }


def _verdict(cid: str, *, ts: str, verdict: str) -> dict[str, Any]:
    return {
        "ts": ts,
        "block": "challenge-verdict",
        "resolved": {"challenge_id": cid, "verdict": verdict, "reasoning": "widget reasoning"},
    }


def _withdraw(cid: str, *, ts: str, reason: str = "widget withdrawn") -> dict[str, Any]:
    return {
        "ts": ts,
        "block": "challenge-withdraw",
        "resolved": {"challenge_id": cid, "reason": reason},
    }


def test_reduce_newest_resolution_wins_verdict_after_withdraw() -> None:
    """When a withdrawal is followed by a verdict (verdict is newest / last in
    append order), the VERDICT wins the headline.

    kills: a mutation that picks the FIRST resolution record instead of the newest
    (which would report ``withdrawn`` here)."""
    recs = [
        _filing("widget-c1", ts="2025-01-01T00:00:00Z", target=_run_target("r", "s")),
        _withdraw("widget-c1", ts="2025-02-01T00:00:00Z"),
        _verdict("widget-c1", ts="2025-03-01T00:00:00Z", verdict="upheld"),
    ]
    status = challenges.reduce_challenge(recs, challenge_id="widget-c1")
    assert status.status == "upheld"
    assert status.verdict == "upheld"


def test_reduce_newest_resolution_wins_withdraw_after_verdict() -> None:
    """The mirror: a verdict followed by a withdrawal (withdrawal newest) reduces to
    ``withdrawn``. Together with the sibling this pins the LAST-wins ordering in
    both directions."""
    recs = [
        _filing("widget-c1", ts="2025-01-01T00:00:00Z", target=_run_target("r", "s")),
        _verdict("widget-c1", ts="2025-02-01T00:00:00Z", verdict="upheld"),
        _withdraw("widget-c1", ts="2025-03-01T00:00:00Z", reason="mistaken"),
    ]
    status = challenges.reduce_challenge(recs, challenge_id="widget-c1")
    assert status.status == "withdrawn"
    assert status.withdrawn_reason == "mistaken"


def test_reduce_unknown_verdict_value_stays_open() -> None:
    """A verdict record carrying a value OUTSIDE {upheld, dismissed} does not set a
    headline — the status stays ``open`` and no verdict is disclosed.

    kills: a mutation that sets ``base_status = raw_verdict`` unconditionally
    (letting an arbitrary string become the challenge's headline status)."""
    recs = [
        _filing("widget-c1", ts="2025-01-01T00:00:00Z", target=_run_target("r", "s")),
        _verdict("widget-c1", ts="2025-02-01T00:00:00Z", verdict="maybe"),
    ]
    status = challenges.reduce_challenge(recs, challenge_id="widget-c1")
    assert status.status == "open"
    assert status.verdict is None


def test_reduce_superseded_wins_over_withdrawn_headline() -> None:
    """A withdrawn challenge whose target subject MOVED (superseded computed True)
    reports ``superseded`` as the headline while the withdrawal stays disclosed
    beneath it.

    kills: a mutation that lets ``base_status`` (withdrawn) win over the computed
    ``superseded`` headline (the existing suite only covers superseded-over-verdict
    and superseded-over-open)."""
    recs = [
        _filing("widget-c1", ts="2025-01-01T00:00:00Z", target=_run_target("r", "s")),
        _withdraw("widget-c1", ts="2025-02-01T00:00:00Z", reason="mistaken"),
    ]
    status = challenges.reduce_challenge(recs, challenge_id="widget-c1", superseded=True)
    assert status.status == "superseded"  # headline
    assert status.withdrawn_reason == "mistaken"  # still disclosed beneath


def test_reduce_ignores_foreign_challenge_id_records() -> None:
    """Resolution records for a DIFFERENT challenge_id interleaved in the list are
    ignored — a stray verdict for another challenge must not resolve this one.

    kills: dropping the ``resolved.challenge_id != challenge_id`` filter in the
    reduce loop (which would let a foreign verdict hijack the headline)."""
    recs = [
        _filing("widget-c1", ts="2025-01-01T00:00:00Z", target=_run_target("r", "s")),
        _verdict("widget-OTHER", ts="2025-02-01T00:00:00Z", verdict="dismissed"),
    ]
    status = challenges.reduce_challenge(recs, challenge_id="widget-c1")
    assert status.status == "open"  # the foreign dismissal did not touch this id


def test_reduce_newest_filing_target_wins() -> None:
    """With two filings for one id, the NEWEST (last in append order) supplies the
    headline's target + filed_at.

    kills: a mutation that reads ``filing_records[0]`` instead of ``[-1]``."""
    recs = [
        _filing("widget-c1", ts="2025-01-01T00:00:00Z", target=_run_target("r", "old")),
        _filing("widget-c1", ts="2025-05-01T00:00:00Z", target=_run_target("r", "new")),
    ]
    status = challenges.reduce_challenge(recs, challenge_id="widget-c1")
    assert status.filed_at == "2025-05-01T00:00:00Z"
    assert status.target is not None and status.target["content_sha"] == "new"


# ── validate_verdict_resolved: the VERDICTS membership boundary ───────────────


@pytest.mark.parametrize("bad", ["UPHELD", "upheld ", " dismissed", "Dismissed", "uphold"])
def test_verdict_membership_is_exact_and_case_sensitive(bad: str) -> None:
    """The verdict must be EXACTLY ``upheld`` or ``dismissed`` — no case-folding,
    no whitespace tolerance.

    kills: a mutation that ``.strip()``/``.lower()``s the verdict before the
    membership test (which would silently accept ``UPHELD`` / ``dismissed ``)."""
    with pytest.raises(errors.SpecInvalid, match="verdict"):
        challenges.validate_verdict_resolved(
            {"challenge_id": "widget-c1", "verdict": bad, "reasoning": "r"}
        )


def test_verdict_membership_accepts_the_two_exact_values() -> None:
    """The companion: the two exact members pass (the boundary is a gate, not an
    always-refuse)."""
    for good in ("upheld", "dismissed"):
        parsed = challenges.validate_verdict_resolved(
            {"challenge_id": "widget-c1", "verdict": good, "reasoning": "r"}
        )
        assert parsed.verdict == good


# ── the gate: a dismissal with no filing on record is refused ─────────────────

_TGT_RUN = "widget-run-tgt"
_TGT_SHA = "a3f2c9d1beef0011223344556677"


def _seed_run(experiment_dir: Path) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=_TGT_RUN,
        cmd_sha=_TGT_SHA,
        hpc_agent_version="0.0.0-test",
        submitted_at="2025-11-14T00:00:00Z",
        executor="widget_executor.py",
        result_dir_template="results/{run_id}",
        task_count=1,
        tasks_py_sha="tasks-sha-aaa",
    )


def test_dismissal_without_a_filing_refused(tmp_path: Path) -> None:
    """A ``challenge-verdict`` DISMISSAL for a challenge_id that was never FILED is
    refused loudly — a verdict resolves a challenge that exists.

    kills: dropping the ``if not citations`` guard in the dismissal branch (which
    would let a dismissal be recorded against a phantom challenge)."""
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "challenge",
            "scope_id": "widget-phantom",
            "block": "challenge-verdict",
            "response": "dismiss widget-phantom — unconvinced",
            "resolved": {
                "challenge_id": "widget-phantom",
                "verdict": "dismissed",
                "reasoning": "the objection does not hold",
            },
        }
    )
    with pytest.raises(errors.SpecInvalid, match="no parseable filing"):
        append_decision(experiment_dir=tmp_path, spec=spec)


# ── the gate helpers: filing citations + attestor NEWEST-WINS ─────────────────


def _seed_filing_record(
    experiment_dir: Path,
    cid: str,
    *,
    ts: str,
    cite_sha: str,
    attestor_id: str | None,
) -> None:
    """Append one raw, VALID challenge filing record to the id's journal thread."""
    path = decisions_path(experiment_dir, "challenge", cid)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {
        "ts": ts,
        "block": "challenge",
        "scope_kind": "challenge",
        "scope_id": cid,
        "response": "y",
        "resolved": {
            "challenge_id": cid,
            "target": _run_target(_TGT_RUN, _TGT_SHA),
            "citations": [{"kind": "run", "ref": "widget-run-1", "sha": cite_sha}],
            "grounds": "widget grounds",
        },
    }
    if attestor_id is not None:
        rec["attestor_id"] = attestor_id
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def test_filing_citations_from_newest_filing(tmp_path: Path) -> None:
    """``_challenge_filing_citations`` returns the NEWEST filing's citations (a
    re-filing supersedes its predecessor's evidence set).

    kills: a mutation that returns the FIRST filing's citations."""
    _seed_filing_record(
        tmp_path, "widget-c1", ts="2025-01-01T00:00:00Z", cite_sha="old", attestor_id=None
    )
    _seed_filing_record(
        tmp_path, "widget-c1", ts="2025-05-01T00:00:00Z", cite_sha="new", attestor_id=None
    )
    citations = _challenge_filing_citations(tmp_path, "widget-c1")
    assert [c.sha for c in citations] == ["new"]


def test_filing_citations_empty_without_a_filing(tmp_path: Path) -> None:
    """No filing → ``()`` (the caller turns this into the 'no parseable filing'
    refusal).

    kills: a mutation that returns a non-empty default when nothing was filed."""
    assert _challenge_filing_citations(tmp_path, "widget-none") == ()


def test_filing_attestor_newest_wins(tmp_path: Path) -> None:
    """``_challenge_filing_attestor`` reads the NEWEST filing's attestor_id — the
    MH7 resolver≠challenger comparison keys on WHO filed most recently.

    kills: a mutation that returns the first filing's attestor (the identity the
    suppression/self-adjudication guards would then compare against is wrong)."""
    _seed_filing_record(
        tmp_path, "widget-c1", ts="2025-01-01T00:00:00Z", cite_sha="s", attestor_id="alice"
    )
    _seed_filing_record(
        tmp_path, "widget-c1", ts="2025-05-01T00:00:00Z", cite_sha="s", attestor_id="bob"
    )
    assert _challenge_filing_attestor(tmp_path, "widget-c1") == "bob"


def test_filing_attestor_none_when_unattributed(tmp_path: Path) -> None:
    """A filing written WITHOUT an attestor_id (the unattributed / pre-actors shape)
    reads ``None`` — the RULING-4 anonymous-filing precondition.

    kills: a mutation that coerces a missing attestor_id into a non-None default."""
    _seed_filing_record(
        tmp_path, "widget-c1", ts="2025-01-01T00:00:00Z", cite_sha="s", attestor_id=None
    )
    assert _challenge_filing_attestor(tmp_path, "widget-c1") is None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
