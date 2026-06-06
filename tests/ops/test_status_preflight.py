"""Tests for the ``status-preflight`` composite primitive (WS5 #3 scaffold).

Pins the install-commands ∥ load-context fan-out (concurrent — #291,
write-disjoint AND read-disjoint), argv composition, skip behavior,
overall-derivation precedence, and the synthesised-ErrorEnvelope shape
on spawn / timeout / parse failures.

The ``subprocess.run`` plumbing is mocked at :func:`_run_subprocess` so
these tests don't depend on a real ``hpc-agent`` binary being on PATH
inside the venv.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hpc_agent.ops import status_preflight as sp


def _ok_subresult(envelope_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Canned SubResult with ``ok: true``."""
    env: dict[str, Any] = {"ok": True, "idempotent": True, "data": envelope_data or {}}
    return {"envelope": env, "elapsed_sec": 0.05, "ok": True}


def _err_subresult(error_code: str = "spec_invalid") -> dict[str, Any]:
    """Canned SubResult with ``ok: false`` carrying *error_code*."""
    env = {
        "ok": False,
        "error_code": error_code,
        "message": "synthetic test failure",
        "category": "user",
        "retry_safe": False,
    }
    return {"envelope": env, "elapsed_sec": 0.05, "ok": False}


def _patch_run_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    by_name: dict[str, dict[str, Any]],
    *,
    record_order: list[str] | None = None,
) -> None:
    """Patch :func:`_run_subprocess` to return canned SubResults by name."""

    def fake(call: sp.SubCall, *, timeout_sec: float) -> dict[str, Any]:
        if record_order is not None:
            record_order.append(call.name)
        return by_name.get(call.name, _ok_subresult())

    monkeypatch.setattr(sp, "_run_subprocess", fake)


class TestBuildSubcalls:
    """argv composition + per-skip wiring."""

    def test_both_built_with_required_fields_only(self) -> None:
        calls = sp._build_subcalls(experiment_dir=Path("/exp"), skip=[])
        # Both members of _PARALLEL_SUBCALLS (#291): listing order is purely
        # conventional — the runner fans them on a thread pool.
        assert {c.name for c in calls} == {"install-commands", "load-context"}
        exp = str(Path("/exp"))
        ic = next(c for c in calls if c.name == "install-commands")
        assert ic.argv == ["hpc-agent", "install-commands"]
        lc = next(c for c in calls if c.name == "load-context")
        assert lc.argv == ["hpc-agent", "load-context", "--experiment-dir", exp]

    def test_skip_install_commands_drops_only_that_subcall(self) -> None:
        calls = sp._build_subcalls(experiment_dir=Path("/exp"), skip=["install-commands"])
        assert [c.name for c in calls] == ["load-context"]

    def test_skip_both_yields_empty_list(self) -> None:
        calls = sp._build_subcalls(
            experiment_dir=Path("/exp"), skip=["install-commands", "load-context"]
        )
        assert calls == []


class TestOverallDerivation:
    """``overall`` is ``pass`` iff every non-skipped sub-call returned ``ok``."""

    def test_both_succeed_overall_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _ok_subresult(),
                "load-context": _ok_subresult({"in_flight": []}),
            },
        )
        result = sp.status_preflight(experiment_dir=tmp_path)
        assert result["overall"] == "pass"
        assert result["install_commands"]["ok"] is True
        assert result["load_context"]["ok"] is True

    def test_install_fails_overall_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _err_subresult("config_invalid"),
                "load-context": _ok_subresult(),
            },
        )
        result = sp.status_preflight(experiment_dir=tmp_path)
        assert result["overall"] == "fail"
        # The failed install_commands envelope is preserved verbatim so a
        # consumer can read its remediation without re-running.
        assert result["install_commands"]["envelope"]["error_code"] == "config_invalid"

    def test_load_context_fails_overall_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _ok_subresult(),
                "load-context": _err_subresult("journal_corrupt"),
            },
        )
        result = sp.status_preflight(experiment_dir=tmp_path)
        assert result["overall"] == "fail"
        assert result["load_context"]["envelope"]["error_code"] == "journal_corrupt"


class TestConcurrentFanOut:
    """install-commands ∥ load-context overlap (#291): they fan concurrently.

    Write-disjoint AND read-disjoint — install writes only
    ``~/.claude/{commands,skills,agents}/`` plus ``~/.claude/settings.json``;
    load-context reads only the experiment's ``.hpc/{runs,journal,campaigns}``
    tree. The earlier "install must succeed first" claim was inert; the
    #289 audit and source-walk confirmed no ``~/.claude`` reads anywhere in
    load-context's transitive call tree.
    """

    def test_install_and_load_context_dispatch_concurrently(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # threading.Barrier(2) releases only when BOTH arms have arrived;
        # if they ran sequentially the first wait() would block until the
        # 5s timeout and raise BrokenBarrierError, failing the test.
        barrier = threading.Barrier(2, timeout=5)

        def fake(call: sp.SubCall, *, timeout_sec: float) -> dict[str, Any]:
            barrier.wait()  # releases only if the OTHER arm is ALSO running
            return _ok_subresult()

        monkeypatch.setattr(sp, "_run_subprocess", fake)

        result = sp.status_preflight(experiment_dir=tmp_path)
        assert result["overall"] == "pass"
        assert result["install_commands"]["ok"] is True
        assert result["load_context"]["ok"] is True

    def test_parallel_pair_overlaps_not_serial(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Each arm sleeps ~0.4s. Serial: ~0.8s; fanned: bounded by the
        # slower arm (~0.4s). Assert well under the serial sum.
        def slow(call: sp.SubCall, *, timeout_sec: float) -> dict[str, Any]:
            time.sleep(0.4)
            return _ok_subresult()

        monkeypatch.setattr(sp, "_run_subprocess", slow)

        started = time.monotonic()
        result = sp.status_preflight(experiment_dir=tmp_path)
        elapsed = time.monotonic() - started

        assert elapsed < 0.7, f"parallel pair did not overlap (elapsed={elapsed:.3f}s)"
        assert result["overall"] == "pass"


class TestSkipBehavior:
    """``skip=[...]`` excludes the named sub-call from dispatch AND output."""

    def test_skip_install_commands_yields_null_slot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ordered: list[str] = []
        _patch_run_subprocess(
            monkeypatch,
            {"load-context": _ok_subresult()},
            record_order=ordered,
        )
        result = sp.status_preflight(experiment_dir=tmp_path, skip=["install-commands"])
        assert ordered == ["load-context"]  # the skipped one never dispatched
        # The skipped slot is null (not a SubResult with ok: false) so a
        # re-run can target only the missing piece.
        assert result["install_commands"] is None
        assert result["load_context"] is not None
        assert result["overall"] == "pass"


class TestSynthErrorSubresult:
    """:func:`_synth_error_subresult` shape for spawn / timeout / parse failures."""

    def test_shape_matches_subresult_contract(self) -> None:
        result = sp._synth_error_subresult(
            error_code="cluster_timeout",
            message="probe timed out",
            category="cluster",
            elapsed_sec=42.0,
        )
        assert result["ok"] is False
        assert result["elapsed_sec"] == 42.0
        env = result["envelope"]
        # Conforms to ErrorEnvelope in envelope.json.
        assert env["ok"] is False
        assert env["error_code"] == "cluster_timeout"
        assert env["category"] == "cluster"
        assert env["retry_safe"] is False
