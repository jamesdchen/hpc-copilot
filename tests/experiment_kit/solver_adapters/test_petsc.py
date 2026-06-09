"""Tests for the PETSc solver adapter (checkpoint injection via PETSc hooks)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hpc_agent.experiment_kit import checkpoint as ck
from hpc_agent.experiment_kit.solver_adapters import petsc


@pytest.fixture(autouse=True)
def _isolate_checkpoint_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the stable checkpoint dir at tmp_path and reset cadence state."""
    monkeypatch.setenv("HPC_CHECKPOINT_DIR", str(tmp_path / "_checkpoints"))
    monkeypatch.delenv("HPC_WALLTIME_END_EPOCH", raising=False)
    ck._reset_should_checkpoint_state()
    yield
    ck._reset_should_checkpoint_state()


# ─── detection ─────────────────────────────────────────────────────────────


_TS_SCRIPT = """\
from petsc4py import PETSc

ts = PETSc.TS().create(comm=PETSc.COMM_WORLD)
ts.setProblemType(PETSc.TS.ProblemType.NONLINEAR)
ts.setFromOptions()
ts.solve(u)
"""

_SNES_SCRIPT = """\
import petsc4py.PETSc
from petsc4py import PETSc

snes = PETSc.SNES()
snes.solve(None, x)
"""


def test_detects_ts_solve_with_set_from_options() -> None:
    hit = petsc.detect_petsc_solver(_TS_SCRIPT)
    assert hit is not None
    assert hit.solver_kind == "ts"
    assert hit.solver_var == "ts"
    assert hit.sets_from_options is True
    assert "PETSC_OPTIONS" in hit.evidence


def test_detects_snes_solve_without_set_from_options() -> None:
    hit = petsc.detect_petsc_solver(_SNES_SCRIPT)
    assert hit is not None
    assert hit.solver_kind == "snes"
    assert hit.sets_from_options is False


def test_detects_qualified_constructor() -> None:
    src = "import petsc4py.PETSc as p4\nts = p4.PETSc.TS()\nts.solve(u)\n"
    # Constructor reached through ``<anything>.PETSc.TS`` still matches.
    hit = petsc.detect_petsc_solver(src)
    assert hit is not None and hit.solver_kind == "ts"


@pytest.mark.parametrize(
    "src",
    [
        # No petsc4py import at all — a plain argparse script.
        "import argparse\nts = object()\nts.solve()\n",
        # Import present but no solver construction.
        "from petsc4py import PETSc\nprint(PETSc.COMM_WORLD)\n",
        # Construction but never solved.
        "from petsc4py import PETSc\nts = PETSc.TS()\nts.setUp()\n",
        # Unparseable source is a miss, not an error.
        "def broken(:\n",
    ],
    ids=["no-import", "no-ctor", "no-solve", "syntax-error"],
)
def test_detection_negatives(src: str) -> None:
    assert petsc.detect_petsc_solver(src) is None


# ─── options rendering ─────────────────────────────────────────────────────


def test_checkpoint_options_renders_binary_viewer_spec(tmp_path: Path) -> None:
    opt = petsc.checkpoint_options(solver_kind="ts", solution_path=tmp_path / "sol.bin")
    assert opt == f"-ts_monitor_solution binary:{tmp_path / 'sol.bin'}"
    opt = petsc.checkpoint_options(solver_kind="snes", solution_path="/r/sol.bin")
    assert opt == "-snes_monitor_solution binary:/r/sol.bin"


def test_checkpoint_options_rejects_whitespace_path() -> None:
    # PETSC_OPTIONS tokenizes on whitespace with no quoting — fail loudly.
    with pytest.raises(ValueError, match="whitespace"):
        petsc.checkpoint_options(solver_kind="ts", solution_path="/a dir/sol.bin")


def test_canary_options_caps_the_solve() -> None:
    assert petsc.canary_options("ts") == "-ts_max_steps 2"
    assert petsc.canary_options("snes") == "-snes_max_it 2"


def test_unknown_solver_kind_rejected() -> None:
    with pytest.raises(ValueError, match="unknown PETSc solver kind"):
        petsc.canary_options("ksp")
    with pytest.raises(ValueError, match="unknown PETSc solver kind"):
        petsc.checkpoint_options(solver_kind="ksp", solution_path="/x")


def test_resume_args_validates_flag_shape() -> None:
    assert petsc.resume_args("-restart_file", "/c/k.bin") == ["-restart_file", "/c/k.bin"]
    assert petsc.resume_args("--resume-from", "/c/k.bin") == ["--resume-from", "/c/k.bin"]
    for bad in ("restart", "-", "--", "-flag with space", "; rm -rf /"):
        with pytest.raises(ValueError, match="CLI flag"):
            petsc.resume_args(bad, "/c/k.bin")


# ─── wrapper-path restart rotation ─────────────────────────────────────────


def test_promote_restart_fresh_run_returns_none() -> None:
    assert petsc.promote_restart() is None


def test_promote_restart_rotates_solution_into_restart_slot() -> None:
    sol = petsc.wrapper_solution_path()
    sol.parent.mkdir(parents=True, exist_ok=True)
    sol.write_bytes(b"vec-dump")

    restart = petsc.promote_restart()

    assert restart is not None and restart.name == "petsc-restart.bin"
    assert restart.read_bytes() == b"vec-dump"
    # The solution slot is now free for the new attempt's monitor to truncate.
    assert not sol.exists()


