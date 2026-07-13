"""T1 tests — ``state/challenges.py``: the challenge record + the ONE collector.

Toy WIDGET vocabulary only (never harxhar/quant — the domain-packs toy-fixture
rule). Crafted journals/sidecars exercise: every filing/verdict/withdraw refusal,
the target existence scan (present / absent / non-newest allowed), the reduction
paths (open / upheld / dismissed / withdrawn / superseded-computed /
superseded-with-verdict-disclosed), the collector's address filtering, the
all-zero omission, the non-creating pin, and the route-through pins (the ONE
kernel + the evidence resolver table).
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from hpc_agent import errors
from hpc_agent.state import challenges  # type: ignore[attr-defined]

# --- tiny toy-store writers (NON-CREATING globs read these back) -------------


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _write_sidecar(exp: Path, run_id: str, *, cmd_sha: str) -> None:
    p = exp / ".hpc" / "runs" / f"{run_id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"run_id": run_id, "cmd_sha": cmd_sha}), encoding="utf-8")


def _run_target(subject_id: str, content_sha: str) -> dict:
    """A ``run``-kind target address (subject_id == run_id, sha == cmd_sha)."""
    return {
        "kind": "run",
        "subject_kind": "widget-run",
        "subject_id": subject_id,
        "content_sha": content_sha,
        "scope": {"scope_kind": "run", "scope_id": subject_id},
    }


def _attestation_target(scope_id: str, content_sha: str) -> dict:
    """An ``attestation``-kind target addressing a widget notebook journal."""
    return {
        "kind": "attestation",
        "subject_kind": "widget-signoff",
        "subject_id": scope_id,
        "content_sha": content_sha,
        "scope": {"scope_kind": "notebook", "scope_id": scope_id},
    }


_WIDGET_CITE = [{"kind": "run", "ref": "widget-run-1", "sha": "sha-widget-1"}]


def _challenge_record(cid: str, *, ts: str, target: dict, grounds: str = "widget grounds") -> dict:
    return {
        "ts": ts,
        "scope_kind": "challenge",
        "scope_id": cid,
        "block": "challenge",
        "response": "y",
        "resolved": {
            "challenge_id": cid,
            "target": target,
            "citations": _WIDGET_CITE,
            "grounds": grounds,
        },
    }


def _verdict_record(
    cid: str, *, ts: str, verdict: str, reasoning: str = "widget reasoning"
) -> dict:
    return {
        "ts": ts,
        "scope_kind": "challenge",
        "scope_id": cid,
        "block": "challenge-verdict",
        "response": "y",
        "resolved": {"challenge_id": cid, "verdict": verdict, "reasoning": reasoning},
    }


def _withdraw_record(cid: str, *, ts: str, reason: str = "widget withdrawn") -> dict:
    return {
        "ts": ts,
        "scope_kind": "challenge",
        "scope_id": cid,
        "block": "challenge-withdraw",
        "response": "y",
        "resolved": {"challenge_id": cid, "reason": reason},
    }


def _write_challenge(exp: Path, cid: str, record: dict) -> None:
    _append_jsonl(exp / ".hpc" / "challenges" / f"{cid}.decisions.jsonl", record)


# --- vocabulary equality pins ------------------------------------------------


def test_block_family_and_status_pins() -> None:
    assert challenges.CHALLENGE_BLOCK == "challenge"
    assert challenges.CHALLENGE_VERDICT_BLOCK == "challenge-verdict"
    assert challenges.CHALLENGE_WITHDRAW_BLOCK == "challenge-withdraw"
    assert (
        frozenset({"challenge", "challenge-verdict", "challenge-withdraw"})
        == challenges.CHALLENGE_BLOCK_FAMILY
    )
    assert challenges.SUBJECT_KIND == "challenge"
    assert frozenset({"upheld", "dismissed"}) == challenges.VERDICTS
    assert (
        frozenset({"open", "upheld", "dismissed", "withdrawn", "superseded"}) == challenges.STATUSES
    )


def test_contested_never_imports_a_status_vocabulary() -> None:
    # C-status: `contested` is orthogonal — the module must not fold registration
    # statuses into a merged vocabulary. It never imports the registration module.
    src = inspect.getsource(challenges)
    assert "from hpc_agent.state.registration import" not in src
    assert "import registration" not in src


# --- target validation refusals ----------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        {**_run_target("r", "s"), "kind": "widget"},  # unknown kind
        {**_run_target("r", "s"), "subject_kind": ""},  # empty subject_kind
        {**_run_target("r", "s"), "subject_id": ""},  # empty subject_id
        {**_run_target("r", "s"), "content_sha": ""},  # empty content_sha
        {k: v for k, v in _run_target("r", "s").items() if k != "scope"},  # no scope
        {**_run_target("r", "s"), "scope": {"scope_kind": "run"}},  # scope missing id
    ],
)
def test_validate_target_refuses(target: dict) -> None:
    with pytest.raises(errors.SpecInvalid):
        challenges.validate_target(target)


# --- filing validation refusals + allowances ---------------------------------


def test_challenge_resolved_refuses_bad_challenge_id() -> None:
    with pytest.raises(errors.SpecInvalid, match="challenge_id"):
        challenges.validate_challenge_resolved(
            {
                "challenge_id": "bad/id",
                "target": _run_target("r", "s"),
                "citations": _WIDGET_CITE,
                "grounds": "g",
            }
        )


def test_challenge_resolved_refuses_empty_citations() -> None:
    with pytest.raises(errors.SpecInvalid, match="NON-EMPTY"):
        challenges.validate_challenge_resolved(
            {
                "challenge_id": "widget-c1",
                "target": _run_target("r", "s"),
                "citations": [],
                "grounds": "g",
            }
        )


def test_challenge_resolved_refuses_empty_grounds() -> None:
    with pytest.raises(errors.SpecInvalid, match="grounds"):
        challenges.validate_challenge_resolved(
            {
                "challenge_id": "widget-c1",
                "target": _run_target("r", "s"),
                "citations": _WIDGET_CITE,
                "grounds": "",
            }
        )


def test_challenge_resolved_refuses_malformed_target() -> None:
    with pytest.raises(errors.SpecInvalid, match="target"):
        challenges.validate_challenge_resolved(
            {
                "challenge_id": "widget-c1",
                "target": "not-a-mapping",
                "citations": _WIDGET_CITE,
                "grounds": "g",
            }
        )


def test_challenge_resolved_computes_content_sha() -> None:
    parsed = challenges.validate_challenge_resolved(
        {
            "challenge_id": "widget-c1",
            "target": _run_target("widget-run-1", "cmd-1"),
            "citations": _WIDGET_CITE,
            "grounds": "the widget batch leaks",
        }
    )
    assert parsed.content_sha  # a canonical sha over {target, citations}
    # the helper agrees with the validator
    assert parsed.content_sha == challenges.challenge_content_sha(parsed.target, parsed.citations)


def test_verdict_resolved_refuses_unknown_verdict_and_empty_reasoning() -> None:
    with pytest.raises(errors.SpecInvalid, match="verdict"):
        challenges.validate_verdict_resolved(
            {"challenge_id": "widget-c1", "verdict": "maybe", "reasoning": "r"}
        )
    with pytest.raises(errors.SpecInvalid, match="reasoning"):
        challenges.validate_verdict_resolved(
            {"challenge_id": "widget-c1", "verdict": "upheld", "reasoning": ""}
        )


def test_withdraw_resolved_refuses_empty_reason() -> None:
    with pytest.raises(errors.SpecInvalid, match="reason"):
        challenges.validate_withdraw_resolved({"challenge_id": "widget-c1", "reason": ""})


# --- target existence scan (filing) ------------------------------------------


def test_target_existence_run_present(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "widget-run-1", cmd_sha="cmd-1")
    target = challenges.validate_target(_run_target("widget-run-1", "cmd-1"))
    res = challenges.resolve_target_existence(tmp_path, target)
    assert res.resolved and res.matches


def test_target_existence_run_absent(tmp_path: Path) -> None:
    target = challenges.validate_target(_run_target("widget-run-1", "cmd-1"))
    res = challenges.resolve_target_existence(tmp_path, target)
    # unresolvable → the caller (T5) refuses filing; the R3 rejection working.
    assert not res.matches


def test_target_existence_attestation_scans_committed_records(tmp_path: Path) -> None:
    # A widget notebook journal with TWO sign-off records (older + newest).
    p = tmp_path / ".hpc" / "notebooks" / "widget-nb.decisions.jsonl"
    _append_jsonl(p, {"block": "sign-off", "resolved": {"content_sha": "sha-old"}})
    _append_jsonl(p, {"block": "sign-off", "resolved": {"content_sha": "sha-new"}})
    # Newest is findable.
    res_new = challenges.resolve_target_existence(
        tmp_path, challenges.validate_target(_attestation_target("widget-nb", "sha-new"))
    )
    assert res_new.resolved and res_new.matches
    # NON-NEWEST is ALSO findable (challenging a superseded record is permitted).
    res_old = challenges.resolve_target_existence(
        tmp_path, challenges.validate_target(_attestation_target("widget-nb", "sha-old"))
    )
    assert res_old.resolved and res_old.matches
    # An absent sha → the existence check reports no committed record.
    res_absent = challenges.resolve_target_existence(
        tmp_path, challenges.validate_target(_attestation_target("widget-nb", "sha-ghost"))
    )
    assert res_absent.resolved and not res_absent.matches


def test_target_existence_attestation_empty_journal(tmp_path: Path) -> None:
    res = challenges.resolve_target_existence(
        tmp_path, challenges.validate_target(_attestation_target("widget-nb", "sha-x"))
    )
    assert not res.resolved and not res.matches


# --- the reduction paths -----------------------------------------------------


def _open_recs(cid: str, target: dict) -> list[dict]:
    return [_challenge_record(cid, ts="2025-01-01T00:00:00Z", target=target)]


def test_reduce_open() -> None:
    recs = _open_recs("widget-c1", _run_target("widget-run-1", "cmd-1"))
    status = challenges.reduce_challenge(recs, challenge_id="widget-c1")
    assert status.status == "open"
    assert status.filed_at == "2025-01-01T00:00:00Z"
    assert status.content_sha  # recomputed from the filing


def test_reduce_upheld() -> None:
    recs = _open_recs("widget-c1", _run_target("widget-run-1", "cmd-1"))
    recs.append(_verdict_record("widget-c1", ts="2025-02-01T00:00:00Z", verdict="upheld"))
    status = challenges.reduce_challenge(recs, challenge_id="widget-c1")
    assert status.status == "upheld"
    assert status.verdict == "upheld"
    assert status.reasoning == "widget reasoning"
    assert status.resolved_at == "2025-02-01T00:00:00Z"


def test_reduce_dismissed() -> None:
    recs = _open_recs("widget-c1", _run_target("widget-run-1", "cmd-1"))
    recs.append(_verdict_record("widget-c1", ts="2025-02-01T00:00:00Z", verdict="dismissed"))
    status = challenges.reduce_challenge(recs, challenge_id="widget-c1")
    assert status.status == "dismissed"
    assert status.verdict == "dismissed"


def test_reduce_withdrawn() -> None:
    recs = _open_recs("widget-c1", _run_target("widget-run-1", "cmd-1"))
    recs.append(_withdraw_record("widget-c1", ts="2025-02-01T00:00:00Z", reason="mistaken"))
    status = challenges.reduce_challenge(recs, challenge_id="widget-c1")
    assert status.status == "withdrawn"
    assert status.withdrawn_reason == "mistaken"


def test_reduce_superseded_computed_wins_headline() -> None:
    recs = _open_recs("widget-c1", _run_target("widget-run-1", "cmd-1"))
    status = challenges.reduce_challenge(recs, challenge_id="widget-c1", superseded=True)
    assert status.status == "superseded"
    assert status.superseded is True
    assert status.verdict is None  # still open underneath, no verdict disclosed


def test_reduce_superseded_discloses_underlying_verdict() -> None:
    recs = _open_recs("widget-c1", _run_target("widget-run-1", "cmd-1"))
    recs.append(_verdict_record("widget-c1", ts="2025-02-01T00:00:00Z", verdict="upheld"))
    status = challenges.reduce_challenge(recs, challenge_id="widget-c1", superseded=True)
    assert status.status == "superseded"  # headline
    assert status.verdict == "upheld"  # disclosed beneath the headline (C-reduce)


# --- the ONE collector: address filtering + omission + non-creating ----------


def test_valid_then_invalid_filing_keeps_the_challenge_standing(tmp_path: Path) -> None:
    # #71 regression: a LATER unvalidatable filing record (hand-appended, or a
    # kind that fell out of the closed set) must not erase the earlier VALID
    # filing's target and silently drop the whole challenge from the standing
    # view — the last VALID filing stands.
    _write_sidecar(tmp_path, "widget-run-1", cmd_sha="cmd-1")
    _write_challenge(
        tmp_path,
        "widget-c1",
        _challenge_record(
            "widget-c1", ts="2025-01-01T00:00:00Z", target=_run_target("widget-run-1", "cmd-1")
        ),
    )
    bad = _challenge_record(
        "widget-c1", ts="2025-01-02T00:00:00Z", target=_run_target("widget-run-1", "cmd-1")
    )
    del bad["resolved"]["citations"]  # unvalidatable: fails validate_challenge_resolved
    _write_challenge(tmp_path, "widget-c1", bad)

    out = challenges.standing_challenges(tmp_path)
    assert {s.challenge_id for s in out.statuses} == {"widget-c1"}


def test_only_invalid_filings_disclosed_as_skipped_not_dropped(tmp_path: Path) -> None:
    # #71 sibling: when filings exist but NONE validate, the challenge lands in
    # skipped (disclosure), never a silent continue (B10).
    _write_sidecar(tmp_path, "widget-run-1", cmd_sha="cmd-1")
    bad = _challenge_record(
        "widget-c9", ts="2025-01-01T00:00:00Z", target=_run_target("widget-run-1", "cmd-1")
    )
    del bad["resolved"]["citations"]
    _write_challenge(tmp_path, "widget-c9", bad)

    out = challenges.standing_challenges(tmp_path)
    assert out.statuses == ()
    assert any(s.challenge_id == "widget-c9" and "none validate" in s.reason for s in out.skipped)


def test_standing_challenges_filters_by_content_sha(tmp_path: Path) -> None:
    # Two challenges against two different run cmd_shas; sidecars present + current
    # so neither is superseded.
    _write_sidecar(tmp_path, "widget-run-1", cmd_sha="cmd-1")
    _write_sidecar(tmp_path, "widget-run-2", cmd_sha="cmd-2")
    _write_challenge(
        tmp_path,
        "widget-c1",
        _challenge_record(
            "widget-c1", ts="2025-01-01T00:00:00Z", target=_run_target("widget-run-1", "cmd-1")
        ),
    )
    _write_challenge(
        tmp_path,
        "widget-c2",
        _challenge_record(
            "widget-c2", ts="2025-01-02T00:00:00Z", target=_run_target("widget-run-2", "cmd-2")
        ),
    )
    # unfiltered → both
    both = challenges.standing_challenges(tmp_path)
    assert {s.challenge_id for s in both.statuses} == {"widget-c1", "widget-c2"}
    assert both.contested is not None
    assert both.contested.open == 2
    assert both.contested.challenge_ids == ("widget-c1", "widget-c2")
    # by content_sha → exactly one
    one = challenges.standing_challenges(tmp_path, content_sha="cmd-1")
    assert [s.challenge_id for s in one.statuses] == ["widget-c1"]
    assert one.statuses[0].status == "open"
    # by subject_id narrows too
    by_subj = challenges.standing_challenges(tmp_path, subject_id="widget-run-2")
    assert [s.challenge_id for s in by_subj.statuses] == ["widget-c2"]
    # by subject_kind
    by_kind = challenges.standing_challenges(tmp_path, subject_kind="widget-run")
    assert len(by_kind.statuses) == 2


def test_standing_challenges_all_zero_omits_contested(tmp_path: Path) -> None:
    # A fresh namespace matches nothing → contested is None (the omission).
    out = challenges.standing_challenges(tmp_path, content_sha="nope")
    assert out.statuses == ()
    assert out.contested is None


def test_standing_challenges_computes_superseded_live(tmp_path: Path) -> None:
    # The run's cmd_sha is now cmd-NEW; the challenge named the OLD cmd-1 → the
    # newest-wins re-resolution finds no match → superseded, computed live.
    _write_sidecar(tmp_path, "widget-run-1", cmd_sha="cmd-NEW")
    _write_challenge(
        tmp_path,
        "widget-c1",
        _challenge_record(
            "widget-c1", ts="2025-01-01T00:00:00Z", target=_run_target("widget-run-1", "cmd-1")
        ),
    )
    out = challenges.standing_challenges(tmp_path, subject_id="widget-run-1")
    assert len(out.statuses) == 1
    assert out.statuses[0].status == "superseded"
    assert out.contested is not None and out.contested.superseded == 1


def test_standing_challenges_non_creating(tmp_path: Path) -> None:
    exp = tmp_path / "fresh"
    exp.mkdir()
    out = challenges.standing_challenges(exp)
    assert out.statuses == ()
    # No directory was created under the fresh namespace (the non-creating pin).
    assert not (exp / ".hpc").exists()


def test_standing_challenges_tolerates_corrupt_lines(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "widget-run-1", cmd_sha="cmd-1")
    p = tmp_path / ".hpc" / "challenges" / "widget-c1.decisions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not json\n", encoding="utf-8")
    _write_challenge(
        tmp_path,
        "widget-c1",
        _challenge_record(
            "widget-c1", ts="2025-01-01T00:00:00Z", target=_run_target("widget-run-1", "cmd-1")
        ),
    )
    out = challenges.standing_challenges(tmp_path)
    assert len(out.statuses) == 1
    assert out.skipped and out.skipped[0].reason.endswith("corrupt line(s)")


# --- route-through pins (the ONE kernel + the evidence resolver table) --------


def test_reduce_routes_through_the_one_kernel() -> None:
    src = inspect.getsource(challenges.reduce_challenge)
    assert "attestation.reduce" in src  # never a re-inlined newest-first/sha-compare


def test_target_resolution_dispatches_to_the_evidence_resolver_table() -> None:
    existence = inspect.getsource(challenges.resolve_target_existence)
    current = inspect.getsource(challenges.resolve_target_current)
    assert "resolve_citation" in existence  # run/fingerprint/dossier route through evidence
    assert "resolve_citation" in current  # newest-wins re-resolution routes through evidence
    # and no second resolver table is grown here
    assert "_STATE_RESOLVERS" not in inspect.getsource(challenges)
