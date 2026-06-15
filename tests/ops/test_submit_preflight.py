"""Tests for the repurposed ``submit-preflight`` composite primitive (WS5 #1).

Pins the concurrent fan-out of all four mutually-independent sub-calls
(install-commands, load-context, check-preflight, resolve-resources;
#277, #289 — resolve-resources joins only when ``--cluster`` is supplied):
argv composition (including the optional ``--cluster`` that drives
check-preflight's cluster_ssh_echo branch and gates resolve-resources),
skip behavior, overall-derivation precedence, the concurrency of the
arms, and the synthesised-ErrorEnvelope shape.

The ``subprocess.run`` plumbing is mocked at :func:`_run_subprocess` so
these tests don't depend on a real ``hpc-agent`` binary being on PATH
inside the venv.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from hpc_agent.ops import submit_preflight as sp


def _empty_resolve_kwargs() -> dict[str, Any]:
    """The full passthrough-kwargs dict :func:`submit_preflight` builds.

    ``_resolve_resources_argv`` declares all seven overrides as required
    keyword-only args (no defaults), so ``_build_subcalls`` needs every key
    present even when each is ``None`` — that is exactly the dict
    ``submit_preflight`` assembles before delegating. Direct
    ``_build_subcalls`` callers in these tests reuse it instead of relying on
    the ``resolve_kwargs={}`` default, which would raise ``TypeError`` once a
    cluster is supplied.
    """
    return {
        "profile": None,
        "cmd_sha": None,
        "walltime_sec": None,
        "gpu_type": None,
        "safety_mult": None,
        "partition": None,
        "user_preferred_partition": None,
    }


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
        calls = sp._build_subcalls(
            experiment_dir=Path("/exp"),
            cluster="hoffman2",
            skip=[],
            resolve_kwargs=_empty_resolve_kwargs(),
        )
        # Order pin: install-commands → load-context → check-preflight →
        # resolve-resources. resolve-resources now also builds when a cluster
        # is supplied (#277); the last two fan out concurrently at run time.
        assert [c.name for c in calls] == [
            "install-commands",
            "load-context",
            "check-preflight",
            "resolve-resources",
        ]
        exp = str(Path("/exp"))
        ic = next(c for c in calls if c.name == "install-commands")
        assert ic.argv == ["hpc-agent", "install-commands"]
        lc = next(c for c in calls if c.name == "load-context")
        assert lc.argv == ["hpc-agent", "load-context", "--experiment-dir", exp]
        cp = next(c for c in calls if c.name == "check-preflight")
        assert cp.argv == ["hpc-agent", "preflight", "--cluster", "hoffman2"]
        # resolve_kwargs defaults to {} so no optional overrides are forwarded;
        # only --cluster + --experiment-dir appear.
        rr = next(c for c in calls if c.name == "resolve-resources")
        assert rr.argv == [
            "hpc-agent",
            "resolve-resources",
            "--cluster",
            "hoffman2",
            "--experiment-dir",
            exp,
        ]

    def test_cluster_none_omits_flag_on_check_preflight(self) -> None:
        calls = sp._build_subcalls(experiment_dir=Path("/exp"), cluster=None, skip=[])
        cp = next(c for c in calls if c.name == "check-preflight")
        # Without --cluster, check-preflight runs the local-env checks only
        # (no cluster_ssh_echo probe).
        assert cp.argv == ["hpc-agent", "preflight"]
        assert "--cluster" not in cp.argv
        # resolve-resources requires a cluster, so it is omitted entirely.
        assert "resolve-resources" not in [c.name for c in calls]

    def test_skip_check_preflight_drops_only_that_subcall(self) -> None:
        calls = sp._build_subcalls(
            experiment_dir=Path("/exp"),
            cluster="hoffman2",
            skip=["check-preflight"],
            resolve_kwargs=_empty_resolve_kwargs(),
        )
        # check-preflight is skipped but resolve-resources (cluster supplied,
        # not skipped) still builds — the two are independent.
        assert [c.name for c in calls] == [
            "install-commands",
            "load-context",
            "resolve-resources",
        ]

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
    """All four sub-calls are independent (#277, #289) and fan out concurrently."""

    def test_all_subcalls_run_concurrently(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ordered: list[str] = []
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _ok_subresult(),
                "load-context": _ok_subresult(),
                "check-preflight": _ok_subresult(),
                "resolve-resources": _ok_subresult(),
            },
            record_order=ordered,
        )
        sp.submit_preflight(experiment_dir=tmp_path, cluster="hoffman2")
        # No serialized prelude any more: all four fan out, so the only
        # guarantee is that every one ran (relative order is nondeterministic).
        assert set(ordered) == {
            "install-commands",
            "load-context",
            "check-preflight",
            "resolve-resources",
        }


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
        assert set(ordered) == {"install-commands", "load-context"}
        # Skipped slot is null — not a SubResult with ok: false.
        assert result["check_preflight"] is None
        assert result["install_commands"] is not None
        assert result["load_context"] is not None
        assert result["overall"] == "pass"


class TestConcurrentFanOut:
    """check-preflight ∥ resolve-resources overlap (#277): timing + failure."""

    def test_parallel_pair_overlaps_not_serial(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Each of the two slow arms sleeps ~0.4s; the instant prelude calls do
        # not. Run serially that pair would cost ~0.8s; fanned out it is bounded
        # by the slower arm (~0.4s). Assert well under the serial sum.
        def slow(call: sp.SubCall, *, timeout_sec: float) -> dict[str, Any]:
            if call.name in ("check-preflight", "resolve-resources"):
                time.sleep(0.4)
            return _ok_subresult()

        monkeypatch.setattr(sp, "_run_subprocess", slow)

        started = time.monotonic()
        result = sp.submit_preflight(experiment_dir=tmp_path, cluster="hoffman2")
        elapsed = time.monotonic() - started

        assert elapsed < 0.7, f"parallel pair did not overlap (elapsed={elapsed:.3f}s)"
        # Both arms ran and populated their slots.
        assert result["check_preflight"] is not None
        assert result["resolve_resources"] is not None
        assert result["overall"] == "pass"

    def test_resolve_resources_failure_flips_overall_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A resolve-resources failure must surface under its own slot and flip
        # overall to fail — the concurrent fan-out never swallows it.
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _ok_subresult(),
                "load-context": _ok_subresult(),
                "check-preflight": _ok_subresult(),
                "resolve-resources": _err_subresult("resource_resolution_failed"),
            },
        )
        result = sp.submit_preflight(experiment_dir=tmp_path, cluster="hoffman2")
        assert result["overall"] == "fail"
        assert result["resolve_resources"]["ok"] is False
        assert result["resolve_resources"]["envelope"]["error_code"] == "resource_resolution_failed"
        # The healthy sibling arm's work is preserved.
        assert result["check_preflight"]["ok"] is True


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
