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
    probe_preamble: bool = Field(
        default=False,
        description=(
            "Opt in to the PREAMBLE-CLASS rung: after the connect legs, run the "
            "cluster's own activation (`module load … && source …/conda.sh && "
            "conda activate …`, taken from clusters.yaml unless `activation` "
            "overrides it) over SSH and report {connect, preamble} SEPARATELY. "
            "Opt-in because it costs real connections and a login-shell "
            "round-trip. It is the rung that discriminates the 2026-07-30 "
            "ambiguity: a cheap connect succeeding while the preamble hangs fits "
            "BOTH node-local degradation and a tunnel dropping mid-command, and "
            "only running the SAME command class over the DIRECT (jump-bypassed) "
            "route tells them apart."
        ),
    )
    activation: str | None = Field(
        default=None,
        description=(
            "Override the activation prefix the preamble rung runs (default: the "
            "matched cluster's own, built from clusters.yaml). Ignored unless "
            "`probe_preamble` is true."
        ),
    )
    preamble_timeout_sec: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description=(
            "Per-attempt budget for one preamble-class SSH reading. Short by "
            "design: this rung exists to catch a preamble that HANGS."
        ),
    )


class BreakerState(BaseModel):
    """One host's SSH circuit-breaker state, read from its durable state file.

    Read-only: triage NEVER writes breaker state, never claims the half-open
    probe slot, and never counts toward the failure ledger. ``missing`` means
    no state file exists (a healthy host that never failed) and is treated as
    closed everywhere — fail-open, same posture as the breaker itself.
    """

    model_config = ConfigDict(extra="forbid", title="net-triage breaker state")

    state: Literal["closed", "open", "half_open_eligible", "missing"] = Field(
        description=(
            "EFFECTIVE breaker state at read time: 'open' fails SSH fast "
            "(ban-risk protection, still cooling), 'half_open_eligible' means "
            "the cooldown has lapsed and the next real SSH connection will run "
            "the single half-open probe (success closes the circuit, failure "
            "re-opens it with a doubled cooldown) — nothing fails fast anymore, "
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
            "When the cooldown ends/ended and the half-open probe becomes "
            "eligible (ISO-8601 UTC). Null unless state is 'open' or "
            "'half_open_eligible' (where it is the already-past lapse time)."
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


class RouteChainModel(BaseModel):
    """The EFFECTIVE ssh chain for one host, as ``ssh -G`` itself resolved it.

    Never a re-parse of ``ssh_config``: ``Host`` patterns, ``Match`` blocks and
    ``Include`` chains are OpenSSH's business, and a second parser would drift
    from the client that actually dials. ``resolved=false`` means the resolution
    could not run, and every route-aware claim is then withheld (fail-open).
    """

    model_config = ConfigDict(extra="forbid", title="net-triage effective route")

    resolved: bool = Field(description="Whether ``ssh -G`` produced an answer for this host.")
    hostname: str = Field(
        default="", description="The HostName ssh resolved (may differ from the alias)."
    )
    user: str = Field(default="", description="The username ssh resolved for this destination.")
    port: int = Field(default=22, description="The port ssh resolved for this destination.")
    proxy_jump: list[str] = Field(
        default_factory=list,
        description=(
            "The ordered ProxyJump hops the connection actually traverses. EMPTY "
            "means a direct route. A non-empty chain is why 'the bare hostname "
            "answered' is NOT the same claim as 'your path works' (2026-07-30)."
        ),
    )
    detail: str = Field(default="", description="The resolution line, or why it could not run.")


class ReadinessAtom(BaseModel):
    """One readiness SENSOR reading — what was probed, the verdict, when.

    The unit of record of the standing per-cluster readiness ledger
    (``docs/design/s2-readiness.md`` pillar 1); ``net-triage`` is a thin composer
    over the same atoms the ledger will store, so a rendered reading and a stored
    one can never disagree about what was seen.
    """

    model_config = ConfigDict(extra="forbid", title="net-triage readiness atom")

    sensor: Literal[
        "hop",
        "direct",
        "path",
        "connect",
        "preamble",
        "auth",
        "scratch",
        "scheduler",
        "env",
    ] = Field(
        description=(
            "Which leg this reading speaks for: 'hop' a ProxyJump waypoint, "
            "'direct' the target's own hostname with the jump BYPASSED, 'path' "
            "the derived end-to-end verdict for the effective chain, "
            "'connect'/'preamble' the two command classes of the preamble rung, "
            "and the four named invariants — 'auth' (credentials accepted, "
            "DERIVED from the connect reading's own exit/stderr signature and "
            "costing no probe), 'scratch' (the scratch dir exists and its "
            "filesystem answers), 'scheduler' (the backend family's CLI "
            "answered), 'env' (the remote hpc-agent fingerprint). The last three "
            "appear only when the caller opted into their rungs."
        )
    )
    target: str = Field(description="What was probed (a hostname or an ssh destination).")
    verdict: Literal["ok", "down", "timeout", "unknown", "skipped"] = Field(
        description=(
            "'unknown' means the sensor ran but could not settle it; 'skipped' "
            "means it never ran (detail says why). Neither is 'fine'."
        )
    )
    detail: str = Field(
        default="", description="Reading detail, error, or the reason it was skipped."
    )
    latency_ms: float | None = Field(
        default=None, description="Wall-clock of the reading, or null when it never ran."
    )
    at: str = Field(default="", description="When the reading was taken (ISO-8601 UTC).")
    route: Literal["effective", "direct", "n/a"] = Field(
        default="n/a",
        description="Which route the command-class reading rode; 'n/a' for TCP legs.",
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
            "probe was SKIPPED — always skipped while the breaker is open and "
            "still cooling (the half-open probe slot belongs to the breaker, "
            "never to triage) and when DNS already failed. A half_open_eligible "
            "breaker DOES get the probe: it is evidence only, never a circuit "
            "transition (triage never claims the probe slot)."
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
    route: RouteChainModel | None = Field(
        default=None,
        description=(
            "The EFFECTIVE ssh chain for this host. Null when route resolution "
            "was not attempted. Present-and-jumped is what makes `tcp_ok` above "
            "a claim about the BARE HOSTNAME only, never about your actual path."
        ),
    )
    readiness: list[ReadinessAtom] = Field(
        default_factory=list,
        description=(
            "One LABELLED atom per sensed leg — every ProxyJump hop, the direct "
            "(jump-bypassed) alternative, the derived end-to-end path, and (when "
            "the preamble rung ran) the connect/preamble classes per route. This "
            "is the list that makes 'reachable' unable to mean 'the bare hostname "
            "answered while your actual path is dead' (2026-07-30)."
        ),
    )
    path_cause: str | None = Field(
        default=None,
        description=(
            "The NAMED discriminated cause for this host's path — the same "
            "vocabulary the submit-s2 pre-detach refusal and the staging retry "
            "quote, so the human reads one word everywhere: path_ok, "
            "hop_down_direct_ok, hop_down_direct_dead, hop_down_direct_unprobed, "
            "target_unreachable, path_unproven, preamble_degraded, "
            "transport_flap, route_unresolved. Null when no route read ran."
        ),
    )
    path_summary: str | None = Field(
        default=None,
        description=(
            "The honest one-line route verdict for a JUMPED host, e.g. 'path dead "
            "(hop usc-discovery down); direct alternative OK'. Null for an "
            "un-jumped host, whose summary line is unchanged by this layer."
        ),
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
    active_env_overrides: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Every framework-relevant HPC_* environment variable currently "
            "exported in THIS process's environment, verbatim. Pure disclosure "
            "— triage never judges the values (run-12 finding 24 addendum, B15). "
            "A stray transport override like HPC_SSH_ENGINE='asyncssh' or "
            "HPC_SSH_CIRCUIT_OVERRIDE silently reroutes/short-circuits the very "
            "SSH this differential diagnoses, so the env that shaped the verdict "
            "is echoed alongside it: an override that shows up here unexpectedly "
            "IS the finding. Empty when no HPC_* variable is set."
        ),
    )
