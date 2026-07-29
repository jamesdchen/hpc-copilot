"""``ops/monitor/sentinel`` — the W1 sentinel script + opportunistic submit.

Pins: the rendered body writes the SHIPPED ``.run_terminal`` vocabulary
(atomic tmp+mv, always exit 0, no core imports), the staging is one bounded
exec whose failure raises typed, and the flag-gated entry point is
opportunistic — flag off is a zero-traffic no-op, every failure path discloses
and never raises, and a submitted sentinel lands on the sidecar's SEPARATE
``sentinel_job_id`` field (never ``job_ids``).
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from hpc_agent import errors
from hpc_agent.ops.monitor import sentinel as sen
from hpc_agent.ops.monitor.announce import ANNOUNCE_RUN_TERMINAL, ANNOUNCE_SUBPATH


class TestRenderSentinelScript:
    def test_body_writes_the_shipped_run_terminal_vocabulary(self) -> None:
        body = sen.render_sentinel_script(remote_path="/u/proj/exp", run_id="r-42")
        # The announce dir + the SHIPPED wake-marker name (not the design doc's
        # retired .hpc_TERMINAL sketch) — sourced from the ONE announce
        # definition, so a vocabulary move breaks this test loudly.
        assert f"/u/proj/exp/{ANNOUNCE_SUBPATH}/r-42" in body
        assert ANNOUNCE_RUN_TERMINAL in body
        assert ".hpc_TERMINAL" not in body

    def test_body_is_atomic_tmp_plus_mv_and_always_exit_zero(self) -> None:
        body = sen.render_sentinel_script(remote_path="/r", run_id="x")
        assert body.startswith("#!/bin/sh\n")
        # tmp written first, then a same-dir mv (atomic rename).
        assert ".tmp" in body
        mv_line = f'mv "$__hpc_tmp" "$__hpc_dir/{ANNOUNCE_RUN_TERMINAL}"'
        assert body.index("printf") < body.index(mv_line)
        # Best-effort: the last line exits 0 and every fallible step degrades.
        assert body.rstrip().endswith("exit 0")
        assert "|| exit 0" in body

    def test_body_never_imports_core(self) -> None:
        # The standalone-files-don't-import-core rule: the job body is plain
        # POSIX sh with the vocabulary baked in — it can never import the
        # package on a compute node.
        body = sen.render_sentinel_script(remote_path="/r", run_id="x")
        assert "hpc_agent" not in body
        assert "python" not in body

    def test_trailing_slash_remote_path_normalised(self) -> None:
        a = sen.render_sentinel_script(remote_path="/r/", run_id="x")
        b = sen.render_sentinel_script(remote_path="/r", run_id="x")
        assert a == b


class TestStageSentinelScript:
    def test_one_exec_tmp_then_mv_then_relative_path_back(self) -> None:
        calls: list[tuple[str, str]] = []

        def _ssh(cmd, *, ssh_target):
            calls.append((cmd, ssh_target))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        rel = sen.stage_sentinel_script(
            ssh_target="u@h", remote_path="/r", run_id="run7", _ssh_run=_ssh
        )
        assert rel == f"{ANNOUNCE_SUBPATH}/run7.sentinel.sh"
        ((cmd, target),) = calls
        assert target == "u@h"
        # tmp write precedes the mv into place (atomic same-dir rename).
        assert cmd.index(".sentinel.sh.tmp") < cmd.index("&& mv ")
        assert f"/r/{ANNOUNCE_SUBPATH}/run7.sentinel.sh" in cmd

    def test_nonzero_rc_raises_typed(self) -> None:
        def _ssh(cmd, *, ssh_target):
            return SimpleNamespace(returncode=1, stdout="", stderr="disk full")

        with pytest.raises(errors.RemoteCommandFailed, match="disk full"):
            sen.stage_sentinel_script(ssh_target="u@h", remote_path="/r", run_id="x", _ssh_run=_ssh)


class _StubBackend:
    """Duck-typed backend for the entry point (requires_ssh defaults True)."""

    requires_ssh = True

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def submit_sentinel(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("scheduler said no")
        return "31337"


def _ok_ssh(cmd, *, ssh_target):
    return SimpleNamespace(returncode=0, stdout="", stderr="")


class TestMaybeSubmitRunTerminalSentinel:
    def test_flag_off_is_a_zero_traffic_noop(self, tmp_path: Path) -> None:
        backend = _StubBackend()

        def _boom(cmd, *, ssh_target):  # any dial with the flag off is a bug
            raise AssertionError("flag off must issue zero cluster traffic")

        out = sen.maybe_submit_run_terminal_sentinel(
            backend,  # type: ignore[arg-type]
            experiment_dir=tmp_path,
            ssh_target="u@h",
            remote_path="/r",
            run_id="rid",
            job_name="j",
            depend_job_ids=["1"],
            _ssh_run=_boom,
        )
        assert out == {"enabled": False, "submitted": False, "reason": "flag_off"}
        assert backend.calls == []

    def test_flag_on_submits_and_stamps_the_separate_sidecar_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hpc_agent
        from hpc_agent.state.runs import read_run_sidecar, write_run_sidecar

        monkeypatch.setenv(sen.SENTINEL_JOB_ENV, "1")
        write_run_sidecar(
            tmp_path,
            run_id="rid",
            cmd_sha="sha",
            hpc_agent_version=hpc_agent.__version__,
            submitted_at="2026-07-29T00:00:00Z",
            executor="python x.py",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=2,
            tasks_py_sha="tsha",
            job_ids=["11", "22"],
        )
        backend = _StubBackend()
        out = sen.maybe_submit_run_terminal_sentinel(
            backend,  # type: ignore[arg-type]
            experiment_dir=tmp_path,
            ssh_target="u@h",
            remote_path="/r",
            run_id="rid",
            job_name="jname",
            depend_job_ids=["11", "22"],
            _ssh_run=_ok_ssh,
        )
        assert out["submitted"] is True
        assert out["sentinel_job_id"] == "31337"
        (call,) = backend.calls
        assert call["depend_job_ids"] == ["11", "22"]
        assert call["job_name"] == "jname_sentinel"
        assert call["script_path"] == f"{ANNOUNCE_SUBPATH}/rid.sentinel.sh"
        sidecar = read_run_sidecar(tmp_path, "rid")
        # Separate field; the compute-job accounting is untouched.
        assert sidecar["sentinel_job_id"] == "31337"
        assert sidecar["job_ids"] == ["11", "22"]

    def test_submit_failure_is_disclosed_never_raised(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv(sen.SENTINEL_JOB_ENV, "1")
        backend = _StubBackend(fail=True)
        with caplog.at_level(logging.WARNING, logger="hpc_agent.ops.monitor.sentinel"):
            out = sen.maybe_submit_run_terminal_sentinel(
                backend,  # type: ignore[arg-type]
                experiment_dir=tmp_path,
                ssh_target="u@h",
                remote_path="/r",
                run_id="rid",
                job_name="j",
                depend_job_ids=["11"],
                _ssh_run=_ok_ssh,
            )
        assert out["submitted"] is False
        assert "scheduler said no" in out["reason"]
        assert any("not submitted" in r.message for r in caplog.records)

    def test_staging_failure_is_disclosed_never_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(sen.SENTINEL_JOB_ENV, "1")

        def _bad_ssh(cmd, *, ssh_target):
            return SimpleNamespace(returncode=255, stdout="", stderr="severed")

        backend = _StubBackend()
        out = sen.maybe_submit_run_terminal_sentinel(
            backend,  # type: ignore[arg-type]
            experiment_dir=tmp_path,
            ssh_target="u@h",
            remote_path="/r",
            run_id="rid",
            job_name="j",
            depend_job_ids=["11"],
            _ssh_run=_bad_ssh,
        )
        assert out["submitted"] is False
        assert backend.calls == []  # never reached the qsub edge

    def test_pure_api_backend_skips_with_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(sen.SENTINEL_JOB_ENV, "1")

        class _PureApi(_StubBackend):
            requires_ssh = False

        backend = _PureApi()
        out = sen.maybe_submit_run_terminal_sentinel(
            backend,  # type: ignore[arg-type]
            experiment_dir=tmp_path,
            ssh_target="u@h",
            remote_path="/r",
            run_id="rid",
            job_name="j",
            depend_job_ids=["11"],
            _ssh_run=_ok_ssh,
        )
        assert out == {"enabled": True, "submitted": False, "reason": "backend_not_ssh"}
        assert backend.calls == []

    def test_no_depend_ids_skips_with_reason(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(sen.SENTINEL_JOB_ENV, "1")
        backend = _StubBackend()
        out = sen.maybe_submit_run_terminal_sentinel(
            backend,  # type: ignore[arg-type]
            experiment_dir=tmp_path,
            ssh_target="u@h",
            remote_path="/r",
            run_id="rid",
            job_name="j",
            depend_job_ids=[],
            _ssh_run=_ok_ssh,
        )
        assert out == {"enabled": True, "submitted": False, "reason": "no_depend_job_ids"}
        assert backend.calls == []

    def test_missing_sidecar_does_not_fail_the_submit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # stamp_sentinel_job is best-effort: no sidecar (a hand-driven submit
        # outside the pipeline) still reports the submitted sentinel.
        monkeypatch.setenv(sen.SENTINEL_JOB_ENV, "1")
        backend = _StubBackend()
        out = sen.maybe_submit_run_terminal_sentinel(
            backend,  # type: ignore[arg-type]
            experiment_dir=tmp_path,
            ssh_target="u@h",
            remote_path="/r",
            run_id="no-sidecar",
            job_name="j",
            depend_job_ids=["1"],
            _ssh_run=_ok_ssh,
        )
        assert out["submitted"] is True
