"""Direct-atom tests for the ``notebook-draft`` mutate primitive (multi-human MH5).

Writes a source ``.py`` under the experiment dir, optionally an interview.json
declaring actors, sets ``HPC_ACTOR`` in the env, and asserts:

* a happy attributed draft stamps the session actor as ``attestor_id`` and reads
  back as the section's author at the current sha;
* a redraft (which moves the sha) leaves the OLD draft stale via the reducer —
  authorship follows the current content;
* the fabricated-sha refusal fires on the state writer's recompute (bind) leg;
* >1 declared actor with NO resolvable session actor is a loud refusal;
* zero declared actors records ``attestor_id=None`` (comparisons off);
* the wire spec carries NO actor field (the enforcement row);
* an unknown section slug is a loud refusal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.notebook_draft import NotebookDraftSpec
from hpc_agent.ops.notebook.draft_op import notebook_draft
from hpc_agent.state.audit_source import parse_percent_source

# read_draft_author / record_draft are multi-human branch symbols; a mypy env
# pinned to the pre-multi-human package flags them (installed-pkg skew) — one
# narrow ignore here, resolves cleanly against the worktree src.
from hpc_agent.state.notebook_audit import (  # type: ignore[attr-defined]
    read_draft_author,
    record_draft,
)

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent._wire.actions.notebook_draft import NotebookDraftResult

_AUDIT = "demo-audit"

_SOURCE = """\
# %%
# hpc-audit-section: model
def train():
    return 42

