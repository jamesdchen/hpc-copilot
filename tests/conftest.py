"""Shared test fixtures and helpers.

Reduces duplication across the seven test files that hand-write a
sidecar JSON and/or a stub ``.hpc/tasks.py``. Helpers are intentionally
plain functions (not pytest fixtures) so callers compose them with
their own ``tmp_path`` and ``monkeypatch``.

- :func:`make_sidecar_json` writes a per-run sidecar at
  ``<dir>/.hpc/runs/<run_id>.json`` with sensible defaults; any field
  may be overridden via kwargs. Returns the path written.
- :func:`write_hpc_tasks` writes a ``.hpc/tasks.py`` exposing
  ``total()`` / ``resolve(i)`` over a list of kwarg dicts. Returns the
  path written.

Both helpers default to the v1 sidecar shape — that is what the
existing fixtures wrote, and the production read path
(:func:`hpc_agent.state.runs.read_run_sidecar`) backfills v1 to v2
on read.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# Populate the primitive registry at conftest IMPORT time so test
# modules whose top-level imports trigger ``@primitive(composes=[...])``
# decorators with string-name composes find the dependency primitives
# already registered. Pytest fixtures (including the session-scoped
# autouse one below) run during execution; collection is too late for
# import-time decorator errors. The call is idempotent.
from hpc_agent import register_primitives as _register_primitives_at_collection_time  # noqa: E402

_register_primitives_at_collection_time()


# ---------------------------------------------------------------------------
# Windows-only: relocate pytest's temp ROOT off %TEMP% onto a space-free,
# Defender-excludable path to reclaim the file-by-file AV-scan tax.
#
# %TEMP% is not on the Defender exclusion list (the repo dir is), so every
# tmp_path file pytest writes gets scanned inline — measured as 2-3x
# full-suite wall-clock variance (see the pyproject ``[tool.pytest.ini_options]``
# comment block). The obvious ``--basetemp C:\hpc-pytest-tmp`` is the WRONG
# lever twice over: (1) an explicit ``--basetemp`` makes pytest ``rm_rf`` the
# WHOLE dir at every session start (``TempPathFactory.getbasetemp``), clobbering
# any concurrently-running slice's subdirs — it also drops the numbered-dir
# rotation entirely; and (2) a hardcoded ``C:\`` path in ``addopts`` would break
# the Linux CI runner. We instead set ``PYTEST_DEBUG_TEMPROOT`` — pytest's
# supported knob for the temp *root* — which keeps the default
# ``make_numbered_dir_with_cleanup(keep=…)`` rotation (each run gets its own
# ``pytest-of-<user>/pytest-<N>/`` subtree; old ones rotate out; no run ever
# wipes another's). A space-free root also sidesteps the reason ``--basetemp``
# under THIS repo is impossible: several hook tests embed ``tmp_path`` UNQUOTED
# in command strings (e.g. ``tests/_kernel/hooks/test_skill_return_autofetch``'s
# ``_emit_command`` interpolates ``--experiment-dir {tmp_path}`` bare), so a
# path containing the repo's ``CC Allowed`` space would split mid-argument.
#
# CI is untouched and byte-identical: the guard is a hard ``sys.platform ==
# "win32"`` gate, and it no-ops silently if the dir can't be created or an
# explicit override is already in play. To claim the full win the user should
# exclude the dir from Defender (see the pyproject comment for the exact
# ``Add-MpPreference`` line).
_WIN_PYTEST_TEMPROOT = r"C:\hpc-pytest-tmp"


def pytest_configure(config: pytest.Config) -> None:  # noqa: ARG001
    """Point pytest's temp root at a space-free, Defender-excludable dir on Windows.

    No-op on every non-Windows platform (CI is Linux → this function returns
    immediately, leaving the temp root at the default ``tempfile.gettempdir()``).
    Also yields to an explicit ``PYTEST_DEBUG_TEMPROOT`` already in the env and
    to an explicit ``--basetemp`` (which pytest honours over the env root). The
    directory is created on first use; creation failure falls back silently to
    the default root rather than aborting the session.
    """
    if sys.platform != "win32":
        return
    if os.environ.get("PYTEST_DEBUG_TEMPROOT") or config.option.basetemp is not None:
        return
    try:
        os.makedirs(_WIN_PYTEST_TEMPROOT, exist_ok=True)
    except OSError:
        # Un-creatable (no C:\ write access, read-only mount, …): leave the
        # default %TEMP% root in place. The suite still runs, just without the
        # AV-scan speedup.
        return
    os.environ["PYTEST_DEBUG_TEMPROOT"] = _WIN_PYTEST_TEMPROOT


# Default sidecar fields reproduced verbatim from the seven existing
# call sites. Test overrides take precedence; anything not overridden
# matches the historical fixture.
_DEFAULT_SIDECAR: dict[str, Any] = {
    "sidecar_schema_version": 1,
    "cmd_sha": "deadbeef" * 8,
    "hpc_agent_version": "0.0.0+test",
    "submitted_at": "2026-01-01T00:00:00Z",
    "executor": "true",
    "task_count": 1,
    "tasks_py_sha": "abc",
}


def make_sidecar_json(
    tmp_path: Path,
    *,
    run_id: str = "test_run",
    result_dir_template: str | None = None,
    **overrides: Any,
) -> Path:
    """Write ``<tmp_path>/.hpc/runs/<run_id>.json`` and return its path.

    Overrides may include any sidecar field (``executor``,
    ``task_count``, ``wave_map``, ``sidecar_schema_version``, …) and
    are merged on top of the historical defaults.

    *result_dir_template* defaults to ``<tmp_path>/out`` to match the
    most common pattern in the existing tests; pass an explicit value
    when the test cares about format placeholders.
    """
    runs_dir = tmp_path / ".hpc" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    sidecar: dict[str, Any] = dict(_DEFAULT_SIDECAR)
    sidecar["run_id"] = run_id
    sidecar["result_dir_template"] = result_dir_template or str(tmp_path / "out")
    sidecar.update(overrides)

    target = runs_dir / f"{run_id}.json"
    target.write_text(json.dumps(sidecar))
    return target


def write_hpc_tasks(hpc_dir: Path, tasks: list[dict[str, Any]]) -> Path:
    """Write a ``.hpc/tasks.py`` stub exposing ``total()``/``resolve()``.

    *hpc_dir* must already exist (call :func:`make_sidecar_json` first
    when both are needed; or create the dir yourself).
    """
    hpc_dir.mkdir(parents=True, exist_ok=True)
    tasks_py = hpc_dir / "tasks.py"
    # Use repr() rather than json.dumps so tests can exercise richer Python
    # literals (tuples, sets, etc.) that production handles fine.
    tasks_py.write_text(
        f"_TASKS = {tasks!r}\ndef total(): return len(_TASKS)\ndef resolve(i): return _TASKS[i]\n"
    )
    return tasks_py


@pytest.fixture(autouse=True)
def _isolated_journal_home(tmp_path: Path) -> Iterator[None]:
    """Redirect the hpc journal home to ``tmp_path`` for EVERY test.

    Everything under ``~/.claude/hpc/`` — the per-repo journal
    (``<repo_hash>/``), the detached-worker spec/log/lease files
    (``_detached/``), and the global caches (canary / discover /
    preflight / describe / skill-return breadcrumb) — resolves through
    :func:`hpc_agent.state.run_record._current_homedir`. Any test that
    exercises those paths without redirecting the home writes into the
    developer's REAL ``~/.claude/hpc/`` (proving-run #3 findings item g:
    thousands of leaked ``<repo_hash>/`` dirs keyed to pytest tmp paths,
    plus ``_detached/submit-s2-ml_run_abcd1234-*`` spec files).

    Per-test opt-outs remain fully honoured because this fixture uses
    the LOWEST-precedence knob and runs at setup time, before any
    test-owned fixture:

    * ``monkeypatch.setenv("HPC_JOURNAL_DIR", ...)`` (the documented
      idiom) — env wins over the ``HPC_HOMEDIR`` attribute patched here.
    * ``monkeypatch.setattr(run_record, "HPC_HOMEDIR", ...)`` (the
      legacy idiom) — the test's setattr lands after this fixture's, so
      its value wins for the test body and monkeypatch undo restores
      this fixture's value, which teardown here then restores again.

    Any ``HPC_JOURNAL_DIR`` inherited from the invoking shell is
    removed for the test's duration (and restored after) — otherwise it
    would out-rank the attribute and defeat the isolation.

    Env and attr are saved/restored by hand rather than via
    ``monkeypatch`` for the same finalizer-order-neutrality reason as
    ``_hermetic_cluster_binaries`` below.
    """
    from hpc_agent.state import run_record

    saved_env = os.environ.pop("HPC_JOURNAL_DIR", None)
    saved_attr = run_record.HPC_HOMEDIR
    run_record.HPC_HOMEDIR = tmp_path / "hpc_journal_home"
    try:
        yield
    finally:
        run_record.HPC_HOMEDIR = saved_attr
        if saved_env is not None:
            os.environ["HPC_JOURNAL_DIR"] = saved_env
        else:
            os.environ.pop("HPC_JOURNAL_DIR", None)


@pytest.fixture(scope="session")
def _claude_config_sandbox(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The session's stand-in for ``~/.claude`` — created OUTSIDE any test's tmp_path.

    Deliberately NOT ``tmp_path / "..."``: a config dir nested inside the test's
    own temp dir is visible to every test that ENUMERATES that dir, and two do
    exactly that (``ops/test_dir_digest`` counts subdirectories;
    ``execution/mapreduce/test_metrics_io`` asserts its dir is clean). An
    isolation guard must not be observable by the code it isolates.
    """
    return tmp_path_factory.mktemp("claude_config_home")


