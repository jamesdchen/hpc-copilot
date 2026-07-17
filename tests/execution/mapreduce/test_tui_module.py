"""Smoke tests for hpc_agent.execution.mapreduce.reduce.tui.

We don't exercise the interactive Live loop here — that requires rich and
an interactive terminal.  Instead assert:

1. The module imports without rich installed (lazy import contract).
2. The pure-Python helpers (elapsed formatting, failure classification
   rollup, failing-tail builder) behave sensibly.
3. ``run_tui`` surfaces a clear error when rich is missing instead of
   crashing with an unhandled ``ImportError``.
"""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest


def test_module_imports_without_rich(monkeypatch):
    # Ensure rich is not cached from a previous test, and hide it.
    monkeypatch.setitem(sys.modules, "rich", None)
    monkeypatch.setitem(sys.modules, "rich.console", None)
    monkeypatch.setitem(sys.modules, "rich.live", None)

    # Force a fresh import of the tui module under the hidden-rich condition.
    sys.modules.pop("hpc_agent.execution.mapreduce.reduce.tui", None)
    mod = importlib.import_module("hpc_agent.execution.mapreduce.reduce.tui")
    assert hasattr(mod, "run_tui")


def test_format_elapsed():
    from hpc_agent.execution.mapreduce.reduce.tui import _fmt_elapsed

    assert _fmt_elapsed(0) == "0s"
    assert _fmt_elapsed(45) == "45s"
    assert _fmt_elapsed(90) == "1m30s"
    assert _fmt_elapsed(3700) == "1h01m40s"


def test_failing_tail_builds_from_err_logs(tmp_path):
    from hpc_agent.execution.mapreduce.reduce.tui import _failing_tail

    log = tmp_path / "err.log"
    log.write_text("lots of output\nERROR: boom the final line\n")

    report = {
        "tasks": {
            "1": {"status": "complete"},
            "2": {"status": "failed"},
            "3": {"status": "unknown"},
        },
        "err_log_paths": {"2": str(log)},
    }
    tail = _failing_tail(report, limit=10)
    tids = [tid for tid, _ in tail]
    assert tids == ["2", "3"]
    # Task 2 has a log -> diagnostic is the last non-empty line.
    assert dict(tail)["2"].startswith("ERROR: boom")
    # Task 3 has no log -> empty diagnostic.
    assert dict(tail)["3"] == ""


def test_classify_failures_buckets_logs(tmp_path):
    from hpc_agent.execution.mapreduce.reduce.tui import _classify_failures

    oom = tmp_path / "oom.err"
    oom.write_text("torch.cuda.OutOfMemoryError: CUDA out of memory\n")
    walltime = tmp_path / "walltime.err"
    walltime.write_text("CANCELLED DUE TO TIME LIMIT\n")
    report = {
        "tasks": {
            "1": {"status": "failed"},
            "2": {"status": "failed"},
            "3": {"status": "failed"},  # no err log -> unknown
        },
        "err_log_paths": {"1": str(oom), "2": str(walltime)},
    }
    buckets = _classify_failures(report, per_task_dict={})
    assert buckets.get("gpu_oom") == 1
    assert buckets.get("walltime") == 1
    assert buckets.get("unknown") == 1


def test_run_tui_errors_cleanly_when_rich_missing(monkeypatch, tmp_path, capsys):
    # Simulate rich being unavailable: make `import rich...` raise.
    real_import = builtins.__import__

    def fake_import(name, *a, **kw):
        if name == "rich.console" or name == "rich.live" or name == "rich":
            raise ImportError(f"no module named {name}")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    # Minimal valid per-task dict so we don't short-circuit on load errors.
    per_task_dict_path = tmp_path / "m.json"
    per_task_dict_path.write_text('{"total_tasks": 0, "tasks": {}}')

    from hpc_agent.execution.mapreduce.reduce import tui

    rc = tui.run_tui(per_task_dict_path)
    assert rc == 2
    err = capsys.readouterr().err
    assert "rich" in err.lower()


def _write_run(tmp_path, run_id: str, sidecar: dict, tasks_py: str | None = None):
    """Materialize .hpc/runs/<run_id>.json (+ optional .hpc/tasks.py) under tmp."""
    import json

    runs = tmp_path / ".hpc" / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(json.dumps(sidecar))
    if tasks_py is not None:
        (tmp_path / ".hpc" / "tasks.py").write_text(tasks_py)


