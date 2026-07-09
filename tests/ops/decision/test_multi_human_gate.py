"""MT7 — the multi-human actor gate (``ops/decision/journal.py``).

Fires each multi-human refusal on a synthetic violation and proves the
byte-identity floor: under zero/one declared actor every path behaves exactly as
before multi-human (no attestor_id stamp, no new refusal, no policy read). Covers:

* the ``attestor_id`` stamp (server-resolved, on disk only when >1 actor + a
  resolved session actor);
* MH4 actor-scoped evidence — cross-actor laundering refused (tokens only in the
  OTHER actor's log);
* MH6 reviewer≠author — self-sign / missing-draft-attribution / missing-session-
  actor refusals on a notebook sign-off;
* MH8 policy — a non-member session actor refused for a delegated block;
* MH7 (landed here) — resolver==challenger / unattributed resolution refused on a
  verdict, withdrawer!=challenger refused on a withdrawal.

The broader byte-identity battery is the full ``tests/ops/decision/`` +
``test_decision_journal_primitives`` suites running untouched with no ``actors``
block; this module adds the explicit on-disk assertions.

TOY VOCABULARY ONLY (alice / bob, widget lineage) — never a role word.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.state.decision_journal import decisions_path, read_decisions
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path


def _append(experiment_dir: Path, **base: Any) -> Any:
    return append_decision(
        experiment_dir=experiment_dir, spec=AppendDecisionInput.model_validate(base)
    )


def _write_interview(experiment_dir: Path, doc: dict[str, Any]) -> None:
    (experiment_dir / "interview.json").write_text(json.dumps(doc), encoding="utf-8")


def _raw_records(experiment_dir: Path, scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    path = decisions_path(experiment_dir, scope_kind, scope_id)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ── attestor_id stamp + byte-identity floor ──────────────────────────────────


def _campaign_greenlight(experiment_dir: Path) -> Any:
    return _append(
        experiment_dir,
        scope_kind="campaign",
        scope_id="widget-camp",
        block="campaign-greenlight",
        response="y",
        resolved={},
    )


def test_zero_actors_no_attestor_id_on_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No actors block + HPC_ACTOR set → the record carries NO attestor_id key
    (byte-identical to pre-multi-human)."""
    monkeypatch.setenv("HPC_ACTOR", "alice")  # a stray env must not leak into a stamp
    _campaign_greenlight(tmp_path)
    (rec,) = _raw_records(tmp_path, "campaign", "widget-camp")
    assert "attestor_id" not in rec


def test_one_actor_no_attestor_id_on_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Exactly one declared actor → comparisons stay off, no stamp (len<=1)."""
    _write_interview(tmp_path, {"actors": {"ids": ["alice"]}})
    monkeypatch.setenv("HPC_ACTOR", "alice")
    _campaign_greenlight(tmp_path)
    (rec,) = _raw_records(tmp_path, "campaign", "widget-camp")
    assert "attestor_id" not in rec


def test_multi_actor_stamps_session_actor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """>1 declared + a resolved session actor → attestor_id stamped on disk + result."""
    _write_interview(tmp_path, {"actors": {"ids": ["alice", "bob"]}})
    monkeypatch.setenv("HPC_ACTOR", "alice")
    out = _campaign_greenlight(tmp_path)
    assert out.record.attestor_id == "alice"
    (rec,) = _raw_records(tmp_path, "campaign", "widget-camp")
    assert rec["attestor_id"] == "alice"


def test_multi_actor_unresolvable_env_no_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """>1 declared but HPC_ACTOR names an UNDECLARED actor → no stamp (unresolvable)."""
    _write_interview(tmp_path, {"actors": {"ids": ["alice", "bob"]}})
    monkeypatch.setenv("HPC_ACTOR", "carol")  # not in ids
    _campaign_greenlight(tmp_path)
    (rec,) = _raw_records(tmp_path, "campaign", "widget-camp")
    assert "attestor_id" not in rec


# ── MH8 policy delegation ─────────────────────────────────────────────────────


def test_policy_non_member_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_interview(
        tmp_path,
        {"actors": {"ids": ["alice", "bob"], "policy": {"campaign-greenlight": ["alice"]}}},
    )
    monkeypatch.setenv("HPC_ACTOR", "bob")  # not delegated
    with pytest.raises(errors.SpecInvalid, match="actor-policy gate"):
        _campaign_greenlight(tmp_path)


def test_policy_member_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_interview(
        tmp_path,
        {"actors": {"ids": ["alice", "bob"], "policy": {"campaign-greenlight": ["alice"]}}},
    )
    monkeypatch.setenv("HPC_ACTOR", "alice")
    out = _campaign_greenlight(tmp_path)
    assert out.record.attestor_id == "alice"


def test_policy_unlisted_block_unrestricted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A block absent from the policy is unrestricted even for a non-listed actor."""
    _write_interview(
        tmp_path,
        {"actors": {"ids": ["alice", "bob"], "policy": {"registration": ["alice"]}}},
    )
    monkeypatch.setenv("HPC_ACTOR", "bob")
    out = _campaign_greenlight(tmp_path)  # campaign-greenlight not in policy → allowed
    assert out.record.attestor_id == "bob"


