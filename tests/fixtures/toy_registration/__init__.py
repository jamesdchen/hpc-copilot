"""Toy registration substrate — the T10 first-consumer fixture builders.

A deliberately DUMB widget-batch lineage exercising the registration kernel end
to end (``docs/design/registration-kernel.md`` T10): a real run's dossier, a real
prerequisite chain (a notebook audit + a reproduction receipt over the run), and
the register → verify → drift → re-register → revoke round-trip. Every sha the
gate recomputes is produced from REAL substrate here — nothing is stubbed.

Toy vocabulary ONLY (``widget-owner`` / ``jam-threshold`` field slugs, the widget
lineage). Never harxhar/quant words — real domain vocabulary in a fixture reads
as core knowledge to the next maintainer (R4 mechanism #4).

The builders are plain functions (not pytest fixtures) so a test composes them:

* :func:`build_substrate` lays down the audit source/template + signed sections,
  the registered run's sidecar, and the reproduction run's sidecar + receipt.
* :func:`resign_audit` re-signs both audit sections at a new source (the remedy
  after an edit drifts the audit).
* :func:`register` computes every leg from the LIVE stores (via the ONE dossier
  signature seam + ``check_chain`` + the pre-append ``build_view`` projection, the
  drift-log's ``registered_at=None`` wiring) and appends the registration through
  the gated ``append-decision`` — the maximal human ceremony, gate and all.
* :func:`revoke` appends a ``registration-revoke`` with a mandatory reason.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent._wire.actions.verify_registration import (
    DossierLeg,
    FieldsBlock,
    PrerequisiteLeg,
    TemplateLeg,
)
from hpc_agent.ops import export_dossier
from hpc_agent.ops.decision.journal import append_decision as _ops_append_decision
from hpc_agent.ops.registration import prereqs
from hpc_agent.ops.registration.verify_op import build_view
from hpc_agent.state.audit_source import parse_percent_source, sha256_normalized
from hpc_agent.state.decision_journal import append_decision as _state_append_decision
from hpc_agent.state.registration import CURRENT, parse_chain_entry
from hpc_agent.state.runs import write_run_sidecar

# ── the toy identities ──────────────────────────────────────────────────────

REG_ID = "reg-widget-batch"
RUN_ID = "widget-batch-run"  # the ORIGINAL run — the registration's dossier subject
REPRO_RUN = "widget-batch-repro"  # the reproduction run whose receipt cross-links RUN_ID
AUDIT_ID = "widget-batch-audit"
TEMPLATE_REL = "registration_template.json"

AUDIT_SLOT = "audit-slot"
REPRO_SLOT = "repro-slot"

FIELD_SLUGS = ["widget-owner", "jam-threshold"]
FIELDS = {"widget-owner": "wanda", "jam-threshold": "5"}

# The caller-authored registration template (mirrors examples/toy_registration/).
TEMPLATE: dict[str, Any] = {
    "fields": list(FIELD_SLUGS),
    "prerequisites": [
        {"slot": AUDIT_SLOT, "kind": "notebook-audit"},
        {"slot": REPRO_SLOT, "kind": "reproduction"},
    ],
}

# ── the audited notebook source + template (percent-format) ──────────────────

AUDIT_TEMPLATE_PY = """\
# %%
# hpc-audit-section: widget-load
pass

# %%
# hpc-audit-section: widget-jam
pass
"""

SOURCE_PY = """\
# %%
# hpc-audit-section: widget-load
crate = load_crate("widgets.csv")

# %%
# hpc-audit-section: widget-jam
jam = compute_jam(crate)
"""

SOURCE_PY_EDITED = """\
# %%
# hpc-audit-section: widget-load
crate = load_crate("widgets.csv")