def test_main_uses_frozen_manifest_without_importing_tasks_py(tmp_path, monkeypatch):
    """#29: the TUI resolves per-task kwargs from the sidecar's frozen
    ``trial_params`` and NEVER imports/replays ``tasks.py`` — a state-dependent
    ``resolve()`` would mint a phantom trial and point at next-iteration dirs."""
    import json

    from hpc_agent.execution.mapreduce.reduce import tui

    run_id = "trial-run"
    _write_run(
        tmp_path,
        run_id,
        {
            "run_id": run_id,
            "result_dir_template": "results/{run_id}/lr_{lr}/task_{task_id}",
            "trial_params": [{"lr": 0.1}, {"lr": 0.2}],
            "task_count": 2,
        },
        # Importing this would raise — proving the frozen path skips the import.
        tasks_py="raise RuntimeError('tasks.py must not be imported by the TUI')\n",
    )

    captured: dict[str, object] = {}

    def _fake_run_tui(per_task_dict_path, **kwargs):
        captured["path"] = per_task_dict_path
        return 0

    monkeypatch.setattr(tui, "run_tui", _fake_run_tui)
    monkeypatch.chdir(tmp_path)

    rc = tui._main(["--run-id", run_id])
    assert rc == 0  # did NOT hard-fail on the raising tasks.py

    per_task = json.loads((tmp_path / ".hpc" / "runs" / f"{run_id}.per-task-dict.json").read_text())
    # result_dir was formatted from the FROZEN manifest, not resolve().
    assert per_task["tasks"]["0"]["result_dir"] == f"results/{run_id}/lr_0.1/task_0"
    assert per_task["tasks"]["1"]["result_dir"] == f"results/{run_id}/lr_0.2/task_1"


def test_main_degrades_on_foreign_tasks_py(tmp_path, monkeypatch):
    """#29: a legacy sidecar (no manifest) whose ``tasks.py`` won't import must
    DEGRADE to task_id-only dirs like the status CLI — not wedge with ``return 2``."""
    from hpc_agent.execution.mapreduce.reduce import tui

    run_id = "legacy-run"
    _write_run(
        tmp_path,
        run_id,
        {
            "run_id": run_id,
            "result_dir_template": "results/{run_id}/lr_{lr}/task_{task_id}",
            "task_count": 1,
        },
        tasks_py="import definitely_not_installed_xyz  # ImportError at load\n",
    )

    monkeypatch.setattr(tui, "run_tui", lambda *a, **k: 0)
    monkeypatch.chdir(tmp_path)

    rc = tui._main(["--run-id", run_id])
    assert rc == 0  # degraded, not the old hard `return 2`


class _FakeLive:
    """Minimal stand-in for a Rich ``Live`` — records stop/start handovers."""

    def __init__(self) -> None:
        self.stopped = 0
        self.started = 0

    def stop(self) -> None:
        self.stopped += 1

    def start(self, refresh: bool = False) -> None:
        self.started += 1


class _FakeProc:
    """Minimal ``subprocess.Popen`` stand-in for the interactive pager."""

    def __init__(self, rc: int = 0, wait_exc: BaseException | None = None) -> None:
        self._rc = rc
        self._wait_exc = wait_exc
        self._alive = True
        self.killed = 0

    def wait(self, timeout: float | None = None) -> int:
        if self._wait_exc is not None:
            raise self._wait_exc
        self._alive = False
        return self._rc

    def poll(self) -> int | None:
        return None if self._alive else self._rc

    def kill(self) -> None:
        self.killed += 1
        self._alive = False
        # A killed child is reaped by the follow-up wait(); drop any pending
        # injected exception so the bounded post-kill wait returns cleanly.
        self._wait_exc = None


