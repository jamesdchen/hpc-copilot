"""Tests for the ``aggregate-preflight`` composite primitive (WS5 #3b).

Pins the install-commands ∥ load-context fan-out (concurrent — #291,
write-disjoint AND read-disjoint) plus the conditional reconcile branch
that distinguishes aggregate from its two siblings: reconcile fires only
when load-context's *output* reports ``next_step_hint == "monitor"`` AND
``--reconcile-scheduler`` was supplied, and its argv (run_id) is read
from load-context's envelope — a real data dependency, so reconcile
stays sequential AFTER the fan.

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

from hpc_agent.ops import aggregate_preflight as ap


def _ok_subresult(envelope_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """Canned SubResult with ``ok: true`` carrying *envelope_data* under data."""
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


def _monitor_lc(run_id: str = "run-abc") -> dict[str, Any]:
    """A load-context SubResult whose data triggers the reconcile branch."""
    return _ok_subresult(
        {
            "next_step_hint": "monitor",
            "in_flight": [{"run_id": run_id, "cluster": "hoffman2"}],
        }
    )


def _patch_run_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    by_name: dict[str, dict[str, Any]],
    *,
    record: list[ap.SubCall] | None = None,
) -> None:
    """Patch :func:`_run_subprocess` to return canned SubResults by name.

    When *record* is supplied, each dispatched :class:`SubCall` is
    appended so tests can assert both order and the reconcile argv that
    was assembled from load-context's output.
    """

    def fake(call: ap.SubCall, *, timeout_sec: float) -> dict[str, Any]:
        if record is not None:
            record.append(call)
        return by_name.get(call.name, _ok_subresult())

    monkeypatch.setattr(ap, "_run_subprocess", fake)


class TestBuildBaseSubcalls:
    """The always-run base sub-calls (install-commands + load-context)."""

    def test_both_built_with_required_fields_only(self) -> None:
        calls = ap._build_subcalls(experiment_dir=Path("/exp"), skip=[])
        # Both members of _PARALLEL_SUBCALLS (#291): listing order is purely
        # conventional — the runner fans them on a thread pool.
        assert {c.name for c in calls} == {"install-commands", "load-context"}
        exp = str(Path("/exp"))
        ic = next(c for c in calls if c.name == "install-commands")
        assert ic.argv == ["hpc-agent", "install-commands"]
        lc = next(c for c in calls if c.name == "load-context")
        assert lc.argv == ["hpc-agent", "load-context", "--experiment-dir", exp]

    def test_reconcile_is_not_a_base_subcall(self) -> None:
        # reconcile is conditional + built post-load-context; never in base.
        calls = ap._build_subcalls(experiment_dir=Path("/exp"), skip=[])
        assert "reconcile" not in [c.name for c in calls]

    def test_skip_install_commands_drops_only_that_subcall(self) -> None:
        calls = ap._build_subcalls(experiment_dir=Path("/exp"), skip=["install-commands"])
        assert [c.name for c in calls] == ["load-context"]

    def test_skip_both_yields_empty_list(self) -> None:
        calls = ap._build_subcalls(
            experiment_dir=Path("/exp"), skip=["install-commands", "load-context"]
        )
        assert calls == []


class TestMaybeBuildReconcile:
    """The reconcile-branch decision read off load-context's envelope."""

    def test_fires_on_monitor_hint_with_scheduler(self) -> None:
        call = ap._maybe_build_reconcile(
            load_context_result=_monitor_lc("run-xyz"),
            experiment_dir=Path("/exp"),
            reconcile_scheduler="sge",
            skip=[],
        )
        assert call is not None
        assert call.name == "reconcile"
        assert call.argv == [
            "hpc-agent",
            "reconcile",
            "--run-id",
            "run-xyz",
            "--scheduler",
            "sge",
            "--experiment-dir",
            str(Path("/exp")),
        ]

    def test_no_scheduler_means_no_reconcile(self) -> None:
        # Even with a monitor hint, omitting --reconcile-scheduler skips it.
        call = ap._maybe_build_reconcile(
            load_context_result=_monitor_lc(),
            experiment_dir=Path("/exp"),
            reconcile_scheduler=None,
            skip=[],
        )
        assert call is None

    def test_non_monitor_hint_means_no_reconcile(self) -> None:
        # The normal post-monitor path: hint is 'aggregate', nothing to reconcile.
        lc = _ok_subresult({"next_step_hint": "aggregate", "in_flight": []})
        call = ap._maybe_build_reconcile(
            load_context_result=lc,
            experiment_dir=Path("/exp"),
            reconcile_scheduler="slurm",
            skip=[],
        )
        assert call is None

    def test_skip_reconcile_suppresses_branch(self) -> None:
        call = ap._maybe_build_reconcile(
            load_context_result=_monitor_lc(),
            experiment_dir=Path("/exp"),
            reconcile_scheduler="sge",
            skip=["reconcile"],
        )
        assert call is None

    def test_monitor_hint_but_empty_in_flight_means_no_reconcile(self) -> None:
        # Hint says monitor but no run_id to target — degrade gracefully.
        lc = _ok_subresult({"next_step_hint": "monitor", "in_flight": []})
        call = ap._maybe_build_reconcile(
            load_context_result=lc,
            experiment_dir=Path("/exp"),
            reconcile_scheduler="sge",
            skip=[],
        )
        assert call is None

    def test_failed_load_context_means_no_reconcile(self) -> None:
        # A load-context that errored carries no trustworthy hint.
        call = ap._maybe_build_reconcile(
            load_context_result=_err_subresult("journal_corrupt"),
            experiment_dir=Path("/exp"),
            reconcile_scheduler="sge",
            skip=[],
        )
        assert call is None

    def test_skipped_load_context_means_no_reconcile(self) -> None:
        # load-context skipped → no result to read the trigger from.
        call = ap._maybe_build_reconcile(
            load_context_result=None,
            experiment_dir=Path("/exp"),
            reconcile_scheduler="sge",
            skip=[],
        )
        assert call is None


