"""Pydantic models for the ``conformance-record`` mutate verb (live-conformance T2).

Wire surface over :mod:`hpc_agent.ops.conformance.record_op` — the EMITTER's
journaling surface for live conformance observations (``docs/design/live-conformance.md``
C-verbs). Each observation is one journaled CODE attestation, sha-linked to the
registration it tests: "evidence FOR/AGAINST registration R" at production
cadence (C1).

**The trust boundary (C1 / F8 honesty, verbatim).** The verb BINDS the exact
recorded bytes — it recomputes the payload ``content_sha`` SERVER-SIDE at append
and stamps it into the ledger; the caller CANNOT assert a sha into existence.
There is therefore **no sha field on the input spec** — a sha on the wire would
be a claim the server ignores, so it is not accepted at all (pinned by test).
Truthfulness of the ``payload`` / ``observed_at`` VALUES is the emitter's own
(the same trust class as a conforming harness's out-of-band writes); core vouches
for the bytes it hashed, never for the world they describe.

**Opaque by construction.** ``payload`` keys are caller vocabulary (the same
keys the sealed baseline carries); values are opaque scalars — identity-compared,
range-compared, counted, never read for meaning. ``labels`` are opaque caller
data (a cluster, a batch id, a venue tag — core never learns which); label
NOVELTY relative to a window is disclosed evidence, never interpreted. No field
name here carries domain semantics (the market-vocabulary walk, mirrored).

**``agent_facing=False``** at the verb layer (C-verbs): a human/cron-invoked CLI
verb, never an agent tool — an agent authoring the outcome stream that judges its
own registration is the receipt-laundering class at the operation boundary.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class ConformanceRecordSpec(BaseModel):
    """Inputs to ``conformance-record`` — one live observation receipt.

    Deliberately carries NO ``content_sha``: the sha is recomputed SERVER-SIDE
    over ``{payload, labels, observed_at}`` and bound at append
    (``state/attestation.py::bind``), so a caller-supplied sha would be a claim
    core ignores — it is not accepted on the wire at all (C1's recompute lock).

    An observation naming a registration that is ABSENT is refused loudly at the
    verb (there is no hypothesis to test); an observation against a registration
    that reduces ``stale``/``revoked``/``superseded`` is RECORDED (production is
    the experiment that never stops) with the reduced status stamped
    ``status_at_record`` — disclosed, never silently mixed.
    """

    model_config = ConfigDict(extra="forbid", title="conformance-record input spec")

    registration_id: RunIdStrict = Field(
        description=(
            "The registration this observation tests (the sealed hypothesis). A "
            "caller-authored filesystem-safe slug; the ledger is keyed on it "
            "(_aggregated/_conformance/<registration_id>.jsonl). An absent "
            "registration is refused loudly (no hypothesis to test)."
        ),
    )
    payload: dict[str, float | int | str | bool] = Field(
        description=(
            "The flat, already-reduced observation — {caller-key: opaque scalar}, "
            "using the SAME keys the registered baseline carries. Values are opaque: "
            "identity-compared, range-compared, counted; never read for meaning. "
            "Keys are caller vocabulary (core never learns what a key means)."
        ),
    )
    observed_at: str = Field(
        min_length=1,
        description=(
            "ISO timestamp the CALLER says the observation occurred (caller-attested; "
            "core hashes it, never verifies it). Distinct from the server-stamped "
            "record ts. Feeds the query-time window selection."
        ),
    )
    labels: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Opaque caller labels for this observation (a cluster, a batch id, a "
            "venue tag). Core never learns which; label NOVELTY relative to a window "
            "is disclosed evidence at query time, never interpreted."
        ),
    )
    emitter: str | None = Field(
        default=None,
        description=(
            "Opaque caller-declared emitter id — the caller-side machinery that "
            "produced this observation. Recorded for provenance; never parsed."
        ),
    )


class ConformanceRecordResult(BaseModel):
    """Echo of one appended ledger line (C-store record shape).

    Everything here is what core COMPUTED or STAMPED, not what the caller
    claimed: ``content_sha`` is the server-recomputed canonical-JSON sha over
    ``{payload, labels, observed_at}``; ``status_at_record`` is the registration's
    reduced status at record time (fail-open recording discloses it, never mixes
    it silently). ``observed_at`` is echoed for the caller to reconcile.
    """

    model_config = ConfigDict(extra="forbid", title="conformance-record output data")

    registration_id: str = Field(
        description="The registration the observation was recorded against.",
    )
    content_sha: str = Field(
        description=(
            "SERVER-recomputed canonical-JSON SHA-256 over {payload, labels, "
            "observed_at} (the harness sha canonicalization), bound at append. The "
            "sha the ledger line carries — never a value the caller supplied."
        ),
    )
    status_at_record: Literal["current", "stale", "revoked", "superseded"] = Field(
        description=(
            "The registration's REDUCED status at record time, stamped into the "
            "ledger line. Recording is fail-open: a stale/revoked/superseded "
            "registration is recorded-and-stamped, never refused (refusing evidence "
            "is the one thing an evidence system must not do)."
        ),
    )
    observed_at: str = Field(
        description="The caller-declared observation timestamp, echoed back.",
    )
    ledger_path: str | None = Field(
        default=None,
        description=(
            "Path of the append-only conformance ledger this observation appended to "
            "(_aggregated/_conformance/<registration_id>.jsonl)."
        ),
    )