@pytest.fixture(autouse=True)
def _isolated_claude_config_dir(_claude_config_sandbox: Path) -> Iterator[None]:
    """Redirect the harness CONFIG dir away from the real ``~/.claude`` for EVERY test.

    The sibling of ``_isolated_journal_home`` on the OTHER axis. Everything that
    resolves through :func:`hpc_agent.agent_assets.resolve_claude_dir` — the
    installed ``settings.json``, the capability probe's read side, and
    ``doctor``'s consent-forwarding-hook drift check — otherwise lands on the
    DEVELOPER's real ``~/.claude``.

    That makes verdicts depend on the workstation: ``doctor``'s envelope grows a
    real alert whenever the developer's own install is mid-upgrade, so
    ``assert out["alerts"] == []`` passes in clean CI and fails locally. Two
    call sites already read the config dir from an un-isolated ``doctor()``
    (``tests/ops/recover/*``, ``tests/ops/decision/test_overnight_self_heal.py``),
    and any future one inherits the guard for free — which is the point of
    putting it here rather than in one package's conftest.

    Lowest precedence, setup-time, env-only, saved/restored by hand rather than
    via ``monkeypatch`` — the same finalizer-order-neutrality rationale as
    ``_hermetic_cluster_binaries`` and ``_default_native_ssh_engine`` below. A
    test that exercises the resolver sets its own ``CLAUDE_CONFIG_DIR`` via
    ``monkeypatch.setenv`` (``tests/cli/test_install_config_dir.py``,
    ``tests/ops/test_harness_capabilities.py``), which lands after this fixture
    and therefore wins for the test body.
    """
    saved = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(_claude_config_sandbox)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = saved
        # The sandbox is SESSION-scoped, so anything a test wrote into it would
        # leak forward (a later test's `doctor` would see an installed harness).
        # Nothing in-tree writes here — every test that installs sets its own
        # CLAUDE_CONFIG_DIR — so this is a cheap scandir on an empty dir that
        # keeps the guard true even if one ever does.
        for leftover in _claude_config_sandbox.iterdir():
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
            else:
                leftover.unlink(missing_ok=True)


