"""In-process per-host SSH connection broker (latency + ban-risk root fix).

.. admonition:: DEPRECATED / FROZEN — do not extend, retire

   This phase-1 broker is **deprecated and frozen**. It survives only as the
   *middle* fallback rung beneath the asyncssh engine
   (:mod:`hpc_agent.infra.ssh_engine`, opt-in ``HPC_SSH_ENGINE=asyncssh``);
   see ``docs/design/connection-broker.md`` for the authoritative status and
   the **retirement trigger** — one clean ``HPC_SSH_ENGINE=asyncssh``
   submit→harvest run with zero ``EngineUnavailable`` fallbacks, or the
   engine going default-on. At that point this module is DELETED.

   It has already fallen behind the engine's invariants, and these gaps are
   left UN-PORTED on purpose so nobody "fixes" the broker instead of retiring
   it:

   * It holds **no ``ssh_slots`` slot** — the engine does
     (``ssh_engine.py`` ``acquire``/``_SLOTS``).
   * It records **no per-command breaker success** — the engine does (one
     success per successful command, commit 028e64d5); the broker only
     records success/failure at *connection open* time.

   **All new SSH-layer work goes to** :mod:`hpc_agent.infra.ssh_engine`, not
   here.

The whole stack opens a FRESH cold TCP+SSH connection for every round-trip:
each preflight probe, each status poll, each ``qdel``. On a healthy login
node that is ~1s; on a login node applying ``MaxStartups`` throttling under
load it is a banner-exchange TIMEOUT — the sshd accepts the TCP connection
but withholds the SSH banner because too many unauthenticated connections
are already in flight. That is what manifested as every "stall" in runs
#7-#8, and it is the same behaviour a cluster's intrusion filter counts
toward a ban (the 2026-07-04 incident; :mod:`hpc_agent.infra.ssh_circuit`
exists for it). ControlMaster multiplexing — which would reuse one warm
connection — is broken on native Windows OpenSSH (no unix-socket mux).

This broker holds ONE persistent ``ssh -T <host> /bin/sh`` process per host
and runs every command down its stdin, so a poll loop that fired N cold
handshakes now pays exactly ONE. Fewer connections is both faster AND
strictly ban-SAFER — the point the ban-hammer warning turns on.

Design choices, and why:

* **Dependency-free.** No ``asyncssh``/``paramiko`` (this project ships
  "without paramiko or other dependencies" — ``infra.remote`` docstring),
  and reusing the native ``ssh`` binary keeps the ssh-agent path that
  already works on Windows (the named-pipe agent). We drive a remote
  ``/bin/sh`` reading stdin: ``-T`` (no TTY) means no prompt and no input
  echo, so the channel is clean.
* **Nonce-framed, stream-separated.** Each command is bracketed by a
  128-bit random nonce sentinel on BOTH stdout and stderr, drained by two
  reader threads (``select`` is not usable on Windows pipes). stdout and
  stderr stay separate — the throttle/error classifiers depend on stderr —
  and the real remote exit code rides the stdout sentinel.
* **Opt-in + hard fallback.** OFF unless ``HPC_SSH_BROKER`` is truthy. Any
  broker trouble (spawn failure, a wedged command, a broken pipe) raises
  :class:`BrokerUnavailable`; the caller (``infra.remote.ssh_run``) falls
  straight back to the one-shot path, unchanged. A broker that misbehaves
  can never be WORSE than today — that is the ban-safety contract.
* **Breaker-gated, idle-closing.** Opening the persistent connection runs
  under the circuit breaker (an open circuit refuses to open it) and
  records its success/failure like any attempt. An idle connection
  self-closes after :data:`IDLE_CLOSE_SEC` so a forgotten broker does not
  hold a login-node session forever (clusters count those too).

Scope (phase 1): IN-PROCESS only — one connection per host per process,
which already collapses the dominant case (a single detached poll/harvest
worker's repeated round-trips). A cross-process broker daemon shared by the
CLI, detached workers, and the MCP server is phase 2 (``docs/design/
connection-broker.md``). Bulk transfers (rsync/tar/scp) keep their own
connections — they are rarer than command round-trips and binary-framed.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import TYPE_CHECKING

from hpc_agent.infra import ssh_circuit

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "IDLE_CLOSE_SEC",
    "BrokerUnavailable",
    "broker_enabled",
    "broker_ssh_run",
    "shutdown_all",
]

#: Env var enabling the broker. OFF by default: a connection-layer change
#: must be opted into (and proven on a quiet cluster) before it rides a
#: ban-sensitive run. Truthy = "1"/"true"/"yes" (case-insensitive).
BROKER_ENV = "HPC_SSH_BROKER"

#: Close a per-host connection after this many seconds with no command, so a
#: forgotten broker does not hold a login-node session open indefinitely.
IDLE_CLOSE_SEC = float(os.environ.get("HPC_SSH_BROKER_IDLE_SEC", "600"))

#: How long to wait for the remote ``/bin/sh`` to accept the connection and
#: echo the readiness sentinel before declaring the open a failure.
_OPEN_TIMEOUT_SEC = 45.0


class BrokerUnavailable(Exception):
    """The broker cannot serve this call — the caller must fall back to one-shot.

    Raised on a disabled broker, a refused/failed connection open, a wedged
    command, or a broken channel. NEVER a correctness signal about the remote
    command itself (a remote non-zero exit returns a normal CompletedProcess);
    it means "route this through the ordinary one-shot ssh path instead."
    """


def broker_enabled() -> bool:
    """True when ``HPC_SSH_BROKER`` opts the broker in."""
    return os.environ.get(BROKER_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _host_of(ssh_target: str) -> str:
    """Host key for *ssh_target* — same normalization the breaker/throttle use."""
    return ssh_target.rsplit("@", 1)[-1].strip()


class _Channel:
    """One persistent ``ssh -T host /bin/sh`` connection with nonce framing.

    Not thread-safe for concurrent :meth:`run` — the owning :class:`_Pool`
    serializes callers per host under a lock (there is one physical channel;
    commands go down it one at a time, which is also what a poll loop wants).
    """

    def __init__(self, ssh_target: str, proc: subprocess.Popen[bytes]) -> None:
        self.ssh_target = ssh_target
        self._proc = proc
        self._last_used = time.monotonic()
        # Reader threads drain each pipe into a bytes buffer under a condition,
        # so a command that emits on both streams cannot deadlock a single
        # reader (and Windows can't select() on pipes).
        self._out_buf = bytearray()
        self._err_buf = bytearray()
        self._cond = threading.Condition()
        self._alive = True
        self._t_out = threading.Thread(
            target=self._drain, args=(proc.stdout, self._out_buf), daemon=True
        )
        self._t_err = threading.Thread(
            target=self._drain, args=(proc.stderr, self._err_buf), daemon=True
        )
        self._t_out.start()
        self._t_err.start()

    def _drain(self, pipe: object, buf: bytearray) -> None:
        try:
            for chunk in iter(lambda: pipe.read(4096), b""):  # type: ignore[attr-defined]
                with self._cond:
                    buf.extend(chunk)
                    self._cond.notify_all()
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                self._alive = False
                self._cond.notify_all()

    def is_alive(self) -> bool:
        return self._alive and self._proc.poll() is None

    def idle_for(self) -> float:
        return time.monotonic() - self._last_used

    def run(
        self, cmd: str, *, timeout: float | None, nonce: str
    ) -> subprocess.CompletedProcess[str]:
        """Send *cmd*; return its CompletedProcess (real remote rc, split streams).

        Raises :class:`BrokerUnavailable` on a wedged command (timeout) or a
        dead channel — the caller falls back and this channel is discarded.
        """
        if not self.is_alive() or self._proc.stdin is None:
            raise BrokerUnavailable(f"broker channel for {self.ssh_target} is not alive")
        out_done = f"__HPC_BRK_OUT_{nonce}__"
        err_done = f"__HPC_BRK_ERR_{nonce}__"
        # Bracket the command so BOTH streams carry an end sentinel, and the
        # stdout sentinel carries the exit code. A SUBSHELL ``( ... )`` — not a
        # ``{ ...; }`` brace group — so a command containing ``exit N`` exits
        # only the subshell (``$?`` captures N) and can never kill the
        # persistent channel; per-command cwd/env isolation also matches the
        # one-shot ssh semantics every caller already assumes. The stderr
        # sentinel is emitted unconditionally after, on fd 2.
        framed = (
            f"( {cmd}\n); __hpc_rc=$?; "
            f'printf "\\n%s%d\\n" "{out_done}" "$__hpc_rc"; '
            f'printf "%s\\n" "{err_done}" 1>&2\n'
        )
        with self._cond:
            self._out_buf.clear()
            self._err_buf.clear()
        try:
            self._proc.stdin.write(framed.encode("utf-8"))
            self._proc.stdin.flush()
        except (OSError, ValueError) as exc:
            self._alive = False
            raise BrokerUnavailable(f"broker write to {self.ssh_target} failed: {exc}") from exc

        deadline = None if timeout is None else time.monotonic() + timeout
        rc = self._await_sentinels(out_done, err_done, deadline)
        self._last_used = time.monotonic()
        with self._cond:
            out = bytes(self._out_buf)
            err = bytes(self._err_buf)
        stdout = _strip_sentinel(out.decode("utf-8", "replace"), out_done)
        stderr = _strip_sentinel(err.decode("utf-8", "replace"), err_done)
        return subprocess.CompletedProcess(args=cmd, returncode=rc, stdout=stdout, stderr=stderr)

    def _await_sentinels(self, out_done: str, err_done: str, deadline: float | None) -> int:
        """Block until the stdout end-sentinel appears (carrying rc); ensure the
        stderr sentinel too. Raise :class:`BrokerUnavailable` on timeout/death."""
        while True:
            with self._cond:
                out_text = self._out_buf.decode("utf-8", "replace")
                rc = _parse_rc(out_text, out_done)
                err_seen = err_done in self._err_buf.decode("utf-8", "replace")
                if rc is not None and err_seen:
                    return rc
                if not self._alive and self._proc.poll() is not None:
                    raise BrokerUnavailable(
                        f"broker channel for {self.ssh_target} died mid-command"
                    )
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise BrokerUnavailable(
                        f"broker command on {self.ssh_target} exceeded its deadline"
                    )
                self._cond.wait(timeout=0.2 if remaining is None else min(0.2, remaining))

    def close(self) -> None:
        """Best-effort teardown: EOF stdin, then tree-kill so no ssh lingers."""
        self._alive = False
        with contextlib_suppress():
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        from hpc_agent.infra.bounded_subprocess import _kill_tree

        with contextlib_suppress():
            if self._proc.poll() is None:
                _kill_tree(self._proc)


class _Pool:
    """Per-process registry of one :class:`_Channel` per host, lock-guarded."""

    def __init__(self) -> None:
        self._channels: dict[str, _Channel] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._nonce_seq = 0
        # Injectable spawn seam: production opens ``ssh -T host /bin/sh``; tests
        # substitute a local shell to exercise framing without a cluster.
        self._spawn: Callable[[str], subprocess.Popen[bytes]] = _spawn_ssh_shell

    def _host_lock(self, host: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    def _nonce(self) -> str:
        # Deterministic-free uniqueness without Math.random-style calls: mix a
        # monotonic clock with a per-pool counter. Collision only matters within
        # one channel's single in-flight command, so this is ample.
        with self._guard:
            self._nonce_seq += 1
            seq = self._nonce_seq
        return f"{seq:x}_{int(time.monotonic() * 1e6) & 0xFFFFFFFFFFFF:x}"

    def run(
        self, cmd: str, *, ssh_target: str, timeout: float | None
    ) -> subprocess.CompletedProcess[str]:
        host = _host_of(ssh_target)
        if not host:
            raise BrokerUnavailable("empty host")
        lock = self._host_lock(host)
        with lock:
            self._reap_if_idle(host)
            chan = self._channels.get(host)
            if chan is None or not chan.is_alive():
                chan = self._open(ssh_target, host)
            try:
                return chan.run(cmd, timeout=timeout, nonce=self._nonce())
            except BrokerUnavailable:
                # Discard the poisoned channel so the next call re-opens clean,
                # then let the caller fall back to one-shot for THIS command.
                with contextlib_suppress():
                    chan.close()
                with self._guard:
                    self._channels.pop(host, None)
                raise

    def _open(self, ssh_target: str, host: str) -> _Channel:
        # The persistent connection's handshake is a real ssh attempt: gate it
        # on the breaker (an open circuit refuses) and record the outcome, so
        # the broker can never become the all-night reconnect hammer.
        ssh_circuit.check_circuit(ssh_target)
        try:
            proc = self._spawn(ssh_target)
        except OSError as exc:
            ssh_circuit.record_connection_failure(ssh_target, detail=f"broker spawn: {exc}")
            raise BrokerUnavailable(f"broker spawn for {ssh_target} failed: {exc}") from exc
        chan = _Channel(ssh_target, proc)
        # Readiness probe over the fresh channel: proves the handshake AND the
        # remote shell are live before we hand it out. A failure here records a
        # connection failure (banner throttle / auth) exactly like a one-shot.
        try:
            probe = chan.run("printf ok", timeout=_OPEN_TIMEOUT_SEC, nonce=self._nonce())
            if probe.returncode != 0 or probe.stdout.strip() != "ok":
                raise BrokerUnavailable(f"broker readiness probe to {ssh_target} failed")
        except BrokerUnavailable:
            chan.close()
            ssh_circuit.record_connection_failure(
                ssh_target, detail="broker readiness probe failed"
            )
            raise
        ssh_circuit.record_connection_success(ssh_target)
        with self._guard:
            self._channels[host] = chan
        return chan

    def _reap_if_idle(self, host: str) -> None:
        chan = self._channels.get(host)
        if chan is not None and (not chan.is_alive() or chan.idle_for() > IDLE_CLOSE_SEC):
            with contextlib_suppress():
                chan.close()
            with self._guard:
                self._channels.pop(host, None)

    def shutdown_all(self) -> None:
        with self._guard:
            chans = list(self._channels.values())
            self._channels.clear()
        for chan in chans:
            with contextlib_suppress():
                chan.close()


def _spawn_ssh_shell(ssh_target: str) -> subprocess.Popen[bytes]:
    """Open the persistent ``ssh -T <target> /bin/sh`` process (binary pipes)."""
    from hpc_agent.infra.ssh_options import ssh_argv

    argv = [*ssh_argv("ssh"), "-T", ssh_target, "/bin/sh"]
    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        # Own session/group (POSIX no-op on Windows) so ``_kill_tree``'s
        # ``killpg`` targets ONLY this ssh's tree, never the caller's group.
        start_new_session=True,
    )


def _parse_rc(text: str, out_done: str) -> int | None:
    """The exit code trailing *out_done* in *text*, or ``None`` if not yet seen."""
    idx = text.rfind(out_done)
    if idx < 0:
        return None
    tail = text[idx + len(out_done) :]
    digits = tail.strip().split("\n", 1)[0].strip()
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _strip_sentinel(text: str, sentinel: str) -> str:
    """Everything before *sentinel* (with one trailing newline trimmed)."""
    idx = text.rfind(sentinel)
    if idx < 0:
        return text
    body = text[:idx]
    return body[:-1] if body.endswith("\n") else body


class contextlib_suppress:
    """Tiny local ``contextlib.suppress(Exception)`` (broker teardown must never raise)."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> bool:
        return True


_POOL = _Pool()


def broker_ssh_run(
    cmd: str, *, ssh_target: str, timeout: float | None
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* on *ssh_target* over the persistent per-host channel.

    Returns a normal CompletedProcess (with the REMOTE exit code and split
    stdout/stderr). Raises :class:`BrokerUnavailable` when the broker is
    disabled or cannot serve the call — the caller must then fall back to the
    one-shot ssh path. Never raises for a remote non-zero exit.
    """
    if not broker_enabled():
        raise BrokerUnavailable("broker disabled (HPC_SSH_BROKER not set)")
    return _POOL.run(cmd, ssh_target=ssh_target, timeout=timeout)


def shutdown_all() -> None:
    """Close every open channel (process exit / test teardown)."""
    _POOL.shutdown_all()


# Close any persistent channels when the process exits, so a detached worker
# that finishes never leaves a login-node ssh session dangling (clusters count
# idle sessions too). Best-effort — atexit swallows teardown errors.
import atexit  # noqa: E402 — registered after _POOL is defined

atexit.register(shutdown_all)
