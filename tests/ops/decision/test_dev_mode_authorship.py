"""Dev-mode authorship — cross-repo utterance-log trust, opt-in per repo.

The enforcement pins for ``docs/design/dev-mode-authorship.md`` legs (b)–(d)
(leg (a) — the strict per-repo default — landed in ``efb980d6`` and is pinned
by ``test_authorship_scalar_adjacency.py``):

* **(b) the opt-in is a journaled record in the SECOND repo** —
  ``scope_kind="scope"``, ``scope_id="authorship-home"``, the code-owned block
  ``authorship-home``, ``resolved.action ∈ {"grant", "revoke"}``, newest-wins
  (the scope-lock state machine verbatim). A grant must be BOOTSTRAPPED from
  the home log: the human's naming utterance (the 12-hex ``home_repo_hash`` as
  a whole token) must exist in the HOME namespace — the one place the model
  has no write path; a journal ``response`` carries no weight.
* **(c) every acceptance stamps WHICH log satisfied it** — the code-owned
  ``provenance["human_authorship"]`` disclosure gains ``evidence_logs`` (every
  namespace consulted) and a per-field ``source_log ∈ {own, home, own+home}``.
* **(d) revocation is forward-only** — newest-wins on the next append; prior
  accepted records are grandfathered, never retro-invalidated.

The 11 named pins from the design's enforcement map live here; the 12th (the
route-through contract extension) lives in
``tests/contracts/test_utterance_route_through.py``.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput, AppendDecisionResult
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.state.run_record import repo_hash

if TYPE_CHECKING:
    from pathlib import Path


def _append(experiment_dir: Path, **overrides: object) -> AppendDecisionResult:
    base: dict[str, object] = {
        "scope_kind": "run",
        "scope_id": "run-1",
        "block": "s1",
        "response": "y",
    }
    base.update(overrides)
    return append_decision(
        experiment_dir=experiment_dir, spec=AppendDecisionInput.model_validate(base)
    )


def _repos(tmp_path: Path, tag: str = "") -> tuple[Path, Path]:
    """A (second, home) repo pair sharing this test's isolated journal home."""
    second = tmp_path / f"repo_b{tag}"
    home = tmp_path / f"repo_h{tag}"
    second.mkdir()
    home.mkdir()
    return second, home


def _utter(repo: Path, text: str, actor: str | None = None) -> None:
    """Simulate the harness-side UserPromptSubmit capture for *repo*."""
    from hpc_agent.state.run_record import journal_dir
    from hpc_agent.state.utterances import append_utterance

    journal_dir(repo)  # the namespace a real state write would have created
    assert append_utterance(repo, text, actor=actor) is not None


def _naming_utterance(home: Path) -> str:
    """The bootstrap utterance the design's remedy text prescribes."""
    return f"trust this repo's utterance log {repo_hash(home)}"


def _grant(second: Path, home: Path, **resolved_overrides: object) -> AppendDecisionResult:
    """The leg-(b) opt-in record: a journaled grant in the SECOND repo."""
    resolved: dict[str, object] = {
        "action": "grant",
        "home_experiment_dir": str(home),
        "home_repo_hash": repo_hash(home),
    }
    resolved.update(resolved_overrides)
    return _append(
        second,
        scope_kind="scope",
        scope_id="authorship-home",
        block="authorship-home",
        response="grant home-log trust",
        resolved=resolved,
    )


def _sweep(n_samples: int = 1_000_000, seeds: int = 20) -> dict[str, Any]:
    return {
        "kind": "items_x_seeds",
        "params": {"items": [{"n_samples": n_samples}], "seeds": list(range(seeds))},
    }