# %%
# hpc-audit-section: widget-jam
jam = compute_jam(crate, tighten=True)
"""


def _section_sha(source: str, slug: str) -> str:
    parsed = parse_percent_source(source)
    return next(s.section_sha for s in parsed.sections if s.slug == slug)


def _sign_sections(experiment_dir: Path, *, sign_at: str) -> None:
    """Sign both audit sections at the *sign_at* source (state layer — no gate).

    The registration gate reads the RESULTING journal through ``check_chain``; the
    sign-offs themselves are ordinary attestations written straight to the state
    store (exactly as ``tests/ops/registration/test_prereqs.py`` builds them).
    """
    for slug in ("widget-load", "widget-jam"):
        _state_append_decision(
            experiment_dir,
            scope_kind="notebook",
            scope_id=AUDIT_ID,
            block="notebook-sign-off",
            response="y",
            resolved={
                "audit_id": AUDIT_ID,
                "section": slug,
                "section_sha": _section_sha(sign_at, slug),
                "view_sha": "widget-view",
            },
        )


def build_substrate(experiment_dir: Path, *, source: str = SOURCE_PY) -> None:
    """Lay down the whole toy substrate: audit + registered run + reproduction."""
    experiment_dir = Path(experiment_dir)

    # (1) the audited notebook: source/template .py + interview echo + signatures.
    (experiment_dir / "source.py").write_text(source, encoding="utf-8")
    (experiment_dir / "template.py").write_text(AUDIT_TEMPLATE_PY, encoding="utf-8")
    (experiment_dir / "interview.json").write_text(
        json.dumps(
            {
                "audited_source": {
                    "source": "source.py",
                    "template": "template.py",
                    "audit_id": AUDIT_ID,
                }
            }
        ),
        encoding="utf-8",
    )
    _sign_sections(experiment_dir, sign_at=source)

    # (2) the registration template on disk (bind-as-data, raw-bytes sha'd).
    (experiment_dir / TEMPLATE_REL).write_text(json.dumps(TEMPLATE, indent=2), encoding="utf-8")

    # (3) the registered (original) run's sidecar — the dossier subject.
    write_run_sidecar(
        experiment_dir,
        run_id=RUN_ID,
        cmd_sha="widget-batch-cmd",
        hpc_agent_version="0.0.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="run.py",
        result_dir_template="results/{i}",
        task_count=1,
        tasks_py_sha="widget-batch-code",
    )

    # (4) the reproduction run: a sidecar (for the no-code-drift check) + a receipt
    #     whose ORIGINAL names RUN_ID (the dossier cross-link).
    write_run_sidecar(
        experiment_dir,
        run_id=REPRO_RUN,
        cmd_sha="widget-batch-cmd",
        hpc_agent_version="0.0.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="run.py",
        result_dir_template="results/{i}",
        task_count=1,
        tasks_py_sha="widget-batch-code",
    )
    _write_receipt(experiment_dir)


def _write_receipt(experiment_dir: Path) -> dict[str, Any]:
    receipt = {
        "ts": "2026-01-01T00:00:00Z",
        "overall": "match",
        "original": {"run_id": RUN_ID, "tasks_py_sha": "widget-batch-code"},
        "repro": {"run_id": REPRO_RUN, "tasks_py_sha": "widget-batch-code"},
    }
    from hpc_agent.ops.verify_reproduction import _receipt_path

    rpath = _receipt_path(experiment_dir, REPRO_RUN)
    rpath.parent.mkdir(parents=True, exist_ok=True)
    rpath.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    return receipt


def resign_audit(experiment_dir: Path, *, source: str) -> None:
    """Re-sign both audit sections at *source* (the remedy after a drift edit)."""
    _sign_sections(Path(experiment_dir), sign_at=source)


def _chain(experiment_dir: Path) -> list[dict[str, Any]]:
    """The prerequisite chain, content-shas recomputed from the LIVE stores."""
    experiment_dir = Path(experiment_dir)
    source_on_disk = (experiment_dir / "source.py").read_text(encoding="utf-8")
    module_sha = sha256_normalized(source_on_disk)
    receipt = prereqs._newest_receipt(experiment_dir, REPRO_RUN)
    assert receipt is not None, "toy substrate: reproduction receipt missing"
    receipt_sha = prereqs.canonical_sha(receipt)
    return [
        {
            "slot": AUDIT_SLOT,
            "kind": "notebook-audit",
            "subject_id": AUDIT_ID,
            "content_sha": module_sha,
        },
        {
            "slot": REPRO_SLOT,
            "kind": "reproduction",
            "subject_id": REPRO_RUN,
            "content_sha": receipt_sha,
        },
    ]


def register(experiment_dir: Path, *, response: str | None = None) -> Any:
    """Register the widget batch through the gated ``append-decision`` (R6).

    Computes every recompute leg from the live stores — the dossier signature via
    the ONE seam, the chain verdicts via ``check_chain``, the template raw-bytes
    sha — then renders the pre-append ``view_sha`` with ``registered_at=None`` (the
    drift-log wiring: a POST-registration verify reads a timestamp and would bind a
    different witness, so the human's binding witness derives from THIS pre-append
    projection). Appends through the ops gate, which independently recomputes and
    must agree. Returns the ``AppendDecisionResult``.
    """
    experiment_dir = Path(experiment_dir)
    chain = _chain(experiment_dir)

    sig = export_dossier.compute_dossier_signature(experiment_dir, RUN_ID)
    entries = [parse_chain_entry(c) for c in chain]
    verdicts = prereqs.check_chain(experiment_dir, entries, dossier_run_ids=set(sig.run_ids))
    assert all(v.status == CURRENT for v in verdicts), (
        f"toy substrate: expected every prerequisite CURRENT before register; got "
        f"{[(v.slot, v.status) for v in verdicts]}"
    )

    template_sha = hashlib.sha256((experiment_dir / TEMPLATE_REL).read_bytes()).hexdigest()

    # Build the pre-append view exactly as the gate does (all legs CURRENT).
    dossier_leg = DossierLeg(
        recorded_sha=sig.bundle_sha256, recomputed_sha=sig.bundle_sha256, drifted_stores=[]
    )
    template_leg = TemplateLeg(
        status="current", recorded_sha=template_sha, recomputed_sha=template_sha
    )
    prereq_legs = [
        PrerequisiteLeg(
            slot=v.slot,
            kind=v.kind,  # type: ignore[arg-type]
            status=v.status,  # type: ignore[arg-type]
            recorded_sha=v.recorded_sha,
            recomputed_sha=v.recomputed_sha,
            evidence_note=v.evidence_note,
        )
        for v in verdicts
    ]
    declared = list(FIELD_SLUGS)
    fields_block = FieldsBlock(declared=declared, present=declared, missing=[])
    _, view_sha = build_view(
        status=CURRENT,
        registration_id=REG_ID,
        registered_at=None,
        dossier=dossier_leg,
        template=template_leg,
        prerequisites=prereq_legs,
        fields=fields_block,
    )

    module_sha = chain[0]["content_sha"]
    if response is None:
        response = f"register {REG_ID} — signed off after reviewing prerequisite {module_sha[:12]}"

    return _ops_append_decision(
        experiment_dir=experiment_dir,
        spec=AppendDecisionInput(
            scope_kind="registration",
            scope_id=REG_ID,
            block="registration",
            response=response,
            resolved={
                "registration_id": REG_ID,
                "run_id": RUN_ID,
                "dossier_sha": sig.bundle_sha256,
                "template": TEMPLATE_REL,
                "template_sha": template_sha,
                "fields": dict(FIELDS),
                "prerequisites": chain,
                "view_sha": view_sha,
            },
        ),
    )


def revoke(experiment_dir: Path, *, reason: str) -> Any:
    """Append a ``registration-revoke`` (human, non-bare, mandatory reason, R7)."""
    return _ops_append_decision(
        experiment_dir=Path(experiment_dir),
        spec=AppendDecisionInput(
            scope_kind="registration",
            scope_id=REG_ID,
            block="registration-revoke",
            response=f"revoke {REG_ID} — {reason}",
            resolved={"registration_id": REG_ID, "reason": reason},
        ),
    )
