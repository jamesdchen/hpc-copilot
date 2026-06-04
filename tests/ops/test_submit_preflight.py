"""Tests for the repurposed ``submit-preflight`` composite primitive (WS5 #1).

Pins the sequential install-commands → load-context → check-preflight
orchestration: argv composition (including the optional ``--cluster``
that drives check-preflight's cluster_ssh_echo branch), skip behavior,
overall-derivation precedence, and the synthesised-ErrorEnvelope shape.

The ``subprocess.run`` plumbing is mocked at :func:`_run_subprocess` so
these tests don't depend on a real ``hpc-agent`` binary being on PATH
inside the venv.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hpc_agent.ops import submit_preflight as sp


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
    """argv composition + per-skip wiring + the optional --cluster branch."""

    def test_all_three_built_when_cluster_supplied(self) -> None:
        calls = sp._build_subcalls(experiment_dir=Path("/exp"), cluster="hoffman2", skip=[])
        # Order pin: install-commands → load-context → check-preflight.
        assert [c.name for c in calls] == [
            "install-commands",
            "load-context",
            "check-preflight",
        ]
        exp = str(Path("/exp"))
        ic = next(c for c in calls if c.name == "install-commands")
        assert ic.argv == ["hpc-agent", "install-commands"]
        lc = next(c for c in calls if c.name == "load-context")
        assert lc.argv == ["hpc-agent", "load-context", "--experiment-dir", exp]
        cp = next(c for c in calls if c.name == "check-preflight")
        assert cp.argv == ["hpc-agent", "preflight", "--cluster", "hoffman2"]

    def test_cluster_none_omits_flag_on_check_preflight(self) -> None:
        calls = sp._build_subcalls(experiment_dir=Path("/exp"), cluster=None, skip=[])
        cp = next(c for c in calls if c.name == "check-preflight")
        # Without --cluster, check-preflight runs the local-env checks only
        # (no cluster_ssh_echo probe).
        assert cp.argv == ["hpc-agent", "preflight"]
        assert "--cluster" not in cp.argv

    def test_skip_check_preflight_drops_only_that_subcall(self) -> None:
        calls = sp._build_subcalls(
            experiment_dir=Path("/exp"),
            cluster="hoffman2",
            skip=["check-preflight"],
        )
        assert [c.name for c in calls] == ["install-commands", "load-context"]

    def test_skip_all_yields_empty_list(self) -> None:
        calls = sp._build_subcalls(
            experiment_dir=Path("/exp"),
            cluster=None,
            skip=["install-commands", "load-context", "check-preflight"],
        )
        assert calls == []


class TestOverallDerivation:
    """``overall`` is ``pass`` iff every non-skipped sub-call returned ``ok``."""

    def test_all_succeed_overall_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _ok_subresult(),
                "load-context": _ok_subresult({"in_flight": []}),
                "check-preflight": _ok_subresult({"all_ok": True}),
            },
        )
        result = sp.submit_preflight(experiment_dir=tmp_path, cluster="hoffman2")
        assert result["overall"] == "pass"
        assert result["install_commands"]["ok"] is True
        assert result["load_context"]["ok"] is True
        assert result["check_preflight"]["ok"] is True

    def test_install_fails_overall_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _err_subresult("config_invalid"),
                "load-context": _ok_subresult(),
                "check-preflight": _ok_subresult(),
            },
        )
        result = sp.submit_preflight(experiment_dir=tmp_path)
        assert result["overall"] == "fail"
        assert result["install_commands"]["envelope"]["error_code"] == "config_invalid"

    def test_check_preflight_fails_overall_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The 2026-06-04 regression: cluster_ssh_echo failure surfaces here,
        # not lost mid-submit when rsync blows up.
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _ok_subresult(),
                "load-context": _ok_subresult(),
                "check-preflight": _err_subresult("ssh_unreachable"),
            },
        )
        result = sp.submit_preflight(experiment_dir=tmp_path, cluster="hoffman2")
        assert result["overall"] == "fail"
        assert result["check_preflight"]["envelope"]["error_code"] == "ssh_unreachable"
        # Sibling work preserved.
        assert result["install_commands"]["ok"] is True
        assert result["load_context"]["ok"] is True


class TestExecutionOrder:
    """Sequential — install-commands MUST run before load-context."""

    def test_install_commands_runs_first(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ordered: list[str] = []
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _ok_subresult(),
                "load-context": _ok_subresult(),
                "check-preflight": _ok_subresult(),
            },
            record_order=ordered,
        )
        sp.submit_preflight(experiment_dir=tmp_path, cluster="hoffman2")
        assert ordered == ["install-commands", "load-context", "check-preflight"]


class TestSkipBehavior:
    """``skip=[...]`` excludes the named sub-call from dispatch AND output."""

    def test_skip_check_preflight_yields_null_slot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ordered: list[str] = []
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _ok_subresult(),
                "load-context": _ok_subresult(),
            },
            record_order=ordered,
        )
        result = sp.submit_preflight(experiment_dir=tmp_path, skip=["check-preflight"])
        assert ordered == ["install-commands", "load-context"]
        # Skipped slot is null — not a SubResult with ok: false.
        assert result["check_preflight"] is None
        assert result["install_commands"] is not None
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
        assert env["ok"] is False
        assert env["error_code"] == "cluster_timeout"
        assert env["category"] == "cluster"
        assert env["retry_safe"] is False
