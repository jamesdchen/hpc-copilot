"""The ``audit`` block chain: the notebook-audit loop, driven by code (P2.b).

The 2026-07-30 ruling reversed "a block-drive-style loop driver is REJECTED"
(``docs/design/notebook-audit.md`` Amendment 2 item 3) for SEQUENCING ONLY. This
file pins what that reversal is allowed to mean:

* the LOOP's edges — the draft park, the redraft re-entry at lint, the sign-off
  park, the passing hand-off — expressed as stage-keyed successors;
* the BYTE-COMPAT floor: a call that supplies no chain seat gets ``next_block:
  None`` and is otherwise indistinguishable from the pre-chain verb (the render
  markdown, the findings, the statuses, the ``passed`` predicate);
* and an END-TO-END drive of a real fixture audit — preflight → lint →
  auto-clear → view → status(passed) → the hand-off seam — through the actual
  ``block-drive`` tick, over tmp-dir fixtures with no SSH anywhere.

Everything here runs on real files and real journals: the whole point of a code
-driven chain is that its hops are dispatchable for real, and a test that faked
the spans would pin the table rather than the loop.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent._wire.queries.audit_preflight import AuditPreflightSpec
from hpc_agent._wire.queries.notebook_status import NotebookStatusSpec
from hpc_agent._wire.workflows.block_drive import BlockDriveAuditSeat, BlockDriveSpec
from hpc_agent.infra import block_chain
from hpc_agent.ops.audit_preflight import audit_preflight
from hpc_agent.ops.notebook.status_op import notebook_status

if TYPE_CHECKING:
    from pathlib import Path

_AUDIT_ID = "audit_chain_probe"

# A percent-format template + a source that reproduces it EXACTLY. Byte-identical
# sections classify `inherited`, tier `auto_cleared`, so the CODE attestor can
# clear the whole module and the graduation gate passes with no human sign-off —
# which is what lets one test drive the chain end to end without forging one.
_TEMPLATE = "# %%\n# hpc-audit-section: intro\nx = 1\n\n# %%\n# hpc-audit-section: compute\ny = 2\n"


def _git(args: list[str], cwd: Path) -> None:
    """Test setup, failing loudly — an unsigned template is a real NO-GO blocker."""
    import subprocess

    subprocess.run(
        ["git", *args], cwd=str(cwd), timeout=60, check=True, capture_output=True, text=True
    )


@pytest.fixture(autouse=True)
def _no_version_skew(monkeypatch: pytest.MonkeyPatch) -> None:
    """No version skew (the reused ``doctor`` detector) — a runner-checkout artifact.

    A tmp repo is never the hpc-agent source repo, so the real detector already
    returns None; pinning it keeps the chain tests independent of whatever state
    the developer's own checkout happens to be in.
    """
    from hpc_agent.ops import audit_preflight as ap_mod

    monkeypatch.setattr(ap_mod._doctor, "_detect_version_skew", lambda experiment_dir: None)


def _seed_audit(tmp_path: Path) -> tuple[str, str]:
    """A git repo with a COMMITTED-CLEAN template + a byte-identical source.

    The commit is not decoration: an uncommitted template is an "unsigned
    template" NO-GO, so a fixture that skipped it would test the blocked path
    while claiming to test the loop.

    IDEMPOTENT — a test that seeds twice (to compare a seated call against a
    seatless one over the same tree) must get the same tree back, not a second
    commit of nothing.
    """
    if (tmp_path / ".git").exists():
        return "analysis.py", "audit_template.py"
    _git(["init"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    (tmp_path / "audit_template.py").write_text(_TEMPLATE, encoding="utf-8")
    _git(["add", "audit_template.py"], tmp_path)
    _git(["commit", "-m", "add template"], tmp_path)
    (tmp_path / "analysis.py").write_text(_TEMPLATE, encoding="utf-8")
    return "analysis.py", "audit_template.py"


def _preflight(tmp_path: Path, **over: Any) -> Any:
    source, template = _seed_audit(tmp_path)
    spec = AuditPreflightSpec(
        audit_id=_AUDIT_ID,
        template=template,
        source=over.pop("source", source),
        source_roots=[],
        input_roots=[],
        **over,
    )
    return audit_preflight(experiment_dir=tmp_path, spec=spec)


# ── the table: membership, class, and what must NOT be true of it ─────────────


def test_audit_chain_membership_and_order() -> None:
    assert block_chain.ORDER["audit"] == [
        "audit-preflight",
        "notebook-lint",
        "notebook-auto-clear",
        "notebook-audit-view",
        "notebook-status",
    ]
    for verb in block_chain.ORDER["audit"]:
        assert block_chain.WORKFLOW_OF[verb] == "audit"
    # audit-handoff is the P2.c SEAM, in its own single-member family — putting it
    # in the audit chain would shift every audit block_index the §4 field-change
    # routing compares.
    assert block_chain.ORDER["audit-handoff"] == ["audit-handoff"]
    assert block_chain.WORKFLOW_OF["audit-handoff"] == "audit-handoff"


def test_no_audit_block_is_gated_or_watch_class() -> None:
    """Nothing in the loop reaches a cluster, so nothing in it may park for a greenlight.

    A gated member would make the driver stop for a spend approval on a step that
    spends nothing — training the human to type ``y`` at boundaries that mean
    nothing, which is how a real greenlight stops being read.
    """
    for verb in [*block_chain.ORDER["audit"], "audit-handoff"]:
        assert not block_chain.is_gated(verb), verb
        assert verb not in block_chain.WATCH_VERBS, verb
        # Quick class: a local parse + journal replay, never the 24h watch default.
        assert block_chain.verb_deadline_seconds(verb, {}) == 600.0, verb


def test_audit_successor_edges() -> None:
    """The loop, spelled out — including every edge that must NOT chain."""
    s = block_chain.successor_verb
    assert s("audit-preflight", "preflight_go") == "notebook-lint"
    assert s("notebook-lint", "linted") == "notebook-auto-clear"
    assert s("notebook-auto-clear", "cleared") == "notebook-audit-view"
    assert s("notebook-audit-view", "viewed") == "notebook-status"
    assert s("notebook-status", "audit_passed") == "audit-handoff"
    # The two parks and the NO-GO have NO deterministic successor.
    assert s("audit-preflight", "awaiting_draft") is None
    assert s("audit-preflight", "preflight_blocked") is None
    assert s("notebook-status", "sections_pending") is None


# ── the LOOP edges, driven on real state ──────────────────────────────────────


def test_no_source_parks_for_the_agent_draft(tmp_path: Path) -> None:
    """GO but nothing to lint ⇒ the AGENT park, not a chain into a lint of nothing."""
    result = _preflight(tmp_path, source="never_drafted.py")
    assert result.verdict == "GO"
    assert result.source_present is False
    assert result.stage_reached == "awaiting_draft"
    assert result.needs_decision is True
    assert result.next_block is None
    assert block_chain.park_actor("audit-preflight", result.stage_reached) == "agent"


def test_source_present_chains_to_lint(tmp_path: Path) -> None:
    """The redraft RE-ENTRY edge: once the draft is on disk, preflight advances."""
    result = _preflight(tmp_path)
    assert result.verdict == "GO"
    assert result.source_present is True
    assert result.stage_reached == "preflight_go"
    assert result.needs_decision is False
    assert result.next_block is not None
    assert result.next_block["verb"] == "notebook-lint"
    # The successor's spec is COMPOSED, not a skeleton: it carries the seat plus
    # the roots the preflight itself resolved (one declaration, not a second one).
    assert result.next_block["spec_hint"] == {
        "audit_id": _AUDIT_ID,
        "source": "analysis.py",
        "template": "audit_template.py",
        "input_roots": [],
        "source_roots": [],
    }


def test_a_nudge_since_the_last_draft_re_parks_for_a_redraft(tmp_path: Path) -> None:
    """A nudge recorded AFTER the newest draft supersedes it — even with a source on disk.

    This is the loop's other draft edge: the human asked for a change, so linting
    the stale draft would spend the human's next review on content they already
    rejected. Position-only — the code never reads the nudge TEXT.
    """
    from hpc_agent.state.decision_journal import append_decision

    source, template = _seed_audit(tmp_path)
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=_AUDIT_ID,
        block="notebook-draft",
        response="y",
        resolved={"drafted": True},
    )
    fresh = audit_preflight(
        experiment_dir=tmp_path,
        spec=AuditPreflightSpec(
            audit_id=_AUDIT_ID, template=template, source=source, source_roots=[], input_roots=[]
        ),
    )
    assert fresh.stage_reached == "preflight_go"

    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=_AUDIT_ID,
        block="notebook-sign-off",
        response="please split the compute section",
        resolved={},
    )
    after_nudge = audit_preflight(
        experiment_dir=tmp_path,
        spec=AuditPreflightSpec(
            audit_id=_AUDIT_ID, template=template, source=source, source_roots=[], input_roots=[]
        ),
    )
    assert after_nudge.stage_reached == "awaiting_draft"
    assert after_nudge.next_block is None

    # A NEW draft record after the nudge clears the debt — the loop re-enters at lint.
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=_AUDIT_ID,
        block="notebook-draft",
        response="y",
        resolved={"drafted": True},
    )
    redrafted = audit_preflight(
        experiment_dir=tmp_path,
        spec=AuditPreflightSpec(
            audit_id=_AUDIT_ID, template=template, source=source, source_roots=[], input_roots=[]
        ),
    )
    assert redrafted.stage_reached == "preflight_go"
    assert redrafted.next_block is not None
    assert redrafted.next_block["verb"] == "notebook-lint"


def test_sections_pending_parks_and_names_no_successor(tmp_path: Path) -> None:
    """The SIGN-OFF rendezvous: unsigned sections park, and no verb may stand in.

    The answer here is the human's typed ``notebook-sign-off`` through
    ``append-decision``. If this ever emitted a ``next_block``, the driver would
    chain past a review nobody performed.
    """
    source, template = _seed_audit(tmp_path)
    result = notebook_status(
        experiment_dir=tmp_path,
        spec=NotebookStatusSpec(audit_id=_AUDIT_ID, source=source, template=template),
    )
    assert result.passed is False
    assert result.stage_reached == "sections_pending"
    assert result.needs_decision is True
    assert result.next_block is None
    # There is no "sign-off" VERB anywhere in the chain — the sign-off is a
    # decision record, not a block.
    assert not any("sign-off" in verb for verb in block_chain.ORDER["audit"])


# ── byte-compat: an absent seat changes nothing ───────────────────────────────


def test_preflight_without_a_seat_is_byte_identical(tmp_path: Path) -> None:
    """No audit_id / no source ⇒ no hint, and every pre-existing field unmoved."""
    _seed_audit(tmp_path)
    seatless = audit_preflight(
        experiment_dir=tmp_path,
        spec=AuditPreflightSpec(template="audit_template.py", source_roots=[], input_roots=[]),
    )
    assert seatless.next_block is None
    assert seatless.source_present is False
    assert seatless.verdict == "GO"
    seated = _preflight(tmp_path)
    # The additive chain fields are the ONLY difference between the two results.
    a = seatless.model_dump(mode="json")
    b = seated.model_dump(mode="json")
    chain_fields = {"next_block", "stage_reached", "needs_decision", "source_present"}
    differing = {k for k in a if a[k] != b[k]}
    assert differing <= chain_fields | {"audit_id", "brief", "disclosures"}


def test_lint_without_an_audit_id_emits_no_hint_and_lints_identically(tmp_path: Path) -> None:
    """The standalone lint is unchanged: same findings, no chain hint."""
    from hpc_agent._wire.actions.notebook_lint import NotebookLintInput
    from hpc_agent.ops.notebook.lint import notebook_lint

    source, template = _seed_audit(tmp_path)
    bare = notebook_lint(
        experiment_dir=tmp_path, spec=NotebookLintInput(source=source, template=template)
    )
    seated = notebook_lint(
        experiment_dir=tmp_path,
        spec=NotebookLintInput(audit_id=_AUDIT_ID, source=source, template=template),
    )
    assert bare.next_block is None
    assert seated.next_block is not None
    assert seated.next_block["verb"] == "notebook-auto-clear"
    # The lint REPORT itself is identical — the seat is carriage, never an input
    # to a rule (a seat that changed a finding would make the chain load-bearing
    # for what the human is shown).
    for field in ("findings", "unverifiable_paths", "linked_sources", "declared_outputs"):
        assert getattr(bare, field) == getattr(seated, field), field
    assert bare.stage_reached == seated.stage_reached == "linted"
    assert bare.needs_decision is False


def test_view_markdown_is_unaffected_by_the_chain_fields(tmp_path: Path) -> None:
    """The relayed render is byte-identical with and without the seat.

    The view's ``markdown`` is what the human READS and what a sign-off binds via
    ``view_sha``. If the chain fields could move either, the reversal would have
    changed the human boundary — which it must not.
    """
    from hpc_agent._wire.queries.notebook_audit_view import NotebookAuditViewSpec
    from hpc_agent.ops.notebook.view_op import notebook_audit_view

    source, template = _seed_audit(tmp_path)
    spec = NotebookAuditViewSpec(audit_id=_AUDIT_ID, source=source, template=template)
    result = notebook_audit_view(experiment_dir=tmp_path, spec=spec)
    again = notebook_audit_view(experiment_dir=tmp_path, spec=spec)
    assert result.markdown == again.markdown
    assert result.view_sha == again.view_sha
    assert result.next_block is not None
    assert result.next_block["verb"] == "notebook-status"
    # The hint is NOT folded into the sha the human signs.
    assert "next_block" not in result.markdown


# ── end to end, through the real driver ───────────────────────────────────────


def _span_via_registry(verb: str, spec: dict[str, Any], experiment_dir: Path) -> tuple[dict, int]:
    """Run one block span through the LIVE registry primitive.

    The driver's own span runner drives ``hpc-agent <verb> --spec`` (in-process or
    subprocess), and that seam pre-validates the spec against the CHECKED-IN JSON
    schema — which this branch deliberately leaves stale (the 8-file regen debt is
    declared in `docs/internals/regen-debt-ledger.md`, not paid here). So the
    schema pre-check would refuse the new `source` / `audit_id` fields for reasons
    that have nothing to do with the chain.

    This substitutes the ONE thing the debt makes unavailable and nothing else:
    the spec is still validated — by the verb's LIVE pydantic model, resolved
    through the same ``VERB_MODULE_MAP`` → registry path the CLI uses — and the
    real op runs against real files and real journals. The CLI-seam version of
    this drive is the xfail'd test below, which turns hard-red the moment the
    schemas are rebaked.
    """
    from hpc_agent._kernel.registry.primitive import get_meta, register_single_module
    from hpc_agent.cli._verb_module_map import VERB_MODULE_MAP

    primitive_name, module_name = VERB_MODULE_MAP[verb]
    register_single_module(module_name)
    meta = get_meta(primitive_name)
    model = getattr(meta.cli, "spec_model", None)
    assert model is not None, f"{verb} has no spec_model — the driver could not dispatch it"
    result = meta.func(experiment_dir=experiment_dir, spec=model.model_validate(spec))
    return json.loads(result.model_dump_json()), 0


def _drive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    via_cli_seam: bool = False,
    **seat: Any,
) -> Any:
    """One real ``block-drive`` tick over the audit chain."""
    from hpc_agent._kernel.lifecycle import block_drive as bd_mod
    from hpc_agent.ops.block_drive_op import block_drive

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    import hpc_agent._kernel.lifecycle.drive as drive_mod

    monkeypatch.setattr(drive_mod, "_stamp_driver_tick", lambda *_a, **_k: None)
    if not via_cli_seam:
        monkeypatch.setattr(bd_mod, "_run_block_verb", _span_via_registry)
    return block_drive(
        tmp_path,
        spec=BlockDriveSpec(workflow="audit", audit=BlockDriveAuditSeat(**seat)),
    )


def test_bare_tick_without_the_seat_skips_with_a_naming_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit chain is keyed by audit_id, so a run_id cannot start it.

    The refusal must NAME the missing seat: a bare ``skip`` with no remedy is how
    an agent ends up hand-authoring the preflight spec, which is the whole cost
    the chain removes.
    """
    from hpc_agent.ops.block_drive_op import block_drive

    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    result = block_drive(tmp_path, spec=BlockDriveSpec(workflow="audit"))
    assert result.action == "skip"
    assert "audit seat" in result.reason
    assert "audit_id" in result.reason