# %%
# hpc-audit-section: report
print("ok")
"""


def _write(tmp_path: Path, source: str = _SOURCE) -> None:
    (tmp_path / "source.py").write_text(source, encoding="utf-8")


def _write_interview(tmp_path: Path, ids: list[str]) -> None:
    import json

    (tmp_path / "interview.json").write_text(
        json.dumps({"goal": "g", "task_count": 1, "actors": {"ids": ids}}),
        encoding="utf-8",
    )


def _run(tmp_path: Path, section: str = "model") -> NotebookDraftResult:
    return notebook_draft(
        experiment_dir=tmp_path,
        spec=NotebookDraftSpec.model_validate(
            {"audit_id": _AUDIT, "source": "source.py", "section": section}
        ),
    )


def _shas(tmp_path: Path) -> dict[str, str]:
    source = parse_percent_source((tmp_path / "source.py").read_text(encoding="utf-8"))
    return {s.slug: s.section_sha for s in source.sections}


def test_no_actor_field_on_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    # The enforcement row: the mutate spec must not carry an actor / attestor_id
    # field the gate could trust — the actor is resolved server-side only.
    fields = set(NotebookDraftSpec.model_fields)
    assert fields == {"audit_id", "source", "section"}
    assert "actor" not in fields
    assert "attestor_id" not in fields


def test_happy_attributed_draft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path)
    _write_interview(tmp_path, ["alice", "bob"])
    monkeypatch.setenv("HPC_ACTOR", "alice")

    result = _run(tmp_path)
    shas = _shas(tmp_path)
    assert result.section == "model"
    assert result.section_sha == shas["model"]
    assert result.actor == "alice"

    # Read back the author at the current sha: alice, fresh.
    author = read_draft_author(tmp_path, _AUDIT, "model", current_sha=shas["model"])
    assert author == "alice"


def test_redraft_stales_old_draft_via_the_reducer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path)
    _write_interview(tmp_path, ["alice", "bob"])

    # alice drafts first.
    monkeypatch.setenv("HPC_ACTOR", "alice")
    _run(tmp_path)
    old_sha = _shas(tmp_path)["model"]
    assert read_draft_author(tmp_path, _AUDIT, "model", current_sha=old_sha) == "alice"

    # bob redrafts the section — the sha moves; the OLD (alice) draft reads stale,
    # so authorship follows the CURRENT content (bob), via the ONE reducer.
    edited = _SOURCE.replace("return 42", "return 7")
    _write(tmp_path, edited)
    monkeypatch.setenv("HPC_ACTOR", "bob")
    _run(tmp_path)
    new_sha = _shas(tmp_path)["model"]
    assert new_sha != old_sha
    assert read_draft_author(tmp_path, _AUDIT, "model", current_sha=new_sha) == "bob"
    # Alice's draft is now stale: at the OLD sha there is no current author (the
    # newest draft binds the new sha).
    assert read_draft_author(tmp_path, _AUDIT, "model", current_sha=old_sha) is None


def test_fabricated_sha_refused_on_recompute_leg(tmp_path: Path) -> None:
    # The op is fabrication-proof by construction (no sha on the wire — the parse
    # IS the recompute); the recompute leg is exercised at the state writer, which
    # binds through the kernel and refuses a sha that does not match a fresh parse.
    _write(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="does not match"):
        record_draft(
            tmp_path,
            audit_id=_AUDIT,
            section="model",
            section_sha="deadbeef" * 8,  # a fabricated sha
            recompute=_shas(tmp_path)["model"],  # the real, different sha
            actor="alice",
        )


def test_multi_actor_no_session_actor_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path)
    _write_interview(tmp_path, ["alice", "bob"])
    monkeypatch.delenv("HPC_ACTOR", raising=False)
    with pytest.raises(errors.SpecInvalid, match="HPC_ACTOR"):
        _run(tmp_path)


def test_multi_actor_undeclared_session_actor_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An HPC_ACTOR that is not among the declared ids resolves to None → the same
    # loud refusal (an undeclared actor may not draft).
    _write(tmp_path)
    _write_interview(tmp_path, ["alice", "bob"])
    monkeypatch.setenv("HPC_ACTOR", "charlie")
    with pytest.raises(errors.SpecInvalid, match="more than one actor"):
        _run(tmp_path)


def test_zero_actors_records_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No interview.json, no actors declared → a draft still lands, attributed to
    # None (comparisons off), even with HPC_ACTOR set.
    _write(tmp_path)
    monkeypatch.setenv("HPC_ACTOR", "alice")
    result = _run(tmp_path)
    assert result.actor is None
    author = read_draft_author(tmp_path, _AUDIT, "model", current_sha=_shas(tmp_path)["model"])
    assert author is None


def test_sole_actor_census_nulled_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A9 (RULED 2026-07-12, census-null): with exactly ONE declared actor the
    # draft record holds the same zero/one-actor byte-identity floor as every
    # decision record — attestor_id stays None, and the journaled bytes are
    # IDENTICAL whether or not HPC_ACTOR is exported.
    # Freeze the record clock: this assertion is about actor byte-identity, and
    # append_decision stamps ``ts`` via utcnow_iso() on each run — without the
    # freeze the two runs flake apart whenever they straddle a wall-clock second
    # boundary (observed red on the slow Windows CI runner). decision_journal
    # binds the symbol with ``from hpc_agent.infra.time import utcnow_iso``, so
    # the freeze MUST target that consumer alias — patching the definition site
    # (``hpc_agent.infra.time.utcnow_iso``) leaves the already-bound alias live
    # and the clock un-frozen (the prior fix's residual flake, verified).
    monkeypatch.setattr(
        "hpc_agent.state.decision_journal.utcnow_iso",
        lambda: "2026-07-12T00:00:00+00:00",
    )
    _write(tmp_path)
    _write_interview(tmp_path, ["alice"])
    monkeypatch.setenv("HPC_ACTOR", "alice")
    result = _run(tmp_path)
    assert result.actor is None
    journal = tmp_path / ".hpc" / "notebooks" / f"{_AUDIT}.decisions.jsonl"
    with_actor = journal.read_text(encoding="utf-8")

    import shutil

    shutil.rmtree(tmp_path / ".hpc")
    monkeypatch.delenv("HPC_ACTOR", raising=False)
    result = _run(tmp_path)
    assert result.actor is None
    assert journal.read_text(encoding="utf-8") == with_actor


def test_unknown_section_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path)
    monkeypatch.delenv("HPC_ACTOR", raising=False)
    with pytest.raises(errors.SpecInvalid, match="not found in source"):
        _run(tmp_path, section="no-such-section")


def test_missing_source_is_spec_invalid(tmp_path: Path) -> None:
    with pytest.raises(errors.SpecInvalid, match="source"):
        _run(tmp_path)