def test_promote_restart_survives_attempt_that_never_dumped() -> None:
    """Attempt 2 dies before its first dump; attempt 3 must still resume
    from the restart file attempt 1 produced."""
    sol = petsc.wrapper_solution_path()
    sol.parent.mkdir(parents=True, exist_ok=True)
    sol.write_bytes(b"attempt-1")
    first = petsc.promote_restart()
    assert first is not None

    # No new solution dump happened — rotation falls back to the old restart.
    second = petsc.promote_restart()
    assert second == first
    assert second.read_bytes() == b"attempt-1"


def test_promote_restart_ignores_empty_solution_file() -> None:
    sol = petsc.wrapper_solution_path()
    sol.parent.mkdir(parents=True, exist_ok=True)
    sol.touch()
    assert petsc.promote_restart() is None


# ─── monitor-path checkpoint discovery ─────────────────────────────────────


def test_latest_petsc_checkpoint_picks_highest_step_skipping_empty() -> None:
    d = ck.checkpoint_dir()
    d.mkdir(parents=True, exist_ok=True)
    petsc.petsc_checkpoint_path(3).write_bytes(b"x")
    petsc.petsc_checkpoint_path(12).write_bytes(b"y")
    petsc.petsc_checkpoint_path(20).touch()  # empty → skipped
    (d / "checkpoint-7.pkl").write_bytes(b"pickle")  # pickle helper's file → ignored

    latest = petsc.latest_petsc_checkpoint()
    assert latest is not None and latest.name == "checkpoint-12.petscbin"


def test_latest_petsc_checkpoint_none_when_dir_missing() -> None:
    assert petsc.latest_petsc_checkpoint() is None


def test_petsc_checkpoints_invisible_to_pickle_helpers() -> None:
    """The .petscbin suffix keeps PETSc dumps out of read_latest_checkpoint —
    it must never try to unpickle a PETSc binary Vec."""
    d = ck.checkpoint_dir()
    d.mkdir(parents=True, exist_ok=True)
    petsc.petsc_checkpoint_path(5).write_bytes(b"not-a-pickle")
    assert ck.latest_checkpoint() is None


# ─── monitor factory ───────────────────────────────────────────────────────


class _FakeViewer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.destroyed = False

    def destroy(self) -> None:
        self.destroyed = True


class _FakeVec:
    def __init__(self, payload: bytes = b"solution") -> None:
        self.payload = payload

    def view(self, viewer: _FakeViewer) -> None:
        viewer.path.write_bytes(self.payload)


class _FakeSnes:
    def __init__(self, vec: _FakeVec) -> None:
        self._vec = vec

    def getSolution(self) -> _FakeVec:
        return self._vec


def _expired_walltime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the walltime_margin strategy due NOW (deadline already passed)."""
    monkeypatch.setenv("HPC_WALLTIME_END_EPOCH", "1")


def test_monitor_writes_ts_checkpoint_when_due(monkeypatch: pytest.MonkeyPatch) -> None:
    _expired_walltime(monkeypatch)
    viewers: list[_FakeViewer] = []

    def factory(path: Path) -> _FakeViewer:
        v = _FakeViewer(path)
        viewers.append(v)
        return v

    monitor = petsc.make_checkpoint_monitor(_viewer_factory=factory)
    # TS signature: (ts, step, time, u).
    monitor(object(), 4, 0.25, _FakeVec(b"ts-state"))

    target = petsc.petsc_checkpoint_path(4)
    assert target.read_bytes() == b"ts-state"
    # Atomic promote: no temp residue, viewer released.
    assert list(target.parent.glob("*.tmp")) == []
    assert viewers and viewers[0].destroyed


def test_monitor_fetches_solution_for_snes_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _expired_walltime(monkeypatch)
    monitor = petsc.make_checkpoint_monitor(_viewer_factory=_FakeViewer)
    # SNES signature: (snes, its, fnorm) — no Vec argument.
    monitor(_FakeSnes(_FakeVec(b"snes-state")), 2, 1e-9)
    assert petsc.petsc_checkpoint_path(2).read_bytes() == b"snes-state"


def test_monitor_is_noop_when_not_due(monkeypatch: pytest.MonkeyPatch) -> None:
    """walltime_margin with no deadline exported never guesses — no write."""

    def explode(path: Path) -> _FakeViewer:
        raise AssertionError("viewer must not be created when not due")

    monitor = petsc.make_checkpoint_monitor(_viewer_factory=explode)
    monitor(object(), 1, 0.1, _FakeVec())
    assert petsc.latest_petsc_checkpoint() is None


def test_monitor_cleans_temp_on_write_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _expired_walltime(monkeypatch)

    class _BrokenVec:
        def view(self, viewer: _FakeViewer) -> None:
            raise RuntimeError("disk full")

    monitor = petsc.make_checkpoint_monitor(_viewer_factory=_FakeViewer)
    with pytest.raises(RuntimeError, match="disk full"):
        monitor(object(), 9, 0.5, _BrokenVec())
    d = ck.checkpoint_dir()
    assert list(d.glob("*.tmp")) == []
    assert petsc.latest_petsc_checkpoint() is None


def test_monitor_honors_explicit_result_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _expired_walltime(monkeypatch)
    other = tmp_path / "elsewhere"
    monitor = petsc.make_checkpoint_monitor(result_dir=other, _viewer_factory=_FakeViewer)
    monitor(object(), 0, 0.0, _FakeVec(b"v"))
    assert (other / "_checkpoints" / "checkpoint-0.petscbin").read_bytes() == b"v"
    # An explicit result_dir wins over HPC_CHECKPOINT_DIR, like the helpers.
    assert petsc.latest_petsc_checkpoint() is None
