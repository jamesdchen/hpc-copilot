"""Cluster readiness SENSORS — the verdict-atom layer under every path claim.

This is the sensor layer of the standing per-cluster readiness ledger
(``docs/design/s2-readiness.md`` pillar 1). Every function here is a PURE
SENSOR: it takes a *resolved target* (a chain element, the direct alternative,
or a command class) and returns a structured :class:`VerdictAtom` — what was
probed, the verdict, how long it took, and when. Nothing here knows about
``net-triage``, ``submit-s2``, or any verb: the triage verb is a thin composer
over these sensors, the S2 pre-detach gate is a consult-then-sense composer over
the same atoms, and the readiness ledger's opportunistic refresh path
(pillar 3) will be a third composer that stores atoms instead of rendering them.
Coupling any verb's shape into this layer would close that seam.

The 2026-07-30 night incident that earned the layer
---------------------------------------------------

``net-triage`` reported ``hoffman2: reachable`` while the configured
``ProxyJump usc-discovery`` hop was dead. It probed the BARE HOSTNAME, which
answered, and blessed a cluster failover INTO the dead hop. Two detached S2
workers then died in ~16s each and the cause was only discoverable by a human
reading worker logs afterwards. Separately, the SSH circuit breaker read
"probe-OK + preamble-timeout" as node-local degradation ("NOT a transport
fault", recommending a host-retarget to a sibling *through the same dead hop*)
when a flapping VPN tunnel dropping mid-command produces the IDENTICAL
signature.

Three mechanisms, one per leg of that chain:

1. **The effective chain is read from ssh's OWN resolution.** :func:`resolve_route`
   shells ``ssh -G <host>`` and text-parses the answer (``hostname`` / ``user`` /
   ``port`` / ``proxyjump``). It NEVER re-implements ``ssh_config`` parsing —
   ``Host`` patterns, ``Match`` blocks, ``Include`` chains and per-host overrides
   are OpenSSH's business, and a second parser would drift from the client that
   actually dials. ``ssh -G`` opens NO connection: it is config resolution only.

2. **Every leg is sensed SEPARATELY and labelled.** :func:`sense_route_legs`
   emits one atom per ``ProxyJump`` hop, one for the direct (jump-bypassed)
   alternative, and one DERIVED ``path`` atom. A jumped host whose hop is dead
   can therefore never read "reachable" off the bare hostname again: the path is
   dead, the direct alternative may be fine, and both are said out loud.

3. **The preamble class is sensed over BOTH routes.** :func:`sense_preamble` runs
   the caller-supplied activation (the cluster's ``module load … && source
   …/conda.sh && conda activate …`` from ``clusters.yaml``) as ``{connect,
   preamble}`` atoms, over the effective chain and — when a jump exists and the
   effective route showed trouble — over the direct route with
   ``-o ProxyJump=none``. That second run is the DISCRIMINATOR the 2026-07-30
   incident was actually settled by: the same command class that hung through the
   tunnel returning instant preamble-OK on the direct route proves a TRANSPORT
   fault, not node-local degradation.

The four named invariants (pillar 3)
------------------------------------

Transport is one of five invariants S2 needs, and the design's pillar 3 gives
each its own verdict vocabulary and owner. The remaining four live here as
sensors of the same shape — one bounded command class, one :class:`VerdictAtom`:

* :func:`sense_scratch` — ``test -d`` + ``df -P`` of the cluster's scratch, so a
  hung mount answers rather than a cached ``stat``.
* :func:`sense_scheduler` — the backend family's own cheap CLI banner
  (``squeue --version`` / ``qstat -help`` class), resolved from ``clusters.yaml``
  via :func:`scheduler_family_for_cluster`. An unknown family is ``skipped``,
  never a guessed binary.
* :func:`sense_env` — ``hpc-agent --version`` under the cluster activation: the
  command class the release flow ALREADY reads on every target env, reused so a
  sensed fingerprint is comparable to a released one.
* :func:`auth_atom` — no probe at all. It reads the connect sensor's own
  exit/stderr signature and separates "the host refused our credentials" from
  "we never reached the host". This is the discrimination the circuit breaker
  structurally cannot make (its SUCCESS verdict folds an auth rejection into
  "reached the host"), which is why ``auth`` was a seam by construction until
  this leg existed.

The three probing rungs are OPT-IN on :func:`read_path_readiness` — each costs
one connection, and a gate that only needs the path question answered must not
pay for storage. Their atoms are evidence for the standing ledger; they never
enter :func:`_classify`, because a full scratch disk is not a reason to call a
path dead.

Transport discipline
--------------------

Every probe goes through the framework's own seams —
:func:`hpc_agent.infra.ssh_options.ssh_argv` builds the argv (binary resolution,
``BatchMode``, ``ConnectTimeout``, crypto, multiplexing) and
:func:`hpc_agent.infra.remote.capture_via_select` is the bounded capture. This is
the same pair :func:`hpc_agent.infra.ssh_circuit.liveness_probe` uses, and for
the same two reasons: a sensor must be able to pin ``-o ProxyJump=none`` (which
the ``ssh_run`` string seam cannot express) and must NOT re-enter the breaker /
slot machinery it is sensing. Like that probe, a sensor takes no per-host
connection slot and never transitions a circuit — every atom is EVIDENCE only.

Bounded and never a storm: at most one connect per leg per call, no retry loop
inside a sensor (a retry here is exactly the connection storm the breaker exists
to prevent), and the ``ssh -G`` resolution is process-cached (ssh config does not
change mid-process).
"""

from __future__ import annotations

import functools
import socket
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from hpc_agent.infra.time import utcnow_iso

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

__all__ = [
    "AUTH_REJECTION_MARKERS",
    "DEFAULT_SSH_PORT",
    "ENV_FINGERPRINT_COMMAND",
    "NEVER_REACHED_MARKERS",
    "SCHEDULER_TOUCH_COMMANDS",
    "PathCause",
    "PathReadiness",
    "RouteChain",
    "SensorKind",
    "SensorVerdict",
    "VerdictAtom",
    "auth_atom",
    "auth_signature",
    "clear_readiness_ledger",
    "clear_route_cache",
    "consult_readiness",
    "env_fingerprint_command",
    "fresh_atoms",
    "path_remediation",
    "read_path_readiness",
    "record_readiness",
    "resolve_route",
    "scheduler_family_for_cluster",
    "scheduler_touch_command",
    "scratch_command",
    "sense_command_class",
    "sense_env",
    "sense_leg",
    "sense_preamble",
    "sense_route_legs",
    "sense_scheduler",
    "sense_scratch",
    "storage_failure_marker",
    "tcp_connect",
]

#: The port every leg senses — SSH, the only port this framework needs.
DEFAULT_SSH_PORT = 22

#: Budget for the ``ssh -G`` config resolution. No network happens, so this is a
#: generous guard against a wedged local binary, not a connect bound.
ROUTE_RESOLVE_TIMEOUT_SEC = 10.0

#: Budget for one bounded TCP connect to a leg's ``host:port``.
LEG_CONNECT_TIMEOUT_SEC = 8.0

#: Budget for ONE command-class ssh attempt. Deliberately short relative to
#: ``SSH_TIMEOUT_SEC``: this rung exists to catch a preamble that HANGS, and the
#: point of the discriminator is that the human learns the answer in seconds
#: rather than from a dead worker's log minutes later.
PREAMBLE_TIMEOUT_SEC = 30.0

