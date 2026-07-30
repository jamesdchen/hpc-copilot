"""``cluster-readiness`` — the read surface over the standing readiness ledger.

Pillar 1 of ``docs/design/s2-readiness.md``. S2's failure mode is discovering at
fire time that transport / env / storage / scheduler / permissions do not all
work, and reporting it through worker-log archaeology. The ledger's durable tier
(:mod:`hpc_agent.state.readiness`) accumulates verdict atoms — the sensor
layer's own ``infra/readiness_sensors.VerdictAtom``, whether harvested
opportunistically from traffic the system was making anyway or written through
by a composed sensor read — and this verb is how a human or an agent READS them,
with the age of every atom, before the y.

Read-only and honestly so: ``verb="query"``, ``side_effects=[]``,
``idempotent=True``, no SSH. It opens no connection and runs no probe — every
number it prints was already on disk. It deliberately does NOT call the sensor
layer: a read surface that senses on read is the fire-time discovery this design
removes. A sensor nothing has fed reads ``unknown``.

Scope: ``clusters.yaml`` entries (so a configured-but-never-contacted cluster is
visible as ``unknown`` rather than missing) UNIONed with every host that has a
ledger (so a host reached outside the config is not hidden). Both sides are
fail-open — an unreadable config contributes nothing and never raises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.cluster_readiness import (
    ClusterReadinessEntry,
    ClusterReadinessResult,
    ClusterReadinessSpec,
    ReadinessAtomModel,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.time import parse_iso_utc_or_none, utcnow_iso
from hpc_agent.ops.cluster_readiness_render import render_readiness
from hpc_agent.state import readiness

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

__all__ = ["cluster_readiness"]


def _configured_hosts() -> dict[str, str]:
    """``{cluster_key: host}`` from ``clusters.yaml``.

    Fail-open: any config error yields ``{}`` so the verb still reports whatever
    ledgers exist. A readiness READ must never be the thing that surfaces a
    config problem by refusing to answer.
    """
    try:
        from hpc_agent.infra.clusters import load_clusters_config

        clusters = load_clusters_config()
    except Exception:  # noqa: BLE001 — a broken config must not fail a pure read
        return {}
    if not isinstance(clusters, dict):
        return {}
    out: dict[str, str] = {}
    for name, cfg in clusters.items():
        if not isinstance(cfg, dict):
            continue
        host = str(cfg.get("host") or "").strip()
        if host:
            out[str(name)] = host
    return out


def _atoms_for(doc: dict[str, Any], *, now: datetime) -> list[ReadinessAtomModel]:
    """Every recorded atom, plus an ``unknown`` placeholder per unfed sensor.

    Ordered by ``(sensor position in SENSOR_KINDS, route, target)`` so the digest
    is stable and the chain reads top-down. Absence is EMITTED (``verdict
    ="unknown"``, every other field null), never omitted — the one rendering rule
    that stops an unfed invariant from reading as a green one. A sensor with at
    least one recorded atom gets no placeholder: it is not unfed.
    """
    stored = doc.get("atoms")
    stored = stored if isinstance(stored, list) else []
    order = {sensor: index for index, sensor in enumerate(readiness.SENSOR_KINDS)}

    def _sort_key(atom: dict[str, Any]) -> tuple[int, str, str]:
        sensor, route, target = readiness.atom_identity(atom)
        return (order.get(sensor, len(order)), route, target)

    out: list[ReadinessAtomModel] = []
    for atom in sorted((a for a in stored if isinstance(a, dict)), key=_sort_key):
        sensor = str(atom.get("sensor") or "")
        age = readiness.atom_age_sec(atom, now=now)
        latency = atom.get("latency_ms")
        detail = atom.get("detail")
        source = atom.get("source")
        out.append(
            ReadinessAtomModel(
                sensor=sensor,
                target=str(atom.get("target")) if atom.get("target") else None,
                route=str(atom.get("route")) if atom.get("route") else None,
                verdict=str(atom.get("verdict")),
                at=atom.get("at") if isinstance(atom.get("at"), str) else None,
                age_seconds=None if age is None else int(age),
                stale=readiness.atom_is_stale(atom, now=now),
                stale_after_seconds=int(readiness.stale_after_sec(sensor)),
                latency_ms=int(latency) if isinstance(latency, (int, float)) else None,
                source=str(source) if source else None,
                detail=str(detail) if detail else None,
            )
        )

    fed = {str(atom.get("sensor") or "") for atom in stored if isinstance(atom, dict)}
    out.extend(
        ReadinessAtomModel(
            sensor=sensor,
            verdict="unknown",
            stale=True,
            stale_after_seconds=int(readiness.stale_after_sec(sensor)),
        )
        for sensor in readiness.SENSOR_KINDS
        if sensor not in fed
    )
    return out


@primitive(
    name="cluster-readiness",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Report the standing per-cluster readiness ledger: for each cluster, "
            "the verdict atoms already on disk (sensor hop/direct/path/connect/"
            "preamble plus auth/scratch/scheduler/env, each over its route) with "
            "the AGE of each, plus one overall verdict from {ready, stale, "
            "degraded, unknown}. Pure local read: no SSH, no probe, no side "
            "effects — every atom was learned by traffic the system was already "
            "making or by a sensor run someone else paid for, and a sensor "
            "nothing has fed reads 'unknown' rather than being measured on "
            "demand. Recomputed on every read; a corrupt ledger file is "
            "disclosed, never fatal."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=ClusterReadinessSpec,
        schema_ref=SchemaRef(input="cluster_readiness"),
    ),
    agent_facing=True,
)
def cluster_readiness(
    *, experiment_dir: Path, spec: ClusterReadinessSpec
) -> ClusterReadinessResult:
    """Project the readiness ledger for every in-scope cluster / host.

    ``experiment_dir`` is accepted for CLI-shape uniformity and is deliberately
    unused: the ledger is MACHINE-scoped (it lives under the journal home beside
    the ssh circuit breaker's state), because the transport and env facts it
    records are properties of the cluster, not of one experiment.

    Raises :class:`errors.SpecInvalid` if ``spec.now`` is a non-ISO-8601 string.
    """
    del experiment_dir  # machine-scoped ledger; see the docstring
    now_iso = (spec.now or "").strip() or utcnow_iso()
    now_dt = parse_iso_utc_or_none(now_iso)
    if now_dt is None:
        raise errors.SpecInvalid(
            f"cluster-readiness: now override {spec.now!r} is not ISO-8601 UTC"
        )

    configured = _configured_hosts()
    want_host = (spec.host or "").rsplit("@", 1)[-1].strip()

    # (cluster_key_or_None, host) pairs: config entries UNION ledger-only hosts.
    pairs: list[tuple[str | None, str]] = [(name, host) for name, host in configured.items()]
    covered = set(configured.values())
    pairs.extend((None, host) for host in readiness.known_hosts() if host not in covered)
    if spec.cluster is not None:
        pairs = [p for p in pairs if p[0] == spec.cluster]
    if want_host:
        pairs = [p for p in pairs if p[1] == want_host]
    pairs.sort(key=lambda p: (p[0] or "", p[1]))

    entries: list[ClusterReadinessEntry] = []
    counts: dict[str, int] = {name: 0 for name in readiness.OVERALL_VERDICTS}
    for cluster, host in pairs:
        doc = readiness.read_ledger(host)
        verdict = readiness.overall_verdict(doc, now=now_dt)
        counts[verdict] = counts.get(verdict, 0) + 1
        entries.append(
            ClusterReadinessEntry(
                cluster=cluster,
                host=host,
                verdict=verdict,
                atoms=_atoms_for(doc, now=now_dt),
                ledger_corrupt=bool(doc.get("corrupt")),
            )
        )

    return ClusterReadinessResult(
        computed_at=now_iso,
        clusters=entries,
        counts=counts,
        render=render_readiness(
            [entry.model_dump(mode="json") for entry in entries],
            computed_at=now_iso,
            counts=counts,
        ),
    )