def _write_grant_line(
    second: Path, home: Path, *, home_repo_hash: str | None = None, action: str = "grant"
) -> None:
    """Hand-write an authorship-home record, BYPASSING the append gate.

    The drifted-record case (a hand-edited / cross-machine-copied journal): the
    match-time revalidation exists precisely for records the bootstrap gate
    never vetted, so the test authors one directly.
    """
    from hpc_agent.state.decision_journal import decisions_path

    path = decisions_path(second, "scope", "authorship-home")
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "schema_version": 1,
        "ts": "2026-07-20T00:00:00Z",
        "scope_kind": "scope",
        "scope_id": "authorship-home",
        "block": "authorship-home",
        "evidence_digest": "",
        "proposal": "",
        "response": "grant home-log trust",
        "resolved": {
            "action": action,
            "home_experiment_dir": str(home),
            "home_repo_hash": repo_hash(home) if home_repo_hash is None else home_repo_hash,
        },
        "provenance": {},
    }
    with path.open("a", encoding="utf-8", newline="") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _read_journal(experiment_dir: Path, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    from hpc_agent.state.decision_journal import decisions_path

    path = decisions_path(experiment_dir, scope_kind, scope_id)
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


# ── leg (b): the grant enables home-log derivation ───────────────────────────


def test_grant_record_enables_home_log_derivation(tmp_path: Path) -> None:
    """A second-repo commit whose tokens derive ONLY from the home log is
    ACCEPTED under a valid grant — the whole point of the opt-in."""
    second, home = _repos(tmp_path)
    _utter(second, "unrelated chat about the weather")  # own log exists, no sweep
    _utter(home, "20 seeds at 1M samples")
    _utter(home, _naming_utterance(home))
    _grant(second, home)

    out = _append(second, resolved={"task_generator": _sweep()})
    stamp = out.record.provenance["human_authorship"]
    assert stamp["evidence_source"] == "harness_captured"
    assert stamp["evidence_logs"] == [repo_hash(second), repo_hash(home)]
    assert stamp["fields"]["task_generator"]["source_log"] == "home"


def test_no_grant_home_only_tokens_refused(tmp_path: Path) -> None:
    """The strict default (leg a) held: WITHOUT a grant the home log is never
    consulted — home-only tokens refuse, byte-identical to pre-ruling (the
    refusal names the own namespace and never mentions the home's)."""
    second, home = _repos(tmp_path)
    _utter(second, "unrelated chat about the weather")
    _utter(home, "20 seeds at 1M samples")
    _utter(home, _naming_utterance(home))  # present but irrelevant: no grant

    with pytest.raises(errors.SpecInvalid) as ei:
        _append(second, resolved={"task_generator": _sweep()})
    msg = str(ei.value)
    assert "task_generator is human-authored" in msg
    assert f"repo namespace {repo_hash(second)}" in msg
    assert repo_hash(home) not in msg  # the home namespace was never consulted
    # No consultation → the opt-in journal is never scaffolded by a READ.
    assert not (second / ".hpc" / "scopes" / "authorship-home.decisions.jsonl").exists()


# ── leg (b): the bootstrap rule ───────────────────────────────────────────────


def test_grant_requires_home_log_naming_utterance(tmp_path: Path) -> None:
    """A grant is refused when the naming utterance is in the SECOND repo's log
    (presence in the home is what attests), when the home log carries only a
    bare ack, or when no naming utterance exists at all — and accepted when the
    HOME log names the hash as a whole token."""
    # (a) the naming utterance lives in the SECOND repo's log only.
    second, home = _repos(tmp_path, "a")
    _utter(second, _naming_utterance(home))
    _utter(home, "20 seeds at 1M samples")
    with pytest.raises(errors.SpecInvalid, match="authorship-home grant gate"):
        _grant(second, home)

    # (b) the home log carries only a bare ack.
    second, home = _repos(tmp_path, "b")
    _utter(home, "y")
    with pytest.raises(errors.SpecInvalid, match="authorship-home grant gate"):
        _grant(second, home)

    # (c) no naming utterance anywhere (home namespace exists — structural legs pass).
    second, home = _repos(tmp_path, "c")
    _utter(home, "20 seeds at 1M samples")
    with pytest.raises(errors.SpecInvalid, match="authorship-home grant gate"):
        _grant(second, home)

    # The positive: the HOME log names the hash → the grant lands.
    second, home = _repos(tmp_path, "d")
    _utter(home, _naming_utterance(home))
    out = _grant(second, home)
    assert out.record.resolved["action"] == "grant"
    assert out.record.resolved["home_repo_hash"] == repo_hash(home)


def test_agent_relayed_grant_without_home_utterance_refused(tmp_path: Path) -> None:
    """The journal ``response`` carries NO weight for a grant: an agent-relayed
    'the human says trust <hash>' is self-minted trust (harness tier only —
    the overnight-consent bound ruling's tightening)."""
    second, home = _repos(tmp_path)
    _utter(home, "20 seeds at 1M samples")  # home log exists, no naming utterance
    with pytest.raises(errors.SpecInvalid, match="authorship-home grant gate"):
        _append(
            second,
            scope_kind="scope",
            scope_id="authorship-home",
            block="authorship-home",
            response=f"the human says trust this repo's utterance log {repo_hash(home)}",
            resolved={
                "action": "grant",
                "home_experiment_dir": str(home),
                "home_repo_hash": repo_hash(home),
            },
        )


def test_grant_refusals_carry_no_authorship_marker(tmp_path: Path) -> None:
    """Every grant refusal is a PLAIN SpecInvalid — no E2
    ``authorship_evidence: missing`` marker: the marker drives the MCP
    elicitation retry, and a re-elicited utterance lands in the CURRENT
    session's namespace (the second repo) — the wrong log, a guaranteed-failing
    round-trip. The value-gate refusal keeps its marker (the contrast pin)."""
    second, home = _repos(tmp_path)
    _utter(home, "20 seeds at 1M samples")

    # Structural leg: a hash that does not recompute from the home path.
    with pytest.raises(errors.SpecInvalid) as ei_structural:
        _grant(second, home, home_repo_hash="0" * 12)
    assert getattr(ei_structural.value, "failure_features", None) is None

    # Bootstrap leg: no naming utterance in the home log.
    with pytest.raises(errors.SpecInvalid) as ei_bootstrap:
        _grant(second, home)
    assert getattr(ei_bootstrap.value, "failure_features", None) is None

    # Contrast: the value-derivation refusal STILL arms the elicitation retry.
    _utter(home, _naming_utterance(home))
    _grant(second, home)
    with pytest.raises(errors.SpecInvalid) as ei_value:
        _append(second, resolved={"task_generator": _sweep(n_samples=999_999)})
    assert ei_value.value.failure_features == {"authorship_evidence": "missing"}  # type: ignore[attr-defined]


# ── leg (b): match-time revalidation ─────────────────────────────────────────


def test_grant_hash_mismatch_is_dangling_not_trusted(tmp_path: Path) -> None:
    """A grant whose recorded ``home_repo_hash`` does not recompute from the
    recorded path (home moved/renamed, a hand-edited record) is DANGLING —
    own-only, disclosed, never trusted-blind, never an exception."""
    second, home = _repos(tmp_path)
    _utter(second, "calibration chatter")
    _utter(home, "20 seeds at 1M samples")
    _write_grant_line(second, home, home_repo_hash="0" * 12)  # drifted record

    with pytest.raises(errors.SpecInvalid) as ei:
        _append(second, resolved={"task_generator": _sweep()})
    msg = str(ei.value)
    assert "dangling" in msg
    assert "does not recompute" in msg
    assert f"repo namespace {repo_hash(second)}" in msg


def test_missing_home_namespace_degrades_to_own_only_disclosed(tmp_path: Path) -> None:
    """A validly-granted home whose namespace later disappears degrades to
    own-only, DISCLOSED: an own-derived accept still passes (stamped with the
    dangling disclosure), and a home-only value refuses with the disclosure in
    the message. Nothing throws."""
    second, home = _repos(tmp_path)
    _utter(second, "20 seeds at 1M samples")  # own-sufficient sweep
    _utter(home, _naming_utterance(home))
    _grant(second, home)

    # The home namespace disappears AFTER the grant (home uninstalled/moved).
    from hpc_agent.state.run_record import journal_root_if_exists

    shutil.rmtree(journal_root_if_exists(home))

    out = _append(second, resolved={"task_generator": _sweep()})
    stamp = out.record.provenance["human_authorship"]
    assert stamp["evidence_logs"] == [repo_hash(second)]  # own-only consultation
    assert stamp["dangling_home"] == repo_hash(home)  # ... disclosed, never silent
    assert stamp["fields"]["task_generator"]["source_log"] == "own"

    with pytest.raises(errors.SpecInvalid) as ei:
        _append(second, scope_id="run-2", resolved={"task_generator": _sweep(n_samples=999_999)})
    assert "dangling" in str(ei.value)


# ── leg (d): revocation newest-wins, forward-only ────────────────────────────


def test_revoke_mid_session_next_append_refuses_and_prior_commit_stands(tmp_path: Path) -> None:
    """Newest-wins: after a revoke the very next gated append re-reads the
    scope journal (no cache), consults own-only, and a home-only value
    refuses — with the state change disclosed. The PRE-revocation accepted
    record STANDS: its ``source_log: "home"`` stamp reads back intact
    (grandfathered — revocation changes future evaluation, never rewrites a
    past record)."""
    second, home = _repos(tmp_path)
    _utter(second, "calibration chatter")
    _utter(home, "20 seeds at 1M samples")
    _utter(home, _naming_utterance(home))
    _grant(second, home)

    accepted = _append(second, resolved={"task_generator": _sweep()})
    assert (
        accepted.record.provenance["human_authorship"]["fields"]["task_generator"]["source_log"]
        == "home"
    )

    # Revocation: the safe direction — a journaled record, no authorship bar.
    _append(
        second,
        scope_kind="scope",
        scope_id="authorship-home",
        block="authorship-home",
        response="revoke the home-log trust",
        resolved={"action": "revoke", "home_repo_hash": repo_hash(home)},
    )

    with pytest.raises(errors.SpecInvalid) as ei:
        _append(second, scope_id="run-2", resolved={"task_generator": _sweep()})
    msg = str(ei.value)
    assert "home-log trust revoked" in msg
    assert repo_hash(home) in msg

    # Grandfathering: the pre-revocation record's stamp is the audit trail of
    # why it was allowed — it is NOT retro-invalidated.
    prior = _read_journal(second, "run", "run-1")
    assert (
        prior[-1]["provenance"]["human_authorship"]["fields"]["task_generator"]["source_log"]
        == "home"
    )
    assert prior[-1]["provenance"]["human_authorship"]["evidence_logs"] == [
        repo_hash(second),
        repo_hash(home),
    ]


# ── leg (c): the provenance stamp ────────────────────────────────────────────


def test_accept_stamp_records_source_log_and_overwrites_caller_keys(tmp_path: Path) -> None:
    """The accept-side stamp is COMPUTED at commit time: ``evidence_logs``
    lists every namespace consulted and the per-field ``source_log`` names the
    contributing set (``own+home`` here — the scalar from the own log, the
    range claims from the home's "20 seeds"). A caller-asserted stamp is
    overwritten, never trusted (the efb980d6 code-owned-key rule, extended)."""
    second, home = _repos(tmp_path)
    _utter(second, "run it at n_samples=1000000")  # the scalar derives from OWN
    _utter(home, "20 seeds")  # the range claims derive from HOME
    _utter(home, _naming_utterance(home))
    _grant(second, home)

    out = _append(
        second,
        resolved={"task_generator": _sweep()},
        provenance={
            "human_authorship": {
                "evidence_logs": ["deadbeef0000"],
                "fields": {"task_generator": {"source_log": "own"}},
            }
        },
    )
    stamp = out.record.provenance["human_authorship"]
    assert stamp["evidence_logs"] == [repo_hash(second), repo_hash(home)]
    field = stamp["fields"]["task_generator"]
    assert field["source_log"] == "own+home"
    assert field["numbers"] == {
        "0": "zero",
        "19": "off_by_one",
        "20": "verbatim",
        "1000000": "verbatim",
    }


# ── composition: range-gating + actor scoping hold across the union pool ──────


def test_standalone_scalar_adjacent_to_home_log_number_still_refused(tmp_path: Path) -> None:
    """The home log widens the human's STATEMENTS, never the derivation
    grammar: a standalone ``n_samples=10000004`` adjacent to the HOME log's
    stated ``10000003`` is still refused (run-15 gate finding 2, cross-repo
    form) — and the refusal now discloses the granted home consultation."""
    second, home = _repos(tmp_path)
    _utter(second, "20 seeds")
    _utter(home, "rerun the drill at n=10000003")
    _utter(home, _naming_utterance(home))
    _grant(second, home)

    with pytest.raises(errors.SpecInvalid) as ei:
        _append(
            second,
            resolved={
                "task_generator": {
                    "kind": "items_x_seeds",
                    "params": {"items": [{"n_samples": 10000004}], "seeds": list(range(20))},
                }
            },
        )
    msg = str(ei.value)
    assert "10000004" in msg
    assert f"granted home namespace {repo_hash(home)}" in msg  # consulted, disclosed


def test_home_log_read_is_actor_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MH4 composes across namespaces: under >1 declared actors the home read
    is the SESSION ACTOR'S suffixed home log ONLY — actor A's agent cannot
    commit a value only actor B ever typed, whichever namespace B typed it in,
    and the anonymous (unsuffixed) home log never satisfies a scoped read."""
    second, home = _repos(tmp_path)
    (second / "interview.json").write_text(
        json.dumps({"actors": {"ids": ["alice", "bob"]}}), encoding="utf-8"
    )
    monkeypatch.setenv("HPC_ACTOR", "alice")
    _utter(home, "20 seeds at 1M samples", actor="alice")
    _utter(home, _naming_utterance(home), actor="alice")
    _utter(home, "50 seeds at 2M samples", actor="bob")
    _utter(home, "77 seeds at 3M samples")  # the anonymous home log
    _grant(second, home)

    # The session actor's own home statements derive (the fresh-second-repo
    # window: no own log at all, the grant covers it honestly).
    out = _append(second, resolved={"task_generator": _sweep()})
    assert (
        out.record.provenance["human_authorship"]["fields"]["task_generator"]["source_log"]
        == "home"
    )

    # Bob's home statements are invisible to alice's session.
    with pytest.raises(errors.SpecInvalid):
        _append(second, scope_id="run-2", resolved={"task_generator": _sweep(2_000_000, 50)})

    # The anonymous home log is invisible to a scoped read.
    with pytest.raises(errors.SpecInvalid):
        _append(second, scope_id="run-3", resolved={"task_generator": _sweep(3_000_000, 77)})


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