# ── MH4 actor-scoped evidence (cross-actor laundering refused) ─────────────────


def _scaffold_namespace(experiment_dir: Path) -> None:
    """Make *experiment_dir* an hpc repo so the utterance no-scaffold rule is
    satisfied (the namespace dir the suffixed logs live in must pre-exist)."""
    from hpc_agent.state.run_record import journal_dir

    journal_dir(experiment_dir)


def _unlock(experiment_dir: Path, response: str) -> Any:
    return _append(
        experiment_dir,
        scope_kind="scope",
        scope_id="calib",
        block="scope-unlock",
        response=response,
        resolved={"scope_action": "unlock"},
    )


def test_cross_actor_evidence_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Under >1 actor, the unlock rationale must derive from the SESSION ACTOR'S
    log — words only in the OTHER actor's log are refused (anti-laundering)."""
    from hpc_agent.state.utterances import append_utterance

    _write_interview(tmp_path, {"actors": {"ids": ["alice", "bob"]}})
    _scaffold_namespace(tmp_path)
    append_utterance(tmp_path, "reopen the calibration scope for another recount", actor="alice")
    append_utterance(tmp_path, "the widget threshold drifted overnight", actor="bob")

    # Session is alice; the rationale uses only BOB'S words → refused.
    monkeypatch.setenv("HPC_ACTOR", "alice")
    with pytest.raises(errors.SpecInvalid, match="SESSION ACTOR"):
        _unlock(tmp_path, "the widget threshold drifted")


def test_own_actor_evidence_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The companion: the session actor's OWN typed words satisfy the tier."""
    from hpc_agent.state.utterances import append_utterance

    _write_interview(tmp_path, {"actors": {"ids": ["alice", "bob"]}})
    _scaffold_namespace(tmp_path)
    append_utterance(tmp_path, "reopen the calibration scope for another recount", actor="alice")
    monkeypatch.setenv("HPC_ACTOR", "alice")
    out = _unlock(tmp_path, "reopen the calibration scope for a recount")
    assert out.record.attestor_id == "alice"


# ── MH6 reviewer ≠ author (notebook sign-off) ─────────────────────────────────

_NB_TEMPLATE = """# %%
# hpc-audit-section: model-fit
model = fit(data)
"""

_NB_SOURCE = """# %%
# hpc-audit-section: model-fit
model = fit(data, regularization=0.5)
assert model.converged
"""

_AUDIT_ID = "widget-audit"
_SECTION = "model-fit"


def _nb_section_sha(source: str = _NB_SOURCE, slug: str = _SECTION) -> str:
    from hpc_agent.state.audit_source import parse_percent_source

    return next(s.section_sha for s in parse_percent_source(source).sections if s.slug == slug)


def _nb_view_sha(slug: str = _SECTION) -> str:
    from hpc_agent.ops.notebook.audit_view import build_audit_view
    from hpc_agent.state.audit_source import parse_percent_source

    src = parse_percent_source(_NB_SOURCE)
    tmpl = parse_percent_source(_NB_TEMPLATE)
    return next(v.view_sha for v in build_audit_view(src, tmpl, ()).sections if v.slug == slug)


def _write_notebook(experiment_dir: Path, *, actors: dict[str, Any] | None) -> None:
    (experiment_dir / "source.py").write_text(_NB_SOURCE, encoding="utf-8")
    (experiment_dir / "template.py").write_text(_NB_TEMPLATE, encoding="utf-8")
    doc: dict[str, Any] = {
        "audited_source": {"source": "source.py", "template": "template.py", "audit_id": _AUDIT_ID}
    }
    if actors is not None:
        doc["actors"] = actors
    _write_interview(experiment_dir, doc)
    # The content-addressed trusted-display render for the section (T8 lock).
    from hpc_agent.ops.notebook.audit_view import build_audit_view
    from hpc_agent.ops.notebook.render_store import write_render
    from hpc_agent.state.audit_source import parse_percent_source

    src = parse_percent_source(_NB_SOURCE)
    tmpl = parse_percent_source(_NB_TEMPLATE)
    sv = next(v for v in build_audit_view(src, tmpl, ()).sections if v.slug == _SECTION)
    write_render(experiment_dir, audit_id=_AUDIT_ID, view=sv)


