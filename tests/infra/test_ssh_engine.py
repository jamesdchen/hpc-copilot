"""Tests for the asyncssh-backed SSH engine (infra.ssh_engine).

The engine's contract mirrors the broker's: reuse ONE persistent connection
per host, preserve split stdout/stderr and the real remote exit code, gate the
connection open on the circuit breaker, hold a per-host slot while open, and
degrade to :class:`EngineUnavailable` (never a hang, never a wrong answer) so
the ssh seam can fall back to one-shot.

No cluster and no sshd: the module-level ``_connect`` coroutine seam is
monkeypatched to return a STUB connection whose ``run()`` returns canned
results / raises chosen asyncssh exceptions (analogous to the broker's
``_Pool._spawn`` local-shell seam). The real asyncio loop thread, the circuit
breaker, and the slot limiter all run for real; breaker/slot STATE isolation
comes from the autouse ``_isolated_journal_home`` fixture.

Per the split of ownership, the exhaustive throttle/fatal CLASSIFICATION table
(ban-safety parity with the stderr classifiers) is owned by the orchestrator;
here we assert only that the classification SEAM is consulted and that a
fatal-vs-throttle connect path is observably distinct.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterator

import pytest

asyncssh = pytest.importorskip("asyncssh")

from hpc_agent.errors import SshCircuitOpen  # noqa: E402 — after importorskip guard
from hpc_agent.infra import ssh_circuit, ssh_engine, ssh_slots  # noqa: E402
from hpc_agent.infra.ssh_engine import EngineUnavailable, _Engine  # noqa: E402


class _StubResult:
    """Stand-in for ``asyncssh.SSHCompletedProcess`` — only the attrs the engine
    reads (``returncode`` already carries the negative-signal convention)."""

    def __init__(
        self, *, returncode: int, stdout: str = "", stderr: str = "", exit_status: int | None = None
    ) -> None:
        self.returncode = returncode
        self.exit_status = exit_status if exit_status is not None else returncode
        self.stdout = stdout
        self.stderr = stderr


class _StubConn:
    """A fake asyncssh connection: ``run`` yields a canned result or raises."""

    def __init__(
        self,
        responder: Callable[[str], _StubResult] | None = None,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self._responder = responder
        self._raises = raises
        self.closed = False
        self.run_calls: list[str] = []

    async def run(
        self, cmd: str, *, check: bool = False, timeout: float | None = None
    ) -> _StubResult:
        self.run_calls.append(cmd)
        if self._raises is not None:
            raise self._raises
        if self._responder is not None:
            return self._responder(cmd)
        return _StubResult(returncode=0, stdout="", stderr="")

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


def _install_connect(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[str], _StubConn],
    *,
    spy: list[str] | None = None,
) -> None:
    """Point the module-level ``_connect`` seam at *factory* (one stub per open)."""

    async def _fake(ssh_target: str) -> _StubConn:
        if spy is not None:
            spy.append(ssh_target)
        return factory(ssh_target)

    monkeypatch.setattr(ssh_engine, "_connect", _fake)


def _recorder(sink: list[object]) -> Callable[..., None]:
    """A monkeypatch stand-in that records the ``detail`` kwarg (breaker
    failures) or the first positional (targets/tokens) into *sink* and returns
    None — a def, not an ``append`` lambda, so mypy doesn't flag the implicit
    return of ``list.append``'s ``None``."""

    def _rec(*args: object, **kwargs: object) -> None:
        sink.append(kwargs["detail"] if "detail" in kwargs else (args[0] if args else None))

    return _rec


@pytest.fixture
def engine() -> Iterator[_Engine]:
    eng = _Engine()
    try:
        yield eng
    finally:
        eng.shutdown_all()


# --- the opt-in facade -------------------------------------------------------


def test_engine_disabled_by_default_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The public facade refuses when the opt-in flag is unset — the ssh seam
    then uses its one-shot path, i.e. today's behaviour, unchanged."""
    monkeypatch.delenv(ssh_engine.ENGINE_ENV, raising=False)
    assert ssh_engine.engine_enabled() is False
    with pytest.raises(EngineUnavailable):
        ssh_engine.engine_ssh_run("echo x", ssh_target="u@h", timeout=5)


