"""Pydantic models for the ``cluster-readiness`` query (s2-readiness pillar 1).

The read surface over the standing readiness ledger's DURABLE tier
(:mod:`hpc_agent.state.readiness`): per cluster, the verdict atoms the system
has already harvested, each with its AGE, plus one overall verdict from the
fixed vocabulary ``{ready, stale, degraded, unknown}``.

An atom is ``infra/readiness_sensors.VerdictAtom`` — the sensor layer's own unit
of record — so a stored reading and a live one can never disagree about what was
seen: ``sensor`` (``hop`` / ``direct`` / ``path`` / ``connect`` / ``preamble``,
extended by the durable tier with ``auth`` / ``scratch`` / ``scheduler`` /
``env``), ``target``, ``route`` (``effective`` / ``direct`` / ``n/a`` — the
discriminator that catches a dead ``ProxyJump`` behind a hostname that answers),
``verdict``, ``latency_ms`` and ``at``.

Pure local projection: this verb opens NO connection and runs NO probe (it is
``side_effects=[]`` and means it). Everything it reports was learned by traffic
the system was making anyway, or by a sensor run someone else paid for. A sensor
nothing has fed reads ``unknown`` — the honest answer — and the render says so
rather than leaving a gap.

``now`` is the deterministic-testing override (the ``doctor`` / ``attention-queue``
precedent), never an agent-facing knob for reshaping ages.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ClusterReadinessSpec(BaseModel):
    """Input spec for the ``cluster-readiness`` verb.

    Every field is optional: the empty spec ``{}`` is valid and means "every
    cluster in ``clusters.yaml`` plus every host that has a ledger".
    """

    model_config = ConfigDict(extra="forbid", title="cluster-readiness input spec")

    cluster: str | None = Field(
        default=None,
        description=(
            "Restrict the report to this clusters.yaml key. Unknown keys are not "
            "refused — they report as unknown-with-no-ledger, because 'I have "
            "never observed this' is a real answer and refusing it would hide it."
        ),
    )
    host: str | None = Field(
        default=None,
        description=(
            "Restrict the report to this ssh host (user@host is accepted and "
            "normalized to the host key the ssh circuit breaker uses). Combines "
            "with 'cluster' as an intersection."
        ),
    )
    now: str | None = Field(
        default=None,
        description=(
            "Optional ISO-8601 UTC 'now' override for deterministic testing (the "
            "doctor precedent). Sets the computed_at stamp and the single instant "
            "every atom age and freshness horizon is measured against — never an "
            "agent-facing knob for reshaping ages."
        ),
    )


class ReadinessAtomModel(BaseModel):
    """One verdict atom: what was probed, over which route, when, and how long.

    The wire projection of ``infra/readiness_sensors.VerdictAtom`` plus the two
    derived reading aids (``age_seconds``, ``stale``) and the durable tier's
    additive ``source``.

    A sensor that nothing has fed is still emitted, with ``verdict='unknown'``
    and every other field null — absence is reported, not omitted, so a reader
    can never mistake an unfed invariant for a green one.
    """

    model_config = ConfigDict(extra="forbid", title="cluster-readiness atom")

    sensor: str = Field(
        description=(
            "What was probed: hop / direct / path / connect / preamble (the "
            "sensor layer's vocabulary), extended by the durable ledger with "
            "auth / scratch / scheduler / env."
        )
    )
    target: str | None = Field(
        default=None,
        description=(
            "The chain element or host the reading is about (a hop's own "
            "hostname, or the target); null when no observation exists."
        ),
    )
    route: str | None = Field(
        default=None,
        description=(
            "Which route the reading was taken over: 'effective' (what ssh -G "
            "resolved, hops included), 'direct' (hop-bypassing — the "
            "discriminator for a dead ProxyJump), or 'n/a'. Null when unknown."
        ),
    )
    verdict: str = Field(
        description=(
            "ok / down / timeout / unknown / skipped as recorded, or 'unknown' "
            "when no observation of this sensor exists. 'unknown' means the "
            "sensor could not settle it and 'skipped' means it never ran — "
            "neither is 'fine'."
        )
    )
    at: str | None = Field(
        default=None,
        description="ISO-8601 UTC instant the observation was recorded; null when unknown.",
    )
    age_seconds: int | None = Field(
        default=None,
        description=(
            "Age of the observation at computed_at, in whole seconds; null when "
            "unknown or when the stamp is unparseable."
        ),
    )
    stale: bool = Field(
        description=(
            "True when the observation is past this sensor's freshness horizon "
            "(or has no usable stamp, or does not exist). A stale verdict is "
            "never read as current — that assertion is the whole point."
        )
    )
    stale_after_seconds: int = Field(
        description="This sensor's freshness horizon in seconds, so the age reads against it."
    )
    latency_ms: int | None = Field(
        default=None,
        description=(
            "The observation's own measured duration, when the site held one; "
            "null otherwise. Never estimated."
        ),
    )
    source: str | None = Field(
        default=None,
        description=(
            "Durable-tier provenance naming the seam that recorded it (rendered, "
            "never interpreted); not part of VerdictAtom."
        ),
    )
    detail: str | None = Field(
        default=None,
        description="The reading's own short opaque note; null when it recorded none.",
    )


class ClusterReadinessEntry(BaseModel):
    """The readiness of one cluster (or one bare host with a ledger)."""

    model_config = ConfigDict(extra="forbid", title="cluster-readiness entry")

    cluster: str | None = Field(
        default=None,
        description="The clusters.yaml key, or null for a host with a ledger but no config entry.",
    )
    host: str = Field(description="The ssh host key the ledger is filed under.")
    verdict: str = Field(
        description=(
            "ready / stale / degraded / unknown — the single overall verdict "
            "computed from the atoms below by state/readiness.overall_verdict."
        )
    )
    atoms: list[ReadinessAtomModel] = Field(
        default_factory=list,
        description=(
            "Recorded atoms ordered by (sensor position, route, target), each "
            "followed by an 'unknown' placeholder for every sensor kind nothing "
            "has fed — absence is emitted, never omitted."
        ),
    )
    ledger_corrupt: bool = Field(
        default=False,
        description=(
            "True when the ledger file existed but could not be parsed. The entry "
            "then reports an EMPTY ledger (verdict 'unknown') and says so — a "
            "corrupt file is disclosed, never a crash and never silently green."
        ),
    )


class ClusterReadinessResult(BaseModel):
    """Shape of the ``data`` field on a ``cluster-readiness`` envelope.

    ``render`` rides the result the way it does on ``attention-queue`` — the
    agent relays it VERBATIM. The single ``computed_at`` stamp dates the whole
    projection and is the instant every age was measured against.
    """

    model_config = ConfigDict(extra="forbid", title="cluster-readiness output data")

    computed_at: str = Field(
        description="The single instant readiness was computed against (ISO-8601 UTC)."
    )
    clusters: list[ClusterReadinessEntry] = Field(
        default_factory=list,
        description="One entry per cluster/host, ordered by (cluster or '', host).",
    )
    counts: dict[str, int] = Field(
        default_factory=dict,
        description="Entry count per overall verdict ({ready, stale, degraded, unknown}).",
    )
    render: str = Field(
        description="The deterministic markdown digest — relayed to the human verbatim."
    )