class TestReconcileOrchestration:
    """End-to-end: reconcile dispatched after load-context, argv from its output."""

    def test_reconcile_runs_last_with_run_id_from_load_context(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        recorded: list[ap.SubCall] = []
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _ok_subresult(),
                "load-context": _monitor_lc("run-77"),
                "reconcile": _ok_subresult({"lifecycle_state": "completed"}),
            },
            record=recorded,
        )
        result = ap.aggregate_preflight(experiment_dir=tmp_path, reconcile_scheduler="pbspro")
        # install-commands ∥ load-context fan concurrently (#291) so their
        # relative order is nondeterministic; reconcile stays sequential
        # AFTER the fan (its argv depends on load-context's envelope).
        assert set(c.name for c in recorded[:2]) == {"install-commands", "load-context"}
        assert recorded[-1].name == "reconcile"
        recon = recorded[-1]
        assert recon.argv[:5] == ["hpc-agent", "reconcile", "--run-id", "run-77", "--scheduler"]
        assert recon.argv[5] == "pbspro"
        assert result["reconcile"]["ok"] is True
        assert result["overall"] == "pass"

    def test_reconcile_absent_when_hint_not_monitor(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        recorded: list[ap.SubCall] = []
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _ok_subresult(),
                "load-context": _ok_subresult({"next_step_hint": "aggregate", "in_flight": []}),
            },
            record=recorded,
        )
        result = ap.aggregate_preflight(experiment_dir=tmp_path, reconcile_scheduler="sge")
        # Nondeterministic order across the fan (#291); both arms must run.
        assert {c.name for c in recorded} == {"install-commands", "load-context"}
        # Not-applicable reconcile is a null slot, not a SubResult with ok: false.
        assert result["reconcile"] is None
        assert result["overall"] == "pass"

    def test_reconcile_absent_when_no_scheduler(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        recorded: list[ap.SubCall] = []
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _ok_subresult(),
                "load-context": _monitor_lc(),
            },
            record=recorded,
        )
        result = ap.aggregate_preflight(experiment_dir=tmp_path)
        assert {c.name for c in recorded} == {"install-commands", "load-context"}
        assert result["reconcile"] is None
        assert result["overall"] == "pass"


