"""Toy live-conformance substrate — the T10 instrument-QC fixture builders.

A deliberately DUMB fake-instrument (``sensor-7``) calibration scenario exercising
live conformance end to end (``docs/design/live-conformance.md`` T10): a real
run's dossier sealing a calibration-readings baseline, a real registration through
the gated ``append-decision`` carrying a ``conformance`` declaration, and the
record → status → drift → verdict → horizon → review → re-register loop. Every
sha the gate recomputes is produced from REAL substrate here — nothing is stubbed.

Toy vocabulary ONLY: a ``sensor-7`` instrument, a ``reading`` metric key, a
``calibration`` baseline. Never trading vocabulary; never harxhar/quant words —
real domain vocabulary in a fixture reads as core knowledge to the next maintainer.

The builders are plain functions (not pytest fixtures) so a test composes them:

* :func:`build_substrate` writes the registered run's sidecar and seals the
  calibration baseline as an aggregated artifact (a dossier MEMBER by construction).
* :func:`register` computes the dossier signature from the LIVE stores and appends
  a registration carrying the ``conformance`` declaration through the gated
  ``append-decision`` — baseline membership, view_sha, and all.
* :func:`record` journals one live observation through the ``conformance-record``
  verb (the emitter surface).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hpc_agent._wire.actions.conformance_record import ConformanceRecordSpec
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent._wire.actions.verify_registration import (
    FieldsBlock,
    PrerequisiteLeg,
    TemplateLeg,
)
from hpc_agent.ops import export_dossier
from hpc_agent.ops.conformance.record_op import conformance_record
from hpc_agent.ops.decision.journal import append_decision as _ops_append_decision
from hpc_agent.ops.registration import prereqs
from hpc_agent.ops.registration.verify_op import build_view
from hpc_agent.state.decision_journal import append_decision as _state_append_decision
from hpc_agent.state.registration import CURRENT, parse_chain_entry
from hpc_agent.state.runs import write_run_sidecar

# ── the toy identities ──────────────────────────────────────────────────────

REG_ID = "reg-sensor-7"
RUN_ID = "sensor-cal-run"
TEMPLATE_REL = "registration_template.json"
FIELD_SLUGS = ["instrument-owner"]
FIELDS = {"instrument-owner": "wanda"}
KEY = "reading"
MIN_WINDOW_N = 3
REVIEW_HORIZON = "2027-01-01T00:00:00Z"

# The sealed calibration baseline: readings in [20.0, 21.0], n=5 (well-evidenced).
BASELINE_ROWS: list[dict[str, Any]] = [
    {"reading": 20.0},
    {"reading": 20.5},
    {"reading": 21.0},
    {"reading": 20.2},
    {"reading": 20.8},
]
# On-disk under _aggregated/<run>/ so compute_dossier_signature seals its bytes as a
# dossier member; the DECLARATION names this EXPERIMENT-RELATIVE path (the read-side
# locator T5/T8 resolve), and T7 checks membership by the sealed-bytes sha.
_BASELINE_ON_DISK = f"_aggregated/{RUN_ID}/calibration.json"
BASELINE_REL = _BASELINE_ON_DISK

# One generic attestation prerequisite (the gate requires a non-empty chain): a
# calibration sign-off record the attestation checker finds by its content_sha.
ATTEST_SLOT = "cal-slot"
ATTEST_SCOPE = "cal-attest"
ATTEST_SUBJECT_ID = f"notebook:{ATTEST_SCOPE}"
ATTEST_SHA = "a" * 64

TEMPLATE: dict[str, Any] = {
    "fields": list(FIELD_SLUGS),
    "prerequisites": [{"slot": ATTEST_SLOT, "kind": "attestation"}],
}


def _baseline_bytes() -> bytes:
    return json.dumps(BASELINE_ROWS).encode("utf-8")


def build_substrate(experiment_dir: Path) -> None:
    """Write the registered run's sidecar and seal the calibration baseline."""
    experiment_dir = Path(experiment_dir)
    write_run_sidecar(
        experiment_dir,
        run_id=RUN_ID,
        cmd_sha="sensor-cal-cmd",
        hpc_agent_version="0.0.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="calibrate.py",
        result_dir_template="results/{i}",
        task_count=1,
        tasks_py_sha="sensor-cal-code",
    )
    baseline = experiment_dir / _BASELINE_ON_DISK
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_bytes(_baseline_bytes())
    (experiment_dir / TEMPLATE_REL).write_text(json.dumps(TEMPLATE, indent=2), encoding="utf-8")

    # The calibration attestation the one prerequisite resolves against (written
    # straight to the state store — the substrate, not a gated human act).
    _state_append_decision(
        experiment_dir,
        scope_kind="notebook",
        scope_id=ATTEST_SCOPE,
        block="attest",
        response="calibration attested",
        resolved={"attestor": "human", "content_sha": ATTEST_SHA},
    )