@pytest.fixture
def journal_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Re-point the journal home to a NAMED tmp dir and return that path.

    The dozens of per-file ``journal_home``/``_journal_home`` fixtures all did
    exactly this: patch ``run_record.HPC_HOMEDIR`` to ``tmp_path / "home_hpc"``
    and hand the path back so the test can seed sidecars/records under a home it
    can also read. This is the single shared version they collapse onto.

    It COEXISTS with the autouse ``_isolated_journal_home`` above rather than
    replacing it: that guard runs first at setup and redirects the home to a
    *different* tmp subdir for EVERY test (the leak-proof floor, honoured whether
    or not a test asks for a named home). Requesting this fixture re-points the
    same attribute again — via the identical ``monkeypatch.setattr`` idiom, so
    ordering is deterministic and undo restores cleanly — to the ``home_hpc``
    path it returns, which the test body then reads.
    """
    from hpc_agent.state import run_record

    home = tmp_path / "home_hpc"
    monkeypatch.setattr(run_record, "HPC_HOMEDIR", home)
    return home


@pytest.fixture(scope="session", autouse=True)
def _register_primitives_once() -> None:
    """Populate the @primitive registry once per pytest session.

    The C\u2032-v2 spine no longer auto-imports primitive-bearing modules
    on first registry query; ``register_primitives()`` must be called
    explicitly. Tests that exercise ``get_registry`` / ``get_meta``
    would otherwise hit the new RuntimeError. Idempotent.

    The duplicate top-level call below (executed at conftest IMPORT
    time, before pytest collection scans test files) covers the case
    where a test module's top-level imports trigger a primitive
    decorator whose ``composes=[...]`` uses string names \u2014 the
    registry must already be populated when that decoration runs.
    Without it, e.g. ``from hpc_agent.ops import aggregate_flow``
    fails at collection with ``ValueError: composes references
    'poll-run-status' which is not a registered primitive``.
    """
    from hpc_agent import register_primitives

    register_primitives()


# ---------------------------------------------------------------------------
# Default-tier hermeticity: no real cluster binary in a non-``slow`` test.
#
# A default-tier (non-``slow``) test that reaches a real ``ssh``/``scp``/
# ``rsync``/``ssh-add`` is non-hermetic: it passes or fails on whether the
# *host* happens to ship that binary, not on the code under test. The leak
# that motivated this guard: ``tests/ops/aggregate/test_flow_preconditions``
# expected an ``HpcError`` from the transport seam but got a bare
# ``FileNotFoundError: 'scp'`` on a runner without ``scp`` installed — the
# test only "passed" where ``scp`` happened to exist.
#
# The fix is a runtime guard, not a static one: whether a seam reaches the
# cluster is dynamic. We shadow every cluster binary with a stub that exits
# non-zero with a pointer message, applied to every non-``slow`` test:
#
#   * PATH-prepend covers the bare-name lookup (``rsync`` has no env knob and
#     is resolved straight off PATH; see ``infra.ssh_options``).
#   * ``HPC_{SSH,SCP,SSH_ADD}_BINARY`` cover the env-override resolvers, which
#     win unconditionally on every platform.
#
# Net effect: a non-``slow`` test that genuinely talks to a cluster now fails
# loudly and identically on every host (the seam wraps the non-zero exit into
# an ``HpcError``), instead of depending on the host's PATH. ``slow`` tests opt
# back into the real binaries by construction — the marker is the opt-in.
_CLUSTER_BINARY_SHIMS = ("ssh", "scp", "rsync", "ssh-add")


@pytest.fixture(scope="session")
def _cluster_binary_shim_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A dir of POSIX-shell stubs that shadow the cluster binaries."""
    shim_dir = tmp_path_factory.mktemp("hermetic_cluster_shims")
    for name in _CLUSTER_BINARY_SHIMS:
        stub = shim_dir / name
        msg = (
            f"hermetic-guard: a non-slow test invoked the real '{name}'. "
            "A default-tier test must not reach a cluster binary: mark it "
            "@pytest.mark.slow, or stub the transport seam "
            "(hpc_agent.infra.remote / hpc_agent.infra.ssh_options)."
        )
        stub.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(msg)} 1>&2\nexit 97\n")
        stub.chmod(0o755)
    return shim_dir


