"""Tests for the asyncssh-backed SSH engine (infra.ssh_engine).

The engine's contract mirrors the broker's: reuse ONE persistent connection
per host, preserve split stdout/stderr and the real remote exit code, gate the
connection open on the circuit breaker, hold a per-host slot per in-flight op
(connect + each command window, released when the connection goes idle — the
2026-07-16 run-14 correction, was "held while open"), and degrade to
:class:`EngineUnavailable` (never a hang, never a wrong answer) so the ssh seam
can fall back to one-shot.

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
import threading
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


async def _async_none(*_a: object, **_k: object) -> None:
    """An async stand-in returning None — e.g. for the multi-address dial seam
    (``None`` = let asyncssh dial), so a test can reach the real ``_connect``."""
    return None


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


def test_engine_enabled_by_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine is default-ON (latency-audit rank-3 flip, 2026-07-16): an UNSET
    env selects it. ``HPC_SSH_ENGINE=native`` is the explicit one-shot opt-out."""
    monkeypatch.delenv(ssh_engine.ENGINE_ENV, raising=False)
    assert ssh_engine.engine_enabled() is True

    monkeypatch.setenv(ssh_engine.ENGINE_ENV, "native")
    assert ssh_engine.engine_enabled() is False
    with pytest.raises(EngineUnavailable):
        # native selected → the facade refuses so the ssh seam uses one-shot.
        ssh_engine.engine_ssh_run("echo x", ssh_target="u@h", timeout=5)


def test_engine_enabled_flag_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ssh_engine.ENGINE_ENV, "asyncssh")
    assert ssh_engine.engine_enabled() is True
    # Post-flip: unset/blank OR the exact 'asyncssh' token enables the engine;
    # 'native' and any UNRECOGNISED value select the one-shot path (unknown
    # values behave exactly as before the flip — off).
    for val in ("native", "1", "true", "yes", "one-shot", "off", "bogus"):
        monkeypatch.setenv(ssh_engine.ENGINE_ENV, val)
        assert ssh_engine.engine_enabled() is False
    for on in ("", " ", "asyncssh", "ASYNCSSH "):
        monkeypatch.setenv(ssh_engine.ENGINE_ENV, on)
        # blank strips to "" → default-on; "ASYNCSSH " lowercases+strips → on.
        assert ssh_engine.engine_enabled() is True
    monkeypatch.delenv(ssh_engine.ENGINE_ENV, raising=False)
    assert ssh_engine.engine_enabled() is True  # truly unset → default-on