def _chain() -> list[dict[str, Any]]:
    return [
        {
            "slot": ATTEST_SLOT,
            "kind": "attestation",
            "subject_id": ATTEST_SUBJECT_ID,
            "content_sha": ATTEST_SHA,
        }
    ]


def _conformance_block(baseline_sha: str, *, review_horizon: str | None) -> dict[str, Any]:
    block: dict[str, Any] = {
        "baseline": {"path": BASELINE_REL, "sha256": baseline_sha},
        "keys": [KEY],
        "min_window_n": MIN_WINDOW_N,
    }
    if review_horizon is not None:
        block["review_horizon"] = review_horizon
    return block


def register(
    experiment_dir: Path,
    *,
    review_horizon: str | None = REVIEW_HORIZON,
    baseline_sha_override: str | None = None,
) -> Any:
    """Register the instrument through the gated ``append-decision`` (R6 + C-declare).

    Computes the dossier signature from the live stores (the baseline is a sealed
    MEMBER by construction), assembles the ``conformance`` declaration, renders the
    pre-append ``view_sha``, and appends through the ops gate — which independently
    recomputes the dossier sha, the baseline membership, and the view_sha.
    """
    experiment_dir = Path(experiment_dir)
    chain = _chain()
    sig = export_dossier.compute_dossier_signature(experiment_dir, RUN_ID)
    entries = [parse_chain_entry(c) for c in chain]
    verdicts = prereqs.check_chain(experiment_dir, entries, dossier_run_ids=set(sig.run_ids))
    assert all(v.status == CURRENT for v in verdicts), (
        f"toy substrate: expected every prerequisite CURRENT; got "
        f"{[(v.slot, v.status) for v in verdicts]}"
    )
    baseline_sha = (
        baseline_sha_override
        or hashlib.sha256((experiment_dir / _BASELINE_ON_DISK).read_bytes()).hexdigest()
    )
    template_sha = hashlib.sha256((experiment_dir / TEMPLATE_REL).read_bytes()).hexdigest()

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
    _, view_sha = build_view(
        status=CURRENT,
        registration_id=REG_ID,
        registered_at=None,
        dossier=_dossier_leg(sig.bundle_sha256),
        template=TemplateLeg(
            status="current", recorded_sha=template_sha, recomputed_sha=template_sha
        ),
        prerequisites=prereq_legs,
        fields=FieldsBlock(declared=declared, present=declared, missing=[]),
    )
    response = (
        f"register {REG_ID} — signed off after reviewing prerequisite {ATTEST_SHA[:12]} "
        f"and dossier {sig.bundle_sha256[:12]}"
    )
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
                "conformance": _conformance_block(baseline_sha, review_horizon=review_horizon),
            },
        ),
    )


def _dossier_leg(sha: str) -> Any:
    from hpc_agent._wire.actions.verify_registration import DossierLeg

    return DossierLeg(recorded_sha=sha, recomputed_sha=sha, drifted_stores=[])


def record(experiment_dir: Path, *, reading: float, observed_at: str) -> str:
    """Journal one live observation through the ``conformance-record`` verb."""
    result = conformance_record(
        experiment_dir=Path(experiment_dir),
        spec=ConformanceRecordSpec(
            registration_id=REG_ID,
            payload={KEY: reading},
            observed_at=observed_at,
            labels={"emitter": "sensor-7"},
            emitter="sensor-7",
        ),
    )
    return str(result.content_sha)