@pytest.fixture(autouse=True)
def _hermetic_cluster_binaries(request: pytest.FixtureRequest) -> Iterator[None]:
    """Shadow real cluster binaries for every non-``slow`` test.

    POSIX-only: the shims are shell scripts, and the *blocking* CI matrix is
    Linux. The Windows lane is non-blocking (``continue-on-error``), so we skip
    the guard there rather than ship ``.exe`` shims.

    Env is saved/restored by hand rather than via the ``monkeypatch`` fixture
    *on purpose*: depending on ``monkeypatch`` from an autouse fixture forces it
    to set up before every test's own fixtures, which silently reorders
    finalizers for any test that relies on ``monkeypatch`` undo running before a
    sibling autouse teardown (e.g. an ``lru_cache.cache_clear()`` teardown).
    Owning the env directly keeps this guard finalizer-order-neutral.
    """
    if request.node.get_closest_marker("slow") is not None or sys.platform == "win32":
        yield
        return
    shim_dir = request.getfixturevalue("_cluster_binary_shim_dir")
    overrides = {
        "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HPC_SSH_BINARY": str(shim_dir / "ssh"),
        "HPC_SCP_BINARY": str(shim_dir / "scp"),
        "HPC_SSH_ADD_BINARY": str(shim_dir / "ssh-add"),
    }
    saved = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, prev in saved.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