def test_end_to_end_fixture_audit_drives_from_preflight_to_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ONE tick carries a fixture audit the whole way — no SSH, no hand-authored spec.

    Every section of the source is byte-identical to the template, so the CODE
    attestor clears the module and the graduation gate passes without a human
    sign-off; the chain therefore runs preflight → lint → auto-clear → view →
    status → the ``audit-handoff`` seam and terminates. That is the reversal's
    entire claim, executed rather than asserted.
    """
    source, template = _seed_audit(tmp_path)
    result = _drive(
        tmp_path,
        monkeypatch,
        audit_id=_AUDIT_ID,
        source=source,
        template=template,
        source_roots=[],
        input_roots=[],
    )
    assert result.action == "terminal", result.reason
    assert result.current_verb == "audit-handoff"

    # The audit genuinely PASSED — read it back off the journal, not off the tick.
    status = notebook_status(
        experiment_dir=tmp_path,
        spec=NotebookStatusSpec(audit_id=_AUDIT_ID, source=source, template=template),
    )
    assert status.passed is True
    assert status.stage_reached == "audit_passed"
    assert status.next_block is not None
    assert status.next_block["verb"] == "audit-handoff"

    # The auto-clear attestations are real journal records, not an in-memory pass.
    from hpc_agent.state.decision_journal import read_decisions

    kinds = {rec.get("block") for rec in read_decisions(tmp_path, "notebook", _AUDIT_ID)}
    assert "notebook-auto-clear" in kinds


def test_end_to_end_stops_at_the_agent_park_when_no_draft_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no source on disk the very first span parks for the AGENT — and spends nothing.

    The substrate is otherwise GO (committed-clean template, no skew, no roots),
    so the ONLY thing missing is the draft: this pins that the draft park is
    reached on its own merits and is not a NO-GO wearing a different name.
    """
    source, template = _seed_audit(tmp_path)
    (tmp_path / source).unlink()  # the audit is open; the draft was never written
    result = _drive(
        tmp_path,
        monkeypatch,
        audit_id=_AUDIT_ID,
        source=source,
        template=template,
        source_roots=[],
        input_roots=[],
    )
    assert result.action == "awaiting_decision"
    assert result.current_verb == "audit-preflight"
    assert result.stage_reached == "awaiting_draft"
    assert result.next_verb is None
    # No consent affordance anywhere in what the tick hands back.
    brief = json.dumps(result.brief or {})
    assert "approve_hint" not in brief
    assert "answer_menu" not in brief
    assert "draft_ask" in brief