def _record_draft(experiment_dir: Path, actor: str) -> None:
    from hpc_agent.state.notebook_audit import record_draft

    sha = _nb_section_sha()
    record_draft(
        experiment_dir,
        audit_id=_AUDIT_ID,
        section=_SECTION,
        section_sha=sha,
        recompute=sha,
        actor=actor,
    )


def _signoff(experiment_dir: Path, response: str) -> Any:
    return _append(
        experiment_dir,
        scope_kind="notebook",
        scope_id=_AUDIT_ID,
        block="notebook-sign-off",
        response=response,
        resolved={
            "audit_id": _AUDIT_ID,
            "section": _SECTION,
            "section_sha": _nb_section_sha(),
            "view_sha": _nb_view_sha(),
        },
    )


_ENGAGING = "model-fit reviewed — the regularization is sound and it converged"


def test_signoff_self_sign_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The drafter's actor cannot sign their own section (MH6)."""
    _write_notebook(tmp_path, actors={"ids": ["alice", "bob"]})
    _record_draft(tmp_path, actor="alice")
    monkeypatch.setenv("HPC_ACTOR", "alice")  # alice drafted AND signs
    with pytest.raises(errors.SpecInvalid, match="reviewer.author"):
        _signoff(tmp_path, _ENGAGING)


def test_signoff_cross_actor_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A DIFFERENT declared actor signs the alice-drafted section → passes."""
    _write_notebook(tmp_path, actors={"ids": ["alice", "bob"]})
    _record_draft(tmp_path, actor="alice")
    monkeypatch.setenv("HPC_ACTOR", "bob")
    out = _signoff(tmp_path, _ENGAGING)
    assert out.record.attestor_id == "bob"


def test_signoff_missing_draft_attribution_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """>1 actor + NO notebook-draft record → refused (self-review by omission)."""
    _write_notebook(tmp_path, actors={"ids": ["alice", "bob"]})
    # no draft recorded
    monkeypatch.setenv("HPC_ACTOR", "bob")
    with pytest.raises(errors.SpecInvalid, match="NO current draft attribution"):
        _signoff(tmp_path, _ENGAGING)


def test_signoff_missing_session_actor_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """>1 actor + no resolvable session actor → refused (anonymous sign-off)."""
    _write_notebook(tmp_path, actors={"ids": ["alice", "bob"]})
    _record_draft(tmp_path, actor="alice")
    monkeypatch.delenv("HPC_ACTOR", raising=False)
    with pytest.raises(errors.SpecInvalid, match="no resolvable actor"):
        _signoff(tmp_path, _ENGAGING)


def test_signoff_single_actor_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero declared actors → the MH6 gate does not exist: a self-authored session
    signs with no draft record and no refusal (today's behavior, no stamp)."""
    _write_notebook(tmp_path, actors=None)
    monkeypatch.setenv("HPC_ACTOR", "alice")  # stray env, no actors declared
    out = _signoff(tmp_path, _ENGAGING)
    (recs) = read_decisions(tmp_path, "notebook", _AUDIT_ID)
    assert all("attestor_id" not in r for r in recs)
    assert out.count >= 1


# ── MH7 resolver ≠ challenger / withdrawer == challenger ───────────────────────

_TGT_RUN = "widget-run-tgt"
_TGT_SHA = "a3f2c9d1beef0011223344556677"
_CIT_RUN = "widget-run-cite"
_CIT_SHA = "c1d2e3f4aa11bb22cc33dd44ee55"
_CHALLENGE_ID = "widget-dissent"
_GOOD_FILING = (
    "challenge widget-dissent — the row at a3f2c9d1 is refuted by the replication c1d2e3f4"
)


def _seed_runs(experiment_dir: Path) -> None:
    for run_id, cmd_sha in ((_TGT_RUN, _TGT_SHA), (_CIT_RUN, _CIT_SHA)):
        write_run_sidecar(
            experiment_dir,
            run_id=run_id,
            cmd_sha=cmd_sha,
            hpc_agent_version="0.0.0-test",
            submitted_at="2025-11-14T00:00:00Z",
            executor="widget_executor.py",
            result_dir_template="results/{run_id}",
            task_count=1,
            tasks_py_sha="tasks-sha-aaa",
        )


