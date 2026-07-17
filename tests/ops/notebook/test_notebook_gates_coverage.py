"""Behaviour-pinning mutation coverage for the notebook-audit GATE layer.

The trust decisions above the attestation kernel — the ones that actually let (or
refuse) an audited ``.py`` at the submit boundary:

* the **sign-off authorship gate** + the **tiered diff-token bar**
  (``ops/decision/journal/signoff.py``): a bare ack refused, an un-named section
  refused, a HUMAN_REQUIRED section signed WITHOUT engaging a diff identifier
  refused, and a genuine engaging sign-off accepted;
* the **trusted-display render lock** (same gate): the content-addressed render
  the human signs must exist and be CURRENT (missing / stale → refused);
* the **D-attention tier threshold** (``ops/notebook/audit_view.py`` ``_tier``):
  the exact ``inherited ∧ no-flags ∧ green`` bar — a token-count mutation flips
  audited/unaudited;
* the **graduation gate** (``ops/notebook_gate.py``): ``SourceUnaudited`` fires at
  BOTH synchronous submit seats (pre-sidecar resolve, pre-staging submit-flow),
  and the ``audit_currency`` moved-count disclosure.

A silent bug in any of these lets unaudited source through (or falsely blocks
audited source). Sign-offs are journaled through the REAL ``append_decision`` gate
(the friction tier, zero declared actors, no utterance log); graduation-gate
fixtures append raw sign-off records (bypassing T8) exactly as
``tests/ops/test_notebook_gate.py`` does.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.ops.decision.journal import append_decision as gated_append_decision
from hpc_agent.ops.notebook.audit_view import (
    ADDED,
    AUTO_CLEARED,
    HUMAN_REQUIRED,
    INHERITED,
    MODIFIED,
    _tier,
)
from hpc_agent.ops.notebook.canonical import build_canonical_view, read_recorded_config
from hpc_agent.ops.notebook.render_store import write_render
from hpc_agent.ops.notebook_gate import assert_source_audited, audit_currency
from hpc_agent.state.decision_journal import append_decision, read_decisions

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.ops.notebook.audit_view import SectionView

_AUDIT = "gate-audit"
_SECTION = "model-fit"
_MARKER = {"authorship_evidence": "missing"}

_TEMPLATE = """# %%
# hpc-audit-section: model-fit
model = fit(data)
"""


def _source(reg: str = "0.5") -> str:
    """A one-section source MODIFIED from the template (a `regularization` kwarg
    added → a nonempty diff-from-template → HUMAN_REQUIRED, with `regularization`
    as an engageable diff identifier)."""
    return f"""# %%
