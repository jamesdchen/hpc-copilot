"""The DURABLE tier of the standing per-cluster readiness ledger.

Pillar 1 of ``docs/design/s2-readiness.md``. The principle it serves: **S2 never
discovers anything at fire time.** S2 is where local intent becomes remote
reality — transport, remote env, storage, scheduler and harness permissions must
ALL work — and today it learns whether they do by attempting the full operation
serially and reporting failure through detached-worker-log archaeology. The
proper shape is a readiness state computed *before the human sat down*, revealed
at fire time with its AGE, plus the one irreversible act gated on the journaled y.

Two tiers, ONE vocabulary
-------------------------

``infra/readiness_sensors.py`` owns the SENSOR layer and the unit of record:
:class:`~hpc_agent.infra.readiness_sensors.VerdictAtom`
(``sensor``/``target``/``verdict``/``detail``/``latency_ms``/``at``/``at_epoch``/``route``),
the ``ssh -G`` chain resolution, and an IN-PROCESS ``record_readiness`` /
``consult_readiness`` ledger explicitly built as "the API the durable ledger will
offer, so the ledger builder swaps the storage and nothing above it moves."

This module is that storage. It is the same ledger one tier down:

* **cache tier** — the sensors' in-process dict. Sub-second, per-invocation, lost
  at exit.
* **durable tier** — this module. A per-host JSON doc under the journal home,
  shared by every process on the box (CLI invocations, detached workers, the MCP
  server), surviving restarts.

Read path is **consult-process-then-durable**; write path is **write-through**.
A composer that senses records into the process cache and, through the same
call, into this file; a later invocation that finds nothing in its (empty) cache
still gets the reading from disk. That is what makes readiness *standing* rather
than per-invocation.

Atom shape: :func:`record_atoms` accepts anything with ``VerdictAtom``'s field
names (its dataclass instances included, duck-typed so this module never imports
the sensor layer) and stores those fields verbatim, plus one additive
durable-tier-only ``source`` string naming the seam that recorded it.
Reconstruction drops ``source``; nothing in the sensor layer needs to know.

Vocabulary
----------

:data:`SENSOR_KINDS` is the sensor layer's ``SensorKind`` — ``hop`` / ``direct``
/ ``path`` / ``connect`` / ``preamble`` — EXTENDED, in the same flat vocabulary,
with the four invariants the design's pillar 3 names that no sensor covers yet:
``auth``, ``scratch``, ``scheduler``, ``env``. One vocabulary, one definition —
the ``# MIRROR:`` annotations on the constants below name the sensor-layer twin
of each replicated list and the test that fails on drift.

:data:`SENSOR_VERDICTS` is the sensor layer's ``SensorVerdict`` verbatim: ``ok``
/ ``down`` / ``timeout`` / ``unknown`` / ``skipped``. ``unknown`` means the
sensor ran but could not settle it; ``skipped`` means it never ran. Neither is
"fine". :data:`ROUTES` is its ``route`` axis: ``effective`` / ``direct`` /
``n/a`` — the discriminator that catches a dead ``ProxyJump`` hop behind a
hostname that answers.

Feed sites — HARVEST ONLY, never probe
--------------------------------------

:func:`record_observation` is the ONE narrow recording interface for a *single*
harvested atom (:func:`record_atoms` is its bulk sibling for a composed sensor
read). The standing rule for wiring either is: **the ledger only harvests what
the system already learned.** No feed site may open a connection, run a remote
command, or add any network call whatsoever — that is the sensor layer's job,
and a ledger that probes on write is the fire-time discovery this design removes.

Wired today (``infra/ssh_circuit.py``, the breaker's existing record sites):

* ``record_connection_success`` → ``connect`` / ``effective`` = ``ok``
* ``record_connection_failure`` → ``connect`` / ``effective`` = ``timeout`` or
  ``down`` (the breaker already distinguishes a wrapper ``TimeoutError`` from a
  connection marker), plus ``preamble`` / ``effective`` = ``timeout`` when that
  same call's doc satisfies ``is_preamble_degraded``.

Deliberately NOT fed from the breaker: ``auth``. Its ``SUCCESS`` verdict folds
"auth rejected but the host answered" into "reached the host", so an ``auth``
atom fed from there would assert what the evidence does not support. A seam by
construction, not by omission.

Left as seams for their owners: ``scratch`` (a preflight sensor holds the
result), ``scheduler`` (a submit-block sensor holds it), ``env`` (the env-lock
compare holds it). ``hop`` / ``direct`` / ``path`` are produced by the sensor
layer's ``sense_route_legs`` / ``read_path_readiness`` and reach this tier
through :func:`record_atoms`.

The overall verdict
-------------------

:func:`overall_verdict` (:data:`OVERALL_VERDICTS`), the one definition every
surface routes through:

* ``unknown`` — no atoms at all. Nothing has ever been observed for this host.
* ``degraded`` — some FRESH atom reads ``down`` or ``timeout``.
* ``stale`` — no fresh failure, but the evidence does not support ``ready``: a
  required sensor is missing, or ANY atom is past its freshness horizon or is
  not ``ok``. A stale failure lands here too — the host may have healed and
  nothing has looked since, which is exactly "we do not know".
* ``ready`` — every recorded atom is ``ok`` AND fresh, and every sensor in
  :data:`REQUIRED_SENSORS` is present.

:data:`REQUIRED_SENSORS` is deliberately just ``connect``: it is the only atom
anything feeds without probing, and promising more would make ``ready``
unreachable rather than more truthful. The rest never BLOCK ``ready`` by
absence — but a present one that failed or went stale does downgrade, so wiring
a seam can only ever make the verdict more honest, never less.

Storage
-------

``<journal home>/_readiness/<host>.json`` — a sibling of ``_ssh_circuit/``, keyed
by the SAME host normalization the breaker, the throttle and the sensor layer's
``_bare_host`` all use, so every substrate agrees on what "a host" is. Journal
home is :func:`hpc_agent.state.run_record.current_homedir`, so
``HPC_JOURNAL_DIR`` redirection — and the test suite's autouse isolation —
applies.

Writes go through ``infra.io.atomic_locked_update`` (advisory flock + temp file
+ fsync + ``os.replace``), so a crash mid-write leaves the previous doc or the
new doc, never a partial one. Reads are lock-free.

The write leaves a ``<name>.json.lock`` sentinel beside the doc. That is the
repo-wide contract, not this module's litter: ``infra.io.advisory_flock``
DELIBERATELY re-touches the sentinel on release (filelock's Windows backend
deletes it) to preserve the lingering-sentinel behaviour that run-dir loaders
depend on, pinned by
``tests/state/test_session.py::test_lock_file_skipped_by_loader``. Deleting it
here would contradict a checked contract, deviate from every sibling substrate
(``_ssh_circuit/`` leaves them too), and race a process mid-acquire on the same
path. It stays.

Deploy-tree hazard (2026-07-30, found by adversarial verification). This feed
made the breaker's HEALTHY path a journal-home WRITER for the first time — the
success recorder used to be read-only. Anything that enumerates a directory
tree while a feed is running will see ``_readiness/<host>.json`` and its
sentinel appear mid-walk. In production that is harmless because the journal
home is ``~/.claude/hpc/<repo_hash>/`` and deploy trees are experiment
directories — disjoint by construction, and pinned as a checked assumption by
``tests/state/test_readiness.py::TestTheLedgerStaysOutOfDeployTrees``. It is
NOT harmless in tests that redirect ``HPC_JOURNAL_DIR`` inside a ``tmp_path``
they then push: ``tests/infra/test_remote_rsync_fallback.py`` pushed
``tmp_path`` itself and its batch-count pin went 4 → 6. Such a test must push a
dedicated subtree, never the tmp root.

**Corruption is disclosed, never fatal.** :func:`read_ledger` always returns a
well-shaped document; an absent, unreadable or malformed file yields an EMPTY
ledger with ``corrupt=True`` (an ABSENT file reads ``corrupt=False`` — nothing
observed and unreadable are different facts). An empty ledger's verdict is
``unknown``, and the renderer names the corruption. Nothing in this module may
raise into a caller: it is fed from the fleet's own ban-risk protection path,
and a ledger is a freshness signal, never a correctness gate.

Write coalescing
----------------

The breaker's success recorder is a hot path (a lock-free read decides whether
anything needs changing; the steady healthy state costs one file read and zero
lock traffic). A locked ledger write on every success would regress the very
fleet-safety path it feeds. So :func:`record_observation` first reads lock-free
and returns without writing when the stored atom of the same identity already
carries the same verdict from the same source and is younger than
:data:`MIN_REWRITE_SEC`. The cost is that a healthy atom's age can lag reality by
up to that interval — far below every freshness horizon, and disclosed as age
rather than hidden. :func:`record_atoms` does NOT coalesce: a composed sensor
read is a deliberate, already-expensive act whose result must land whole.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

__all__ = [
    "ATOM_FIELDS",
    "AUTH",
    "CONNECT",
    "CONSULT_WINDOW_SEC",
    "DEFAULT_STALE_AFTER_SEC",
    "DIRECT",
    "ENV",
    "FUTURE_SKEW_TOLERANCE_SEC",
    "HOP",
    "MIN_REWRITE_SEC",
    "OVERALL_VERDICTS",
    "PATH",
    "PREAMBLE",
    "REQUIRED_SENSORS",
    "ROUTES",
    "SCHEDULER",
    "SCHEMA_VERSION",
    "SCRATCH",
    "SENSOR_KINDS",
    "SENSOR_VERDICTS",
    "SENSOR_KINDS_FROM_SENSOR_LAYER",
    "STALE_AFTER_SEC",
    "OverallVerdict",
    "atom_age_sec",
    "atom_future_skew_sec",
    "atom_identity",
    "atom_is_stale",
    "atom_mapping",
    "consult_atoms",
    "known_hosts",
    "ledger_age_sec",
    "ledger_host",
    "read_ledger",
    "readiness_path",
    "record_atoms",
    "record_observation",
    "stale_after_sec",
]

#: On-disk shape version. Readers tolerate unknown extra keys (forward-compat),
#: so a purely ADDITIVE atom field never needs a bump.
SCHEMA_VERSION = 1

OverallVerdict = Literal["ready", "stale", "degraded", "unknown"]

# ── the sensor vocabulary (one definition, two tiers) ────────────────────────

#: TCP leg: one ``ProxyJump`` hop of the effective chain.
HOP = "hop"
#: TCP leg: the hop-bypassing direct alternative to the target.
DIRECT = "direct"
#: The derived end-to-end verdict for the effective chain.
PATH = "path"
#: Command-class reading: a session established over a named route.
CONNECT = "connect"
#: Command-class reading: the ``module load`` / ``conda activate`` preamble.
PREAMBLE = "preamble"

# MIRROR: hpc_agent.infra.readiness_sensors::SensorKind pinned-by tests/state/test_readiness.py::test_sensor_vocabulary_is_in_lockstep_with_the_sensor_layer  # noqa: E501
#: The sensor layer's OWN ``SensorKind`` members, replicated (not imported: this
#: module is the storage tier and must not depend on the sensor tier, which
#: depends on subprocess/ssh). The lockstep test pins the two lists together.
SENSOR_KINDS_FROM_SENSOR_LAYER: tuple[str, ...] = (HOP, DIRECT, PATH, CONNECT, PREAMBLE)

#: Credentials accepted. No sensor covers it and the breaker CANNOT feed it (its
#: SUCCESS folds an auth rejection into "reached the host") — a seam by
#: construction.
AUTH = "auth"
#: Scratch reachability / writability. Seam: a preflight sensor holds the result.
SCRATCH = "scratch"
#: The scheduler answered. Seam: a submit-block sensor holds the result.
SCHEDULER = "scheduler"
#: Remote env fingerprint vs the expected wheel. Seam: the env-lock compare holds it.
ENV = "env"

#: Every sensor kind the DURABLE ledger stores, in render order: the sensor
#: layer's vocabulary extended with the four pillar-3 invariants no sensor covers
#: yet. One flat vocabulary — a new invariant is a new member here, never a
#: parallel enum. An atom whose sensor is not listed is ignored on read, so a
#: foreign or future writer can never move a host's verdict.
SENSOR_KINDS: tuple[str, ...] = (
    *SENSOR_KINDS_FROM_SENSOR_LAYER,
    AUTH,
    SCRATCH,
    SCHEDULER,
    ENV,
)

# MIRROR: hpc_agent.infra.readiness_sensors::SensorVerdict pinned-by tests/state/test_readiness.py::test_sensor_vocabulary_is_in_lockstep_with_the_sensor_layer  # noqa: E501
#: The sensor layer's verdict vocabulary, VERBATIM. ``unknown`` = the sensor ran
#: but could not settle it; ``skipped`` = it never ran. Neither is "fine", and
#: neither grants readiness.
SENSOR_VERDICTS: tuple[str, ...] = ("ok", "down", "timeout", "unknown", "skipped")

#: Verdicts that are positive evidence of a failure (as opposed to absence of
#: evidence). Only these can make an overall verdict ``degraded``.
_FAILING_VERDICTS: frozenset[str] = frozenset({"down", "timeout"})

# MIRROR: hpc_agent.infra.readiness_sensors::VerdictAtom.route pinned-by tests/state/test_readiness.py::test_sensor_vocabulary_is_in_lockstep_with_the_sensor_layer  # noqa: E501
#: The route axis an atom was read over. ``effective`` is what ``ssh -G``
#: resolved (hops included); ``direct`` bypasses the hops and is the
#: discriminator that catches a dead ``ProxyJump`` behind a hostname that
#: answers; ``n/a`` is for readings with no route dimension.
ROUTES: tuple[str, ...] = ("effective", "direct", "n/a")

# MIRROR: hpc_agent.infra.readiness_sensors::VerdictAtom pinned-by tests/state/test_readiness.py::test_sensor_vocabulary_is_in_lockstep_with_the_sensor_layer  # noqa: E501
#: ``VerdictAtom``'s field names, stored verbatim. ``source`` is NOT here: it is
#: additive durable-tier-only metadata (which seam recorded the atom) and is
#: dropped when an atom is handed back to the sensor layer.
ATOM_FIELDS: tuple[str, ...] = (
    "sensor",
    "target",
    "verdict",
    "detail",
    "latency_ms",
    "at",
    "at_epoch",
    "route",
)

#: Overall verdict vocabulary, in the order :func:`overall_verdict` tests them.
OVERALL_VERDICTS: tuple[str, ...] = ("unknown", "degraded", "stale", "ready")

#: Sensors that must be present, ``ok`` and fresh for an overall ``ready``. Just
#: the one atom anything feeds without probing — see the module docstring.
REQUIRED_SENSORS: tuple[str, ...] = (CONNECT,)

#: Default freshness horizon (seconds) for the OVERALL verdict. 15 minutes: long
#: enough that a working session keeps its own atoms fresh, short enough that a
#: VPN flap or a login-node reboot between sessions reads ``stale``, not ``ready``.
DEFAULT_STALE_AFTER_SEC = 900.0

#: Per-sensor horizons overriding :data:`DEFAULT_STALE_AFTER_SEC`. The env
#: fingerprint answers "is the right wheel installed", which changes on a
#: deliberate reinstall — hours, not minutes — so holding it to the transport
#: horizon would keep every ledger permanently ``stale`` for no evidence gain.
STALE_AFTER_SEC: dict[str, float] = {ENV: 86400.0}

# MIRROR: hpc_agent.infra.readiness_sensors::DEFAULT_FRESHNESS_WINDOW_SEC pinned-by tests/state/test_readiness.py::test_sensor_vocabulary_is_in_lockstep_with_the_sensor_layer  # noqa: E501
#: Default window for :func:`consult_atoms` — the DURABLE half of the sensor
#: layer's consult-first path, so it must match that layer's own window or a
#: composer would re-dial legs the disk could have answered. Deliberately much
#: shorter than :data:`DEFAULT_STALE_AFTER_SEC`: consulting decides "may I skip a
#: probe", the overall verdict decides "may I claim this cluster is ready".
CONSULT_WINDOW_SEC = 120.0

#: Minimum interval (seconds) between two writes of an UNCHANGED atom — see the
#: module docstring's write-coalescing note.
MIN_REWRITE_SEC = 60.0

#: How far into the future an ``at`` stamp may sit and still be believed
#: (seconds). The ledger is written by every process on the box and read by
#: every other, so a couple of minutes of ordinary clock skew must not turn a
#: healthy atom stale. Beyond it the stamp is not skew but a value we cannot
#: believe, and :func:`atom_is_stale` treats it as undateable — without this, a
#: far-future stamp clamps to age 0 and pins the host at ``ready`` forever.
FUTURE_SKEW_TOLERANCE_SEC = 300.0


def _host(ssh_target: str) -> str:
    """Host key for *ssh_target* (``user@host`` or a bare alias).

    Agrees with :func:`hpc_agent.infra.ssh_circuit._host`,
    :func:`hpc_agent.infra.ssh_throttle._host` and the sensor layer's
    ``_bare_host`` — every substrate MUST key identically or an atom would land
    under a different host than the state it was harvested from.
    """
    return ssh_target.rsplit("@", 1)[-1].strip()


def _safe_name(host: str) -> str:
    """Filesystem-safe filename component for *host* (the breaker's convention)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", host)


def readiness_path(host: str) -> Path:
    """Ledger file for *host*: ``<journal home>/_readiness/<host>.json``."""
    from hpc_agent.state.run_record import current_homedir

    return current_homedir() / "_readiness" / f"{_safe_name(_host(host))}.json"


def _fresh_doc(host: str) -> dict[str, Any]:
    """An empty, well-shaped ledger for *host*."""
    return {"schema_version": SCHEMA_VERSION, "host": host, "atoms": []}


def _parse_at(value: Any) -> datetime | None:
    """Parse an atom's ``at`` stamp, or ``None`` when absent / unparseable.

    ``at`` — the ISO string — is THE freshness ingredient at this tier, and
    ``at_epoch`` is carried verbatim but never consulted here. One definition of
    freshness per tier: the sensor layer ages atoms by ``at_epoch`` (its
    ``fresh_atoms`` / ``consult_readiness`` window), this tier ages them by
    ``at``. The two CAN disagree — the sensor layer stamps ``at=utcnow_iso()``
    from the wall clock while ``at_epoch`` takes its injected ``now``, so under a
    test clock they diverge by however far the injection sits from real time.
    Consulting both, or picking whichever is present, would make an atom's age
    depend on which reader asked. Pinned by
    ``tests/state/test_readiness.py::TestFreshnessHasOneDefinitionPerTier``.
    """
    from hpc_agent.infra.time import parse_iso_utc_or_none

    return parse_iso_utc_or_none(value if isinstance(value, str) else None)


def atom_identity(atom: Mapping[str, Any]) -> tuple[str, str, str]:
    """The ``(sensor, route, target)`` triple an atom is stored under.

    The ledger holds ONE atom per identity, most recent wins — a ledger, not a
    log. ``target`` is part of it because a chain has several hops and each is a
    distinct subject; ``route`` is part of it because the same sensor read over
    the effective and the direct route is the whole dead-hop discriminator.
    """
    return (
        str(atom.get("sensor") or ""),
        str(atom.get("route") or "n/a"),
        str(atom.get("target") or ""),
    )


def atom_mapping(obj: Any, *, source: str = "") -> dict[str, Any]:
    """Project a ``VerdictAtom`` (or any mapping/object with its fields) to a dict.

    Duck-typed on purpose: this tier stores the sensor layer's unit of record
    without importing the sensor layer (which pulls in subprocess/ssh). Unknown
    attributes are ignored, missing ones take their ``VerdictAtom`` defaults.
    """
    get = obj.get if hasattr(obj, "get") else lambda name, default=None: getattr(obj, name, default)
    out: dict[str, Any] = {
        "sensor": str(get("sensor", "") or ""),
        "target": str(get("target", "") or ""),
        "verdict": str(get("verdict", "") or ""),
        "detail": str(get("detail", "") or "")[:300],
        "latency_ms": _num_or_none(get("latency_ms", None)),
        "at": str(get("at", "") or ""),
        "at_epoch": float(get("at_epoch", 0.0) or 0.0),
        "route": str(get("route", "n/a") or "n/a"),
    }
    if source:
        out["source"] = str(source)
    return out


def _num_or_none(value: Any) -> float | None:
    """A real number, or ``None`` (``bool`` is an ``int`` subclass — exclude it)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _clean_atoms(raw: Any) -> list[dict[str, Any]]:
    """The usable atoms of a stored ``atoms`` list.

    Drops anything whose ``sensor`` or ``verdict`` is outside the declared
    vocabulary: a foreign or future writer must never move this host's verdict.
    """
    if not isinstance(raw, list):
        return []
    return [
        atom
        for atom in raw
        if isinstance(atom, dict)
        and atom.get("sensor") in SENSOR_KINDS
        and atom.get("verdict") in SENSOR_VERDICTS
    ]


def read_ledger(host: str) -> dict[str, Any]:
    """Read *host*'s ledger; ALWAYS returns a well-shaped doc, never raises.

    Carries ``schema_version`` / ``host`` / ``atoms`` (a list) plus two read-side
    annotations:

    * ``corrupt`` — this read could not use the file:

      - absent file → empty ledger, ``corrupt=False`` (nothing has been observed;
        "never looked" and "cannot read" are different facts)
      - unreadable / malformed / non-dict / ``atoms`` not a list → EMPTY ledger,
        ``corrupt=True``
      - ``schema_version`` absent, non-integer, or newer than
        :data:`SCHEMA_VERSION` → EMPTY ledger, ``corrupt=True``. A version this
        build does not understand is not a doc to interpret optimistically: the
        atom shape is exactly what a future version would change, and reading a
        future doc under today's rules is how a stale verdict is asserted as
        current.

    * ``corruption_reason`` — which of those it was (``""`` when clean).

    ``corrupt`` / ``corruption_reason`` are never persisted; every write rebuilds
    the document. The write path DOES carry the corruption forward once, as a
    persisted ``last_corruption`` note, so the disclosure survives the rebuild
    (see :func:`_write_atoms`).
    """
    import json

    key = _host(host)
    doc = _fresh_doc(key)
    doc["corrupt"] = False
    doc["corruption_reason"] = ""

    def _corrupt(reason: str) -> dict[str, Any]:
        doc["corrupt"] = True
        doc["corruption_reason"] = reason
        return doc

    try:
        raw = readiness_path(key).read_text(encoding="utf-8")
    except FileNotFoundError:
        return doc
    except (OSError, UnicodeDecodeError) as exc:
        return _corrupt(f"unreadable ({type(exc).__name__})")
    try:
        parsed = json.loads(raw)
    except ValueError:
        return _corrupt("not valid JSON")
    if not isinstance(parsed, dict):
        return _corrupt("not a JSON object")
    version = parsed.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        return _corrupt("no usable schema_version")
    if version > SCHEMA_VERSION:
        return _corrupt(f"schema_version {version} is newer than this build's {SCHEMA_VERSION}")
    if not isinstance(parsed.get("atoms"), list):
        return _corrupt("atoms is not a list")
    doc["atoms"] = _clean_atoms(parsed.get("atoms"))
    stored_host = parsed.get("host")
    if isinstance(stored_host, str) and stored_host:
        doc["host"] = stored_host
    # A note left by the write that rebuilt over a corrupt file — carried so the
    # operator sees the corruption ONCE rather than having it vanish silently.
    last = parsed.get("last_corruption")
    if isinstance(last, dict):
        doc["last_corruption"] = last
    return doc


def ledger_host(doc: dict[str, Any] | None) -> str:
    """The host a ledger *doc* is about (``""`` when the doc is unusable)."""
    if not isinstance(doc, dict):
        return ""
    host = doc.get("host")
    return host if isinstance(host, str) else ""


def stale_after_sec(sensor: str) -> float:
    """The overall-verdict freshness horizon (seconds) for *sensor*."""
    return STALE_AFTER_SEC.get(sensor, DEFAULT_STALE_AFTER_SEC)


def atom_age_sec(atom: Any, *, now: datetime) -> float | None:
    """RENDERING age of *atom* in seconds at *now*; ``None`` when undateable.

    A stamp slightly in the future (ordinary skew between the writing and the
    reading machine) clamps to ``0.0``, because a negative age is not a thing
    that can be rendered. **This clamp is for display only** — do not build
    freshness on it: :func:`atom_is_stale` deliberately does NOT go through the
    clamp, because clamping made a year-9999 stamp read as age 0 and therefore
    permanently ``ready`` (a doc that names itself fresh forever is the exact
    failure the ledger exists to prevent). See :func:`atom_future_skew_sec`.
    """
    if not isinstance(atom, dict):
        return None
    at = _parse_at(atom.get("at"))
    if at is None:
        return None
    return max(0.0, (now - at).total_seconds())


def atom_future_skew_sec(atom: Any, *, now: datetime) -> float:
    """How far *atom*'s stamp lies in the FUTURE of *now* (``0.0`` when it does not).

    Separated from :func:`atom_age_sec` so the render's clamp and the freshness
    decision cannot share a definition: the renderer wants "0s ago", the verdict
    wants to know the stamp is unbelievable.
    """
    if not isinstance(atom, dict):
        return 0.0
    at = _parse_at(atom.get("at"))
    if at is None:
        return 0.0
    return max(0.0, (at - now).total_seconds())


def atom_is_stale(atom: Any, *, now: datetime) -> bool:
    """True when *atom*'s evidence cannot be asserted as current at *now*.

    Three ways to be stale, all forms of "we do not know":

    1. **Undateable** — no usable ``at`` stamp. Evidence that cannot be dated
       cannot be claimed to be current.
    2. **Implausibly future-dated** — the stamp sits more than
       :data:`FUTURE_SKEW_TOLERANCE_SEC` ahead of *now*. Ordinary clock skew
       between the writing and reading machine is absorbed by the tolerance; a
       stamp beyond it is not skew, it is a stamp we cannot believe, and
       believing it would make the atom read fresh forever (a year-9999 ``at``
       would otherwise clamp to age 0 and pin the host at ``ready``).
    3. **Past its horizon** — older than :func:`stale_after_sec` for its sensor.
    """
    if not isinstance(atom, dict):
        return True
    if atom_future_skew_sec(atom, now=now) > FUTURE_SKEW_TOLERANCE_SEC:
        return True
    age = atom_age_sec(atom, now=now)
    if age is None:
        return True
    return age > stale_after_sec(str(atom.get("sensor") or ""))


def overall_verdict(doc: dict[str, Any] | None, *, now: datetime) -> OverallVerdict:
    """The single overall verdict for a ledger *doc* at *now*.

    The one definition every surface routes through. Order of tests — each stated
    as what the evidence supports, never as a judgment:

    1. no atoms at all → ``unknown``
    2. any FRESH ``down`` / ``timeout`` atom → ``degraded``
    3. a missing :data:`REQUIRED_SENSORS` atom, or ANY atom that is stale or not
       ``ok`` → ``stale``
    4. otherwise → ``ready``

    A STALE failure falls to ``stale``, not ``degraded``: the host may have healed
    and nothing has looked since, and fencing a cluster on expired evidence is
    the mistake ``ssh_circuit.effective_state`` exists to avoid.
    """
    atoms = _clean_atoms((doc or {}).get("atoms") if isinstance(doc, dict) else None)
    if not atoms:
        return "unknown"
    for atom in atoms:
        if atom["verdict"] in _FAILING_VERDICTS and not atom_is_stale(atom, now=now):
            return "degraded"
    present = {atom["sensor"] for atom in atoms}
    for sensor in REQUIRED_SENSORS:
        if sensor not in present:
            return "stale"
    for atom in atoms:
        if atom["verdict"] != "ok" or atom_is_stale(atom, now=now):
            return "stale"
    return "ready"


def ledger_age_sec(doc: dict[str, Any] | None, *, as_of: datetime) -> float | None:
    """Age (seconds) of the FRESHEST atom that was already recorded at *as_of*.

    "How old was this ledger when someone consulted it at *as_of*." Atoms stamped
    AFTER *as_of* are excluded — they did not exist yet — so this is an honest
    reconstruction rather than a read of today's file. ``None`` when no atom
    predates *as_of* (including "no ledger at all"), which is the honest answer,
    not zero.

    Exact whenever nothing was refreshed since *as_of*. Because the ledger keeps
    only the LATEST atom per identity, an identity refreshed afterwards is
    excluded rather than back-dated — so the number is always an age some atom
    really had. A live stamp taken at the consult site would make it exact in
    every case; that is the pillar-6 integration seam (see ``state/s2_slo.py``).
    """
    atoms = _clean_atoms((doc or {}).get("atoms") if isinstance(doc, dict) else None)
    newest: datetime | None = None
    for atom in atoms:
        at = _parse_at(atom.get("at"))
        if at is None or at > as_of:
            continue
        if newest is None or at > newest:
            newest = at
    if newest is None:
        return None
    return max(0.0, (as_of - newest).total_seconds())


def consult_atoms(
    host: str, *, window_sec: float = CONSULT_WINDOW_SEC, now: datetime | None = None
) -> list[dict[str, Any]]:
    """The durable tier's half of consult-first: *host*'s atoms read within *window_sec*.

    The sibling of the sensor layer's ``fresh_atoms`` / ``consult_readiness``,
    over the DURABLE store. Read path is consult-process-then-durable: a composer
    asks its in-process cache first (sub-second, this invocation's own readings),
    falls through to here (what any process on this box learned recently), and
    senses only what BOTH returned as absent or stale.

    Integration seam (two lines, in ``infra/readiness_sensors.py``, owned by the
    sensor layer): ``consult_readiness`` falls through to this on a cache miss,
    and ``record_readiness`` write-throughs via :func:`record_atoms`. Nothing
    else moves — that is the point of storing the sensor layer's own atom shape.
    """
    stamp = now if now is not None else datetime.now(timezone.utc)
    return [
        atom
        for atom in read_ledger(host)["atoms"]
        if (age := atom_age_sec(atom, now=stamp)) is not None and age <= float(window_sec)
    ]


def _doc_is_usable(doc: Any) -> bool:
    """Whether a parsed doc may be merged into — the WRITE-side twin of
    :func:`read_ledger`'s validation, so the two agree on what "usable" means.

    A doc whose ``schema_version`` this build does not understand is NOT usable:
    merging into it would silently down-convert a future shape and then write it
    back as if it were current.
    """
    if not isinstance(doc, dict) or not isinstance(doc.get("atoms"), list):
        return False
    version = doc.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool):
        return False
    return version <= SCHEMA_VERSION


def _write_atoms(
    host: str,
    incoming: list[dict[str, Any]],
    *,
    corruption: str = "",
    now: datetime | None = None,
) -> bool:
    """Upsert *incoming* atoms into *host*'s ledger by identity. Never raises.

    *corruption* is the reason the caller's pre-read found the file unusable
    (``""`` when it was clean). A rebuild that discards a corrupt file must not
    also discard the FACT of the corruption — otherwise the operator's only
    signal disappears the instant any traffic touches the host, and a ledger
    that silently ate a file looks identical to one that was always fine. The
    reason is persisted once as ``last_corruption`` and carried forward
    thereafter.
    """
    try:
        from hpc_agent.infra.io import atomic_locked_update

        stamp = now if now is not None else datetime.now(timezone.utc)

        def _mutate(doc: dict[str, Any] | None) -> dict[str, Any]:
            # An absent / corrupt / future-versioned doc rebuilds empty — the
            # corrupt-file honesty contract applies on the WRITE side too: never
            # merge into a document that could not be trusted.
            fresh = _fresh_doc(host)
            kept: dict[tuple[str, str, str], dict[str, Any]] = {}
            usable = _doc_is_usable(doc)
            if usable and isinstance(doc, dict):
                for atom in _clean_atoms(doc.get("atoms")):
                    kept[atom_identity(atom)] = atom
                prior = doc.get("last_corruption")
                if isinstance(prior, dict):
                    fresh["last_corruption"] = prior
            for atom in incoming:
                kept[atom_identity(atom)] = atom
            # Re-detect here as well as trusting the caller's pre-read: a doc
            # that parses but carries an unknown schema_version never reaches
            # the caller's ``corruption`` string (its read returned early), and
            # the file can also be torn between that read and this lock.
            reason = corruption or ("" if usable or doc is None else "unusable document")
            if reason:
                fresh["last_corruption"] = {
                    "at": stamp.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    "reason": reason,
                }
            # Sorted by identity so the file is stable under reordering — a
            # byte-diff of two ledgers reflects verdicts, never write order.
            fresh["atoms"] = [kept[key] for key in sorted(kept)]
            return fresh

        atomic_locked_update(readiness_path(host), _mutate)
        return True
    except Exception:  # noqa: BLE001 — total fail-open; the ledger is never a gate
        return False


def record_atoms(
    host: str,
    atoms: Sequence[Any],
    *,
    source: str = "",
    now: datetime | None = None,
) -> int:
    """Write *atoms* through to the durable ledger. The bulk recording interface.

    Accepts ``VerdictAtom`` instances (duck-typed) or plain mappings with its
    field names — the write-through half of the two-tier ledger, called by a
    composer that has just SENSED. Each atom upserts by :func:`atom_identity`;
    unknown sensors and verdicts are dropped rather than stored.

    Unlike :func:`record_observation` this does NOT coalesce: a composed sensor
    read is a deliberate, already-expensive act whose whole result must land.

    An atom missing its ``at`` stamp is stamped at *now* (defaulting to the wall
    clock) so it is dateable — an undateable atom is permanently stale and would
    silently never satisfy a freshness window.

    :returns: how many atoms were stored. Never raises.
    """
    try:
        key = _host(host)
        if not key:
            return 0
        stamp = now if now is not None else datetime.now(timezone.utc)
        prepared: list[dict[str, Any]] = []
        for obj in atoms:
            mapping = atom_mapping(obj, source=source)
            if mapping["sensor"] not in SENSOR_KINDS or mapping["verdict"] not in SENSOR_VERDICTS:
                continue
            if not mapping["at"]:
                mapping["at"] = stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
                mapping["at_epoch"] = stamp.timestamp()
            prepared.append(mapping)
        if not prepared:
            return 0
        corruption = str(read_ledger(key).get("corruption_reason") or "")
        return len(prepared) if _write_atoms(key, prepared, corruption=corruption, now=stamp) else 0
    except Exception:  # noqa: BLE001 — total fail-open; see the module docstring
        return 0


def record_observation(
    host: str,
    sensor: str,
    verdict: str,
    *,
    source: str,
    target: str = "",
    route: str = "n/a",
    latency_ms: float | None = None,
    detail: str = "",
    now: datetime | None = None,
    min_rewrite_sec: float = MIN_REWRITE_SEC,
) -> bool:
    """Record ONE harvested atom. The narrow single-observation interface.

    The standing rule for every call site is **harvest, never probe**: call this
    where a result is ALREADY in hand. A feed site that would open a connection,
    run a remote command, or otherwise add a network call is out of contract —
    sensing is the sensor layer's job, and the ledger's whole value is that it
    costs nothing.

    :param host: ``user@host`` or a bare host/alias; normalized to the breaker's
        host key so every substrate agrees on identity.
    :param sensor: one of :data:`SENSOR_KINDS`. An unknown sensor is IGNORED
        (returns ``False``) — a typo must not create a phantom atom.
    :param verdict: one of :data:`SENSOR_VERDICTS`. Unknown values are ignored.
    :param source: opaque provenance string naming the seam that observed it
        (e.g. ``"ssh-circuit"``). Durable-tier-only; rendered, never interpreted.
    :param target: what was probed; defaults to *host*.
    :param route: one of :data:`ROUTES`.
    :param latency_ms: the observation's own measured duration when the site
        holds one, else ``None``. Never estimated.
    :param detail: short opaque note; truncated to 300 chars.
    :param now: injected observation instant (tests); defaults to wall clock.
    :param min_rewrite_sec: coalescing window — see the module docstring.

    :returns: ``True`` iff the ledger was written. Never raises.
    """
    try:
        key = _host(host)
        if not key or sensor not in SENSOR_KINDS or verdict not in SENSOR_VERDICTS:
            return False
        if route not in ROUTES:
            route = "n/a"
        stamp = now if now is not None else datetime.now(timezone.utc)
        atom = {
            "sensor": sensor,
            "target": target or key,
            "verdict": verdict,
            "detail": str(detail)[:300],
            "latency_ms": _num_or_none(latency_ms),
            "at": stamp.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "at_epoch": stamp.timestamp(),
            "route": route,
            "source": str(source),
        }

        # Lock-free coalescing read FIRST (the breaker's hot-path idiom): an
        # unchanged atom younger than the window needs no write at all, so the
        # steady healthy state costs one file read and zero lock traffic. (A
        # corrupt doc reads with NO atoms, so it never coalesces — the next
        # observation rebuilds the file rather than skipping the write.)
        identity = atom_identity(atom)
        prior = read_ledger(key)
        for existing in prior["atoms"]:
            if atom_identity(existing) != identity:
                continue
            age = atom_age_sec(existing, now=stamp)
            if (
                age is not None
                and age < min_rewrite_sec
                and existing.get("verdict") == verdict
                and existing.get("source") == source
            ):
                return False
            break

        return _write_atoms(
            key,
            [atom],
            corruption=str(prior.get("corruption_reason") or ""),
            now=stamp,
        )
    except Exception:  # noqa: BLE001 — total fail-open; see the module docstring
        return False


def known_hosts() -> list[str]:
    """Every host with a readiness ledger on this machine, sorted.

    Prefers each doc's own ``host`` field over un-mangling the filename
    (:func:`_safe_name` is lossy). A CORRUPT doc still contributes its host —
    :func:`read_ledger` falls back to the filename stem — deliberately: a host
    whose ledger is unreadable must stay VISIBLE (reported ``unknown`` with
    ``corrupt=True``) rather than vanishing from the readiness report, because
    disappearing from a report reads as "nothing to worry about".

    Fail-open: an unreadable ledger dir yields ``[]`` and never raises.
    """
    try:
        from hpc_agent.state.run_record import current_homedir

        entries = sorted((current_homedir() / "_readiness").glob("*.json"))
    except OSError:
        return []
    hosts: set[str] = set()
    for path in entries:
        if path.name.endswith(".lock"):
            continue
        host = ledger_host(read_ledger(path.stem))
        if host:
            hosts.add(host)
    return sorted(hosts)