#: Default freshness window for a consulted atom (:func:`consult_readiness`).
#: Short by design: a link that flaps invalidates a reading fast, and the cost of
#: re-sensing is one bounded connect.
DEFAULT_FRESHNESS_WINDOW_SEC = 120.0


def _bare_host(value: str) -> str:
    """The bare hostname of a ``[user@]host[:port]`` hop/target token.

    The breaker and the connect sensor both key on the bare host — the SAME
    normalization :func:`hpc_agent.infra.ssh_circuit._host` applies.
    """
    text = value.strip()
    if not text:
        return ""
    text = text.rsplit("@", 1)[-1]
    # An IPv6 literal is bracketed (``[::1]:22``); anything else may carry ``:port``.
    if text.startswith("["):
        closing = text.find("]")
        if closing != -1:
            return text[1:closing]
    return text.split(":", 1)[0].strip()


# ── the verdict atom: the ledger's unit of record ────────────────────────────

# MIRROR: hpc_agent.state.readiness::SENSOR_KINDS_FROM_SENSOR_LAYER pinned-by tests/state/test_readiness.py::test_sensor_vocabulary_is_in_lockstep_with_the_sensor_layer  # noqa: E501
#: What a sensor probed. ``hop`` / ``direct`` are TCP legs, ``path`` is the
#: derived end-to-end verdict for the effective chain, ``connect`` / ``preamble``
#: are command-class readings over a named route, and ``auth`` / ``scratch`` /
#: ``scheduler`` / ``env`` are the four remaining pillar-3 invariants (credentials
#: accepted, storage reachable, the scheduler answers, the remote wheel is the
#: expected one). ONE flat vocabulary — the durable storage tier replicates this
#: exact tuple (it may not import this module, which pulls in subprocess/ssh), so
#: a new invariant is a new member HERE and never a parallel enum there.
SensorKind = Literal[
    "hop",
    "direct",
    "path",
    "connect",
    "preamble",
    "auth",
    "scratch",
    "scheduler",
    "env",
]

#: A sensor's answer. ``unknown`` means the sensor ran but could not settle it;
#: ``skipped`` means it never ran (and ``detail`` says why). Neither is "fine".
SensorVerdict = Literal["ok", "down", "timeout", "unknown", "skipped"]


@dataclass(frozen=True)
class VerdictAtom:
    """ONE sensor reading — the readiness ledger's unit of record (pillar 1).

    Deliberately verb-free and self-describing: *what* was probed (:attr:`sensor`
    + :attr:`target` + :attr:`route`), the answer (:attr:`verdict` + human
    :attr:`detail`), how long it took (:attr:`latency_ms`) and *when*
    (:attr:`at` / :attr:`at_epoch`, which is what makes a freshness window
    computable). A composer renders atoms; the ledger stores them; nothing about
    either shape leaks back into the sensor that produced this.
    """

    sensor: SensorKind
    target: str
    verdict: SensorVerdict
    detail: str = ""
    latency_ms: float | None = None
    at: str = ""
    at_epoch: float = 0.0
    route: Literal["effective", "direct", "n/a"] = "n/a"

    @property
    def ok(self) -> bool | None:
        """Tri-state projection: ``True``/``False``/``None`` (never ran or unsettled)."""
        if self.verdict == "ok":
            return True
        if self.verdict in ("down", "timeout"):
            return False
        return None


def _atom(
    sensor: SensorKind,
    target: str,
    verdict: SensorVerdict,
    detail: str,
    *,
    latency_ms: float | None = None,
    route: Literal["effective", "direct", "n/a"] = "n/a",
    now: float | None = None,
) -> VerdictAtom:
    """Stamp one atom with its wall-clock instant (the freshness ingredient)."""
    epoch = time.time() if now is None else now
    return VerdictAtom(
        sensor=sensor,
        target=target,
        verdict=verdict,
        detail=detail,
        latency_ms=latency_ms,
        at=utcnow_iso(),
        at_epoch=epoch,
        route=route,
    )


def fresh_atoms(
    atoms: Sequence[VerdictAtom], *, window_sec: float, now: float | None = None
) -> tuple[VerdictAtom, ...]:
    """The subset of *atoms* read within *window_sec* of *now*.

    The consult-first primitive: a composer asks what it already knows, senses
    only what this returns as absent, and never re-dials a leg whose reading is
    still good.
    """
    cutoff = (time.time() if now is None else now) - float(window_sec)
    return tuple(a for a in atoms if a.at_epoch >= cutoff)


# ── target resolution: the effective chain, read from ssh's own answer ───────


@dataclass(frozen=True)
class RouteChain:
    """The EFFECTIVE ssh chain for one target, as ``ssh -G`` resolved it.

    ``resolved=False`` means the resolution could not run (no ssh binary, a
    timeout, a non-zero exit) — every composer then degrades to its pre-route
    behaviour rather than guessing. Fail-open is deliberate: a sensing layer must
    never be the reason a healthy submit refuses.
    """

    host: str
    hostname: str = ""
    user: str = ""
    port: int = DEFAULT_SSH_PORT
    proxy_jump: tuple[str, ...] = ()
    resolved: bool = False
    detail: str = ""

    @property
    def jumped(self) -> bool:
        """True when the effective chain traverses at least one ``ProxyJump`` hop."""
        return bool(self.proxy_jump)

    @property
    def final_hostname(self) -> str:
        """The final target's own hostname (falls back to the caller's token)."""
        return self.hostname or _bare_host(self.host)