class TestOpenLog:
    """The ``l`` keybind's log-viewer (transport-robustness AUDIT rank 7)."""

    def test_argv_routes_through_ssh_argv_not_bare_ssh(self, monkeypatch):
        """The pager ssh call is built via the ONE ``ssh_argv`` seam — the
        resolved binary + BatchMode/ConnectTimeout/multiplex options — never a
        bare ``["ssh", target, ...]`` a Git-Bash-shadowed ssh could hijack
        (#145/#154/#156)."""
        import shlex

        from hpc_agent.execution.mapreduce.reduce import tui
        from hpc_agent.infra.ssh_options import ssh_argv

        captured: dict[str, list[str]] = {}

        def _fake_popen(argv):
            captured["argv"] = argv
            return _FakeProc(rc=0)

        monkeypatch.setattr(tui.subprocess, "Popen", _fake_popen)

        live = _FakeLive()
        target = "user@login.hpc.example"
        log_path = "/scratch/runs/task 7/err.log"
        tui._open_log(target, log_path, live)

        expected = [*ssh_argv("ssh"), target, f"less {shlex.quote(log_path)}"]
        assert captured["argv"] == expected
        # The seam markers prove it is NOT a naive bare-"ssh" argv.
        assert "BatchMode=yes" in captured["argv"]
        assert captured["argv"][0] == ssh_argv("ssh")[0]
        # The path with whitespace is shlex-quoted in the remote command word.
        assert "'/scratch/runs/task 7/err.log'" in captured["argv"][-1]
        # Live view was paused for the TTY handover and resumed after.
        assert (live.stopped, live.started) == (1, 1)

    def test_local_less_when_no_ssh_target(self, monkeypatch):
        """No ssh_target -> plain local ``less <path>`` (no ssh)."""
        from hpc_agent.execution.mapreduce.reduce import tui

        captured: dict[str, list[str]] = {}

        def _fake_popen(argv):
            captured["argv"] = argv
            return _FakeProc(rc=0)

        monkeypatch.setattr(tui.subprocess, "Popen", _fake_popen)

        live = _FakeLive()
        tui._open_log(None, "/tmp/err.log", live)
        assert captured["argv"] == ["less", "/tmp/err.log"]
        assert (live.stopped, live.started) == (1, 1)

    def test_nonzero_exit_is_disclosed_not_swallowed(self, monkeypatch, capsys):
        """A non-zero pager/ssh exit is surfaced via ``disclose_child_failure``
        instead of being silently ignored (run-#13 disclosure discipline)."""
        from hpc_agent.execution.mapreduce.reduce import tui

        monkeypatch.setattr(tui.subprocess, "Popen", lambda argv: _FakeProc(rc=3))

        live = _FakeLive()
        tui._open_log("user@host", "/tmp/err.log", live)

        err = capsys.readouterr().err
        assert "exited 3" in err
        assert live.started == 1  # Live view always resumes.

    def test_missing_binary_is_disclosed_not_silent(self, monkeypatch, capsys):
        """A missing ssh/less binary (``FileNotFoundError``) is disclosed, not
        the old silent ``pass`` that made the keybind look inert."""
        from hpc_agent.execution.mapreduce.reduce import tui

        def _fake_popen(argv):
            raise FileNotFoundError(argv[0])

        monkeypatch.setattr(tui.subprocess, "Popen", _fake_popen)

        live = _FakeLive()
        # Does NOT raise — a benign missing binary degrades to a disclosure.
        tui._open_log(None, "/tmp/err.log", live)

        err = capsys.readouterr().err
        assert "127" in err
        assert "command not found" in err
        assert live.started == 1

    def test_child_killed_on_interrupt_no_orphan(self, monkeypatch):
        """Ctrl-C (or any abnormal unwind) while the pager runs kills the child
        so no ssh survives the TUI (audit §7 no-orphan drill); the interrupt
        still propagates and the Live view resumes."""
        from hpc_agent.execution.mapreduce.reduce import tui

        proc = _FakeProc(wait_exc=KeyboardInterrupt())
        monkeypatch.setattr(tui.subprocess, "Popen", lambda argv: proc)

        live = _FakeLive()
        with pytest.raises(KeyboardInterrupt):
            tui._open_log("user@host", "/tmp/err.log", live)

        assert proc.killed == 1  # the orphan-preventing reap fired
        assert live.started == 1  # finally-arm still resumed the view


def test_render_returns_rich_group_when_rich_present(tmp_path):
    # If rich isn't available, skip rather than fail the whole suite.
    pytest.importorskip("rich")
    from hpc_agent.execution.mapreduce.reduce.tui import _render, _UiState

    per_task_dict = {"run_id": "r1", "cluster": "c1", "wave_map": {"0": ["0", "1"]}, "tasks": {}}
    report = {
        "summary": {"complete": 1, "running": 0, "pending": 1, "failed": 0, "unknown": 0},
        "tasks": {"1": {"status": "complete"}, "2": {"status": "pending"}},
        "resource_usage": {
            "cpu_hours": 1.2,
            "gpu_hours": 0.0,
            "elapsed_hours": 1.2,
            "tasks_counted": 1,
        },
        "scheduler": "slurm",
    }
    out = _render(_UiState(), report, per_task_dict, 30)
    # Rich Group is truthy and has a `renderables` attribute.
    assert hasattr(out, "renderables")
    assert len(out.renderables) >= 5
