"""Pydantic models for the ``net-triage`` connectivity differential (query).

``net-triage`` answers "WHY can't I reach the cluster?" deterministically —
the 2026-07-05 proving-run incident: with a host's SSH circuit breaker OPEN
and discovery dark, the driving agent improvised raw ssh probes, saw two
timeouts, and mis-diagnosed a local VPN outage while the ground truth
(breaker open with a recorded cooldown deadline; local network fine) was
derivable from durable local state plus one bounded control probe. This verb
mechanizes that differential so no agent ever has to guess it again.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The five mutually-exclusive answers to "why can't I reach this host?".
TriageVerdict = Literal[
    "reachable",
    "breaker_open_cooling",
    "host_unreachable_network_ok",
    "local_network_down",
    "dns_failure",
]


class NetTriageSpec(BaseModel):
    """Input spec for the ``net-triage`` verb."""

    model_config = ConfigDict(extra="forbid", title="net-triage input spec")

    host: str | None = Field(
        default=None,
        description=(
            "Optional extra host to triage (bare hostname or user@host — the "
            "user part is stripped), IN ADDITION to every host in the cluster "
            "config. Use it for a host not (yet) in clusters.yaml."
        ),
    )
    control_timeout_sec: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
        description="Budget for the one control-plane HTTPS reachability check.",
    )
    dns_timeout_sec: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
        description="Per-host budget for DNS resolution of the cluster hostname.",
    )
    tcp_timeout_sec: float = Field(
        default=8.0,
        ge=1.0,
        le=60.0,
        description="Per-host budget for the single direct TCP connect to host:22.",
    )


class BreakerState(BaseModel):
    """One host's SSH circuit-breaker state, read from its durable state file.

    Read-only: triage NEVER writes breaker state, never claims the half-open
    probe slot, and never counts toward the failure ledger. ``missing`` means
    no state file exists (a healthy host that never failed) and is treated as
    closed everywhere — fail-open, same posture as the breaker itself.
    """

    model_config = ConfigDict(extra="forbid", title="net-triage breaker state")

    state: Literal["closed", "open", "missing"] = Field(
        description=(
            "Breaker state: 'open' fails SSH fast (ban-risk protection), "
            "'closed' is healthy, 'missing' means no state file (never failed)."
        )
    )
    consecutive_failures: int = Field(
        default=0,
        description="Consecutive connection-level failures recorded for this host.",
    )
    cooldown_until: str | None = Field(
        default=None,
        description=(
            "When the cooldown ends and the automatic half-open probe becomes "
            "eligible (ISO-8601 UTC). Null unless the breaker is open. A past "
            "deadline means the breaker is waiting for one probe to succeed."
        ),
    )
    last_failure_at: str | None = Field(
        default=None,
        description="When the last connection-level failure was recorded (ISO-8601 UTC), or null.",
    )
    last_failure_detail: str | None = Field(
        default=None,
        description="The matched failure marker + stderr snippet of the last failure, or null.",
    )


class ControlPlaneCheck(BaseModel):
    """The one bounded HTTPS probe that separates 'my network is down' from
    'the cluster is unreachable' — run once per invocation, not per host."""

    model_config = ConfigDict(extra="forbid", title="net-triage control-plane check")

    https_ok: bool = Field(
        description="True when the control endpoint answered over HTTPS (any HTTP status)."
    )
    url: str = Field(description="The stable public endpoint probed.")
    detail: str = Field(
        description="'HTTP <status>' on success; the exception class + message on failure."
    )


class HostTriage(BaseModel):
    """The full connectivity differential for one host, with a verdict."""

    model_config = ConfigDict(extra="forbid", title="net-triage per-host result")

    host: str = Field(description="The host triaged (breaker key: bare hostname, no user@).")
    cluster: str | None = Field(
        default=None,
        description="The clusters.yaml entry this host came from, or null for a caller host.",
    )
    breaker: BreakerState = Field(description="Circuit-breaker state read from the state file.")
    dns_ok: bool | None = Field(
        default=None,
        description="Whether the hostname resolved (bounded). Null when not attempted.",
    )
    dns_detail: str | None = Field(default=None, description="Resolution detail or error.")
    tcp_ok: bool | None = Field(
        default=None,
        description=(
            "Whether ONE bounded TCP connect to host:22 succeeded. Null when the "
            "probe was SKIPPED — always skipped while the breaker is open (the "
            "half-open probe slot belongs to the breaker, never to triage) and "
            "when DNS already failed."
        ),
    )
    tcp_detail: str | None = Field(
        default=None, description="Connect detail, error, or the reason the probe was skipped."
    )
    verdict: TriageVerdict = Field(
        description=(
            "The differential's answer: 'reachable' (host:22 accepts TCP); "
            "'breaker_open_cooling' (SSH is failing fast by design — wait or "
            "override); 'host_unreachable_network_ok' (control passes, host "
            "doesn't — cluster-side outage or source-IP filter); "
            "'local_network_down' (the control probe failed — fix THIS "
            "machine's network/VPN first); 'dns_failure' (hostname didn't "
            "resolve)."
        )
    )
    remediation: str = Field(
        description="What to do about the verdict — deterministic text, one per verdict arm."
    )


class NetTriageResult(BaseModel):
    """Shape of the ``data`` field on a ``net-triage`` envelope."""

    model_config = ConfigDict(extra="forbid", title="net-triage output data")

    now: str = Field(description="When the triage ran (ISO-8601 UTC).")
    control: ControlPlaneCheck = Field(
        description="The one control-plane HTTPS check (shared by every host verdict)."
    )
    hosts: list[HostTriage] = Field(
        default_factory=list,
        description="Per-host differential: every configured cluster host + any caller host.",
    )
    all_reachable: bool = Field(
        description="True when every triaged host's verdict is 'reachable'."
    )
    summary: str = Field(
        description="One-line human digest: each host's verdict, or 'no hosts to triage'."
    )