@pytest.fixture(autouse=True)
def _s2_path_gate_off_by_default() -> Iterator[None]:
    """Disable the S2 pre-detach path gate for every test that does not opt in.

    The gate SENSES the run's real ssh target before detaching — bounded TCP
    connects and, with an activation, a bounded ssh. That is exactly right in
    production and exactly wrong in CI, where a fixture host like ``h`` would
    turn every ``submit-s2`` detach test into a live dial (and its verdict into a
    function of the CI box's DNS). The suite-wide default is therefore OFF, so
    "no real SSH in CI" is structural rather than a property each test remembers.

    The gate's own behaviour is covered by ``tests/ops/test_path_gate.py``, which
    injects a reader and never touches a socket, and the sensors underneath it by
    ``tests/infra/test_readiness_sensors.py``. A test that wants the gate live
    re-enables it explicitly with ``monkeypatch.delenv(path_gate.GATE_ENV)``.

    Env owned directly (not via ``monkeypatch``) for the finalizer-order reason
    documented on :func:`_hermetic_cluster_binaries`.
    """
    saved = os.environ.get("HPC_S2_PATH_GATE")
    os.environ["HPC_S2_PATH_GATE"] = "0"
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HPC_S2_PATH_GATE", None)
        else:
            os.environ["HPC_S2_PATH_GATE"] = saved


@pytest.fixture(autouse=True)
def _default_native_ssh_engine() -> Iterator[None]:
    """Pin the one-shot ``native`` SSH engine for every test that doesn't opt in.

    The persistent asyncssh engine is default-ON in production since the
    latency-audit rank-3 flip (2026-07-16) — an UNSET ``HPC_SSH_ENGINE`` selects
    it (``hpc_agent.infra.ssh_engine.engine_enabled``). But the whole test suite
    predates that flip and is written assuming the one-shot path: hundreds of
    ``ssh_run`` capture tests stub the ONE-SHOT seam
    (``remote.capture_via_select``) and never install a fake engine, so a
    default-on engine would route them through a REAL ``asyncssh.connect`` to a
    fake host before the one-shot fallback — non-hermetic and slow (the binary
    shims above cannot shadow a Python library). Pinning ``native`` here keeps
    the pre-flip test contract exactly.

    Lowest precedence, setup-time, env-only (no ``monkeypatch`` — same
    finalizer-order-neutrality rationale as ``_hermetic_cluster_binaries``): a
    test that exercises the engine overrides it with its own
    ``monkeypatch.setenv``/``delenv`` (e.g. ``tests/infra/test_ssh_engine.py``,
    ``tests/cli/test_mcp_engine_default.py``), whose value wins for the body and
    whose undo restores this pin before this fixture's own teardown.
    """
    saved = os.environ.get("HPC_SSH_ENGINE")
    os.environ["HPC_SSH_ENGINE"] = "native"
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HPC_SSH_ENGINE", None)
        else:
            os.environ["HPC_SSH_ENGINE"] = saved