def _run_route_resolution(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Bounded capture seam for ``ssh -G`` (module-level so tests patch it)."""
    from hpc_agent.infra import remote

    return remote.capture_via_select(list(argv), timeout=timeout)


def _parse_ssh_g(text: str, *, host: str) -> RouteChain:
    """Parse ``ssh -G`` output — ``key value`` lines, keyword lower-cased by ssh.

    Only four keys are read (``hostname`` / ``user`` / ``port`` / ``proxyjump``);
    everything else is ignored, so an OpenSSH release that grows new keys cannot
    break this. A ``proxyjump`` of ``none`` (ssh's own "no jump" spelling) yields
    an empty chain.
    """
    hostname = user = ""
    port = DEFAULT_SSH_PORT
    hops: tuple[str, ...] = ()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        key, _, value = line.partition(" ")
        key = key.lower()
        value = value.strip()
        if key == "hostname":
            hostname = value
        elif key == "user":
            user = value
        elif key == "port":
            try:
                port = int(value)
            except ValueError:
                port = DEFAULT_SSH_PORT
        elif key == "proxyjump":
            hops = (
                ()
                if value.lower() in ("", "none")
                else tuple(
                    _bare_host(part) for part in value.split(",") if _bare_host(part.strip())
                )
            )
    return RouteChain(
        host=host,
        hostname=hostname,
        user=user,
        port=port,
        proxy_jump=hops,
        resolved=True,
        detail=f"ssh -G {host}: hostname={hostname or '?'} proxyjump={','.join(hops) or 'none'}",
    )


@functools.lru_cache(maxsize=64)
def _resolve_route_cached(host: str, timeout_sec: float) -> RouteChain:
    from hpc_agent.infra.ssh_options import _ssh_binary

    if not host.strip():
        return RouteChain(host=host, detail="no host to resolve")
    argv = [_ssh_binary(), "-G", host]
    try:
        cp = _run_route_resolution(argv, timeout_sec)
    except subprocess.TimeoutExpired:
        return RouteChain(host=host, detail=f"ssh -G {host} timed out after {timeout_sec:g}s")
    except OSError as exc:
        return RouteChain(host=host, detail=f"ssh -G {host} could not run: {exc}"[:200])
    if cp.returncode != 0:
        why = (cp.stderr or "").strip()[:120]
        return RouteChain(host=host, detail=f"ssh -G {host} exited {cp.returncode}: {why}".strip())
    return _parse_ssh_g(cp.stdout or "", host=host)


def resolve_route(host: str, *, timeout_sec: float = ROUTE_RESOLVE_TIMEOUT_SEC) -> RouteChain:
    """The EFFECTIVE ssh chain for *host*, per ``ssh -G`` — never a config re-parse.

    Cached per ``(host, timeout)``: ssh config does not change mid-process, and a
    subprocess per sensed host per surface would be a real cost on the hot paths
    that consult this (the breaker's degradation text, the S2 gate). Fail-open on
    every error — an unresolvable route yields ``resolved=False`` and every
    composer degrades to its pre-route behaviour.
    """
    return _resolve_route_cached(host.strip(), float(timeout_sec))


def clear_route_cache() -> None:
    """Drop the ``ssh -G`` resolution cache (testing seam / post-config-edit)."""
    _resolve_route_cached.cache_clear()


# ── the sensors ──────────────────────────────────────────────────────────────


def tcp_connect(
    host: str, port: int = DEFAULT_SSH_PORT, timeout_sec: float = LEG_CONNECT_TIMEOUT_SEC
) -> tuple[bool, str]:
    """ONE bounded TCP connect to *host:port* — never more.

    A retry loop here is exactly the connection storm the circuit breaker exists
    to prevent (2026-07-04 ban incident), so a sensor gets one attempt per leg per
    call and composers build from there.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True, f"tcp connect to {host}:{port} ok"
    except Exception as exc:  # bounded sensor: every failure is evidence, not an error
        return False, f"{type(exc).__name__}: {exc}"[:200]


def _leg_dial_hostname(token: str) -> str:
    """The DIALABLE hostname for a chain-element *token* — never the raw alias.

    ``ssh -G`` reports a ProxyJump as the LITERAL config token (``usc-discovery``),
    which is an ssh ALIAS with no DNS record; a raw TCP dial on it fails name
    resolution and reported a HEALTHY hop as ``down`` on the sensor's first live
    day (2026-07-30 — caught by the operator asking "are you sure it's down?").
    One more ``ssh -G`` pass resolves the alias to its ``HostName`` exactly the
    way ssh itself would; a token that IS a real hostname resolves to itself, and
    any resolution failure falls back to the token (fail-open — the dial then
    reports the same failure it always did, honestly).
    """
    try:
        resolved = resolve_route(token).final_hostname
    except Exception:  # noqa: BLE001 — resolution is an assist, never a gate
        return token
    return resolved or token


def sense_leg(
    host: str,
    *,
    kind: Literal["hop", "direct"],
    port: int = DEFAULT_SSH_PORT,
    connect: Callable[[str, int, float], tuple[bool, str]] | None = None,
    timeout_sec: float = LEG_CONNECT_TIMEOUT_SEC,
) -> VerdictAtom:
    """SENSOR: one bounded TCP reading of a resolved chain element.

    *connect* is injected so the composer's own bounded connector (and its test
    fake) is what runs; it defaults to :func:`tcp_connect`. The latency is
    measured around the dial so the ledger can rank "slow but up" against "down"
    without a second probe. The atom's IDENTITY stays the chain token *host*
    (the configured name is what the human recognizes); the DIAL goes to the
    alias-resolved hostname (:func:`_leg_dial_hostname`), disclosed in the
    detail whenever the two differ.
    """
    dial = connect or tcp_connect
    dial_host = _leg_dial_hostname(host)
    started = time.perf_counter()
    ok, detail = dial(dial_host, port, timeout_sec)
    latency = (time.perf_counter() - started) * 1000.0
    if dial_host != host:
        detail = f"{detail} (alias {host} -> {dial_host})"
    verdict: SensorVerdict = "ok" if ok else ("timeout" if "timeout" in detail.lower() else "down")
    return _atom(kind, host, verdict, detail, latency_ms=latency)


def _run_probe_ssh(argv: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Bounded capture seam for a command-class sensor (module-level: tests patch it)."""
    from hpc_agent.infra import remote

    return remote.capture_via_select(list(argv), timeout=timeout)


def sense_command_class(
    ssh_target: str,
    command: str,
    *,
    kind: SensorKind,
    direct: bool = False,
    timeout_sec: float = PREAMBLE_TIMEOUT_SEC,
    target: str | None = None,
    accept_output: bool = False,
    detail_from_stdout: bool = False,
) -> VerdictAtom:
    """SENSOR: run ONE bounded command class over a named route; one atom back.

    Built through :func:`ssh_argv` (the single ssh-invocation seam) so the sensor
    inherits ``BatchMode`` / ``ConnectTimeout`` / crypto / multiplexing exactly as
    a real leg does; the only addition is ``-o ProxyJump=none`` on the direct
    route — which is precisely what makes the direct reading a discriminator
    rather than a second opinion.

    *target* overrides the atom's SUBJECT (default: *ssh_target*). The scratch
    sensor's subject is the scratch path and the scheduler sensor's is the backend
    family — atom identity is ``(sensor, route, target)``, so two scratch readings
    on different paths must not collapse onto one row.

    *accept_output* widens ``ok`` to "non-zero exit that nonetheless printed
    something on stdout". It exists for ONE fact class: a scheduler CLI banner.
    ``qstat -help`` exits non-zero on several Grid Engine builds while printing
    its usage — the CLI ANSWERED, which is exactly the fact the ``scheduler``
    sensor reads, and calling that ``down`` would report a broken scheduler on
    every such site. The exit code is disclosed in ``detail`` either way; nothing
    is hidden, only classified honestly. Default OFF: for every other class a
    non-zero exit is a failure.

    *detail_from_stdout* carries the first stdout line into ``detail`` on ``ok``.
    The ``env`` sensor needs it — the whole point of that reading is the wheel
    fingerprint STRING, not merely that the command exited 0.
    """
    from hpc_agent.infra.ssh_options import ssh_argv

    extra = ["-o", "ProxyJump=none"] if direct else []
    argv = [*ssh_argv("ssh", extra_opts=extra), ssh_target, command]
    route: Literal["effective", "direct"] = "direct" if direct else "effective"
    subject = target if target is not None else ssh_target
    started = time.perf_counter()
    try:
        cp = _run_probe_ssh(argv, timeout_sec)
    except subprocess.TimeoutExpired:
        latency = (time.perf_counter() - started) * 1000.0
        return _atom(
            kind,
            subject,
            "timeout",
            f"timed out after {timeout_sec:g}s running {command!r}",
            latency_ms=latency,
            route=route,
        )
    except OSError as exc:
        return _atom(
            kind,
            subject,
            "unknown",
            f"probe could not run: {exc}"[:200],
            route=route,
        )
    latency = (time.perf_counter() - started) * 1000.0
    stdout = (cp.stdout or "").strip()
    if cp.returncode == 0:
        detail = "exit 0"
        if detail_from_stdout and stdout:
            detail = stdout.splitlines()[0].strip()[:200]
        return _atom(kind, subject, "ok", detail, latency_ms=latency, route=route)
    if accept_output and stdout:
        return _atom(
            kind,
            subject,
            "ok",
            f"exit {cp.returncode} but answered: {stdout.splitlines()[0].strip()[:160]}",
            latency_ms=latency,
            route=route,
        )
    return _atom(
        kind,
        subject,
        "down",
        f"exit {cp.returncode}: {(cp.stderr or '').strip()[:200]}",
        latency_ms=latency,
        route=route,
    )


def activation_command(activation: str) -> str:
    """The preamble-class command for *activation* — the SAME class that fails live.

    The cluster's activation prefix (built by
    :func:`hpc_agent.infra.clusters.remote_activation_prefix` from
    ``clusters.yaml``'s ``modules`` / ``conda_source`` / ``conda_envs``)
    conventionally ends in `` && ``; a prefix without it is joined safely. An
    empty activation degenerates to the connect class.
    """
    prefix = (activation or "").strip()
    if prefix and not prefix.endswith("&&"):
        prefix = f"{prefix} &&"
    return f"{prefix} true".strip() if prefix else "true"


# ── the four remaining invariants: scratch / scheduler / env / auth ──────────
#
# Pillar 3 of docs/design/s2-readiness.md: "named invariants, one owner each".
# Each of the three PROBING sensors below is one bounded command class over an
# EXISTING connection-class probe — the same ``ssh_argv`` + bounded-capture pair
# every other sensor rides, no new transport. A sensor is allowed to probe when
# it is EXPLICITLY invoked; what may never probe is a FEED SITE (the ledger's
# harvest-never-probe rule) and a read surface (``cluster-readiness``).
#
# ``auth`` is the odd one out and deliberately so: it adds NO probe at all. It is
# a classification leg over the connect sensor's own exit/stderr signature — the
# discrimination the circuit breaker structurally cannot make, because its
# SUCCESS verdict folds "auth rejected but the host answered" into "reached the
# host".

#: The env-fingerprint command class — the SAME one the release flow reads on
#: every target environment ("activate the cluster env and read
#: ``hpc-agent --version``"). Reusing that command class rather than inventing a
#: second one is what makes a sensed fingerprint comparable to a released one.
ENV_FINGERPRINT_COMMAND = "hpc-agent --version"

#: The cheap scheduler touch per backend FAMILY — the CLI's own version/usage
#: banner, which costs a fork on the login node and (unlike ``qstat`` with no
#: arguments) never enumerates the queue. Families are
#: ``infra.clusters._KNOWN_SCHEDULER_FAMILIES``; the command classes agree with
#: ``infra.scheduler_resolve``'s ``_VERSION_CMD`` / ``_QSUB_VERSION_CMDS``, which
#: is where this framework already decided what a cheap scheduler touch looks
#: like. An UNKNOWN family maps to nothing and the sensor emits ``skipped``
#: rather than guessing a binary — a wrong guess would report a broken scheduler
#: on a working cluster.
SCHEDULER_TOUCH_COMMANDS: dict[str, str] = {
    "slurm": "squeue --version",
    "sge": "qstat -help",
    "pbspro": "qstat --version",
    "torque": "qstat --version",
}

#: ssh stderr fragments that mean the credentials were REJECTED — the remote end
#: was reached and said no. Lower-cased substring match; deliberately short and
#: about the SSH protocol only, so it cannot drift into general error parsing.
AUTH_REJECTION_MARKERS: tuple[str, ...] = (
    "permission denied",
    "publickey",
    "too many authentication failures",
    "no supported authentication methods",
    "authentication failed",
    "host key verification failed",
)

#: ssh stderr fragments that mean the session NEVER got far enough to present a
#: credential. Their presence is what makes "unreachable" a positive reading
#: rather than the absence of an auth marker.
NEVER_REACHED_MARKERS: tuple[str, ...] = (
    "connection refused",
    "connection timed out",
    "connection reset",
    "could not resolve hostname",
    "name or service not known",
    "network is unreachable",
    "no route to host",
    "operation timed out",
    "timed out after",
)

#: Remote-storage failure fragments a STAGING site already holds in a transfer's
#: stderr. Harvest-side vocabulary: a feed site classifies with these, it never
#: runs anything to obtain them.
_STORAGE_FAILURE_MARKERS: tuple[str, ...] = (
    "no space left on device",
    "disk quota exceeded",
    "quota exceeded",
    "read-only file system",
    "input/output error",
    "stale file handle",
)


def _shell_quote(value: str) -> str:
    """POSIX single-quote *value* so a path with spaces reaches the remote whole."""
    return "'" + value.replace("'", "'\\''") + "'"


def scratch_command(scratch: str) -> str:
    """The scratch-class command: the directory EXISTS and the filesystem answers.

    ``test -d`` alone would pass against a hung mount that ``stat``\\ s from cache;
    ``df -P`` forces the filesystem itself to answer and is the reading an
    operator would take by hand. Both in one command class means one round trip
    and one verdict. Deliberately NOT prefixed with the cluster activation: ``test``
    and ``df`` are POSIX built-ins/coreutils present before any ``module load``,
    and prefixing would only add a way for a storage reading to fail for an env
    reason (which is a different invariant, with its own sensor).
    """
    quoted = _shell_quote(scratch.strip())
    return f"test -d {quoted} && df -P {quoted}"


def env_fingerprint_command(activation: str) -> str:
    """``<activation> && hpc-agent --version`` — the release flow's own class."""
    prefix = (activation or "").strip()
    if prefix and not prefix.endswith("&&"):
        prefix = f"{prefix} &&"
    return f"{prefix} {ENV_FINGERPRINT_COMMAND}".strip()


def scheduler_touch_command(family: str, *, activation: str = "") -> str:
    """The cheap touch for a backend *family* (``""`` when the family is unknown).

    *activation* is opt-in: most sites put the scheduler CLI on the default login
    PATH, but a cluster that hides it behind a module needs the prefix, and the
    caller (who holds ``clusters.yaml``) is the one that knows.
    """
    command = SCHEDULER_TOUCH_COMMANDS.get((family or "").strip().lower(), "")
    if not command:
        return ""
    prefix = (activation or "").strip()
    if prefix and not prefix.endswith("&&"):
        prefix = f"{prefix} &&"
    return f"{prefix} {command}".strip()


def scheduler_family_for_cluster(cluster_cfg: Mapping[str, Any]) -> str:
    """The backend family a ``clusters.yaml`` entry schedules through (``""`` if none).

    Reads the entry's ``scheduler`` and, when that names a registered backend
    rather than a curated family, its ``scheduler_profile.family``. Never raises
    and never guesses: an entry this cannot resolve yields ``""`` and the sensor
    emits ``skipped`` with the reason.
    """
    if not isinstance(cluster_cfg, dict):
        return ""
    name = str(cluster_cfg.get("scheduler") or "").strip().lower()
    if name in SCHEDULER_TOUCH_COMMANDS:
        return name
    profile = cluster_cfg.get("scheduler_profile")
    if isinstance(profile, dict):
        family = str(profile.get("family") or "").strip().lower()
        if family in SCHEDULER_TOUCH_COMMANDS:
            return family
    return ""


def storage_failure_marker(text: str) -> str | None:
    """The remote-storage failure named in *text*, or ``None``.

    The HARVEST-side classifier: a staging site holds a failed transfer's stderr
    and asks whether it is positive evidence about STORAGE (as opposed to
    transport, which the flap classifier already owns). ``None`` means "this
    stderr says nothing about scratch" — and a feed site that gets ``None`` must
    record nothing rather than record a guess.
    """
    blob = (text or "").lower()
    return next((marker for marker in _STORAGE_FAILURE_MARKERS if marker in blob), None)


def sense_scratch(
    ssh_target: str,
    scratch: str,
    *,
    direct: bool = False,
    timeout_sec: float = PREAMBLE_TIMEOUT_SEC,
) -> VerdictAtom:
    """SENSOR: is the cluster's scratch there and does its filesystem answer?

    One bounded :func:`sense_command_class` run of :func:`scratch_command` over
    the named route. The atom's subject is the SCRATCH PATH, not the host: atom
    identity is ``(sensor, route, target)`` and two scratch roots on one cluster
    are two facts.
    """
    path = (scratch or "").strip()
    if not path:
        return _atom(
            "scratch",
            ssh_target,
            "skipped",
            "no scratch path configured for this cluster",
            route="direct" if direct else "effective",
        )
    return sense_command_class(
        ssh_target,
        scratch_command(path),
        kind="scratch",
        direct=direct,
        timeout_sec=timeout_sec,
        target=path,
    )


def sense_scheduler(
    ssh_target: str,
    family: str,
    *,
    activation: str = "",
    direct: bool = False,
    timeout_sec: float = PREAMBLE_TIMEOUT_SEC,
) -> VerdictAtom:
    """SENSOR: does this cluster's scheduler CLI answer? One cheap touch, one atom.

    ``accept_output=True``: a banner printed under a non-zero usage exit still
    proves the CLI answered — see :func:`sense_command_class`. An unknown family
    yields ``skipped`` naming the families that have a touch, never a guessed
    binary.
    """
    command = scheduler_touch_command(family, activation=activation)
    subject = (family or "").strip().lower() or "unknown"
    if not command:
        return _atom(
            "scheduler",
            subject,
            "skipped",
            (
                f"no cheap scheduler touch is defined for backend family "
                f"{subject!r} (known: {', '.join(sorted(SCHEDULER_TOUCH_COMMANDS))})"
            ),
            route="direct" if direct else "effective",
        )
    return sense_command_class(
        ssh_target,
        command,
        kind="scheduler",
        direct=direct,
        timeout_sec=timeout_sec,
        target=subject,
        accept_output=True,
    )


def sense_env(
    ssh_target: str,
    activation: str,
    *,
    direct: bool = False,
    timeout_sec: float = PREAMBLE_TIMEOUT_SEC,
) -> VerdictAtom:
    """SENSOR: the remote env fingerprint — the release flow's own command class.

    The ``ok`` atom's ``detail`` carries the fingerprint LINE (``hpc-agent
    0.11.4+g766c293d``), because "which wheel is over there" is the fact, not
    "the command exited 0". Comparing it against an expected wheel is a
    CONSUMER's job; a sensor states what it saw.
    """
    return sense_command_class(
        ssh_target,
        env_fingerprint_command(activation),
        kind="env",
        direct=direct,
        timeout_sec=timeout_sec,
        detail_from_stdout=True,
    )


def auth_signature(detail: str) -> Literal["rejected", "never_reached", "unsettled"]:
    """Classify a failed connect reading's text: credentials refused, or never asked?

    Rejection markers are tested FIRST on purpose. ``Connection closed by
    <host> port 22`` is a NEVER_REACHED-looking phrase that OpenSSH also emits
    immediately after ``Too many authentication failures`` — reading that as
    "unreachable" is exactly the conflation this leg exists to remove.
    """
    blob = (detail or "").lower()
    if any(marker in blob for marker in AUTH_REJECTION_MARKERS):
        return "rejected"
    if any(marker in blob for marker in NEVER_REACHED_MARKERS):
        return "never_reached"
    return "unsettled"


def auth_atom(connect: VerdictAtom, *, now: float | None = None) -> VerdictAtom:
    """DERIVE the ``auth`` atom from a ``connect`` reading — no new probe, ever.

    The whole invariant, from evidence already in hand:

    * connect ``ok`` — a command RAN on the far end, so the credentials were
      accepted. This is the one place in the system that can honestly say so:
      the circuit breaker's SUCCESS verdict means "reached the host", which an
      auth rejection also satisfies, so a breaker-fed ``auth`` atom would assert
      what its evidence does not support (``state/readiness``'s "a seam by
      construction, not by omission").
    * connect failed with a rejection signature — ``down``. The host answered and
      refused us: retrying, retargeting to a sibling, or waiting out a breaker
      cooldown all fix nothing.
    * connect failed without ever reaching the host — ``unknown``, saying so.
      Absence of evidence is a diagnosis, not a verdict: no credential was ever
      presented, so nothing is known about auth.
    """
    subject = connect.target
    route = connect.route
    if connect.ok is True:
        return _atom(
            "auth",
            subject,
            "ok",
            "a remote command ran, so the credentials were accepted",
            route=route,
            now=now,
        )
    signature = auth_signature(connect.detail)
    if signature == "rejected":
        return _atom(
            "auth",
            subject,
            "down",
            f"the host answered and REFUSED the credentials: {connect.detail[:160]}",
            route=route,
            now=now,
        )
    if signature == "never_reached":
        return _atom(
            "auth",
            subject,
            "unknown",
            (
                "the host was never reached, so no credential was presented — "
                f"unreachable, NOT an auth rejection: {connect.detail[:140]}"
            ),
            route=route,
            now=now,
        )
    return _atom(
        "auth",
        subject,
        "unknown",
        f"the connect class failed with no auth or transport signature: {connect.detail[:140]}",
        route=route,
        now=now,
    )


# ── composers over the sensors ───────────────────────────────────────────────


def sense_route_legs(
    route: RouteChain,
    *,
    connect: Callable[[str, int, float], tuple[bool, str]] | None = None,
    timeout_sec: float = LEG_CONNECT_TIMEOUT_SEC,
    probe_direct: bool = True,
    known: Sequence[VerdictAtom] = (),
) -> tuple[VerdictAtom, ...]:
    """Sense every leg of *route* separately and DERIVE the ``path`` atom.

    For an un-jumped route this is one direct reading plus the derived path atom
    (there is one path, and it IS the direct one). For a jumped route: one atom
    per hop, plus the direct alternative (the target's own hostname with the jump
    bypassed), plus the derived ``path`` atom — ``down`` at the FIRST dead hop,
    named. That single derivation rule is the 2026-07-30 fix: a dead hop can
    never again be masked by a bare hostname that answered.

    *known* supplies already-fresh atoms (the consult-first path): a leg whose
    reading is present there is REUSED, not re-dialled.
    """
    by_target = {(a.sensor, a.target): a for a in known}
    atoms: list[VerdictAtom] = []
    dead_hop: str | None = None

    for hop in route.proxy_jump:
        atom = by_target.get(("hop", hop)) or sense_leg(
            hop, kind="hop", connect=connect, timeout_sec=timeout_sec
        )
        atoms.append(atom)
        if atom.ok is False and dead_hop is None:
            dead_hop = hop

    final = route.final_hostname
    if probe_direct and final:
        direct = by_target.get(("direct", final)) or sense_leg(
            final, kind="direct", port=route.port, connect=connect, timeout_sec=timeout_sec
        )
        if route.jumped:
            direct = VerdictAtom(
                **{
                    **direct.__dict__,
                    "detail": f"{direct.detail} (ProxyJump bypassed — the DIRECT alternative)",
                }
            )
    else:
        direct = _atom("direct", final, "skipped", "the direct alternative was not probed")
    atoms.append(direct)

    if not route.jumped:
        atoms.append(
            _atom(
                "path",
                final,
                direct.verdict,
                "no ProxyJump configured — the direct route IS the effective path",
                latency_ms=direct.latency_ms,
            )
        )
        return tuple(atoms)

    if dead_hop is not None:
        atoms.append(
            _atom(
                "path",
                final,
                "down",
                f"blocked at hop {dead_hop} — the effective path never reaches {final}",
            )
        )
    elif direct.ok is False:
        # Every hop answered but the target's own hostname does not. Through the
        # jump the target may still be reachable (the hop's network view differs
        # from ours), so this is UNKNOWN, not down.
        atoms.append(
            _atom(
                "path",
                final,
                "unknown",
                f"hops OK; {final} does not answer from here, but the jump's view may "
                "differ — end-to-end unproven without a command-class reading",
            )
        )
    else:
        atoms.append(
            _atom(
                "path",
                final,
                "unknown",
                "every hop answered; end-to-end through the jump is unproven by TCP "
                "alone (run the preamble rung for a session-level verdict)",
            )
        )
    return tuple(atoms)


def sense_preamble(
    ssh_target: str,
    activation: str,
    *,
    direct: bool = False,
    timeout_sec: float = PREAMBLE_TIMEOUT_SEC,
) -> tuple[VerdictAtom, ...]:
    """Sense ``{connect, auth, preamble}`` for *ssh_target* over one named route.

    Two bounded readings and one DERIVATION: a bare ``true`` (the connect class)
    and the activation command (the preamble class — the SAME command class that
    fails in the field), plus the ``auth`` atom :func:`auth_atom` reads off the
    connect atom's own exit/stderr signature. The auth leg costs NO probe: it is
    a second question asked of evidence already in hand, which is why it can
    exist at all (a dedicated auth probe would be one more connection an
    intrusion filter counts).

    The preamble reading runs only when the connect reading passed, so a dead
    connect never masquerades as a preamble failure; when it did not, a
    ``skipped`` preamble atom says exactly that.
    """
    route: Literal["effective", "direct"] = "direct" if direct else "effective"
    connect_atom = sense_command_class(
        ssh_target, "true", kind="connect", direct=direct, timeout_sec=timeout_sec
    )
    auth = auth_atom(connect_atom)
    if connect_atom.ok is not True:
        return (
            connect_atom,
            auth,
            _atom(
                "preamble",
                ssh_target,
                "skipped",
                "not attempted: the connect class did not pass",
                route=route,
            ),
        )
    return (
        connect_atom,
        auth,
        sense_command_class(
            ssh_target,
            activation_command(activation),
            kind="preamble",
            direct=direct,
            timeout_sec=timeout_sec,
        ),
    )


# ── the discriminated readiness verdict every composer names ─────────────────

#: The named causes a readiness read can settle on. Every refusal, every retry
#: exhaustion, and every summary line quotes ONE of these — so the human reads
#: the same vocabulary at the fire-time gate, in the worker log, and in triage.
PathCause = Literal[
    "path_ok",
    "hop_down_direct_ok",
    "hop_down_direct_dead",
    "hop_down_direct_unprobed",
    "target_unreachable",
    "path_unproven",
    "preamble_degraded",
    "transport_flap",
    "route_unresolved",
]


@dataclass(frozen=True)
class PathReadiness:
    """A composed readiness read: the chain, its atoms, and the named cause.

    The ledger (pillar 1) will persist :attr:`atoms`; the composers here render
    :attr:`sentence` / :attr:`cause`. Both halves are derived from the SAME atoms,
    so a stored reading and a live one can never disagree about what was seen.
    """

    route: RouteChain
    atoms: tuple[VerdictAtom, ...] = ()
    cause: PathCause = "path_ok"
    sentence: str = ""
    reused: tuple[VerdictAtom, ...] = field(default_factory=tuple)

    def atom(self, sensor: SensorKind, *, route: str | None = None) -> VerdictAtom | None:
        """The first atom of *sensor* (optionally pinned to a route), or ``None``."""
        for a in self.atoms:
            if a.sensor == sensor and (route is None or a.route == route):
                return a
        return None

    @property
    def dead_hop(self) -> str | None:
        """The first ``ProxyJump`` hop that did not answer, or ``None``."""
        return next((a.target for a in self.atoms if a.sensor == "hop" and a.ok is False), None)

    #: Causes that do NOT contradict a usable path. Membership is the "only a
    #: POSITIVE discriminated failure refuses" rule, written once.
    #:
    #: ``path_unproven`` belongs here and its absence was a real defect: it is the
    #: ORDINARY reading for a healthy jumped host sensed by TCP alone (every hop
    #: answered; end-to-end needs the preamble rung, which is opt-in and needs an
    #: activation). Excluding it made the S2 gate refuse fully-healthy submits to
    #: any jumped cluster whose activation could not be resolved — the exact
    #: production shape, hoffman2 via ProxyJump. Absence of evidence is a
    #: diagnosis, not a verdict. ``route_unresolved`` is here for the same reason:
    #: ``ssh -G`` told us nothing, so we know nothing new and must not block.
    PASSING_CAUSES: ClassVar[frozenset[str]] = frozenset(
        {"path_ok", "path_unproven", "route_unresolved"}
    )

    @property
    def ok(self) -> bool:
        """True unless the read carries POSITIVE evidence of a broken path."""
        return self.cause in self.PASSING_CAUSES

    @property
    def newest_epoch(self) -> float:
        """The most recent atom's instant — the freshness key of the whole read."""
        return max((a.at_epoch for a in self.atoms), default=0.0)


def _classify(route: RouteChain, atoms: Sequence[VerdictAtom]) -> PathCause:
    """The fixed precedence table — evidence over inference, hop over target.

    1. An unresolved chain yields ``route_unresolved``: nothing new is known, so
       every composer must degrade to its pre-route behaviour (fail-open).
    2. A dead hop outranks everything the target itself said — the whole
       2026-07-30 fix. The arm names what the DIRECT alternative did, because
       that is the actionable half.
    3. The preamble split discriminates the two causes sharing the
       probe-OK/command-hang signature: preamble OK on the direct route while it
       fails on the effective one is a ``transport_flap`` (a tunnel dropping
       mid-command); failing on BOTH is ``preamble_degraded`` (node-local).
    4. Only then do the bare target readings speak.
    """
    if not route.resolved:
        return "route_unresolved"
    dead_hop = next((a for a in atoms if a.sensor == "hop" and a.ok is False), None)
    direct = next((a for a in atoms if a.sensor == "direct"), None)
    if dead_hop is not None:
        if direct is None or direct.verdict == "skipped":
            return "hop_down_direct_unprobed"
        return "hop_down_direct_ok" if direct.ok is True else "hop_down_direct_dead"

    eff_connect = next((a for a in atoms if a.sensor == "connect" and a.route == "effective"), None)
    eff_pre = next((a for a in atoms if a.sensor == "preamble" and a.route == "effective"), None)
    dir_pre = next((a for a in atoms if a.sensor == "preamble" and a.route == "direct"), None)
    # A CONNECT that never established is not a preamble story at all — no
    # activation ever ran, so calling it "node-local degradation" would contradict
    # the very atoms the cause is derived from (the remediation would open with
    # "connect succeeds but…" directly under a FAILED connect atom). Split it out
    # ahead of the preamble discrimination, which presupposes a session that DID
    # establish.
    if eff_connect is not None and eff_connect.ok is False:
        if not route.jumped:
            return "target_unreachable"
        dir_connect = next(
            (a for a in atoms if a.sensor == "connect" and a.route == "direct"), None
        )
        if dir_connect is not None and dir_connect.ok is False:
            return "target_unreachable"
        # Jumped, and the direct route either worked or was not read: the jump is
        # the differing ingredient, so the tunnel is the suspect.
        return "transport_flap"

    if eff_pre is not None and eff_pre.ok is False:
        if dir_pre is not None and dir_pre.ok is True:
            return "transport_flap"
        # Failing on both routes — or with nothing to compare against — is the
        # node-local reading: the session establishes and the activation hangs.
        return "preamble_degraded"

    path = next((a for a in atoms if a.sensor == "path"), None)
    if path is None:
        return "path_unproven"
    if path.ok is False:
        return "target_unreachable"
    if path.ok is None:
        if not route.jumped:
            return "target_unreachable"
        return "path_ok" if eff_pre is not None and eff_pre.ok is True else "path_unproven"
    return "path_ok"


def route_sentence(route: RouteChain, atoms: Sequence[VerdictAtom]) -> str:
    """The honest one-line route verdict for a JUMPED host (``""`` when un-jumped).

    The dead-hop arm is the exact sentence the 2026-07-30 incident needed and did
    not get: ``path dead (hop usc-discovery down); direct alternative OK``.
    An un-jumped host produces no sentence at all, so its summary line stays
    byte-identical to what it was before this layer existed.
    """
    if not route.jumped:
        return ""
    dead = next((a.target for a in atoms if a.sensor == "hop" and a.ok is False), None)
    if dead is None:
        return f"hop {', '.join(route.proxy_jump)} OK"
    direct = next((a for a in atoms if a.sensor == "direct"), None)
    if direct is None or direct.verdict == "skipped":
        tail = "direct alternative unprobed"
    elif direct.ok is True:
        tail = "direct alternative OK"
    else:
        tail = "direct alternative also dead"
    return f"path dead (hop {dead} down); {tail}"


def preamble_sentence(atoms: Sequence[VerdictAtom]) -> str:
    """The bracketed ``[<route>: connect …, preamble …]`` tail(s), or ``""``."""

    def _word(atom: VerdictAtom | None) -> str:
        if atom is None or atom.verdict == "skipped":
            return "not attempted"
        if atom.verdict == "ok":
            return "OK"
        if atom.verdict == "timeout":
            return "TIMEOUT"
        return "unknown" if atom.verdict == "unknown" else "FAILED"

    out: list[str] = []
    for route in ("effective", "direct"):
        connect = next((a for a in atoms if a.sensor == "connect" and a.route == route), None)
        preamble = next((a for a in atoms if a.sensor == "preamble" and a.route == route), None)
        if connect is None and preamble is None:
            continue
        label = "direct route" if route == "direct" else "effective route"
        out.append(f"[{label}: connect {_word(connect)}, preamble {_word(preamble)}]")
    return " ".join(out)


def path_remediation(readiness: PathReadiness) -> str:
    """Deterministic remediation text per :data:`PathCause` arm.

    Each arm names the DISCRIMINATOR that settled it (or would), because the
    2026-07-30 failure was not a missing probe — it was a message that steered
    toward host-retarget *through the same dead hop*.
    """
    route = readiness.route
    hop = readiness.dead_hop or (route.proxy_jump[0] if route.jumped else "")
    target = route.final_hostname
    cause = readiness.cause
    if cause == "hop_down_direct_ok":
        return (
            f"the configured path to {target} runs through ProxyJump {hop}, and {hop} is "
            f"DOWN — but {target} answers on the DIRECT route. Do NOT fail over to a "
            f"sibling login node: every sibling reached through {hop} inherits the same "
            f"dead hop. Either bring {hop} back (VPN/tunnel), or route this run directly "
            f"(drop the ProxyJump for this host, or pin `-o ProxyJump=none`)."
        )
    if cause == "hop_down_direct_dead":
        return (
            f"ProxyJump hop {hop} is DOWN and {target} does not answer directly either — "
            f"both routes are dead, so this is a local network / VPN problem or a "
            f"site-wide outage, not a login-node fault. `host-retarget` cannot help: it "
            f"moves the login node, not the path."
        )
    if cause == "hop_down_direct_unprobed":
        return (
            f"ProxyJump hop {hop} is DOWN; the direct alternative was not probed, so it is "
            f"unknown whether {target} is reachable without the jump. Probe it before "
            f"retargeting — a sibling behind {hop} inherits the dead hop."
        )
    if cause == "transport_flap":
        via = f"bypassing {hop}" if hop else "with no jump"
        return (
            f"the activation preamble fails through the effective route but the SAME "
            f"command class passes on the DIRECT route ({via}) — this is a TRANSPORT "
            f"fault (a flapping tunnel/VPN severing the session mid-command), NOT "
            f"node-local degradation. Fix the tunnel; retargeting to a sibling through "
            f"the same hop reproduces it exactly."
        )
    if cause == "preamble_degraded":
        return (
            f"connect succeeds but the activation preamble fails on EVERY sensed route — "
            f"node-local degradation (module subsystem / degraded mount) on {target}. A "
            f"bare connect/echo verifies nothing here; prefer a healthy sibling login node "
            f"of the same cluster (`host-retarget <sibling>`)."
        )
    if cause == "target_unreachable":
        return (
            f"{target}:{route.port} did not accept a TCP connection and no ProxyJump hop "
            f"explains it — a cluster-side outage or a source-IP filter at their border. "
            f"Do NOT retry-storm; verify out-of-band."
        )
    if cause == "path_unproven":
        return (
            f"every ProxyJump hop answered but end-to-end reachability of {target} through "
            f"the jump is unproven by TCP alone — run the preamble rung (supply the "
            f"cluster's activation) for a session-level verdict."
        )
    if cause == "route_unresolved":
        return (
            f"`ssh -G {route.host}` could not resolve the effective chain "
            f"({route.detail or 'no detail'}), so no route-aware claim is made — this read "
            f"degraded to its pre-route behaviour and blocked nothing."
        )
    return f"the effective path to {target} is clear."


# ── the in-process readiness ledger (pillar 1 seam, pillar 3 consumer) ───────
#
# The standing per-cluster ledger of docs/design/s2-readiness.md is durable and
# cross-process. Until it lands, this is its in-process stand-in: a composer that
# senses RECORDS its atoms here, and a later composer in the SAME invocation
# CONSULTS them under a freshness window instead of re-dialling. The API is
# deliberately the one the durable ledger will offer (record / consult / clear),
# so the ledger builder swaps the storage and nothing above it moves.

_LEDGER: dict[str, tuple[PathReadiness, float]] = {}


def record_readiness(readiness: PathReadiness, *, now: float | None = None) -> None:
    """Record a composed read for later consultation, keyed by the sensed host.

    The RECORDING instant is stored alongside the reading rather than derived
    from its atoms. Atom timestamps are per-leg and a reading may legitimately
    carry none (an unresolved route, a fully-consulted read that re-dialled
    nothing) — deriving freshness from them alone would make such a reading look
    infinitely stale and silently defeat the consult path.
    """
    key = readiness.route.host.strip()
    if key:
        _LEDGER[key] = (readiness, time.time() if now is None else now)
        # Write-through to the DURABLE tier (s2-readiness pillar 1). Total
        # fail-open by construction: record_atoms never raises and never probes.
        from hpc_agent.state.readiness import record_atoms

        record_atoms(key, readiness.atoms, source="readiness-sensors")


def consult_readiness(
    host: str, *, window_sec: float = DEFAULT_FRESHNESS_WINDOW_SEC, now: float | None = None
) -> PathReadiness | None:
    """The most recent read for *host* if it is still within *window_sec*, else ``None``.

    Consult-first is the shape every gate uses: ask, sense only what came back
    absent or stale, then assert. A ``None`` here is not a failure — it just means
    the caller pays for its own sensing this time.
    """
    hit = _LEDGER.get(host.strip())
    if hit is None:
        return None
    readiness, recorded_at = hit
    cutoff = (time.time() if now is None else now) - float(window_sec)
    return readiness if recorded_at >= cutoff else None


def _durable_atoms(host: str, *, window_sec: float) -> tuple[VerdictAtom, ...]:
    """Fresh atoms from the DURABLE tier, as ``VerdictAtom``s. Never raises."""
    try:
        from hpc_agent.state.readiness import ATOM_FIELDS, consult_atoms

        return tuple(
            VerdictAtom(**{k: v for k, v in a.items() if k in ATOM_FIELDS})
            for a in consult_atoms(host, window_sec=window_sec)
        )
    except Exception:  # noqa: BLE001 - consulting is an optimization, never a gate
        return ()


def clear_readiness_ledger() -> None:
    """Drop every recorded read (testing seam; the durable ledger owns eviction)."""
    _LEDGER.clear()


def read_path_readiness(
    host: str,
    *,
    ssh_target: str | None = None,
    activation: str | None = None,
    connect: Callable[[str, int, float], tuple[bool, str]] | None = None,
    connect_timeout_sec: float = LEG_CONNECT_TIMEOUT_SEC,
    preamble_timeout_sec: float = PREAMBLE_TIMEOUT_SEC,
    route_timeout_sec: float = ROUTE_RESOLVE_TIMEOUT_SEC,
    freshness_window_sec: float | None = None,
    scratch: str | None = None,
    scheduler_family: str | None = None,
    env_fingerprint: bool = False,
    invariant_activation: str | None = None,
    record: bool = True,
) -> PathReadiness:
    """Resolve the chain, sense what is stale/absent, classify — the ONE composer.

    Consult-first when *freshness_window_sec* is given: any leg atom still inside
    the window is REUSED rather than re-dialled (:func:`consult_readiness` /
    :func:`fresh_atoms`), which is what lets the S2 pre-detach gate ride an L1
    sweep that already ran in the same invocation instead of paying a second set
    of connections.

    When *activation* is supplied the preamble rung senses *ssh_target* over the
    effective route and — only when a jump exists AND the effective route showed
    trouble — over the DIRECT route, which is the discriminator. That second
    reading is deliberately conditional: it costs a connection, and there is
    nothing to discriminate when the effective route is healthy.

    The three remaining pillar-3 invariants are OPT-IN, each by supplying the
    thing it needs — *scratch* (a path), *scheduler_family* (a backend family),
    *env_fingerprint* (a flag; it rides *activation*). Opt-in because each is one
    more connection: the caller that wants a full standing readiness sweep asks
    for it, while the S2 pre-detach gate — which must stay as cheap as the path
    question it answers — does not. They run ONLY after the transport legs proved
    a route (no dead hop, a resolved chain, the connect class not already known
    dead), because probing storage through a severed tunnel produces a
    ``scratch`` atom that says nothing about storage. Their atoms never touch
    :func:`_classify`: this composer's ``cause`` is a PATH cause, and a full
    scratch disk is not a reason to call the path dead. They reach the standing
    ledger through the same :func:`record_readiness` write-through as everything
    else, which is what makes ``cluster-readiness`` show them with an age.
    """
    route = resolve_route(host, timeout_sec=route_timeout_sec)

    known: tuple[VerdictAtom, ...] = ()
    if freshness_window_sec is not None:
        prior = consult_readiness(host, window_sec=freshness_window_sec)
        if prior is not None:
            known = fresh_atoms(prior.atoms, window_sec=freshness_window_sec)
        else:
            # Cache miss -> consult the DURABLE tier before paying for a probe.
            known = _durable_atoms(host, window_sec=freshness_window_sec)

    leg_atoms = sense_route_legs(
        route, connect=connect, timeout_sec=connect_timeout_sec, known=known
    )
    atoms: list[VerdictAtom] = list(leg_atoms)

    dead_hop = any(a.sensor == "hop" and a.ok is False for a in leg_atoms)
    target = ssh_target or route.host
    if activation is not None and not dead_hop and route.resolved:
        effective = sense_preamble(target, activation, timeout_sec=preamble_timeout_sec)
        atoms.extend(effective)
        troubled = any(a.ok is not True for a in effective)
        if route.jumped and troubled:
            atoms.extend(
                sense_preamble(target, activation, direct=True, timeout_sec=preamble_timeout_sec)
            )

    # The opt-in invariant rungs. Gated on the SAME two facts the preamble rung
    # is (a resolved chain, no dead hop) plus "the connect class is not already
    # known dead" — sensing storage or a scheduler through a route that just
    # failed to carry ``true`` yields an atom about the transport wearing a
    # storage label, which is precisely the conflation this substrate exists to
    # end.
    wants_invariant = scratch is not None or scheduler_family is not None or env_fingerprint
    connect_dead = any(a.sensor == "connect" and a.ok is False for a in atoms)
    if wants_invariant and not dead_hop and route.resolved and not connect_dead:
        # The prefix the scheduler + env rungs run under. Separate from
        # *activation* (which is what TURNS ON the preamble rung) because the two
        # are different uses of the same string: a caller may want the env
        # fingerprint — which is meaningless outside the activated env, since
        # that is where the wheel lives — WITHOUT paying for the preamble rung.
        # Folding them would have made "opt into the invariants" silently also
        # mean "opt into the preamble", or else read the fingerprint from a bare
        # login shell and report the wrong wheel.
        prefix = activation if invariant_activation is None else invariant_activation
        if scratch is not None:
            atoms.append(sense_scratch(target, scratch, timeout_sec=preamble_timeout_sec))
        if scheduler_family is not None:
            atoms.append(
                sense_scheduler(
                    target,
                    scheduler_family,
                    activation=prefix or "",
                    timeout_sec=preamble_timeout_sec,
                )
            )
        if env_fingerprint:
            atoms.append(sense_env(target, prefix or "", timeout_sec=preamble_timeout_sec))

    cause = _classify(route, atoms)
    sentence = route_sentence(route, atoms)
    tail = preamble_sentence(atoms)
    if tail:
        sentence = f"{sentence} {tail}".strip() if sentence else tail
    readiness = PathReadiness(
        route=route, atoms=tuple(atoms), cause=cause, sentence=sentence, reused=known
    )
    if record:
        record_readiness(readiness)
    return readiness