# hpc-audit-section: model-fit
model = fit(data, regularization={reg})
"""


# ── sign-off gate helpers (the REAL T8 gate, friction tier) ───────────────────


def _write_notebook(exp: Path, source_text: str) -> None:
    (exp / "source.py").write_text(source_text, encoding="utf-8")
    (exp / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    (exp / "interview.json").write_text(
        json.dumps(
            {
                "goal": "fit the model",
                "audited_source": {
                    "source": "source.py",
                    "template": "template.py",
                    "audit_id": _AUDIT,
                },
            }
        ),
        encoding="utf-8",
    )


def _canonical_section(exp: Path) -> SectionView:
    """The section's CANONICAL view — the exact object the gate recomputes and the
    render store writes, so a written render + a signed view_sha agree by
    construction (the gate's one-definition guarantee)."""
    cfg = read_recorded_config(exp, _AUDIT)
    view = build_canonical_view(
        exp,
        audit_id=_AUDIT,
        source_relpath="source.py",
        template_relpath="template.py",
        cfg=cfg,
    )
    return next(v for v in view.sections if v.slug == _SECTION)


def _signoff(exp: Path, response: str, *, section_sha: str, view_sha: str) -> Any:
    return gated_append_decision(
        experiment_dir=exp,
        spec=AppendDecisionInput.model_validate(
            {
                "scope_kind": "notebook",
                "scope_id": _AUDIT,
                "block": "notebook-sign-off",
                "response": response,
                "resolved": {
                    "audit_id": _AUDIT,
                    "section": _SECTION,
                    "section_sha": section_sha,
                    "view_sha": view_sha,
                },
            }
        ),
    )


def _marker_of(exc: BaseException) -> Any:
    return getattr(exc, "failure_features", None)


# ── authorship floor: a bare ack cannot sign off (a HUMAN act) ────────────────


def test_signoff_bare_ack_refused_with_authorship_marker(tmp_path: Path) -> None:
    """A bare ``y`` cannot sign off a section — refused at the base authorship
    floor, and the refusal carries the E2 authorship-missing marker (the popup
    cue). Kills a mutant that drops the ``_is_bare_ack`` leg."""
    _write_notebook(tmp_path, _source())
    sv = _canonical_section(tmp_path)
    write_render(tmp_path, audit_id=_AUDIT, view=sv)  # render present → not the render leg
    with pytest.raises(errors.SpecInvalid) as ei:
        _signoff(tmp_path, "y", section_sha=sv.section_sha, view_sha=sv.view_sha)
    assert "bare" in str(ei.value)
    assert _marker_of(ei.value) == _MARKER


def test_signoff_not_naming_the_slug_refused(tmp_path: Path) -> None:
    """A non-bare response that does NOT name the section slug is refused — a
    generic ack cannot attest a specific section (token-exact naming leg)."""
    _write_notebook(tmp_path, _source())
    sv = _canonical_section(tmp_path)
    write_render(tmp_path, audit_id=_AUDIT, view=sv)
    with pytest.raises(errors.SpecInvalid) as ei:
        _signoff(tmp_path, "looks solid, ship it", section_sha=sv.section_sha, view_sha=sv.view_sha)
    assert "NAME the section" in str(ei.value)
    assert _marker_of(ei.value) == _MARKER


# ── the tiered diff-token bar (the exact engagement threshold) ────────────────
# A HUMAN_REQUIRED section demands the sign-off ENGAGE the change: name at least
# ONE identifier from the section's diff. A response that names the slug but
# engages ZERO diff identifiers is refused; the SAME response plus one diff
# identifier passes. This is the token threshold whose mutation flips
# audited/unaudited.


def test_signoff_human_required_without_engaging_a_diff_token_refused(tmp_path: Path) -> None:
    _write_notebook(tmp_path, _source())
    sv = _canonical_section(tmp_path)
    assert sv.tier == HUMAN_REQUIRED  # the bar only raises here
    write_render(tmp_path, audit_id=_AUDIT, view=sv)
    # Names the slug (passes the floor + render lock + view recompute) but engages
    # NO identifier from the diff (`regularization` / `data`) → refused at the bar.
    with pytest.raises(errors.SpecInvalid) as ei:
        _signoff(
            tmp_path,
            "model-fit reviewed and it looks correct to me",
            section_sha=sv.section_sha,
            view_sha=sv.view_sha,
        )
    assert "HUMAN-REQUIRED" in str(ei.value)
    assert _marker_of(ei.value) == _MARKER


def test_signoff_human_required_engaging_a_diff_token_accepted(tmp_path: Path) -> None:
    _write_notebook(tmp_path, _source())
    sv = _canonical_section(tmp_path)
    write_render(tmp_path, audit_id=_AUDIT, view=sv)
    # The SAME shape of response, now engaging the `regularization` diff identifier
    # → the bar is satisfied and the sign-off lands.
    _signoff(
        tmp_path,
        "model-fit reviewed — the regularization term is sound",
        section_sha=sv.section_sha,
        view_sha=sv.view_sha,
    )
    recs = read_decisions(tmp_path, "notebook", _AUDIT)
    assert any(r.get("block") == "notebook-sign-off" for r in recs)


# ── trusted-display render lock (the render the human signs is byte-bound) ────


def test_signoff_refused_when_render_artifact_missing(tmp_path: Path) -> None:
    """No content-addressed render on disk → the sign-off is refused: the chat
    view is model-carried, only the code-written render is trusted. A STRUCTURAL
    refusal — it must NOT carry the authorship marker (re-eliciting cannot fix a
    missing render)."""
    _write_notebook(tmp_path, _source())
    sv = _canonical_section(tmp_path)  # deliberately NOT written to the render store
    with pytest.raises(errors.SpecInvalid) as ei:
        _signoff(
            tmp_path,
            "model-fit reviewed — the regularization term is sound",
            section_sha=sv.section_sha,
            view_sha=sv.view_sha,
        )
    assert "trusted-display lock" in str(ei.value)
    assert _marker_of(ei.value) != _MARKER


def test_signoff_refused_when_render_is_stale(tmp_path: Path) -> None:
    """Render written, then the source edited: signing at the CURRENT section_sha
    (passes the bind) but the OLD view_sha (the old render, whose header
    section_sha no longer matches the recomputed source) is refused STALE — the
    edit-after-render case the record's own bind cannot see."""
    _write_notebook(tmp_path, _source("0.5"))
    sv_old = _canonical_section(tmp_path)
    write_render(tmp_path, audit_id=_AUDIT, view=sv_old)

    # Edit the section body — its sha AND its view_sha move; still HUMAN_REQUIRED.
    (tmp_path / "source.py").write_text(_source("0.9"), encoding="utf-8")
    sv_new = _canonical_section(tmp_path)
    assert sv_new.section_sha != sv_old.section_sha
    assert sv_new.view_sha != sv_old.view_sha

    with pytest.raises(errors.SpecInvalid) as ei:
        _signoff(
            tmp_path,
            "model-fit reviewed — the regularization term is sound",
            section_sha=sv_new.section_sha,  # current → passes the bind lock
            view_sha=sv_old.view_sha,  # the STALE render the human 'saw'
        )
    assert "STALE" in str(ei.value)
    assert _marker_of(ei.value) != _MARKER


# ── the D-attention tier threshold (audit_view._tier) ─────────────────────────
# auto_cleared iff inherited ∧ flags_count == 0 ∧ green. Each leg pinned so a
# widened count / dropped conjunct is killed — a section that should stay
# human_required (unaudited-until-signed) must never flip to auto_cleared.


def test_tier_threshold_is_inherited_and_zero_flags_and_green() -> None:
    assert _tier(INHERITED, 0, True) == AUTO_CLEARED
    # ONE lint flag flips it — the `flags_count == 0` boundary (kills == → <= / >=).
    assert _tier(INHERITED, 1, True) == HUMAN_REQUIRED
    # A modified / added section is never auto_cleared, however clean.
    assert _tier(MODIFIED, 0, True) == HUMAN_REQUIRED
    assert _tier(ADDED, 0, True) == HUMAN_REQUIRED
    # Ungreen assertions block the clear even when inherited + unflagged.
    assert _tier(INHERITED, 0, False) == HUMAN_REQUIRED


# ── graduation gate: SourceUnaudited on the core reduction ────────────────────

_GATE_AUDIT = "grad-audit"
_GATE_MODULE = (
    "# %%\n"
    "# hpc-audit-section: setup\n"
    "import numpy as np\n"
    "\n"
    "# %%\n"
    "# hpc-audit-section: run\n"
    "result = int(np.array([1]).sum())\n"
)


def _write_audited(exp: Path, *, opted_in: bool = True, module: str = _GATE_MODULE) -> None:
    (exp / "source.py").write_text(module, encoding="utf-8")
    (exp / "template.py").write_text(module, encoding="utf-8")
    doc: dict[str, Any] = {"goal": "g"}
    if opted_in:
        doc["audited_source"] = {
            "source": "source.py",
            "template": "template.py",
            "audit_id": _GATE_AUDIT,
        }
    (exp / "interview.json").write_text(json.dumps(doc), encoding="utf-8")


def _section_sha(slug: str, module: str = _GATE_MODULE) -> str:
    from hpc_agent.state.audit_source import parse_percent_source

    return next(s.section_sha for s in parse_percent_source(module).sections if s.slug == slug)


def _raw_sign(exp: Path, slug: str, *, module: str = _GATE_MODULE) -> None:
    """A HUMAN sign-off at the section's CURRENT sha, appended RAW (bypasses the
    T8 authorship gate) — the ``tests/ops/test_notebook_gate.py`` fixture posture."""
    sha = _section_sha(slug, module)
    append_decision(
        exp,
        scope_kind="notebook",
        scope_id=_GATE_AUDIT,
        block="notebook-sign-off",
        response="y",
        resolved={
            "audit_id": _GATE_AUDIT,
            "section": slug,
            "section_sha": sha,
            "view_sha": "view-" + sha[:8],
        },
    )


def test_gate_unsigned_section_raises_source_unaudited(tmp_path: Path) -> None:
    """One required section unsigned → SourceUnaudited naming ONLY it, with the
    precondition_failed / non-retry-safe envelope. Kills the `if failures` and
    `status not in PASSING_STATUSES` mutants."""
    _write_audited(tmp_path)
    _raw_sign(tmp_path, "setup")  # 'run' left unsigned
    with pytest.raises(errors.SourceUnaudited) as ei:
        assert_source_audited(tmp_path)
    msg = str(ei.value)
    assert "run" in msg
    assert "unsigned" in msg
    assert "setup" not in msg  # a signed section is never named
    assert ei.value.error_code == "precondition_failed"
    assert ei.value.retry_safe is False


def test_gate_signed_then_edited_section_named_signed_stale(tmp_path: Path) -> None:
    """Sign both, then EDIT one section: its sign-off reads SIGNED_STALE (drift =
    unsigned by construction) and the gate names it with THAT status — the
    recompute-drift verdict flows into the refusal."""
    _write_audited(tmp_path)
    _raw_sign(tmp_path, "setup")
    _raw_sign(tmp_path, "run")
    edited = _GATE_MODULE.replace(
        "result = int(np.array([1]).sum())", "result = 1  # edited by hand"
    )
    (tmp_path / "source.py").write_text(edited, encoding="utf-8")
    with pytest.raises(errors.SourceUnaudited) as ei:
        assert_source_audited(tmp_path)
    msg = str(ei.value)
    assert "run" in msg
    assert "signed_stale" in msg


def test_gate_all_signed_passes(tmp_path: Path) -> None:
    """The companion: every required section signed current → no raise."""
    _write_audited(tmp_path)
    _raw_sign(tmp_path, "setup")
    _raw_sign(tmp_path, "run")
    assert_source_audited(tmp_path)  # no raise


def test_gate_not_opted_in_is_silent_noop(tmp_path: Path) -> None:
    """No audited_source block → byte-identical no-op even with nothing signed
    (the D7 fail-safe: the gate fires ONLY inside the opted-in surface)."""
    _write_audited(tmp_path, opted_in=False)
    assert_source_audited(tmp_path)  # no raise


# ── graduation gate: BOTH synchronous submit seats ────────────────────────────
# The ONE definition is wired at two seats; each must call it BEFORE its
# cluster/side-effect work. These pin that the seat actually invokes the gate
# (removing the call from a seat is caught here).

_RESOLVE_SEAM = "hpc_agent.ops.resolve_submit_inputs"


def _resolve_atom_mocks(exp: Path):
    """Mock the laptop-side atoms so resolve-submit-inputs reaches its pre-sidecar
    gate seat (mirrors tests/ops/test_notebook_gate.py)."""
    from hpc_agent._wire.actions.build_submit_spec import BuildSubmitSpecInput
    from hpc_agent._wire.actions.write_run_sidecar import WriteRunSidecarInput
    from hpc_agent._wire.workflows.resolve_submit_inputs import ResolveSubmitInputsSpec

    (exp / ".hpc").mkdir(parents=True, exist_ok=True)
    (exp / ".hpc" / "tasks.py").write_text("# stub\n", encoding="utf-8")

    spec = ResolveSubmitInputsSpec(
        run_name="pi",
        submit=BuildSubmitSpecInput(
            profile="pi",
            cluster="h2",
            ssh_target="me@login.h2",
            remote_path="/scratch/me/exp",
            run_id="pi-abcd1234",
            cmd_sha="a" * 64,
            total_tasks=1,
            backend="sge",
        ),
        sidecar=WriteRunSidecarInput(
            run_id="pi-placeholder",
            cmd_sha="0" * 8,
            executor="python -m src.pi",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=1,
        ),
    )
    cr = {
        "run_id": "pi-abcd1234",
        "cmd_sha": "a" * 64,
        "trial_tokens": None,
        "trial_params": [{"x": 1}],
        "total": 1,
    }
    fp = {"found": False, "is_orphan": False, "status": None, "prior_run_id": None, "cluster": None}
    return spec, cr, fp


def test_gate_fires_at_resolve_seat_before_sidecar(tmp_path: Path) -> None:
    """S1 seat (pre-sidecar): an opted-in UNSIGNED source refuses at resolve
    BEFORE the per-run sidecar is written — the write is never reached."""
    from hpc_agent.ops.resolve_submit_inputs import resolve_submit_inputs

    spec, cr, fp = _resolve_atom_mocks(tmp_path)
    _write_audited(tmp_path)  # opted in, nothing signed

    def _no_sidecar(*_a: Any, **_k: Any) -> None:
        raise AssertionError("write_run_sidecar must not run — the gate is pre-sidecar")

    with (
        mock.patch(f"{_RESOLVE_SEAM}.compute_run_id", return_value=cr),
        mock.patch(f"{_RESOLVE_SEAM}.find_prior_run", return_value=fp),
        mock.patch(f"{_RESOLVE_SEAM}.build_submit_spec", return_value={"run_id": "pi-abcd1234"}),
        mock.patch(f"{_RESOLVE_SEAM}.write_run_sidecar", _no_sidecar),
        pytest.raises(errors.SourceUnaudited, match="run"),
    ):
        resolve_submit_inputs(tmp_path, spec=spec)


def _submit_flow_spec(run_id: str = "pi-abcd1234"):
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec, SubmitResources

    return SubmitFlowSpec(
        profile="pi",
        cluster="hoffman2",
        ssh_target="user@h",
        remote_path="/u/scratch/exp",
        job_name="pi",
        run_id=run_id,
        total_tasks=10,
        backend="slurm",
        script=".hpc/templates/cpu_array.sh",
        canary=False,
        job_env={"K": "v"},
        resources=SubmitResources(walltime_sec=3600, cpus=4),
    )


def test_gate_fires_at_submit_flow_seat_before_staging(
    tmp_path: Path, journal_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-staging seat: an opted-in UNSIGNED source refuses in submit-flow BEFORE
    any rsync/deploy — the shared staging prelude is never reached."""
    from hpc_agent.ops import submit_flow as sf
    from hpc_agent.ops.submit_flow import submit_flow

    _write_audited(tmp_path)  # opted in, nothing signed

    def _no_staging(*_a: Any, **_k: Any) -> None:
        raise AssertionError("_run_shared_prelude must not run — the gate is pre-staging")

    monkeypatch.setattr(sf, "_run_shared_prelude", _no_staging)

    with pytest.raises(errors.SourceUnaudited, match="run"):
        submit_flow(tmp_path, spec=_submit_flow_spec())


# ── audit_currency: the S1 moved-count disclosure ─────────────────────────────
# The disclosure seam that mirrors notebook-status: (audit_id, moved) where moved
# counts required sections NOT signed-current. A mutation of the `not in
# PASSING_STATUSES` count would misreport currency; not opted in → None (D7).


def test_audit_currency_not_opted_in_is_none(tmp_path: Path) -> None:
    _write_audited(tmp_path, opted_in=False)
    assert audit_currency(tmp_path) is None


def test_audit_currency_all_signed_reports_zero_moved(tmp_path: Path) -> None:
    _write_audited(tmp_path)
    _raw_sign(tmp_path, "setup")
    _raw_sign(tmp_path, "run")
    assert audit_currency(tmp_path) == (_GATE_AUDIT, 0)


def test_audit_currency_counts_each_unsigned_section(tmp_path: Path) -> None:
    _write_audited(tmp_path)
    _raw_sign(tmp_path, "setup")  # 'run' unsigned → exactly one moved
    assert audit_currency(tmp_path) == (_GATE_AUDIT, 1)