def test_engine_enabled_flag_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ssh_engine.ENGINE_ENV, "asyncssh")
    assert ssh_engine.engine_enabled() is True
    for off in ("native", "", "1", "true", "yes", "ASYNCSSH "):
        monkeypatch.setenv(ssh_engine.ENGINE_ENV, off)
        # Only the exact lowercase token enables it (trailing space is stripped;
        # "ASYNCSSH " lowercases+strips to "asyncssh" → enabled).
        expected = off.strip().lower() == "asyncssh"
        assert ssh_engine.engine_enabled() is expected


def test_run_without_asyncssh_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine enabled but asyncssh unimportable → EngineUnavailable (fall back),
    never an ImportError to the caller."""
    import sys

    monkeypatch.setenv(ssh_engine.ENGINE_ENV, "asyncssh")
    monkeypatch.setitem(sys.modules, "asyncssh", None)  # force ImportError on `import asyncssh`
    with pytest.raises(EngineUnavailable):
        ssh_engine.engine_ssh_run("echo x", ssh_target="u@h", timeout=5)


def test_module_has_no_toplevel_asyncssh_import() -> None:
    """asyncssh is an OPTIONAL dep — the module must import cleanly without it,
    so it may only be imported lazily inside functions, never at module top."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(ssh_engine))
    toplevel_imports: list[str] = []
    for node in tree.body:  # module body only — not nested function bodies
        if isinstance(node, ast.Import):
            toplevel_imports += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            toplevel_imports.append(node.module or "")
    assert "asyncssh" not in toplevel_imports


# --- result shape ------------------------------------------------------------


