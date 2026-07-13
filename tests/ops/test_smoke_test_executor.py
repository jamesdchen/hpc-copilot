"""Tests for the ``smoke-test-executor`` composite primitive (WS5 #2).

Pins the import-and-``compute`` probe that replaces ``hpc-build-executor``'s
banned inline ``python -c`` smoke test: the verb runs the scaffolded
executor's ``compute(Namespace(output_file=...))`` in a child process and
reports ``{exit_code, stdout_tail, stderr_tail, timed_out}`` so the skill
branches deterministically (non-zero exit_code = fix-then-retry).

The argv-building (``build_probe_argv`` / ``_probe_source``) is pinned
directly. The subprocess plumbing is exercised two ways: a handful of
real-subprocess end-to-end cases against temp modules (cheap, ~25ms,
and they prove the recipe actually imports + calls ``compute``), plus
``subprocess.run`` mocked at the module level for the
timeout / non-zero-exit branches that are awkward to trigger for real.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from hpc_agent.ops import smoke_test_executor as ste


def _write_module(tmp_path: Path, body: str) -> Path:
    """Write *body* as an executor module under *tmp_path* and return its path."""
    path = tmp_path / "exec_under_test.py"
    path.write_text(body, encoding="utf-8")
    return path


class TestProbeSource:
    """The child-process recipe mirrors the SKILL.md ``python -c`` it replaces."""

    def test_source_has_the_canonical_load_and_call_steps(self) -> None:
        src = ste._probe_source("/abs/exec.py", "/tmp/smoke.csv")
        # The four load steps + the compute call, in order.
        assert "importlib.util.spec_from_file_location('m', '/abs/exec.py')" in src
        assert "module_from_spec(spec)" in src
        assert "sys.modules['m'] = m" in src
        assert "spec.loader.exec_module(m)" in src
        assert "m.compute(argparse.Namespace(output_file='/tmp/smoke.csv'))" in src

    def test_paths_are_repr_quoted_so_quotes_cannot_break_out(self) -> None:
        # A path containing a quote must be repr-escaped, not naively spliced.
        src = ste._probe_source("/a'/exec.py", "/tmp/o.csv")
        assert "'/a\\'/exec.py'" in src or '"/a\'/exec.py"' in src

    def test_build_probe_argv_uses_current_interpreter(self) -> None:
        argv = ste.build_probe_argv(module_path="/abs/exec.py", output_file="/tmp/o.csv")
        assert argv[0] == sys.executable
        assert argv[1] == "-c"
        assert "m.compute(" in argv[2]


class TestEndToEnd:
    """Real-subprocess cases — prove the recipe imports + calls compute()."""

    def test_clean_compute_returns_exit_zero(self, tmp_path: Path) -> None:
        mod = _write_module(
            tmp_path,
            "def compute(args):\n    print('ok', args.output_file)\n",
        )
        result = ste.smoke_test_executor(module_path=mod)
        assert result["exit_code"] == 0
        assert result["timed_out"] is False
        assert "ok" in result["stdout_tail"]
        assert result["stderr_tail"] == ""

    def test_output_file_is_passed_through_to_namespace(self, tmp_path: Path) -> None:
        mod = _write_module(
            tmp_path,
            "def compute(args):\n    print('OF=' + args.output_file)\n",
        )
        result = ste.smoke_test_executor(module_path=mod, output_file="/tmp/custom.parquet")
        assert "OF=/tmp/custom.parquet" in result["stdout_tail"]

    def test_default_output_is_private_tempdir_not_a_fixed_shared_path(
        self, tmp_path: Path
    ) -> None:
        """Omitting output_file mints a per-invocation private temp path (0700,
        unique, removed on return) — never a fixed ``/tmp/smoke.csv`` a co-tenant
        could pre-plant a symlink at. The dir must not leak after the call."""
        import os

        mod = _write_module(
            tmp_path,
            "def compute(args):\n    print('OF=' + args.output_file)\n",
        )
        result = ste.smoke_test_executor(module_path=mod)
        assert result["exit_code"] == 0
        of_line = next(ln for ln in result["stdout_tail"].splitlines() if ln.startswith("OF="))
        used = of_line[len("OF=") :]
        assert used != "/tmp/smoke.csv"
        assert "/hpc-smoke-" in used  # a private per-invocation dir
        # This invocation's own scratch dir must be gone (race-free vs. sibling
        # workers — we check the exact dir we minted, not a shared glob).
        assert not os.path.exists(os.path.dirname(used))

    def test_raising_compute_returns_nonzero_with_traceback(self, tmp_path: Path) -> None:
        mod = _write_module(
            tmp_path,
            "def compute(args):\n    raise RuntimeError('boom')\n",
        )
        result = ste.smoke_test_executor(module_path=mod)
        assert result["exit_code"] != 0
        assert result["timed_out"] is False
        assert "RuntimeError" in result["stderr_tail"]
        assert "boom" in result["stderr_tail"]

    def test_sys_exit_nonzero_is_captured_not_propagated(self, tmp_path: Path) -> None:
        # A module that sys.exits must NOT take down the CLI process.
        mod = _write_module(
            tmp_path,
            "import sys\ndef compute(args):\n    sys.exit(7)\n",
        )
        result = ste.smoke_test_executor(module_path=mod)
        assert result["exit_code"] == 7

    def test_import_time_error_surfaces_as_nonzero(self, tmp_path: Path) -> None:
        # An executor that raises at module load (before compute is even
        # defined) is still a smoke-test failure, not a CLI crash.
        mod = _write_module(tmp_path, "raise ImportError('missing dep')\n")
        result = ste.smoke_test_executor(module_path=mod)
        assert result["exit_code"] != 0
        assert "ImportError" in result["stderr_tail"]

    def test_path_accepts_str_and_path(self, tmp_path: Path) -> None:
        mod = _write_module(tmp_path, "def compute(args):\n    pass\n")
        assert ste.smoke_test_executor(module_path=str(mod))["exit_code"] == 0
        assert ste.smoke_test_executor(module_path=mod)["exit_code"] == 0


class TestTimeout:
    """The timeout branch is mocked — no real spinning child needed."""

    def test_timeout_yields_null_exit_and_timed_out_true(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        def fake_run(*_args: object, **_kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd=["python"], timeout=60.0, output="partial-out")

        monkeypatch.setattr(ste.subprocess, "run", fake_run)
        result = ste.smoke_test_executor(module_path=tmp_path / "x.py", timeout_sec=60.0)
        assert result["exit_code"] is None
        assert result["timed_out"] is True
        assert "partial-out" in result["stdout_tail"]
        assert "timed out after 60.0s" in result["stderr_tail"]

    def test_timeout_decodes_bytes_streams(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # TimeoutExpired.stdout/stderr may be bytes when text= isn't honored
        # on the partial capture; the result must still be str.
        def fake_run(*_args: object, **_kwargs: object) -> object:
            raise subprocess.TimeoutExpired(
                cmd=["python"], timeout=5.0, output=b"bytes-out", stderr=b"bytes-err"
            )

        monkeypatch.setattr(ste.subprocess, "run", fake_run)
        result = ste.smoke_test_executor(module_path=tmp_path / "x.py", timeout_sec=5.0)
        assert isinstance(result["stdout_tail"], str)
        assert "bytes-out" in result["stdout_tail"]
        assert "bytes-err" in result["stderr_tail"]


class TestTail:
    """Stream tailing keeps the envelope small but actionable."""

    def test_tail_truncates_to_window(self) -> None:
        big = "x" * (ste._TAIL_CHARS + 500)
        assert len(ste._tail(big)) == ste._TAIL_CHARS

    def test_long_stdout_is_tailed_in_result(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        class _Proc:
            returncode = 0
            stdout = "A" * (ste._TAIL_CHARS + 1000)
            stderr = ""

        monkeypatch.setattr(ste.subprocess, "run", lambda *a, **k: _Proc())
        result = ste.smoke_test_executor(module_path=tmp_path / "x.py")
        assert len(result["stdout_tail"]) == ste._TAIL_CHARS
