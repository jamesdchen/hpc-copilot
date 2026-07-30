"""The ONE-LINE readiness digest — the standing ledger where humans already look.

Pillar 1 of ``docs/design/s2-readiness.md`` does not stop at a substrate; it
carries a RENDER MANDATE: the ledger is "rendered with age in the S1 brief and
``suggest-*`` surfaces". A ledger nobody reads before the y changes nothing —
S2 still discovers at fire time, and the human still learns the route was dead
from a worker corpse. This module is the projection those surfaces render:
**one line per cluster, carrying the age, the overall verdict, and — when the
verdict is not ``ready`` — the sensor that says so.**

The surfaces wired to it (the 2026-07-30 user choice of where humans look):

* ``suggest-prelude-action`` — the rung whose action hands off to the cluster
  (``cli/prelude_actions.py``).
* ``status-snapshot`` + the overnight morning brief (``ops/status_blocks.py``,
  ``ops/overnight.py``).
* ``doctor`` (``ops/recover/doctor.py``) — every known host, with its
  ``last_corruption`` disclosure.

Consult-only, structurally
--------------------------

Every function here reads ``<journal home>/_readiness/<host>.json`` through
:mod:`hpc_agent.state.readiness` and ``clusters.yaml`` through
:func:`hpc_agent.infra.clusters.load_clusters_config`. **Nothing else.** No
sensor call, no ``ssh -G``, no socket, no subprocess — a digest that senses on
read would reintroduce, on the busiest surfaces in the product, exactly the
fire-time discovery pillar 1 removes. That is not a comment: every surface test
mounts ``tests/_no_network.py``'s ``BaseException``-based tripwire, which the
2026-07-30 adversarial review proved is the only thing that actually holds the
line through fail-open ``except Exception`` walls.

Because the reading may be old, every line SAYS it is a reading: it ends in
:data:`CONSULT_NOTE` so neither a human nor an agent can mistake a digest for a
live check.

One definition, borrowed not copied
-----------------------------------

* the overall verdict → :func:`hpc_agent.state.readiness.overall_verdict`
* the ledger's age → :func:`hpc_agent.state.readiness.ledger_age_sec`
* staleness → :func:`hpc_agent.state.readiness.atom_is_stale`
* the age phrase and the atom subject → ``ops/cluster_readiness_render``'s own
  ``_age_phrase`` / ``_subject`` (same package; the ``cluster-readiness`` digest
  and this one-liner must never phrase "37m ago" two ways)
* the cluster→host map → ``ops/cluster_readiness_op._configured_hosts``

This module adds exactly one thing of its own: which sensor to NAME when the
verdict is not ``ready``. That note EXPLAINS a verdict; it never computes one
(:func:`not_ready_note`).

Fail-open, always
-----------------

Every public function swallows everything. These surfaces are the ones a human
reads when something is already wrong; a readiness digest that raises would take
the morning brief down with it. An unreadable ledger renders as a disclosed
line, never as an absence and never as an exception.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hpc_agent.state import readiness

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

__all__ = [
    "CONSULT_NOTE",
    "DIGEST_FIELDS",
    "EMPTY_LEDGER_NOTE",
    "NO_LEDGER_NOTE",
    "configured_hosts",
    "digest_for_host",
    "digest_line",
    "digests_for",
    "host_for_cluster",
    "known_host_digests",
    "not_ready_note",
]

#: Every line's suffix. The digest is a READING of a durable file, possibly an
#: old one — never a live check. Stated on every line rather than once per
#: surface, because a line is what gets quoted, pasted, and relayed out of its
#: surrounding context.
CONSULT_NOTE = "ledger read, not probed"

#: The note for a host with no ledger file at all. Distinct from a corrupt one:
#: ``read_ledger`` deliberately reports an ABSENT file as ``corrupt=False``
#: because "nothing has ever looked" and "we cannot read what looked" are
#: different facts, and a digest that blurred them would let an unreachable
#: cluster hide behind a missing file.
NO_LEDGER_NOTE = "no readiness ledger on this machine"

#: The note for a ledger file that reads cleanly but holds no USABLE atom — every
#: stored atom named a sensor or verdict outside the declared vocabulary and was
#: dropped on read (that drop is what stops a foreign or future writer moving a
#: host's verdict). Distinct from :data:`NO_LEDGER_NOTE` because reporting it as
#: "no ledger" would hide that something IS writing here in a shape we refuse.
EMPTY_LEDGER_NOTE = "the ledger holds no usable observation"

#: The digest dict's keys, in render order. A plain dict (not a wire model) so
#: the free-form brief surfaces — ``status-snapshot``, the morning brief — can
#: embed it verbatim; the two schema'd surfaces (``doctor``,
#: ``suggest-prelude-action``) validate it through
#: ``_wire/_shared.ReadinessDigest``, whose field names are these.
DIGEST_FIELDS: tuple[str, ...] = (
    "cluster",
    "host",
    "verdict",
    "age_seconds",
    "note",
    "ledger_corrupt",
    "ledger_corruption_reason",
    "last_corruption",
    "line",
)


def configured_hosts() -> dict[str, str]:
    """``{cluster_key: host}`` from ``clusters.yaml``; ``{}`` on any trouble.

    Delegates to ``ops/cluster_readiness_op._configured_hosts`` — the SAME
    fail-open projection the ``cluster-readiness`` verb scopes itself with — so
    this digest and that verb can never disagree about which clusters exist or
    which host one names. Same-package private import by design (the symbol is
    ``ops``-private, and this module is ``ops``).
    """
    try:
        from hpc_agent.ops.cluster_readiness_op import _configured_hosts

        return _configured_hosts()
    except Exception:  # noqa: BLE001 — a config problem must never fail a digest
        return {}


def host_for_cluster(cluster: str) -> str:
    """The ssh host a ``clusters.yaml`` key names, or ``""``. Never raises."""
    if not cluster:
        return ""
    return configured_hosts().get(cluster, "")


def _sorted_atoms(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """The ledger's atoms in the ``cluster-readiness`` render order.

    ``(position in SENSOR_KINDS, route, target)`` — the same key
    ``cluster_readiness_op._atoms_for`` sorts by, so the sensor this one-liner
    names is the first one a reader would meet in the full digest rather than an
    arbitrary pick that changes between the two surfaces.
    """
    stored = doc.get("atoms")
    stored = stored if isinstance(stored, list) else []
    order = {sensor: index for index, sensor in enumerate(readiness.SENSOR_KINDS)}

    def _key(atom: dict[str, Any]) -> tuple[int, str, str]:
        sensor, route, target = readiness.atom_identity(atom)
        return (order.get(sensor, len(order)), route, target)

    return sorted((a for a in stored if isinstance(a, dict)), key=_key)


def _atom_note(atom: dict[str, Any], *, now: datetime) -> str:
    """``connect/effective → login.example.edu: down`` (+ ``, STALE (horizon Ns)``).

    The subject comes from ``cluster_readiness_render._subject`` — the render's
    own rule that an atom's identity is ``(sensor, route, target)`` and that
    showing fewer than all three makes two distinct atoms print the same line.
    """
    from hpc_agent.ops.cluster_readiness_render import _subject

    note = f"{_subject(atom)}: {atom.get('verdict') or 'unknown'}"
    if readiness.atom_is_stale(atom, now=now):
        horizon = int(readiness.stale_after_sec(str(atom.get("sensor") or "")))
        note += f", STALE (horizon {horizon}s)"
    return note


def not_ready_note(doc: dict[str, Any], *, now: datetime) -> str | None:
    """The sensor to NAME for a not-``ready`` ledger, or ``None`` when all is well.

    **This explains a verdict; it never computes one.** The verdict is
    :func:`hpc_agent.state.readiness.overall_verdict` and nothing here may be
    read as a second opinion about it — so the search is expressed in terms of
    that function's OWN predicates (``atom_is_stale``, ``REQUIRED_SENSORS``,
    "not ``ok``") and simply picks the first atom, in render order, that one of
    them already disqualified.

    Priority — sharpest evidence first, so the named sensor is the one a reader
    would have wanted:

    1. a FRESH atom whose verdict is not ``ok`` (the ``degraded`` case's own
       evidence: something looked recently and it was broken),
    2. any atom whose verdict is not ``ok`` (the same failure, gone stale — the
       host may have healed and nothing has looked since),
    3. a :data:`~hpc_agent.state.readiness.REQUIRED_SENSORS` member with no atom
       at all, rendered as "no observation recorded" — absence is EMITTED, the
       render rule that stops an unfed invariant from reading as a green one,
    4. any stale atom (everything is ``ok``, but not recently enough to assert).

    ``None`` only when every atom is ``ok`` and fresh and every required sensor
    is present — which is exactly ``overall_verdict``'s ``ready``.
    """
    atoms = _sorted_atoms(doc)
    if not atoms:
        return "no observation recorded"
    fresh_bad = [
        a for a in atoms if a.get("verdict") != "ok" and not readiness.atom_is_stale(a, now=now)
    ]
    if fresh_bad:
        return _atom_note(fresh_bad[0], now=now)
    any_bad = [a for a in atoms if a.get("verdict") != "ok"]
    if any_bad:
        return _atom_note(any_bad[0], now=now)
    present = {str(a.get("sensor") or "") for a in atoms}
    missing = [s for s in readiness.REQUIRED_SENSORS if s not in present]
    if missing:
        return f"{missing[0]}: no observation recorded"
    stale = [a for a in atoms if readiness.atom_is_stale(a, now=now)]
    if stale:
        return _atom_note(stale[0], now=now)
    return None


def digest_line(entry: dict[str, Any]) -> str:
    """Compose the one line from an already-built digest dict.

    ``<cluster> (<host>): <verdict> · <age> · <note> (ledger read, not probed)``

    Pure string work over the entry's own fields — same entry, same bytes. The
    age phrase is ``cluster_readiness_render._age_phrase``, so "37m ago" is
    spelled identically here and in the full ``cluster-readiness`` digest.
    """
    from hpc_agent.ops.cluster_readiness_render import _age_phrase

    host = str(entry.get("host") or "?")
    cluster = entry.get("cluster")
    title = f"{cluster} ({host})" if cluster else host
    parts = [str(entry.get("verdict") or "unknown"), _age_phrase(entry.get("age_seconds"))]
    note = entry.get("note")
    if note:
        parts.append(str(note))
    return f"{title}: {' · '.join(parts)} ({CONSULT_NOTE})"


def digest_for_host(
    host: str,
    *,
    cluster: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The readiness digest for one *host*. Consult-only; never raises.

    *host* may be ``user@host`` or a bare alias — the ledger normalizes it to
    the same host key the breaker, the throttle and the sensor layer all use, so
    a run's ``ssh_target`` can be handed in verbatim.

    Returns a dict carrying :data:`DIGEST_FIELDS`. On ANY trouble the result is
    still well-shaped: verdict ``unknown``, age ``None``, and a note saying what
    went wrong — an absent digest would read as "nothing to worry about", which
    is the one thing a readiness surface may never say.
    """
    from hpc_agent.infra.time import utcnow

    entry: dict[str, Any] = {
        "cluster": cluster or None,
        "host": str(host or "?"),
        "verdict": "unknown",
        "age_seconds": None,
        "note": NO_LEDGER_NOTE,
        "ledger_corrupt": False,
        "ledger_corruption_reason": None,
        "last_corruption": None,
    }
    try:
        stamp = now if now is not None else utcnow()
        doc = readiness.read_ledger(host)
        entry["host"] = readiness.ledger_host(doc) or entry["host"]
        entry["verdict"] = readiness.overall_verdict(doc, now=stamp)
        age = readiness.ledger_age_sec(doc, as_of=stamp)
        entry["age_seconds"] = None if age is None else int(age)
        corrupt = bool(doc.get("corrupt"))
        reason = str(doc.get("corruption_reason") or "") or None
        entry["ledger_corrupt"] = corrupt
        entry["ledger_corruption_reason"] = reason
        last = doc.get("last_corruption")
        entry["last_corruption"] = (
            {str(k): str(v) for k, v in last.items()} if isinstance(last, dict) else None
        )
        if corrupt:
            # A corrupt ledger IS 'unknown', but not for the default reason —
            # say which, or the line opens with the reassuring "nothing has
            # looked" while the file it could not read sits right there.
            entry["note"] = f"ledger file could not be read: {reason or 'unknown reason'}"
        elif not doc.get("atoms"):
            # No usable atom. Two different facts, and the file itself is the
            # discriminator: no ledger at all, versus a ledger whose every atom
            # was dropped as foreign (a sensor or verdict outside the declared
            # vocabulary is ignored on read, so a future writer can never move
            # this host's verdict). Reporting the second as "no ledger" would
            # hide that something IS writing here in a shape we do not accept.
            entry["note"] = (
                EMPTY_LEDGER_NOTE if readiness.readiness_path(host).is_file() else NO_LEDGER_NOTE
            )
        else:
            entry["note"] = not_ready_note(doc, now=stamp)
    except Exception:  # noqa: BLE001 — total fail-open; see the module docstring
        entry["note"] = "readiness ledger could not be consulted"
    entry["line"] = digest_line(entry)
    return entry