@pytest.fixture(autouse=True)
def _default_no_ssh_pacing() -> Iterator[None]:
    """Disable the SSH-establishment RATE limiter for every test that doesn't opt in.

    The token-bucket pacer (``hpc_agent.infra.ssh_pacing``, wired into
    ``ssh_circuit.guarded_call`` and ``ssh_engine._Engine._open``) is on by
    default in production, but rate-limiting is inherently about SEQUENTIAL call
    frequency: unlike the concurrency-slot limiter (which only ever sleeps on
    *concurrent* contention, so sequential-call tests never touch it), the pacer
    would make the 4th+ back-to-back ``guarded_call`` in a fixed-clock test
    really sleep. Hundreds of breaker / remote / transport / engine tests fire
    ssh-family calls in tight loops under a frozen ``FakeClock``, so a default-on
    pacer would inject real sub-second sleeps and (with the clock frozen) never
    refill. Pinning ``HPC_NO_SSH_PACING=1`` here keeps the pre-pacing test
    contract byte-identical; ``tests/infra/test_ssh_pacing.py`` opts back in with
    ``monkeypatch.delenv`` + injected clock/sleep.

    Lowest precedence, setup-time, env-only (same finalizer-order-neutrality
    rationale as ``_default_native_ssh_engine`` above).
    """
    saved = os.environ.get("HPC_NO_SSH_PACING")
    os.environ["HPC_NO_SSH_PACING"] = "1"
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HPC_NO_SSH_PACING", None)
        else:
            os.environ["HPC_NO_SSH_PACING"] = saved


# ── no console windows, EVER (win32) ─────────────────────────────────────────
#
# Applied at IMPORT TIME, not as a fixture: pytest-xdist spawns its worker
# processes from the controller BEFORE any session fixture runs, so a fixture
# would silence spawns made inside workers while the worker spawns themselves
# still flash one console each (2026-07-30 popup storm, second wave). This
# module imports in the controller AND in every worker at collection, which
# covers both. ``CREATE_NO_WINDOW`` is mutually exclusive with
# ``DETACHED_PROCESS`` (both steer console creation), so detaching spawns are
# left untouched. POSIX: no-op.
if sys.platform == "win32":
    import subprocess as _subprocess

    _NO_WINDOW = getattr(_subprocess, "CREATE_NO_WINDOW", 0)
    _DETACHED = getattr(_subprocess, "DETACHED_PROCESS", 0)
    _ORIG_POPEN_INIT = _subprocess.Popen.__init__

    def _init_no_window(self: Any, *args: Any, **kwargs: Any) -> None:
        flags = kwargs.get("creationflags", 0)
        if not flags & _DETACHED:
            kwargs["creationflags"] = flags | _NO_WINDOW
        _ORIG_POPEN_INIT(self, *args, **kwargs)

    _subprocess.Popen.__init__ = _init_no_window  # type: ignore[method-assign]

# Hard-error dialogs, also suppressed (win32): the severance tests (worker
# survives job-handle close, pipe-sever fixtures) make Windows raise
# 0x800700E8 "the pipe is being closed" AT CHILD LAUNCH, and without
# SEM_FAILCRITICALERRORS the OS shows a MODAL error box per occurrence —
# the 2026-07-30 popup storm's second species (the first was console
# windows, above). Error mode is inherited, so setting it here covers every
# child the suite spawns. POSIX: no-op.
if sys.platform == "win32":
    import ctypes

    # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
