"""asyncssh-backed persistent SSH engine — the outsourced command channel.

This supersedes the hand-rolled :mod:`hpc_agent.infra.ssh_broker` (phase 1:
a native ``ssh -T host /bin/sh`` process framed with nonce sentinels and two
reader threads). That broker WORKS, but it re-implements a command channel —
stream separation, exit-code plumbing, deadline enforcement, framing safety —
that a real SSH library already owns. This engine keeps the broker's *shape*
(one persistent connection per host, breaker-gated open, slot-held-while-open,
idle self-close, hard fallback to one-shot) and hands the transport to
``asyncssh``.

Why the move is worth a dependency:

* **Typed failures replace stderr parsing.** The broker classified throttle
  vs. fatal by matching stderr substrings against
  :data:`hpc_agent.infra.ssh_circuit._CONNECTION_FAILURE_MARKERS`. asyncssh
  raises *typed* exceptions — a banner/kex withhold is
  :class:`asyncio.TimeoutError`, a torn connection is
  ``asyncssh.ConnectionLost``, an auth reject is ``asyncssh.PermissionDenied``.
  :func:`classify_engine_failure` maps those to ``"throttle"`` / ``"fatal"``
  with no string matching — the migration's crux.
* **A real command channel.** ``conn.run(cmd, timeout=...)`` gives split
  stdout/stderr, the real remote exit status (and negative-signal returncodes,
  subprocess-style), and a per-command deadline whose asyncssh.TimeoutError
  carries partial output — all machinery the broker hand-built.

Design, and the ban-safety invariants it preserves (each has a test):

* **Opt-in + hard fallback.** OFF unless ``HPC_SSH_ENGINE=asyncssh`` (default
  OFF until live-validated; ``"native"``/unset = off). Any engine trouble —
  disabled, asyncssh unimportable, a breaker-refused open, a failed connect, a
  wedged command, a dead channel — raises :class:`EngineUnavailable`, and the
  caller (the ssh seam) falls straight back to the one-shot path. An engine
  that misbehaves is never WORSE than today. NEVER a remote-command
  correctness signal (a remote non-zero exit returns a normal
  CompletedProcess).
* **One loop thread.** asyncssh connections are not thread-safe off their
  loop, so ONE background daemon thread owns a single asyncio event loop
  (created lazily on first use). Every asyncssh op runs on it; sync callers
  block via ``run_coroutine_threadsafe(...).result(timeout=...)``. The
  per-host registry is guarded by a threading lock in the *calling* thread;
  the connection objects are only ever touched on the loop.
* **Invariant 1 — breaker-gated open.** :func:`ssh_circuit.check_circuit`
  runs BEFORE connecting (an open circuit refuses, raising
  :class:`EngineUnavailable`); a connect failure records
  :func:`ssh_circuit.record_connection_failure` (with the exception class name
  in the detail), a success records
  :func:`ssh_circuit.record_connection_success`. Run-time failures do NOT
  touch the breaker — they discard the connection, and the NEXT call's
  reconnect is the breaker-gated attempt (same division as the broker).
* **Invariant 2 — slot-held-while-open.** The persistent connection holds one
  :mod:`hpc_agent.infra.ssh_slots` per-host slot for its whole lifetime
  (acquired at connect, released at close), so it counts against the fleet's
  per-host connection cap.
* **Invariant 3 — idle self-close.** A connection idle past
  :data:`IDLE_CLOSE_SEC` is reaped so a forgotten engine never holds a
  login-node session forever (clusters count those).
* **Invariant 4 — dead/wedged discard.** A wedged command (per-command
  deadline) or a dead channel discards the connection and raises
  :class:`EngineUnavailable` for the CURRENT call; the next call reconnects.

Scope (phase 1, like the broker): IN-PROCESS only — one connection per host
per process. Bulk transfers (rsync/tar/scp) keep their own connections.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import subprocess
import threading
import time
from concurrent.futures import TimeoutError as _FuturesTimeout
from typing import TYPE_CHECKING, Any, Literal

from hpc_agent.errors import SshCircuitOpen
from hpc_agent.infra import ssh_circuit, ssh_options, ssh_slots

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Coroutine

__all__ = [
    "ENGINE_ENV",
    "IDLE_CLOSE_SEC",
    "EngineUnavailable",
    "classify_engine_failure",
    "engine_enabled",
    "engine_ssh_run",
    "shutdown_all",
]

#: Env var selecting the SSH engine. Default OFF: a connection-layer change
#: must be opted into (and proven on a quiet cluster) before it rides a
#: ban-sensitive run. Only the exact value ``"asyncssh"`` enables it —
#: ``"native"`` or unset keeps the one-shot / broker path.
ENGINE_ENV = "HPC_SSH_ENGINE"

#: Close a per-host connection after this many idle seconds so a forgotten
#: engine does not hold a login-node session — and, crucially, its per-host ssh
#: SLOT — open indefinitely. Default 120s (down from the broker's 600): the
#: run-#10 F-B residual was an mcp-serve process that ran ONE quick verb and then
#: held its slot until process exit, because the slot is released only at
#: connection close and nothing closed the idle connection. A ~2-min idle close
#: (enforced by the background sweep, :meth:`_Engine._sweep_idle`, not only the
#: next-run() check) frees that slot promptly. Env: ``HPC_SSH_IDLE_CLOSE_SEC``.
IDLE_CLOSE_SEC = float(os.environ.get("HPC_SSH_IDLE_CLOSE_SEC", "120"))

#: How often the background reaper thread sweeps for idle connections. An
#: mcp-serve that ran one quick verb has no further ``run()`` to trigger the
#: per-acquire idle check (:meth:`_Engine._reap_if_idle`), so a periodic sweep is
#: what actually frees its slot ~:data:`IDLE_CLOSE_SEC` after last use instead of
#: holding it until process exit. Kept well under IDLE_CLOSE_SEC so the close
#: latency is idle + at most one sweep interval.
_SWEEP_INTERVAL_SEC = 30.0

#: Concurrent sessions (channels) allowed on ONE connection. OpenSSH's default
#: ``MaxSessions`` is 10; cap below it so a burst can't trip a channel-open
#: refusal we'd have to treat as connection death.
_MAX_SESSIONS = 8

#: Slack added to a command's own deadline before the outer
#: ``future.result(timeout=)`` backstop fires. The per-command asyncssh
#: timeout should always trip first; this only catches a wedged EVENT LOOP.
#: Module-level (not inlined) so tests can shrink it to force the backstop.
_RESULT_MARGIN = 10.0

#: Slack the IN-LOOP asyncio deadline (:func:`_await_bounded`) adds above the
#: primitive's own timeout (asyncssh's per-op ``timeout=`` / ``connect_timeout``)
#: so on a NORMAL timeout asyncssh's own error trips first (it carries partial
#: output); the ``asyncio.wait_for`` is the backstop for the case asyncssh's own
#: timeout does NOT fire — the live 2026-07-08 hang, a 15-min remote leg against
#: a healthy cluster whose per-command asyncssh ``timeout=`` never tripped. Kept
#: below :data:`_RESULT_MARGIN` so the ordering holds: asyncssh-timeout <
#: in-loop-deadline < thread-backstop.
_LOOP_DEADLINE_MARGIN = 5.0

#: Outer bound for a connect/close op dispatched to the loop.
_CLOSE_DEADLINE = 10.0


class EngineUnavailable(Exception):
    """The engine cannot serve this call — the caller must fall back to one-shot.

    Raised on a disabled engine, an unimportable ``asyncssh``, a
    breaker-refused or failed connect, a wedged command, or a dead
    channel/connection. NEVER a correctness signal about the remote command
    itself (a remote non-zero exit returns a normal CompletedProcess); it means
    "route this through the ordinary one-shot ssh path instead."
    """


def engine_enabled() -> bool:
    """True only when ``HPC_SSH_ENGINE=asyncssh`` opts the engine in."""
    return os.environ.get(ENGINE_ENV, "").strip().lower() == "asyncssh"


def classify_engine_failure(exc: BaseException) -> Literal["throttle", "fatal"]:
    """Map an asyncssh/OS connect failure to ``"throttle"`` or ``"fatal"``.

    The migration's crux: typed exceptions replace the one-shot path's
    stderr-substring matching, and each type must land on the SAME breaker
    outcome its stderr shape produces today
    (``tests/infra/test_ssh_engine_classification.py`` pins the full table).

    ``"throttle"`` — connection-level evidence, exactly the
    :data:`ssh_circuit._CONNECTION_FAILURE_MARKERS` set: a banner/kex withhold
    (:class:`asyncio.TimeoutError` on connect — the MaxStartups case), a torn
    or refused connection (``asyncssh.ConnectionLost``,
    ``ConnectionResetError``, ``ConnectionRefusedError``), or an unreachable
    route (other :class:`OSError`). These record a breaker failure.

    ``"fatal"`` — NOT connection evidence, mirroring the markers' deliberate
    omissions: an auth reject (``asyncssh.PermissionDenied``) or host-key
    mismatch (``asyncssh.HostKeyNotVerifiable``) proves the host ACCEPTED the
    connection, and a DNS failure (:class:`socket.gaierror` — checked before
    its ``OSError`` parent) never opened one; today all three stderr shapes
    fall through ``classify_connection_failure`` and RESET the counter via
    ``record_connection_success``. Fatal also tells callers not to retry.

    asyncssh may be unimportable here (the classifier is public); without it
    only the OS-level types are visible.
    """
    import socket

    if isinstance(exc, socket.gaierror):
        return "fatal"
    try:
        import asyncssh
    except ImportError:
        return "throttle"
    if isinstance(exc, (asyncssh.PermissionDenied, asyncssh.HostKeyNotVerifiable)):
        return "fatal"
    return "throttle"


def _host_of(ssh_target: str) -> str:
    """Host key for *ssh_target* — same normalization the breaker/slots use."""
    return ssh_target.rsplit("@", 1)[-1].strip()


def _user_of(ssh_target: str) -> str | None:
    """The ``user`` of a ``user@host`` target, or ``None`` for a bare alias
    (letting ``~/.ssh/config`` / the local user supply it)."""
    return ssh_target.rsplit("@", 1)[0].strip() if "@" in ssh_target else None


def _connect_timeout() -> float:
    """Connect-phase bound (seconds), DERIVED from the framework's existing
    ``HPC_SSH_CONNECT_TIMEOUT`` knob and :mod:`ssh_options`' default — not a
    fresh restatement. ``default``/invalid/non-positive → the ssh_options
    default (15s today)."""
    raw = (os.environ.get("HPC_SSH_CONNECT_TIMEOUT") or "").strip()
    if not raw or raw.lower() == "default" or not raw.isdigit() or int(raw) <= 0:
        raw = ssh_options._DEFAULT_SSH_CONNECT_TIMEOUT
    return float(raw)


def _sweep_interval() -> float:
    """The idle-reaper sweep cadence, read fresh each loop so tests can shrink
    it (:data:`_SWEEP_INTERVAL_SEC`)."""
    return _SWEEP_INTERVAL_SEC


# --- the asyncio loop thread (one per process, lazily created) ---------------

_loop_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """The engine's single background event loop, starting its daemon thread
    on first use. asyncssh connections are bound to this loop; every asyncssh
    op runs on it."""
    global _loop, _loop_thread
    import asyncio

    with _loop_lock:
        if _loop is not None and not _loop.is_closed():
            return _loop
        loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=_run, name="hpc-ssh-engine", daemon=True)
        thread.start()
        _loop, _loop_thread = loop, thread
        return loop


def _submit(coro: Coroutine[Any, Any, Any], *, deadline: float | None) -> Any:
    """Run *coro* on the engine loop from a sync caller; block up to *deadline*.

    A ``deadline`` of ``None`` blocks indefinitely (the caller passed
    ``timeout=None``). A backstop timeout — the per-op asyncssh deadline should
    always trip first — cancels the future and raises
    :class:`EngineUnavailable` (a wedged event loop, discard + fall back).
    """
    import asyncio

    loop = _get_loop()
    future: Any = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=deadline)
    except _FuturesTimeout as exc:
        # Since Python 3.11 ``concurrent.futures.TimeoutError`` IS the builtin
        # ``TimeoutError``, and asyncssh's per-command timeout is a
        # ``TimeoutError`` subclass — so a timeout RAISED BY THE COROUTINE lands
        # here too. Disambiguate on the future's state: ``done()`` ⇒ the
        # coroutine itself raised (re-raise it so run()/_open() can classify and
        # surface partial output); not done ⇒ the ``.result()`` WAIT expired,
        # the genuine wedged-loop backstop.
        if future.done():
            raise
        future.cancel()
        raise EngineUnavailable(
            f"engine op exceeded its {deadline}s outer deadline (event loop wedged)"
        ) from exc


async def _await_bounded(coro: Any, *, timeout: float | None) -> Any:
    """Await *coro* under an IN-LOOP asyncio deadline — the engine's parity with
    the subprocess path's :func:`bounded_subprocess.run_capture_bounded`.

    Every asyncssh primitive the engine awaits (connect, run, channel close) runs
    through here so a wedged primitive is bounded ON THE LOOP, not merely by the
    thread-side ``future.result(timeout=)`` backstop in :func:`_submit` — which
    cannot reliably interrupt a coroutine stuck inside asyncssh (the live
    2026-07-08 aggregate hang: a 15-min remote leg against a healthy cluster
    whose per-command asyncssh ``timeout=`` never tripped).

    ``timeout=None`` is the caller's documented escape hatch (``ssh_run(...,
    timeout=None)`` disables enforcement) and stays unbounded — the same None
    semantics ``run_capture_bounded`` honours. On expiry
    :func:`asyncio.wait_for` raises :class:`asyncio.TimeoutError` (the builtin
    ``TimeoutError`` from 3.11), the SAME type asyncssh's own per-op timeout
    raises: :func:`classify_engine_failure` maps it to ``"throttle"`` and
    :meth:`_Engine.run` / :meth:`_Engine._open` already wrap it into
    :class:`EngineUnavailable` — no new error class.
    """
    import asyncio

    if timeout is None:
        return await coro
    return await asyncio.wait_for(coro, timeout=timeout)


async def _connect(ssh_target: str) -> Any:
    """Open ONE persistent ``asyncssh`` connection to *ssh_target* (the seam
    tests monkeypatch with a stub).

    ``config=()`` honours ``~/.ssh/config`` (Host/HostName/User/IdentityFile/
    ProxyJump; ControlMaster is silently ignored — fine, we hold ONE
    connection). Default ``known_hosts`` is strict. ``preferred_auth`` pins
    publickey only — the BatchMode equivalent, never prompting. On Windows
    asyncssh auto-tries Pageant then the OpenSSH named-pipe agent, so no
    ``agent_path`` is needed. Keepalives detect a silently-dropped session.
    """
    import asyncssh

    host = _host_of(ssh_target)
    kwargs: dict[str, Any] = {
        "config": (),
        "preferred_auth": ["publickey"],
        "connect_timeout": _connect_timeout(),
        "keepalive_interval": 15,
        "keepalive_count_max": 3,
    }
    user = _user_of(ssh_target)
    if user:
        kwargs["username"] = user
    sock = await _dial_multi_address(host, _connect_timeout())
    if sock is not None:
        # Hand the already-connected socket to asyncssh; *host* still names
        # the connection for known_hosts / config matching.
        kwargs["sock"] = sock
    return await asyncssh.connect(host, **kwargs)


async def _dial_multi_address(host: str, budget_sec: float) -> Any:
    """Dial *host* per-address like native OpenSSH; ``None`` = let asyncssh dial.

    Cluster login DNS is round-robin over several A records, and ONE
    SYN-dropping node must not eat the whole connect budget:
    ``asyncio.create_connection`` (what asyncssh uses) walks the addrinfo list
    sequentially with NO per-address bound, so a dead first address burns the
    entire ``connect_timeout`` before the healthy siblings are tried — the
    live hoffman2 probe failed exactly this way while native ssh (which
    bounds each address separately) connected fine. Only the multi-A case is
    hand-dialed; a single address is equivalent either way, and an
    unresolvable name (an ``~/.ssh/config`` Host alias whose HostName/Port
    only asyncssh's config pass can resolve) returns ``None`` so the plain
    path keeps full config semantics. Port 22 by design on the hand-dial
    path: a nonstandard-port cluster reaches us as an alias (→ ``None``).
    """
    import asyncio
    import socket

    loop = asyncio.get_running_loop()
    try:
        # Bound the resolve: getaddrinfo runs in the default executor and a
        # wedged resolver would otherwise hang the whole connect on the loop.
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, 22, type=socket.SOCK_STREAM), timeout=budget_sec
        )
    except socket.gaierror:
        return None  # alias / unresolvable: asyncssh's config pass owns it
    except (TimeoutError, asyncio.TimeoutError):
        # DNS wedged within the budget: hand off to asyncssh's own (also
        # connect_timeout-bounded) config-pass dial rather than raising here.
        return None
    pairs = [(info[0], info[4]) for info in infos if info[0] in (socket.AF_INET, socket.AF_INET6)]
    if len(pairs) <= 1:
        return None
    per_addr = max(budget_sec / len(pairs), 3.0)
    last_exc: Exception | None = None
    for family, addr in pairs:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            await asyncio.wait_for(loop.sock_connect(sock, addr), timeout=per_addr)
            return sock
        # asyncio.TimeoutError is only an alias of builtin TimeoutError from
        # 3.11; on 3.10 wait_for raises the distinct asyncio class (Linux-CI
        # 3.10 caught this — the local 3.13 suite cannot).
        except (TimeoutError, asyncio.TimeoutError, OSError) as exc:
            sock.close()
            last_exc = exc
    # Every address failed — surface the last error (TimeoutError/OSError both
    # classify "throttle", same as the whole-budget timeout did).
    assert last_exc is not None
    raise last_exc


async def _do_connect(ssh_target: str) -> tuple[Any, asyncio.Semaphore]:
    """Open a connection and its per-connection session semaphore (created on
    the loop so it binds to the running loop)."""
    import asyncio

    # In-loop deadline over the WHOLE connect (multi-address dial + handshake),
    # above the connect_timeout so asyncssh's own error trips first on a normal
    # slow connect; the wait_for is the backstop for a connect that never
    # returns (F-M — a hang here would otherwise ride the thread backstop only).
    conn = await _await_bounded(
        _connect(ssh_target), timeout=_connect_timeout() + _LOOP_DEADLINE_MARGIN
    )
    return conn, asyncio.Semaphore(_MAX_SESSIONS)


async def _do_run(hc: _HostConn, cmd: str, timeout: float | None) -> Any:
    """Run *cmd* on *hc*'s connection under the session semaphore, bounded.

    The whole guarded op (semaphore acquire + ``conn.run``) runs under an in-loop
    deadline (:func:`_await_bounded`) so neither a wedged channel read nor a
    starved semaphore can outlive the caller's timeout — the F-M fix for the
    15-min remote-leg hang. ``timeout=None`` stays unbounded (escape hatch)."""

    async def _guarded() -> Any:
        async with hc.sem:
            return await hc.conn.run(cmd, check=False, timeout=timeout)

    bound = None if timeout is None else timeout + _LOOP_DEADLINE_MARGIN
    return await _await_bounded(_guarded(), timeout=bound)


async def _do_close(hc: _HostConn) -> None:
    """Best-effort connection teardown (never raises — teardown must not)."""
    conn = hc.conn
    with contextlib.suppress(Exception):
        conn.close()
    with contextlib.suppress(Exception):
        waiter = conn.wait_closed()
        if waiter is not None:
            # Bound the close-wait: a connection that refuses to finish closing
            # must not park the teardown on the loop indefinitely.
            await _await_bounded(waiter, timeout=_CLOSE_DEADLINE)


class _HostConn:
    """One persistent connection plus the bookkeeping the calling thread owns.

    ``conn`` and ``sem`` are only ever touched on the engine loop; ``last_used``
    / ``alive`` / ``slot_token`` are managed by the calling thread under the
    engine's registry guard.
    """

    def __init__(self, ssh_target: str, conn: Any, slot_token: Any, sem: Any) -> None:
        self.ssh_target = ssh_target
        self.conn = conn
        self.slot_token = slot_token
        self.sem = sem
        self.last_used = time.monotonic()
        self.alive = True

    def idle_for(self) -> float:
        return time.monotonic() - self.last_used


class _Engine:
    """Per-process registry of one :class:`_HostConn` per host.

    The registry dict + per-host open locks live in the calling thread (a
    threading lock, like the broker's ``_Pool``); the connections themselves
    live on the engine loop.
    """

    def __init__(self) -> None:
        self._conns: dict[str, _HostConn] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        # Background idle-reaper (F-B residual): started on first open, stopped
        # by shutdown_all. Runs OFF the engine loop (a plain daemon thread) so
        # it can drive _discard's thread→loop _submit without deadlocking.
        self._sweeper: threading.Thread | None = None
        self._stop_sweeper = threading.Event()

    def _host_lock(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    def _get_live(self, host: str) -> _HostConn | None:
        with self._guard:
            hc = self._conns.get(host)
            return hc if hc is not None and hc.alive else None

    def run(
        self, cmd: str, *, ssh_target: str, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        host = _host_of(ssh_target)
        if not host:
            raise EngineUnavailable("empty host")
        self._reap_if_idle(host)
        hc = self._get_live(host) or self._open(ssh_target, host)
        outer = None if timeout is None else timeout + _RESULT_MARGIN
        try:
            result = _submit(_do_run(hc, cmd, timeout), deadline=outer)
        except EngineUnavailable:
            # Outer-deadline backstop tripped (wedged loop): discard + fall back.
            self._discard(host, hc)
            raise
        except Exception as exc:
            # A per-command timeout (asyncssh.TimeoutError, carrying partial
            # output), a channel-open refusal, a torn connection, an OSError —
            # the connection is dead/wedged. Discard it (invariant 4) and fall
            # back for THIS call; the next call reconnects breaker-gated. No
            # breaker record here: the breaker is a CONNECT-time gate (broker's
            # same division), and the reconnect is the gated attempt.
            self._discard(host, hc)
            snippet = getattr(exc, "stdout", None)
            partial = f" (partial stdout: {str(snippet)[:200]!r})" if snippet else ""
            raise EngineUnavailable(
                f"engine command on {host} failed: {type(exc).__name__}: {exc}{partial}"
            ) from exc
        hc.last_used = time.monotonic()
        # One-shot parity: every successful guarded_call RESETS the host's
        # consecutive-failure counter, so a healthy engine session must too —
        # otherwise one-shot failures from OTHER processes accumulate against
        # a host this connection is actively proving reachable.
        ssh_circuit.record_connection_success(ssh_target)
        return _to_completed(cmd, result)

    def _open(self, ssh_target: str, host: str) -> _HostConn:
        lock = self._host_lock(host)
        with lock:
            # Re-check under the lock: a peer thread may have just opened it.
            hc = self._get_live(host)
            if hc is not None:
                return hc
            # Invariant 1: gate the open on the breaker (an open circuit refuses;
            # a broker-refused open is EngineUnavailable, not the raw
            # SshCircuitOpen, so the seam falls back to one-shot uniformly).
            try:
                ssh_circuit.check_circuit(ssh_target)
            except SshCircuitOpen as exc:
                raise EngineUnavailable(
                    f"engine connect to {host} refused by the circuit breaker: {exc}"
                ) from exc
            # Invariant 2: the persistent connection holds one per-host slot for
            # its lifetime. A breaker that opens WHILE waiting for a slot raises
            # SshCircuitOpen (→ EngineUnavailable); a slot-wait give-up
            # (SshSlotWaitTimeout) is local contention the one-shot path would
            # hit identically, so it propagates unwrapped.
            try:
                slot_token = ssh_slots.acquire_slot(ssh_target)
            except SshCircuitOpen as exc:
                raise EngineUnavailable(
                    f"engine connect to {host} refused by the circuit breaker: {exc}"
                ) from exc
            try:
                conn, sem = _submit(
                    _do_connect(ssh_target), deadline=_connect_timeout() + _RESULT_MARGIN
                )
            except Exception as exc:
                ssh_slots.release_slot(slot_token)
                if isinstance(exc, EngineUnavailable):
                    # A wedged engine loop (the _submit backstop) is LOCAL
                    # trouble, not host evidence — like SshSlotWaitTimeout it
                    # never touches the breaker.
                    raise
                # Breaker parity with the one-shot path (the classification
                # table in tests/infra/test_ssh_engine_classification.py):
                # "throttle" = connection-level evidence, records a failure
                # exactly like its _CONNECTION_FAILURE_MARKERS analog;
                # "fatal" (auth reject / host-key mismatch / DNS) matches a
                # stderr shape that is deliberately NOT a marker today and
                # therefore RESETS the counter — recording a failure here
                # would let a bad key walk the circuit open, which the
                # one-shot path has never done.
                kind = classify_engine_failure(exc)
                if kind == "throttle":
                    ssh_circuit.record_connection_failure(
                        ssh_target,
                        detail=f"engine connect [throttle]: {type(exc).__name__}: {exc}",
                    )
                else:
                    ssh_circuit.record_connection_success(ssh_target)
                raise EngineUnavailable(
                    f"engine connect to {host} failed [{kind}]: {type(exc).__name__}: {exc}"
                ) from exc
            ssh_circuit.record_connection_success(ssh_target)
            hc = _HostConn(ssh_target, conn, slot_token, sem)
            with self._guard:
                self._conns[host] = hc
            self._ensure_sweeper()
            return hc

    def _reap_if_idle(self, host: str) -> None:
        with self._guard:
            hc = self._conns.get(host)
            stale = hc is not None and (not hc.alive or hc.idle_for() > IDLE_CLOSE_SEC)
        if stale and hc is not None:
            self._discard(host, hc)

    def _ensure_sweeper(self) -> None:
        """Start the background idle-reaper daemon on first open (idempotent)."""
        with self._guard:
            if self._sweeper is not None and self._sweeper.is_alive():
                return
            self._stop_sweeper.clear()
            thread = threading.Thread(
                target=self._sweeper_loop, name="hpc-ssh-engine-reaper", daemon=True
            )
            self._sweeper = thread
            thread.start()

    def _sweeper_loop(self) -> None:
        """Wake every :func:`_sweep_interval` and reap idle connections until
        stopped. Exceptions are swallowed — a reaper must never crash a run."""
        while not self._stop_sweeper.wait(_sweep_interval()):
            with contextlib.suppress(Exception):
                self._sweep_idle()

    def _sweep_idle(self) -> None:
        """Close every connection idle past :data:`IDLE_CLOSE_SEC` (or already
        dead) and free its slot — the background counterpart to
        :meth:`_reap_if_idle`, which only fires on the NEXT run() to a host.

        The F-B residual: an mcp-serve process that opened a connection for one
        quick verb and then sat idle has no such trigger, so without this sweep
        its per-host ssh slot stays claimed (slot is released only at connection
        close) until process exit. The sweep frees it ~IDLE_CLOSE_SEC after last
        use. An ACTIVE connection (idle ≤ threshold) is never touched."""
        with self._guard:
            stale = [
                (host, hc)
                for host, hc in self._conns.items()
                if not hc.alive or hc.idle_for() > IDLE_CLOSE_SEC
            ]
        for host, hc in stale:
            self._discard(host, hc)  # unregisters, closes on the loop, frees slot

    def _discard(self, host: str, hc: _HostConn | None) -> None:
        """Drop *hc*: unregister, close on the loop (best effort), free its slot."""
        if hc is None:
            return
        with self._guard:
            if self._conns.get(host) is hc:
                self._conns.pop(host, None)
            hc.alive = False
        with contextlib.suppress(Exception):
            _submit(_do_close(hc), deadline=_CLOSE_DEADLINE)
        ssh_slots.release_slot(hc.slot_token)

    def shutdown_all(self) -> None:
        self._stop_sweeper.set()  # halt the background reaper first
        with self._guard:
            items = list(self._conns.items())
            self._conns.clear()
        for _host, hc in items:
            hc.alive = False
            with contextlib.suppress(Exception):
                _submit(_do_close(hc), deadline=_CLOSE_DEADLINE)
            ssh_slots.release_slot(hc.slot_token)


def _as_str(value: Any) -> str:
    """Coerce asyncssh stdout/stderr (str under the default utf-8 encoding, or
    bytes if a caller ever set ``encoding=None``) to a str."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _to_completed(cmd: str, result: Any) -> subprocess.CompletedProcess[str]:
    """Convert an ``asyncssh.SSHCompletedProcess`` to a stdlib CompletedProcess.

    ``result.returncode`` already carries subprocess semantics — the remote
    exit status, or the negative signal number when the remote process was
    killed by a signal. ``None`` (no status and no signal) reads as 0.
    """
    rc = getattr(result, "returncode", None)
    if rc is None:
        rc = getattr(result, "exit_status", None) or 0
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=int(rc),
        stdout=_as_str(getattr(result, "stdout", "")),
        stderr=_as_str(getattr(result, "stderr", "")),
    )


_ENGINE = _Engine()


def engine_ssh_run(
    cmd: str, *, ssh_target: str, timeout: float | None
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* on *ssh_target* over the persistent per-host asyncssh connection.

    Returns a normal CompletedProcess (the REMOTE exit code — negative for a
    signal — and split stdout/stderr). Raises :class:`EngineUnavailable` when
    the engine is disabled, ``asyncssh`` is unimportable, or the engine cannot
    serve the call — the caller must then fall back to the one-shot ssh path.
    Never raises for a remote non-zero exit.
    """
    if not engine_enabled():
        raise EngineUnavailable(f"engine disabled ({ENGINE_ENV} != 'asyncssh')")
    try:
        import asyncssh  # noqa: F401 — importability probe; used lazily on the loop
    except ImportError as exc:
        raise EngineUnavailable("asyncssh is not importable (install the 'ssh' extra)") from exc
    return _ENGINE.run(cmd, ssh_target=ssh_target, timeout=timeout)


def shutdown_all() -> None:
    """Close every open connection (process exit / test teardown)."""
    _ENGINE.shutdown_all()


# Close any persistent connections at process exit so a detached worker that
# finishes never leaves a login-node ssh session dangling (clusters count idle
# sessions too). Best-effort — atexit swallows teardown errors.
atexit.register(shutdown_all)
