"""``conformance-record`` — journal one live conformance observation (T4).

The emitter's journaling surface for live conformance observations
(``docs/design/live-conformance.md`` C-verbs / C-store / C-emitter). Each call
records ONE observation receipt against the registration it tests — a journaled
CODE attestation, sha-linked to the sealed hypothesis: "evidence FOR/AGAINST
registration R" at production cadence (C1). A ``mutate`` verb whose ONLY side
effect is exactly one append to the registration-scoped ledger
(``<experiment>/_aggregated/_conformance/<registration_id>.jsonl``).

**``agent_facing=False`` (C-verbs, recorded rationale).** A human/cron-invoked
CLI verb, never an agent tool — an agent authoring the outcome stream that
judges its own registration is the receipt-laundering class at the OPERATION
boundary (the F1 ingest-verb posture). The emitter is caller machinery, not the
driving agent; core never gains a connector, a credential, or a polling loop
(the agency boundary — this verb OBSERVES and RECORDS, it never actuates).

**The trust boundary (C1 / F8 honesty, verbatim).** The verb BINDS the exact
recorded bytes — the payload ``content_sha`` is recomputed SERVER-SIDE over
``{payload, labels, observed_at}`` and bound at append
(``state/attestation.py::bind`` via the T3 store); a caller CANNOT assert a sha
into existence (there is no sha field on the spec). Truthfulness of the
``payload`` / ``observed_at`` VALUES is the emitter's own — core vouches for the
bytes it hashed, never for the world they describe.

**The recording posture (C-store), by registration status:**

1. **ABSENT** — no records reduce for the id → a LOUD :class:`errors.SpecInvalid`
   (there is no hypothesis to test; the fabrication class).
2. **PRESENT** — the registration's journal reduces to a status
   (``current`` / ``stale`` / ``revoked``) that is STAMPED into
   ``status_at_record``. Recording is **fail-open for evidence**: a
   stale/revoked/superseded registration is RECORDED (production is the
   experiment that never stops; refusing evidence is the one thing an evidence
   system must not do), with the reduced status DISCLOSED, never silently mixed.

**The status-resolution choice (recorded — the honest read).** The status stamp
reuses ``verify-registration``'s reader/facade idioms, but resolves ONLY the
dossier-sha leg — NOT the full four-leg verify view (template, prerequisites,
brief, ``view_sha``): that recompute is the query/verify seat's job. Steps: read
the id's registration journal through the ONE reader
(``read_decisions(exp, "registration", id)``), locate the winner, RECOMPUTE the
live dossier signature via the ONE seam
(:func:`~hpc_agent.ops.export_dossier.compute_dossier_signature`, degrade-to-
``None`` on any gap — the reporter posture), and reduce
(:func:`~hpc_agent.state.registration.reduce_registration`) with that live sha.
The reduction then reports ``current`` when the sealed dossier still hashes to
the sha it bound, ``stale`` when it drifted (or the run vanished), and
``revoked`` when the newest family record is an overturn — the honest live read
at record time. (The horizon-lapse ``stale`` cause of C-horizon / registration
T6 is not landed in this worktree; when it lands, the reduction inherits it here
with no change.)

Cost note (recorded deviation): re-gathering the dossier signature per
observation is the honest price of an honest status stamp; at high production
cadence a caching or caller-verified-status optimization is future work (the
ledger append itself stays O(1)). This is disclosed here rather than papered
over with a false ``current``.

Facade import (the ``verify_op`` idiom): ``ops/export_dossier.py`` is a
TOP-LEVEL ops module reached through the ``from hpc_agent.ops import
export_dossier`` FACADE form — the direct
``from hpc_agent.ops.export_dossier import ...`` spelling trips the subject-
imports lint from inside a subject. The module-level alias keeps
:func:`compute_dossier_signature` a patchable attribute for tests; the recompute
seam :func:`_recompute_dossier_sha` is itself stubbable so tests stay in the
instrument-QC toy vocabulary without building a whole dossier substrate.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.conformance_record import (
    ConformanceRecordResult,
    ConformanceRecordSpec,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import utcnow_iso

# Facade form (the verify_op idiom): reach the top-level ops/export_dossier.py
# module without tripping the subject-imports lint. The module-level alias is a
# patchable attribute for tests.
from hpc_agent.ops import export_dossier
from hpc_agent.state import conformance, conformance_store, registration
from hpc_agent.state.decision_journal import read_decisions

if TYPE_CHECKING:
    from hpc_agent.state.registration import RegistrationStatus

compute_dossier_signature = export_dossier.compute_dossier_signature  # type: ignore[attr-defined]

__all__ = ["conformance_record"]

_PRIMITIVE = "conformance-record"


def _recompute_dossier_sha(experiment_dir: Path, winner: Mapping[str, Any]) -> str | None:
    """Re-gather the winner's LIVE dossier signature, or ``None`` on any gap.

    The ``verify_op._recompute_dossier`` idiom, dossier-sha leg only: the run_id
    names the run; ``include_lineage`` mirrors what the registration recorded. A
    missing/moved run — or any read failure — yields ``None`` (→ the winner reads
    :data:`~hpc_agent.state.registration.STALE`), never a raise: the status stamp
    is a reporter. Stubbable in tests so the toy fixtures need no real dossier.
    """
    run_id = winner.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        return None
    include_lineage = bool(winner.get("include_lineage", False))
    try:
        sig = compute_dossier_signature(experiment_dir, run_id, include_lineage=include_lineage)
    except Exception:  # noqa: BLE001 — a reporter never raises on a moved/absent subject
        return None
    bundle = getattr(sig, "bundle_sha256", None)
    return bundle if isinstance(bundle, str) and bundle else None


def _registered_dossier_sha(records: list[dict[str, Any]], registration_id: str) -> str | None:
    """The NEWEST registration record's recorded ``dossier_sha``, or ``None``.

    Status-independent: it names the sealed hypothesis the observation tests,
    even when the id's newest FAMILY record is an overturn (a revoke binds no
    sha, so :func:`reduce_registration`'s winner carries none). Records are in
    append order — the last match is the newest. Reads the block/id vocabulary
    off ``state/registration.py`` (never a re-spelled literal). This is the
    ``dossier_sha`` STAMPED in the observation (the recorded identity), distinct
    from the LIVE recompute used to reduce the status.
    """
    sha: str | None = None
    for record in records:
        if record.get("block") != registration.REGISTRATION_BLOCK:
            continue
        resolved = record.get("resolved")
        if not isinstance(resolved, Mapping):
            continue
        if resolved.get("registration_id") != registration_id:
            continue
        candidate = resolved.get("dossier_sha")
        if isinstance(candidate, str) and candidate:
            sha = candidate
    return sha


@primitive(
    name=_PRIMITIVE,
    verb="mutate",
    side_effects=[
        SideEffect(
            "file_write",
            "<experiment>/_aggregated/_conformance/<registration_id>.jsonl",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    # Append-only: each call journals one fresh observation line. A re-record
    # appends a new line (the ledger never dedups — every observation is
    # evidence), so retries are safe but not byte-idempotent, like append-decision.
    idempotent=False,
    cli=CliShape(
        help=(
            "Journal ONE live conformance observation against the registration it "
            "tests — the emitter's evidence FOR/AGAINST a sealed hypothesis at "
            "production cadence. Recomputes the payload content_sha SERVER-SIDE over "
            "{payload, labels, observed_at} and binds it (a caller cannot assert a "
            "sha; there is no sha field on the spec), stamps the registration's "
            "reduced status_at_record (the live dossier-sha leg — current/stale/"
            "revoked), and appends exactly one line to "
            "<experiment>/_aggregated/_conformance/<registration_id>.jsonl. An absent "
            "registration is refused loudly (no hypothesis to test); a "
            "stale/revoked/superseded registration is RECORDED and its status "
            "disclosed (fail-open for evidence — production is the experiment that "
            "never stops). payload/observed_at values stay caller-attested (the "
            "emitter's truthfulness, not core's). Local read + one journal append, "
            "no SSH."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=ConformanceRecordSpec,
        schema_ref=SchemaRef(input="conformance_record"),
    ),
    agent_facing=False,
)
def conformance_record(
    *, experiment_dir: Path, spec: ConformanceRecordSpec
) -> ConformanceRecordResult:
    """Record one live conformance observation — validate, stamp, bind, append.

    Steps, in order:

    1. **Registration-exists gate** — reduce the id's registration journal; an
       ABSENT reduction is a loud :class:`errors.SpecInvalid` (no hypothesis to
       test). This is the ONE refusal; every present status records.
    2. **Stamp ``status_at_record``** — recompute the live dossier sha (the
       ``verify_op`` idiom, degrade-to-``None``) and reduce with it, so the stamp
       is the honest live read: ``current`` / ``stale`` / ``revoked``.
    3. **Build + bind + append** — assemble the C-store record via the T1 kernel
       (``dossier_sha`` = the winning registration's RECORDED sha — the sealed
       hypothesis identity), then append through the T3 store, which recomputes
       the payload sha server-side, binds it, and writes exactly ONE ledger line
       (the sole side effect).

    Returns the echo of the appended line (server-computed ``content_sha`` +
    stamped ``status_at_record`` + the ledger path). Raises
    :class:`errors.SpecInvalid` on an absent registration or any shape violation.
    """
    experiment_dir = Path(experiment_dir)
    registration_id = str(spec.registration_id)

    records = read_decisions(experiment_dir, "registration", registration_id)

    # Locate the winner (winner selection is independent of the live sha), then
    # RECOMPUTE the live dossier sha and reduce with it — the honest live status.
    peek: RegistrationStatus = registration.reduce_registration(
        records, registration_id=registration_id, live_dossier_sha=None
    )
    if peek.status == registration.ABSENT:
        raise errors.SpecInvalid(
            f"conformance-record: registration {registration_id!r} is ABSENT — there is no "
            "hypothesis to test. Register the subject before recording observations against it."
        )

    winner = peek.winner or {}
    live_sha = _recompute_dossier_sha(experiment_dir, winner)
    reduced = registration.reduce_registration(
        records, registration_id=registration_id, live_dossier_sha=live_sha
    )
    status_at_record = reduced.status

    # The dossier_sha STAMPED in the observation is the RECORDED identity of the
    # sealed hypothesis (winner's recorded sha; falls back to the newest
    # registration record when the winner is an overturn that binds none).
    dossier_sha = winner.get("dossier_sha")
    if not isinstance(dossier_sha, str) or not dossier_sha:
        dossier_sha = _registered_dossier_sha(records, registration_id)
    if not dossier_sha:
        # PRESENT but no registration record carries a dossier sha (a bare revoke
        # with no prior registration): there is no sealed hypothesis to bind the
        # observation to — the same fabrication class as ABSENT, named honestly.
        raise errors.SpecInvalid(
            f"conformance-record: registration {registration_id!r} carries no sealed dossier sha "
            "(no registration record to test against). Register the subject first."
        )

    record = conformance.build_observation_record(
        registration_id=registration_id,
        dossier_sha=dossier_sha,
        status_at_record=status_at_record,
        payload=dict(spec.payload),
        observed_at=spec.observed_at,
        labels=dict(spec.labels),
        emitter=spec.emitter,
        ts=utcnow_iso(),
    )
    appended = conformance_store.append_observation(experiment_dir, record=record)

    ledger_path = conformance_store.conformance_ledger_path(experiment_dir, registration_id)
    return ConformanceRecordResult(
        registration_id=registration_id,
        content_sha=appended["content_sha"],
        status_at_record=status_at_record,  # type: ignore[arg-type]
        observed_at=spec.observed_at,
        ledger_path=str(ledger_path),
    )