def test_run_without_asyncssh_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine enabled but asyncssh unimportable → EngineUnavailable (fall back),
    never an ImportError to the caller."""
    import sys

    monkeypatch.setenv(ssh_engine.ENGINE_ENV, "asyncssh")
    monkeypatch.setitem(sys.modules, "asyncssh", None)  # force ImportError on `import asyncssh`
    with pytest.raises(EngineUnavailable):
        ssh_engine.engine_ssh_run("echo x", ssh_target="u@h", timeout=5)


def test_default_on_with_broken_asyncssh_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """The default (UNSET) path with an unimportable asyncssh degrades to the
    one-shot fallback — EngineUnavailable, never an ImportError — so the flipped
    default is never worse than one-shot even on a box without the ssh extra."""
    import sys

    monkeypatch.delenv(ssh_engine.ENGINE_ENV, raising=False)
    assert ssh_engine.engine_enabled() is True  # default-on
    monkeypatch.setitem(sys.modules, "asyncssh", None)  # force ImportError
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


def test_warm_idle_connection_holds_no_slot(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 2, run-14 correction (FLIPS the old 'slot-held-while-open' pin):
    a warm-but-idle connection holds ZERO slots. This is the starvation scenario
    directly — a warm ``status-watch`` connection between polls no longer blocks a
    concurrent op. After a completed command the connection stays open (reused on
    the next run), yet a concurrent acquirer can claim BOTH per-host slots under
    the default cap of 2; under the old whole-life hold only ONE would be free and
    the second acquire would block for SLOT_WAIT_MAX_SEC then SshSlotWaitTimeout.

    Uses the REAL slot machinery (the autouse journal-home fixture isolates
    state), which is the whole point — the fix lives in the acquire/release
    lifecycle, not in a stub."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    _install_connect(monkeypatch, lambda _t: _StubConn())
    engine.run("echo x", ssh_target="u@h", timeout=15)
    assert "h" in engine._conns  # warm connection retained (reusable)
    # The warm-but-idle connection holds no slot: both per-host slots are free.
    t1 = ssh_slots.acquire_slot("u@h")
    t2 = ssh_slots.acquire_slot("u@h")
    assert t1 is not None and t2 is not None  # cap=2, BOTH claimable → conn holds 0
    ssh_slots.release_slot(t1)
    ssh_slots.release_slot(t2)
    # Reuse still works after the concurrent acquirer released — no leaked state.
    r = engine.run("echo y", ssh_target="u@h", timeout=15)
    assert r.returncode == 0


def test_inflight_command_holds_exactly_one_slot(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 2, the other half: while a command is actually in flight the
    connection DOES hold exactly one per-host slot (the burst bound the slot
    exists for) — and once the command completes the warm connection drops back to
    zero. Real slot machinery; the connect slot is already released by the time
    the command is in flight, so exactly one file exists."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    started = threading.Event()
    release = threading.Event()
    conn = _BlockingConn(started, release)
    _install_connect(monkeypatch, lambda _t: conn)

    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["cp"] = engine.run("slow", ssh_target="u@h", timeout=30)
        except BaseException as exc:  # noqa: BLE001 — record for the assertion
            box["exc"] = exc

    worker = threading.Thread(target=_worker)
    worker.start()
    try:
        assert started.wait(5.0), "the command never reached the in-flight state"
        held = [p for p in ssh_slots.slot_paths("h") if p.exists()]
        assert len(held) == 1, "an in-flight command must hold exactly one slot"
        release.set()
        worker.join(5.0)
    finally:
        release.set()
        worker.join(5.0)
    assert "exc" not in box, f"the command failed: {box.get('exc')!r}"
    # Completed → warm-idle → holds zero slots (freed in run()'s finally).
    assert [p for p in ssh_slots.slot_paths("h") if p.exists()] == []


def test_concurrent_inflight_commands_are_bounded_to_the_cap(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 2, the burst guard is INTACT: two commands multiplexed IN FLIGHT
    on one warm connection hold TWO slots (= the default cap of 2), so a third
    concurrent connection attempt to the host blocks — the MaxStartups burst the
    limiter exists to prevent is still bounded, even though warm-idle connections
    now hold nothing. Real slot machinery; SLOT_WAIT_MAX_SEC shrunk so the third
    attempt bounds out fast."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_slots, "SLOT_WAIT_MAX_SEC", 0.4)
    reached_two = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    state = {"inflight": 0}

    class _MultiBlockingConn(_StubConn):
        async def run(  # type: ignore[override]
            self, cmd: str, *, check: bool = False, timeout: float | None = None
        ) -> _StubResult:
            import asyncio

            self.run_calls.append(cmd)
            with lock:
                state["inflight"] += 1
                if state["inflight"] >= 2:
                    reached_two.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            with lock:
                state["inflight"] -= 1
            return _StubResult(returncode=0, stdout="done")

    conn = _MultiBlockingConn()
    _install_connect(monkeypatch, lambda _t: conn)

    box: dict[str, BaseException] = {}

    def _worker(tag: str) -> None:
        try:
            engine.run(f"cmd-{tag}", ssh_target="u@h", timeout=30)
        except BaseException as exc:  # noqa: BLE001 — record for the assertion
            box[tag] = exc

    workers = [threading.Thread(target=_worker, args=(t,)) for t in ("a", "b")]
    for w in workers:
        w.start()
    try:
        assert reached_two.wait(5.0), "both commands never reached the in-flight state"
        # Two in-flight commands hold both slots → a third acquirer must block and
        # bound out with SshSlotWaitTimeout (the burst guard fires).
        from hpc_agent.errors import SshSlotWaitTimeout

        with pytest.raises(SshSlotWaitTimeout):
            ssh_slots.acquire_slot("u@h")
        held = [p for p in ssh_slots.slot_paths("h") if p.exists()]
        assert len(held) == 2, "two in-flight commands must hold exactly the cap (2) slots"
        release.set()
        for w in workers:
            w.join(5.0)
    finally:
        release.set()
        for w in workers:
            w.join(5.0)
    assert not box, f"a bounded command unexpectedly failed: {box!r}"
    # Both done → warm-idle → zero slots held.
    assert [p for p in ssh_slots.slot_paths("h") if p.exists()] == []


def test_quiet_live_connection_is_reused_not_reaped_on_next_run(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G4 shrink: the reuse path no longer runs a framework idle reaper. A
    connection that merely went quiet (even past IDLE_CLOSE_SEC) but is still
    LIVE is REUSED on the next run() — keepalives, not a framework idle timer,
    own death, and reusing a live connection saves a handshake and one
    connection attempt against the host. (The quiet connection's slot is freed
    only by the courtesy SWEEP when no further run() comes — see the sweep
    tests below.)"""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_engine, "IDLE_CLOSE_SEC", -1.0)  # "quiet" by any measure
    _install_connect(monkeypatch, lambda _t: _StubConn())
    engine.run("echo one", ssh_target="u@h", timeout=15)
    first = engine._conns["h"].conn
    engine.run("echo two", ssh_target="u@h", timeout=15)  # reused, NOT reaped+reopened
    assert engine._conns["h"].conn is first


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