def digests_for(
    pairs: Iterable[tuple[str | None, str]] | Sequence[tuple[str | None, str]],
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Digests for ``(cluster, host)`` pairs — deduped by host, sorted for stability.

    Deduped because two runs on one cluster are one readiness fact, and sorted by
    ``(cluster, host)`` so two reads of the same state render byte-identically.
    A pair with an empty host is dropped: there is no ledger to key on, and
    inventing a placeholder host would put a line under a name no substrate uses.
    """
    seen: dict[str, str | None] = {}
    for cluster, host in pairs:
        key = str(host or "").rsplit("@", 1)[-1].strip()
        if not key:
            continue
        # First non-empty cluster name wins — a later ledger-only sighting of the
        # same host must not blank the name the human configured it under.
        if key not in seen or (seen[key] is None and cluster):
            seen[key] = cluster or seen.get(key)
    ordered = sorted(seen.items(), key=lambda item: (item[1] or "", item[0]))
    return [digest_for_host(host, cluster=cluster, now=now) for host, cluster in ordered]


def known_host_digests(*, now: datetime | None = None) -> list[dict[str, Any]]:
    """Digests for every host that HAS a ledger on this machine (doctor's scope).

    ``doctor`` reports what the machine actually knows, so the scope is
    :func:`hpc_agent.state.readiness.known_hosts` — hosts with a ledger file —
    annotated with the ``clusters.yaml`` name when one claims that host. A
    configured-but-never-contacted cluster is deliberately NOT invented here: it
    would add an ``unknown`` row per config entry to a watchdog whose whole
    contract is that a line means something happened. (``cluster-readiness`` is
    the verb that unions the config in, for the human who asked.)

    Fail-open: an unreadable ledger dir yields ``[]``.
    """
    try:
        by_host = {host: name for name, host in configured_hosts().items()}
        hosts = readiness.known_hosts()
    except Exception:  # noqa: BLE001 — a watchdog read must never raise
        return []
    return digests_for(((by_host.get(host), host) for host in hosts), now=now)