# ── the two HUMAN parks, driven THROUGH block-drive ───────────────────────────
#
# Calling the ops directly (above) proves the terminators; it does NOT prove the
# human ever SEES them. The driver kept a brief only when it was a dict, and the
# audit family renders its brief as markdown TEXT (audit-preflight) or computes
# one only at the rendezvous (notebook-status) — so both human parks reached the
# human carrying `brief: None`: the NO-GO park silently dropped its blockers and
# their pre-drafted remedies, and the sign-off park asked for a signature on a
# view the chain had built one hop earlier and thrown away. These two tests drive
# the REAL tick and assert on what comes back out of it.


def test_no_go_park_carries_the_preflight_brief_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The NO-GO park hands the human the blockers AND their pre-drafted remedies.

    A decision brief whose whole content is "NO-GO" is not decision-ready — the
    remedies are the reason the verb exists. The preflight renders them as
    markdown for verbatim relay, so the driver must carry that text through
    rather than drop it for not being a dict.
    """
    source, template = _seed_audit(tmp_path)
    # Dirty the committed template: an uncommitted template is an "unsigned
    # template" NO-GO with a remedy the brief pre-drafts.
    (tmp_path / template).write_text(_TEMPLATE + "\n# %%\n# hpc-audit-section: extra\nz = 3\n")
    result = _drive(
        tmp_path,
        monkeypatch,
        audit_id=_AUDIT_ID,
        source=source,
        template=template,
        source_roots=[],
        input_roots=[],
    )
    assert result.action == "awaiting_decision"
    assert result.stage_reached == "preflight_blocked"
    assert result.brief is not None, "the NO-GO park reached the human with no brief at all"
    text = result.brief["text"]
    # VERBATIM: the code-rendered brief, not a driver paraphrase of it.
    assert text.startswith("# audit-preflight — NO-GO")
    assert "remedy:" in text, "the park dropped the pre-drafted remedies"
    # Bound to the verb's OWN remedy constant, so a reworded remedy updates in
    # one place instead of leaving a stale literal here that still "passes".
    from hpc_agent.ops.audit_preflight import _UNSIGNED_REMEDY

    assert _UNSIGNED_REMEDY in text
    # And it is a HUMAN park — the consent affordances the agent branch suppresses
    # are present here, so the suppression is genuinely actor-scoped.
    assert "draft_ask" not in result.brief


def test_signoff_park_carries_the_renders_the_human_must_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sign-off park names each pending section AND the render addressed to it.

    The chain writes the content-addressed trusted-display renders in the
    ``notebook-audit-view`` span; the sign-off rendezvous is one hop later and its
    own verb computes none. Without the pointers threaded forward, the human is
    asked to sign a view they have no way to reach — which is precisely the
    "a link is not a relay" failure inverted.
    """
    source, template = _seed_audit(tmp_path)
    # Modify one section so it becomes human_required (a byte-identical source
    # auto-clears entirely and the chain would run straight to passed).
    (tmp_path / source).write_text(
        _TEMPLATE.replace("y = 2", "y = 2 + 40  # a change only a human may clear"),
        encoding="utf-8",
    )
    result = _drive(
        tmp_path,
        monkeypatch,
        audit_id=_AUDIT_ID,
        source=source,
        template=template,
        source_roots=[],
        input_roots=[],
    )
    assert result.action == "awaiting_decision"
    assert result.current_verb == "notebook-status"
    assert result.stage_reached == "sections_pending"
    assert result.brief is not None, "the sign-off park reached the human with no brief at all"

    pending = result.brief["pending_sections"]
    assert pending, "the park named no pending section"
    changed = next(item for item in pending if item["slug"] == "compute")
    # The pointer the human needs, and the file it addresses must actually exist.
    assert "render_path" in changed, "the sign-off park carried no render pointer"
    assert (tmp_path / changed["render_path"]).is_file()
    assert changed["view_sha12"] and changed["view_sha12"] in changed["render_path"]
    # The bar is NOT restated here — it has exactly one renderer (the view's
    # next-actions footer), and a second copy is how it drifted before.
    assert "append-decision" in result.brief["sign_via"]
    assert "notebook-sign-off" in result.brief["sign_via"]
    # A sign-off is never a chained successor: no verb may stand in for the human.
    assert result.next_verb is None