def _file_challenge(experiment_dir: Path) -> Any:
    return _append(
        experiment_dir,
        scope_kind="challenge",
        scope_id=_CHALLENGE_ID,
        block="challenge",
        response=_GOOD_FILING,
        resolved={
            "challenge_id": _CHALLENGE_ID,
            "target": {
                "kind": "run",
                "subject_kind": "conclusion",
                "subject_id": _TGT_RUN,
                "content_sha": _TGT_SHA,
                "scope": {"scope_kind": "run", "scope_id": _TGT_RUN},
            },
            "citations": [{"kind": "run", "ref": _CIT_RUN, "sha": _CIT_SHA}],
            "grounds": "the widget batch replication did not reproduce the concluded row",
        },
    )


def _verdict(experiment_dir: Path, response: str) -> Any:
    return _append(
        experiment_dir,
        scope_kind="challenge",
        scope_id=_CHALLENGE_ID,
        block="challenge-verdict",
        response=response,
        resolved={
            "challenge_id": _CHALLENGE_ID,
            "verdict": "upheld",
            "reasoning": "the batch row does not reproduce; the finding is refuted",
        },
    )


def _withdraw(experiment_dir: Path, response: str) -> Any:
    return _append(
        experiment_dir,
        scope_kind="challenge",
        scope_id=_CHALLENGE_ID,
        block="challenge-withdraw",
        response=response,
        resolved={
            "challenge_id": _CHALLENGE_ID,
            "reason": "a re-run reproduced the row; my dissent no longer holds",
        },
    )


def _setup_filed_challenge(
    experiment_dir: Path, monkeypatch: pytest.MonkeyPatch, *, challenger: str = "alice"
) -> None:
    _write_interview(experiment_dir, {"actors": {"ids": ["alice", "bob"]}})
    _seed_runs(experiment_dir)
    monkeypatch.setenv("HPC_ACTOR", challenger)
    _file_challenge(experiment_dir)


def test_verdict_resolver_equals_challenger_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_filed_challenge(tmp_path, monkeypatch, challenger="alice")
    monkeypatch.setenv("HPC_ACTOR", "alice")  # alice resolves her own challenge
    with pytest.raises(errors.SpecInvalid, match="you may not adjudicate your own"):
        _verdict(tmp_path, "uphold widget-dissent — the replication stands")


def test_verdict_by_other_actor_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_filed_challenge(tmp_path, monkeypatch, challenger="alice")
    monkeypatch.setenv("HPC_ACTOR", "bob")  # a DIFFERENT actor resolves
    out = _verdict(tmp_path, "uphold widget-dissent — the replication stands")
    assert out.record.attestor_id == "bob"


def test_verdict_unattributed_resolution_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_filed_challenge(tmp_path, monkeypatch, challenger="alice")
    monkeypatch.delenv("HPC_ACTOR", raising=False)  # anonymous resolution
    with pytest.raises(errors.SpecInvalid, match="no resolvable actor"):
        _verdict(tmp_path, "uphold widget-dissent — the replication stands")


def test_withdraw_by_non_challenger_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_filed_challenge(tmp_path, monkeypatch, challenger="alice")
    monkeypatch.setenv("HPC_ACTOR", "bob")  # bob tries to silence alice's dissent
    with pytest.raises(errors.SpecInvalid, match="only the CHALLENGER"):
        _withdraw(tmp_path, "withdraw widget-dissent — I no longer stand on it")


def test_withdraw_by_challenger_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_filed_challenge(tmp_path, monkeypatch, challenger="alice")
    monkeypatch.setenv("HPC_ACTOR", "alice")  # the challenger withdraws her own
    out = _withdraw(tmp_path, "withdraw widget-dissent — I no longer stand on it")
    assert out.record.attestor_id == "alice"


def test_challenge_single_actor_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero declared actors → a solo researcher resolves their own past challenge
    (no MH7 comparison), byte-identical."""
    _seed_runs(tmp_path)
    monkeypatch.setenv("HPC_ACTOR", "alice")  # stray env, no actors declared
    _file_challenge(tmp_path)
    _verdict(tmp_path, "uphold widget-dissent — the replication stands")
    recs = read_decisions(tmp_path, "challenge", _CHALLENGE_ID)
    assert all("attestor_id" not in r for r in recs)