class TestOverallDerivation:
    """``overall`` is ``pass`` iff every sub-call that ran returned ``ok``."""

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
        result = ap.aggregate_preflight(experiment_dir=tmp_path)
        assert result["overall"] == "fail"
        assert result["install_commands"]["envelope"]["error_code"] == "config_invalid"

    def test_load_context_fails_overall_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A failed load-context also suppresses reconcile (no trustworthy hint).
        recorded: list[ap.SubCall] = []
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _ok_subresult(),
                "load-context": _err_subresult("journal_corrupt"),
            },
            record=recorded,
        )
        result = ap.aggregate_preflight(experiment_dir=tmp_path, reconcile_scheduler="sge")
        assert result["overall"] == "fail"
        assert result["load_context"]["envelope"]["error_code"] == "journal_corrupt"
        assert result["reconcile"] is None
        # Fan order (#291) is nondeterministic across the parallel pair.
        assert {c.name for c in recorded} == {"install-commands", "load-context"}

    def test_reconcile_fails_overall_fail_siblings_preserved(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_run_subprocess(
            monkeypatch,
            {
                "install-commands": _ok_subresult(),
                "load-context": _monitor_lc("run-9"),
                "reconcile": _err_subresult("ssh_unreachable"),
            },
        )
        result = ap.aggregate_preflight(experiment_dir=tmp_path, reconcile_scheduler="slurm")
        assert result["overall"] == "fail"
        assert result["reconcile"]["envelope"]["error_code"] == "ssh_unreachable"
        # Sibling work preserved on failure.
        assert result["install_commands"]["ok"] is True
        assert result["load_context"]["ok"] is True


class TestSkipBehavior:
    """``skip=[...]`` excludes the named sub-call from dispatch AND output."""

    def test_skip_install_commands_yields_null_slot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        recorded: list[ap.SubCall] = []
        _patch_run_subprocess(
            monkeypatch,
            {"load-context": _ok_subresult()},
            record=recorded,
        )
        result = ap.aggregate_preflight(experiment_dir=tmp_path, skip=["install-commands"])
        assert [c.name for c in recorded] == ["load-context"]
        assert result["install_commands"] is None
        assert result["load_context"] is not None
        assert result["reconcile"] is None
        assert result["overall"] == "pass"


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

        def fake(call: ap.SubCall, *, timeout_sec: float) -> dict[str, Any]:
            barrier.wait()  # releases only if the OTHER arm is ALSO running
            return _ok_subresult({"next_step_hint": "aggregate", "in_flight": []})

        monkeypatch.setattr(ap, "_run_subprocess", fake)

        result = ap.aggregate_preflight(experiment_dir=tmp_path)
        assert result["overall"] == "pass"
        assert result["install_commands"]["ok"] is True
        assert result["load_context"]["ok"] is True
        # reconcile didn't fire (hint != monitor) and so the fan dominates.
        assert result["reconcile"] is None

    def test_parallel_pair_overlaps_not_serial(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Each arm sleeps ~0.4s. Serial: ~0.8s; fanned: bounded by the
        # slower arm (~0.4s). Assert well under the serial sum.
        def slow(call: ap.SubCall, *, timeout_sec: float) -> dict[str, Any]:
            time.sleep(0.4)
            return _ok_subresult({"next_step_hint": "aggregate", "in_flight": []})

        monkeypatch.setattr(ap, "_run_subprocess", slow)

        started = time.monotonic()
        result = ap.aggregate_preflight(experiment_dir=tmp_path)
        elapsed = time.monotonic() - started

        assert elapsed < 0.7, f"parallel pair did not overlap (elapsed={elapsed:.3f}s)"
        assert result["overall"] == "pass"


class TestSynthErrorSubresult:
    """:func:`_synth_error_subresult` shape for spawn / timeout / parse failures."""

    def test_shape_matches_subresult_contract(self) -> None:
        result = ap._synth_error_subresult(
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