def test_signoff_brief_is_empty_on_a_pass(tmp_path: Path) -> None:
    """Nothing is being asked on a PASS, so the brief asserts nothing."""
    source, template = _seed_audit(tmp_path)
    from hpc_agent._wire.actions.notebook_auto_clear import NotebookAutoClearSpec
    from hpc_agent.ops.notebook.auto_clear_op import notebook_auto_clear

    notebook_auto_clear(
        experiment_dir=tmp_path,
        spec=NotebookAutoClearSpec(audit_id=_AUDIT_ID, source=source, template=template),
    )
    result = notebook_status(
        experiment_dir=tmp_path,
        spec=NotebookStatusSpec(audit_id=_AUDIT_ID, source=source, template=template),
    )
    assert result.passed is True
    assert result.brief == {}


def test_status_brief_is_absent_without_the_chain_and_says_so(tmp_path: Path) -> None:
    """A standalone status still briefs the human — and is HONEST about the gap.

    Called outside the chain there are no render pointers to carry, so the brief
    names the pending sections but says plainly that no renders were carried,
    rather than emitting a plausible-looking path nobody wrote.
    """
    source, template = _seed_audit(tmp_path)
    result = notebook_status(
        experiment_dir=tmp_path,
        spec=NotebookStatusSpec(audit_id=_AUDIT_ID, source=source, template=template),
    )
    assert result.brief["pending_sections"]
    assert all("render_path" not in item for item in result.brief["pending_sections"])
    assert "no render pointers" in result.brief["renders"]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "REGEN DEBT, declared in docs/internals/regen-debt-ledger.md: the CLI "
        "--spec seam pre-validates against the CHECKED-IN JSON schemas, and the 9 "
        "schema files this wave moved are deliberately not rebaked on this branch "
        "(regen runs serially, once, at integration). Strict on purpose — the "
        "moment `regen_all --write` runs, this XPASSes and turns hard-red, which "
        "is the prompt to delete the marker and the ledger row together."
    ),
)
def test_end_to_end_through_the_real_cli_spec_seam(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same drive, but with every span dispatched through ``hpc-agent <verb> --spec``.

    This is the leg the registry-dispatch drive above deliberately does not cover:
    that the JSON SCHEMAS the CLI validates against accept the specs the chain
    composes. It cannot pass until the schemas are regenerated — which is exactly
    what makes it the right tripwire for the debt.
    """
    source, template = _seed_audit(tmp_path)
    result = _drive(
        tmp_path,
        monkeypatch,
        via_cli_seam=True,
        audit_id=_AUDIT_ID,
        source=source,
        template=template,
        source_roots=[],
        input_roots=[],
    )
    assert result.action == "terminal", result.reason
    assert result.current_verb == "audit-handoff"