class _HangingConn(_StubConn):
    """A fake connection whose ``run`` never returns within the test — models a
    remote leg that ignores asyncssh's own per-command ``timeout=`` (the live
    2026-07-08 15-min hang against a healthy cluster)."""

    async def run(  # type: ignore[override]
        self, cmd: str, *, check: bool = False, timeout: float | None = None
    ) -> _StubResult:
        import asyncio

        await asyncio.sleep(3600)  # never completes; only cancellation ends it
        raise AssertionError("unreachable")


# --- F-M: in-loop deadlines on every engine remote op ------------------------


def test_run_deadline_fires_when_command_never_returns(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-M fires-test: a command that never returns (asyncssh's own timeout does
    NOT trip) is bounded by the engine's in-loop asyncio deadline — it raises
    EngineUnavailable at ~the caller's timeout, NOT after the +10s thread
    backstop, and discards the wedged connection."""
    import time

    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_slots, "acquire_slot", lambda *a, **k: None)
    # Zero the in-loop slack so wait_for fires at exactly the caller timeout.
    monkeypatch.setattr(ssh_engine, "_LOOP_DEADLINE_MARGIN", 0.0)
    _install_connect(monkeypatch, lambda _t: _HangingConn())
    t0 = time.monotonic()
    with pytest.raises(EngineUnavailable):
        engine.run("stuck", ssh_target="u@h", timeout=0.3)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, (
        f"in-loop deadline did not fire (thread backstop took over): {elapsed:.1f}s"
    )
    assert "h" not in engine._conns  # the wedged connection was discarded


def test_connect_deadline_fires_when_connect_never_returns(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-M fires-test (connect leg): a connect that never returns is bounded by
    the in-loop deadline over ``_do_connect``, raises EngineUnavailable, and
    releases the slot it claimed for the doomed open."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_slots, "acquire_slot", lambda *a, **k: "SLOT")
    released: list[object] = []
    monkeypatch.setattr(ssh_slots, "release_slot", _recorder(released))
    monkeypatch.setattr(ssh_engine, "_LOOP_DEADLINE_MARGIN", 0.0)
    monkeypatch.setattr(ssh_engine, "_connect_timeout", lambda: 0.3)

    async def _hang(_ssh_target: str) -> _StubConn:
        import asyncio

        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    monkeypatch.setattr(ssh_engine, "_connect", _hang)
    with pytest.raises(EngineUnavailable):
        engine.run("x", ssh_target="u@h", timeout=15)
    assert released == ["SLOT"], "the slot claimed for a wedged connect must be freed"


def test_run_deadline_does_not_fire_for_a_fast_command(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-M passes-test: a normal fast command under a tight deadline is
    unaffected — the in-loop bound never trips on healthy traffic."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_engine, "_LOOP_DEADLINE_MARGIN", 0.0)
    _install_connect(
        monkeypatch, lambda _t: _StubConn(lambda _c: _StubResult(returncode=0, stdout="quick"))
    )
    r = engine.run("echo quick", ssh_target="u@h", timeout=0.5)
    assert r.returncode == 0
    assert r.stdout == "quick"


# --- F-B residual: idle-close the pool (background sweep + slot release) ------


def test_sweep_idle_closes_idle_connection_session(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-B fires-test (run-14 correction): a connection idle past the threshold is
    closed by the sweep — WITHOUT a triggering run() — returning its idle
    login-node session. It no longer FREES A SLOT (the warm-idle connection
    already holds none since the per-command release), so the assertion is on the
    session close + zero leaked slots, not on a slot-release count."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_engine, "IDLE_CLOSE_SEC", -1.0)  # everything is "idle"
    conn = _StubConn()
    _install_connect(monkeypatch, lambda _t: conn)
    engine.run("echo x", ssh_target="u@h", timeout=15)
    # Already zero slots held while warm-idle (the whole point of the fix).
    assert "h" in engine._conns
    assert [p for p in ssh_slots.slot_paths("h") if p.exists()] == []
    engine._sweep_idle()
    assert "h" not in engine._conns  # reaped by the sweep alone (no second run())
    assert conn.closed is True  # the idle login-node session was closed
    assert [p for p in ssh_slots.slot_paths("h") if p.exists()] == []  # still no leak


def test_sweep_idle_leaves_an_active_connection(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-B passes-test: a freshly-used connection (idle ≤ threshold) is never
    reaped by the sweep and stays reusable."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_engine, "IDLE_CLOSE_SEC", 3600.0)
    _install_connect(monkeypatch, lambda _t: _StubConn())
    engine.run("echo x", ssh_target="u@h", timeout=15)
    engine._sweep_idle()
    assert "h" in engine._conns  # not idle → untouched
    # Warm-idle between commands holds no slot regardless of the sweep.
    assert [p for p in ssh_slots.slot_paths("h") if p.exists()] == []


class _BlockingConn(_StubConn):
    """A fake connection whose ``run`` blocks (in flight) until the test releases
    it — models a long remote leg (>IDLE_CLOSE_SEC) still executing when the idle
    reaper runs (bug-sweep #8)."""

    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self._started = started
        self._release = release

    async def run(  # type: ignore[override]
        self, cmd: str, *, check: bool = False, timeout: float | None = None
    ) -> _StubResult:
        import asyncio

        self.run_calls.append(cmd)
        self._started.set()  # signal the command is in flight
        while not self._release.is_set():
            await asyncio.sleep(0.01)  # yield the loop; poll the cross-thread gate
        return _StubResult(returncode=0, stdout="done")


def test_sweep_idle_does_not_sever_an_inflight_command(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bug-sweep #8: idleness is measured from last COMPLETION, so a remote leg
    longer than IDLE_CLOSE_SEC would be reaped mid-command — the in-flight
    command then fails and the seam silently re-runs it over one-shot ssh
    (duplicate execution of a possibly non-idempotent command). The inflight
    counter must protect an active connection: a mid-flight sweep (even with
    IDLE_CLOSE_SEC forced to -1, so idleness alone would reap) leaves the
    connection intact and the command completes over the engine — no
    EngineUnavailable, no one-shot fallback."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_engine, "IDLE_CLOSE_SEC", -1.0)  # everything looks "idle"
    started = threading.Event()
    release = threading.Event()
    conn = _BlockingConn(started, release)
    _install_connect(monkeypatch, lambda _t: conn)

    box: dict[str, object] = {}

    def _worker() -> None:
        try:
            box["cp"] = engine.run("slow-remote-leg", ssh_target="u@h", timeout=30)
        except BaseException as exc:  # noqa: BLE001 — record for the assertion
            box["exc"] = exc

    worker = threading.Thread(target=_worker)
    worker.start()
    try:
        assert started.wait(5.0), "the command never reached the in-flight state"
        # Mid-flight sweep: idleness alone (IDLE_CLOSE_SEC=-1) would discard the
        # connection; the inflight counter must veto that.
        engine._sweep_idle()
        assert "h" in engine._conns, "an in-flight connection was severed by the sweep"
        assert engine._conns["h"].inflight == 1
        release.set()
        worker.join(5.0)
    finally:
        release.set()
        worker.join(5.0)
    assert "exc" not in box, f"the command fell back / failed: {box.get('exc')!r}"
    cp = box["cp"]
    assert isinstance(cp, subprocess.CompletedProcess)
    assert cp.returncode == 0 and cp.stdout == "done"
    assert conn.closed is False  # connection never torn down
    assert engine._conns["h"].inflight == 0  # counter restored after completion


def test_background_sweeper_reaps_without_a_triggering_run(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-B fires-test (the real seam): the background reaper thread — started on
    open — closes an idle connection's login-node session with NO further run(),
    which is exactly the immortal mcp-serve case the residual describes. Since the
    run-14 correction the slot is already free while idle, so the observable is the
    connection close (and no leaked slot), not a slot-release."""
    import time

    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_engine, "IDLE_CLOSE_SEC", -1.0)
    monkeypatch.setattr(ssh_engine, "_SWEEP_INTERVAL_SEC", 0.05)  # sweep fast for the test
    conn = _StubConn()
    _install_connect(monkeypatch, lambda _t: conn)
    engine.run("echo x", ssh_target="u@h", timeout=15)  # opens conn + starts the reaper
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and "h" in engine._conns:
        time.sleep(0.05)
    assert "h" not in engine._conns, "the background reaper never closed the idle connection"
    assert conn.closed is True  # the idle login-node session was closed
    assert [p for p in ssh_slots.slot_paths("h") if p.exists()] == []  # no leaked slot


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


# --- F55/F56: dispatched marker + inflight veto on the failure-path teardown --


def test_post_dispatch_failure_is_marked_dispatched(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F55 fire-path: a failure AFTER the command reached the connection (a torn
    channel mid-``run``) raises ``EngineUnavailable(dispatched=True)`` so the ssh
    seam refuses to re-execute a non-idempotent command one-shot (the remote half
    may still be running)."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    _install_connect(monkeypatch, lambda _t: _StubConn(raises=OSError("channel torn")))
    with pytest.raises(EngineUnavailable) as ei:
        engine.run("qsub job.sh", ssh_target="u@h", timeout=15)
    assert ei.value.dispatched is True


def test_pre_dispatch_connect_failure_is_not_dispatched(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F55: a PRE-dispatch failure (the connection never opened) never ran the
    command — ``dispatched`` stays False, so even a non-idempotent command may
    safely fall back to one-shot."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_slots, "acquire_slot", lambda *a, **k: "SLOT")
    monkeypatch.setattr(ssh_slots, "release_slot", lambda *a, **k: None)

    async def _boom(_ssh_target: str) -> _StubConn:
        raise OSError("connection refused")

    monkeypatch.setattr(ssh_engine, "_connect", _boom)
    with pytest.raises(EngineUnavailable) as ei:
        engine.run("qsub job.sh", ssh_target="u@h", timeout=15)
    assert ei.value.dispatched is False


def test_peer_command_survives_a_sibling_failure(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F56 fire-path: a per-command failure must NOT close a shared connection out
    from under PEER commands still multiplexed on it — that severs their channels
    and forces each to re-run one-shot (the burst the slot/breaker machinery
    exists to avoid). With a peer in flight the failing command DRAINS the
    connection (no close) instead of discarding it; the peer completes over the
    engine, and the last finisher performs the deferred close."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_slots, "acquire_slot", lambda *a, **k: "SLOT")
    monkeypatch.setattr(ssh_slots, "release_slot", lambda *a, **k: None)

    peer_started = threading.Event()
    peer_release = threading.Event()

    class _MixedConn(_StubConn):
        async def run(  # type: ignore[override]
            self, cmd: str, *, check: bool = False, timeout: float | None = None
        ) -> _StubResult:
            import asyncio

            self.run_calls.append(cmd)
            if cmd == "peer":
                peer_started.set()  # peer is in flight (inflight == 1)
                while not peer_release.is_set():
                    await asyncio.sleep(0.01)
                return _StubResult(returncode=0, stdout="peer-done")
            raise OSError("channel torn")  # the failing sibling

    conn = _MixedConn()
    _install_connect(monkeypatch, lambda _t: conn)

    box: dict[str, object] = {}

    def _peer() -> None:
        try:
            box["peer"] = engine.run("peer", ssh_target="u@h", timeout=30)
        except BaseException as exc:  # noqa: BLE001 — record for the assertion
            box["peer_exc"] = exc

    worker = threading.Thread(target=_peer)
    worker.start()
    try:
        assert peer_started.wait(5.0), "the peer command never reached the in-flight state"
        # The sibling fails while the peer is in flight (inflight == 2): it must
        # DRAIN the connection, not sever it.
        with pytest.raises(EngineUnavailable) as ei:
            engine.run("failer", ssh_target="u@h", timeout=30)
        assert ei.value.dispatched is True
        assert conn.closed is False, "a sibling failure severed a connection with a peer in flight"
        assert "h" not in engine._conns, "the drained connection must be unregistered (no reuse)"
        peer_release.set()
        worker.join(5.0)
    finally:
        peer_release.set()
        worker.join(5.0)
    assert "peer_exc" not in box, (
        f"the peer was severed by the sibling failure: {box.get('peer_exc')!r}"
    )
    cp = box["peer"]
    assert isinstance(cp, subprocess.CompletedProcess)
    assert cp.stdout == "peer-done"  # the peer completed over the engine, no fallback


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


class TestNativeKeepaliveLifecycle:
    """G4 shrink: liveness is asyncssh-NATIVE (keepalives), not a framework idle
    reaper. These pin that the connect kwargs carry keepalives, that the interval
    shares the native path's one knob, and that death is detected at USE time via
    a library exception (never a framework timer severing a connection)."""

    def test_connect_kwargs_carry_native_keepalives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The asyncssh connect passes keepalive_interval / keepalive_count_max —
        the library's own liveness mechanism keeps a NAT'd flow alive and closes
        a silently-dropped session (the finding-24 fix, delegated to asyncssh)."""
        captured: dict[str, object] = {}

        class _Cap:
            @staticmethod
            async def connect(host: str, **kwargs: object) -> _StubConn:
                captured.update(kwargs)
                captured["host"] = host
                return _StubConn()

        monkeypatch.delenv("HPC_SSH_KEEPALIVE_INTERVAL", raising=False)
        # Drive the REAL _connect (not the stub seam) against a fake asyncssh, and
        # skip the multi-address hand-dial so connect() is reached directly.
        monkeypatch.setattr(ssh_engine, "_dial_multi_address", _async_none)
        monkeypatch.setitem(__import__("sys").modules, "asyncssh", _Cap)
        import asyncio

        asyncio.run(ssh_engine._connect("u@h"))
        assert captured["keepalive_interval"] == ssh_engine._DEFAULT_KEEPALIVE_INTERVAL
        assert captured["keepalive_count_max"] == ssh_engine._KEEPALIVE_COUNT_MAX

    def test_keepalive_interval_shares_the_native_knob(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One keepalive tunable across both transports: the engine reads the
        same HPC_SSH_KEEPALIVE_INTERVAL the native ssh path uses; 'default' /
        invalid falls to the engine default (asyncssh has no ssh_config)."""
        monkeypatch.setenv("HPC_SSH_KEEPALIVE_INTERVAL", "42")
        assert ssh_engine._keepalive_interval() == 42
        for off in ("default", "", "-5", "abc"):
            monkeypatch.setenv("HPC_SSH_KEEPALIVE_INTERVAL", off)
            assert ssh_engine._keepalive_interval() == ssh_engine._DEFAULT_KEEPALIVE_INTERVAL

    def test_death_surfaces_at_use_time_via_library_exception(
        self, engine: _Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No framework liveness probe: a session asyncssh has closed is noticed
        only when the NEXT run() dispatches on it and the library raises — the
        engine discards + reconnects. (Contrast the deleted idle reaper, which
        severed a live connection on a framework timer.)"""
        monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
        calls = {"n": 0}

        def _factory(_t: str) -> _StubConn:
            calls["n"] += 1
            if calls["n"] == 1:
                # First conn dies silently; asyncssh raises ConnectionLost on use.
                return _StubConn(raises=asyncssh.ConnectionLost("keepalives expired"))
            return _StubConn(lambda _c: _StubResult(returncode=0, stdout="reconnected"))

        _install_connect(monkeypatch, _factory)
        with pytest.raises(EngineUnavailable):
            engine.run("cmd", ssh_target="u@h", timeout=15)
        assert "h" not in engine._conns  # the dead session was discarded on use
        r = engine.run("cmd", ssh_target="u@h", timeout=15)
        assert r.stdout == "reconnected"


def test_successful_command_resets_the_breaker_counter(
    engine: _Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One-shot parity: each successful command records a breaker SUCCESS
    (resetting the consecutive-failure counter), exactly as every successful
    guarded_call does — a held connection actively proving the host reachable
    must not let other processes' one-shot failures accumulate against it."""
    monkeypatch.setattr(ssh_circuit, "check_circuit", lambda *a, **k: None)
    monkeypatch.setattr(ssh_slots, "acquire_slot", lambda *a, **k: None)
    successes: list[object] = []
    monkeypatch.setattr(ssh_engine.ssh_circuit, "record_connection_success", _recorder(successes))
    _install_connect(
        monkeypatch, lambda _t: _StubConn(lambda _c: _StubResult(returncode=0, stdout="ok"))
    )
    engine.run("printf ok", ssh_target="u@h", timeout=15)
    connect_time = len(successes)
    engine.run("printf ok", ssh_target="u@h", timeout=15)  # warm reuse, no reconnect
    assert len(successes) > connect_time, (
        "a successful command over the held connection must record a breaker "
        "success (one-shot parity), not only the connect"
    )