def test_result_shape_rc_stdout_stderr(engine: _Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """A remote non-zero exit is a normal CompletedProcess with split streams."""

    def _factory(_t: str) -> _StubConn:
        return _StubConn(lambda _c: _StubResult(returncode=3, stdout="out", stderr="err"))

    _install_connect(monkeypatch, _factory)
    r = engine.run("cmd", ssh_target="u@h", timeout=15)
    assert isinstance(r, subprocess.CompletedProcess)
    assert r.returncode == 3
    assert r.stdout == "out"
    assert r.stderr == "err"


def test_signal_returncode_is_negative(engine: _Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """asyncssh's returncode already encodes a signal kill as -signum
    (subprocess semantics) — the engine passes it through."""
    _install_connect(
        monkeypatch, lambda _t: _StubConn(lambda _c: _StubResult(returncode=-15, exit_status=None))
    )
    r = engine.run("cmd", ssh_target="u@h", timeout=15)
    assert r.returncode == -15


def test_one_connection_reused_across_commands(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: N commands, ONE connect (one handshake)."""
    spy: list[str] = []

    def _factory(_t: str) -> _StubConn:
        return _StubConn(lambda c: _StubResult(returncode=0, stdout=c))

    _install_connect(monkeypatch, _factory, spy=spy)
    for i in range(5):
        r = engine.run(f"echo {i}", ssh_target="u@h", timeout=15)
        assert r.stdout == f"echo {i}"
    assert len(spy) == 1  # five commands, one connection


# --- ban-safety invariants ---------------------------------------------------


def test_open_is_gated_by_the_circuit_breaker(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OPEN circuit refuses to open the connection (no connect attempt), and
    the refusal surfaces as EngineUnavailable so the seam falls back."""

    def _open_circuit(ssh_target: str, **_k: object) -> None:
        raise SshCircuitOpen("circuit open (test)")

    monkeypatch.setattr(ssh_circuit, "check_circuit", _open_circuit)
    spy: list[str] = []
    _install_connect(monkeypatch, lambda _t: _StubConn(), spy=spy)
    with pytest.raises(EngineUnavailable):
        engine.run("echo x", ssh_target="u@h", timeout=15)
    assert spy == []  # never connected against an open circuit


def test_successful_open_records_breaker_success(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[object] = []
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_engine.ssh_circuit, "record_connection_success", _recorder(recorded))
    _install_connect(monkeypatch, lambda _t: _StubConn())
    engine.run("echo ok", ssh_target="u@h", timeout=15)
    assert "u@h" in recorded


def test_connect_failure_records_breaker_failure_and_frees_slot(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed connect records a breaker connection-failure and releases the
    slot it claimed, then raises EngineUnavailable."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    failures: list[object] = []
    monkeypatch.setattr(ssh_engine.ssh_circuit, "record_connection_failure", _recorder(failures))
    released: list[object] = []
    monkeypatch.setattr(ssh_slots, "acquire_slot", lambda *a, **k: "SLOT")
    monkeypatch.setattr(ssh_slots, "release_slot", _recorder(released))

    async def _boom(_ssh_target: str) -> _StubConn:
        raise OSError("connection refused")

    monkeypatch.setattr(ssh_engine, "_connect", _boom)
    with pytest.raises(EngineUnavailable):
        engine.run("echo x", ssh_target="u@h", timeout=15)
    assert failures, "a failed connect must record a breaker connection-failure"
    assert released == ["SLOT"], "the slot claimed for the failed open must be released"


def test_classification_seam_is_consulted_fatal_vs_throttle(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Structural (the full mapping lives in the parity table,
    ``test_ssh_engine_classification.py``): a throttle-classified connect
    failure records a breaker FAILURE; a fatal one (auth reject — a stderr
    shape that is deliberately NOT a marker on the one-shot path) records a
    breaker SUCCESS instead, so a bad key can never walk the circuit open."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_slots, "acquire_slot", lambda *a, **k: None)
    failures: list[object] = []
    successes: list[object] = []
    monkeypatch.setattr(ssh_engine.ssh_circuit, "record_connection_failure", _recorder(failures))
    monkeypatch.setattr(ssh_engine.ssh_circuit, "record_connection_success", _recorder(successes))

    def _fail_with(exc: BaseException) -> None:
        async def _boom(_ssh_target: str) -> _StubConn:
            raise exc

        monkeypatch.setattr(ssh_engine, "_connect", _boom)
        with pytest.raises(EngineUnavailable):
            engine.run("echo x", ssh_target="u@h", timeout=15)

    _fail_with(asyncssh.PermissionDenied("bad key"))  # fatal
    assert not failures, "an auth reject must NOT record a breaker failure"
    assert successes, "an auth reject resets the counter (one-shot parity)"

    _fail_with(OSError("connection reset by peer"))  # throttle
    assert any("[throttle]" in str(d) for d in failures)


def test_slot_held_while_connected_and_released_on_close(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 2: the persistent connection holds a slot for its lifetime and
    frees it at close (here via shutdown_all)."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    acquired: list[str] = []
    released: list[object] = []

    def _acquire(*_a: object, **_k: object) -> str:
        acquired.append("t")
        return "SLOT"

    monkeypatch.setattr(ssh_slots, "acquire_slot", _acquire)
    monkeypatch.setattr(ssh_slots, "release_slot", _recorder(released))
    _install_connect(monkeypatch, lambda _t: _StubConn())

    engine.run("echo x", ssh_target="u@h", timeout=15)
    assert acquired == ["t"]  # one slot claimed at open
    assert released == []  # still held while connected
    engine.run("echo y", ssh_target="u@h", timeout=15)
    assert acquired == ["t"]  # reused — no second claim
    engine.shutdown_all()
    assert released == ["SLOT"]  # freed at close


def test_idle_connection_is_reaped(engine: _Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Invariant 3: an idle connection self-closes so no login-node session
    lingers; the next call reconnects (a fresh connection object)."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    # -1.0, not 0.0: idle_for() must be STRICTLY greater and two back-to-back
    # monotonic() reads can be EQUAL on a fast runner (broker CI flake).
    monkeypatch.setattr(ssh_engine, "IDLE_CLOSE_SEC", -1.0)
    _install_connect(monkeypatch, lambda _t: _StubConn())
    engine.run("echo one", ssh_target="u@h", timeout=15)
    first = engine._conns["h"].conn
    engine.run("echo two", ssh_target="u@h", timeout=15)  # idle>-1 → reaped+reopened
    assert engine._conns["h"].conn is not first


def test_wedged_command_raises_unavailable_and_discards_connection(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 4: a dead/torn channel discards the connection and raises
    EngineUnavailable for THIS call; the next call reconnects clean."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    calls = {"n": 0}

    def _factory(_t: str) -> _StubConn:
        calls["n"] += 1
        if calls["n"] == 1:
            return _StubConn(raises=OSError("channel torn"))
        return _StubConn(lambda _c: _StubResult(returncode=0, stdout="alive"))

    _install_connect(monkeypatch, _factory)
    with pytest.raises(EngineUnavailable):
        engine.run("cmd", ssh_target="u@h", timeout=15)
    assert "h" not in engine._conns  # poisoned connection discarded
    r = engine.run("cmd", ssh_target="u@h", timeout=15)  # reconnects
    assert r.stdout == "alive"
    assert calls["n"] == 2


def test_wedged_command_timeout_surfaces_partial_output(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-command asyncssh.TimeoutError carries partial output — the engine
    surfaces a snippet in the EngineUnavailable message."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    te = asyncssh.TimeoutError(
        env="",
        command="cmd",
        subsystem=None,
        exit_status=None,
        exit_signal=None,
        returncode=None,
        stdout="HALF_A_LINE",
        stderr="",
    )
    _install_connect(monkeypatch, lambda _t: _StubConn(raises=te))
    with pytest.raises(EngineUnavailable, match="HALF_A_LINE"):
        engine.run("cmd", ssh_target="u@h", timeout=1)


def test_shutdown_all_closes_the_connection(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    conns: list[_StubConn] = []

    def _factory(_t: str) -> _StubConn:
        c = _StubConn()
        conns.append(c)
        return c

    _install_connect(monkeypatch, _factory)
    engine.run("echo x", ssh_target="u@h", timeout=15)
    assert conns and conns[0].closed is False
    engine.shutdown_all()
    assert conns[0].closed is True
    assert engine._conns == {}


# --- pure classifier smoke (structural only; the parity table is elsewhere) --


def test_classify_engine_failure_returns_a_valid_label() -> None:
    """The classifier is total: fatal for the unambiguous auth rejects, throttle
    otherwise. (The exhaustive ban-safety parity table is owned elsewhere.)"""
    assert ssh_engine.classify_engine_failure(asyncssh.PermissionDenied("x")) == "fatal"
    assert ssh_engine.classify_engine_failure(asyncssh.HostKeyNotVerifiable("x")) == "fatal"
    assert ssh_engine.classify_engine_failure(OSError("reset")) == "throttle"
    assert ssh_engine.classify_engine_failure(TimeoutError()) == "throttle"


# --- optional live test (skipped by default) ---------------------------------


@pytest.mark.slow
def test_live_engine_round_trip() -> None:
    """OPTIONAL live smoke against a real sshd — skipped by default. Set
    HPC_ENGINE_LIVE_TARGET=user@host (a reachable, key-auth host) and run
    ``pytest -m slow`` to exercise a real asyncssh connection end to end."""
    import os

    target = os.environ.get("HPC_ENGINE_LIVE_TARGET")
    if not target:
        pytest.skip("set HPC_ENGINE_LIVE_TARGET=user@host to run the live engine test")
    eng = _Engine()
    try:
        r = eng.run("echo engine-live-ok", ssh_target=target, timeout=30)
        assert r.returncode == 0
        assert r.stdout.strip() == "engine-live-ok"
    finally:
        eng.shutdown_all()


class TestMultiAddressDial:
    """Per-address TCP dial parity with native OpenSSH (the live-hoffman2 gap:
    round-robin login DNS + one SYN-dropping node ate asyncssh's whole
    sequential connect budget while native ssh, which bounds each address,
    connected fine)."""

    @staticmethod
    def _run(coro):  # tiny helper: the dialer needs a running loop
        import asyncio

        return asyncio.run(coro)

    def test_unresolvable_alias_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A config alias only asyncssh can resolve → None (plain path keeps
        full HostName/Port config semantics)."""
        import socket

        def _gaierror(*_a: object, **_k: object) -> object:
            raise socket.gaierror(11001, "getaddrinfo failed")

        monkeypatch.setattr(socket, "getaddrinfo", _gaierror)
        assert self._run(ssh_engine._dial_multi_address("my-alias", 15.0)) is None

    def test_single_address_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One address: asyncssh's own dial is equivalent — no hand dial."""
        import socket

        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 22))]
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
        assert self._run(ssh_engine._dial_multi_address("onehost", 15.0)) is None

    def test_dead_first_address_does_not_eat_the_healthy_second(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE guard: first address blackholes (TEST-NET, drops SYNs), second
        is a live local listener — the dial must reach it within the
        per-address budget instead of burning the whole budget on the first."""
        import socket
        import time

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        infos = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 22)),  # blackhole
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port)),  # alive
        ]
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: infos)
        try:
            t0 = time.monotonic()
            sock = self._run(ssh_engine._dial_multi_address("multihost", 8.0))
            elapsed = time.monotonic() - t0
            assert sock is not None, "must connect to the healthy second address"
            assert sock.getpeername()[1] == port
            sock.close()
            # 8.0s budget / 2 addrs = 4.0s per-address slice (floor 3.0); the
            # old sequential dial would have needed the WHOLE budget +
            # asyncssh handshake before even trying the second address.
            assert elapsed < 7.5, f"dead first address ate the budget ({elapsed:.1f}s)"
        finally:
            listener.close()
