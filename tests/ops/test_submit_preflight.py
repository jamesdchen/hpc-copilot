"""Tests for the ``submit-preflight`` composite primitive (WS5 #1 scaffold).

The skeleton in :mod:`hpc_agent.ops.submit_preflight` orchestrates three
sub-calls (``export-package`` / ``plan-throughput`` / ``validate-campaign``)
via ``asyncio.gather``. These tests pin the *orchestration* contract:

* the right SubCall argv shape is built per ``skip``/``force_export``/
  pass-through fields,
* the composite's ``overall`` is derived correctly from sub-envelopes,
* the ``skip`` list excludes the corresponding sub-call entirely (its
  output slot stays ``None`` so consumers can re-run only the failing
  pieces),
* the ``sequential`` vs ``asyncio`` strategy selects the right code path
  (order preserved sequentially; concurrent under asyncio).

The asyncio subprocess plumbing itself (spawn / timeout / JSON parse) is
mocked at :func:`_run_async` so these tests run synchronously and don't
depend on a real ``hpc-agent`` binary being on PATH inside the venv.
A small extra test exercises the synthesized-ErrorEnvelope path in
:func:`_synth_error_subresult` directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from hpc_agent.ops import submit_preflight as sp


def _ok_subresult(envelope_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """A canned SubResult whose envelope is ``ok: true`` with optional data."""
    env: dict[str, Any] = {"ok": True, "idempotent": True, "data": envelope_data or {}}
    return {"envelope": env, "elapsed_sec": 0.1, "ok": True}


def _err_subresult(error_code: str = "spec_invalid") -> dict[str, Any]:
    """A canned SubResult whose envelope is ``ok: false`` with *error_code*."""
    env = {
        "ok": False,
        "error_code": error_code,
        "message": "synthetic test failure",
        "category": "user",
        "retry_safe": False,
    }
    return {"envelope": env, "elapsed_sec": 0.1, "ok": False}


def _patch_run_async(
    monkeypatch: pytest.MonkeyPatch,
    by_name: dict[str, dict[str, Any]],
    *,
    record_order: list[str] | None = None,
    record_strategy: list[str] | None = None,
) -> None:
    """Monkey-patch :func:`_run_async` to return canned SubResults by name.

    *by_name* maps SubCall.name → SubResult dict. If *record_order* is
    provided, the patched function appends names in input order so tests
    can verify the sub-calls dispatched matches what _build_subcalls
    produced. *record_strategy* captures the fanout_strategy the
    composite chose.
    """

    async def fake_run_async(
        calls: list[sp.SubCall],
        *,
        fanout_strategy: str,
        timeout_sec: float,
    ) -> list[dict[str, Any]]:
        if record_strategy is not None:
            record_strategy.append(fanout_strategy)
        out: list[dict[str, Any]] = []
        for c in calls:
            if record_order is not None:
                record_order.append(c.name)
            out.append(by_name.get(c.name, _ok_subresult()))
        return out

    monkeypatch.setattr(sp, "_run_async", fake_run_async)


class TestBuildSubcalls:
    """:func:`_build_subcalls` argv composition: per-flag + per-skip wiring."""

    def test_all_three_built_with_required_fields_only(self) -> None:
        calls = sp._build_subcalls(
            experiment_dir=Path("/exp"),
            cluster="hoffman2",
            profile="sge",
            campaign_id=None,
            expected_cmd_sha=None,
            force_export=False,
            notebooks_dir="notebooks",
            skip=[],
        )
        names = [c.name for c in calls]
        assert names == ["export-package", "plan-throughput", "validate-campaign"]
        # Path.__str__ uses the platform-native separator (``/exp`` on POSIX,
        # ``\exp`` on Windows). The composite normalises via ``str(Path)`` so
        # pin against the same.
        exp = str(Path("/exp"))
        ep = next(c for c in calls if c.name == "export-package")
        assert ep.argv == ["hpc-agent", "export-package", "--experiment-dir", exp]
        pt = next(c for c in calls if c.name == "plan-throughput")
        assert pt.argv == [
            "hpc-agent",
            "plan-throughput",
            "--experiment-dir",
            exp,
            "--cluster",
            "hoffman2",
        ]
        vc = next(c for c in calls if c.name == "validate-campaign")
        assert vc.argv == [
            "hpc-agent",
            "validate-campaign",
            "--experiment-dir",
            exp,
            "--cluster",
            "hoffman2",
            "--profile",
            "sge",
        ]

    def test_force_export_appends_flag(self) -> None:
        calls = sp._build_subcalls(
            experiment_dir=Path("/exp"),
            cluster="c",
            profile="sge",
            campaign_id=None,
            expected_cmd_sha=None,
            force_export=True,
            notebooks_dir="notebooks",
            skip=[],
        )
        ep = next(c for c in calls if c.name == "export-package")
        assert "--force" in ep.argv

    def test_non_default_notebooks_dir_passed_through(self) -> None:
        calls = sp._build_subcalls(
            experiment_dir=Path("/exp"),
            cluster="c",
            profile="sge",
            campaign_id=None,
            expected_cmd_sha=None,
            force_export=False,
            notebooks_dir="custom_nb",
            skip=[],
        )
        ep = next(c for c in calls if c.name == "export-package")
        assert "--notebooks-dir" in ep.argv
        assert ep.argv[ep.argv.index("--notebooks-dir") + 1] == "custom_nb"

    def test_campaign_id_and_cmd_sha_appended_to_validate_campaign(self) -> None:
        calls = sp._build_subcalls(
            experiment_dir=Path("/exp"),
            cluster="c",
            profile="sge",
            campaign_id="tune_2026_01",
            expected_cmd_sha="abc123de",
            force_export=False,
            notebooks_dir="notebooks",
            skip=[],
        )
        vc = next(c for c in calls if c.name == "validate-campaign")
        assert "--campaign-id" in vc.argv
        assert vc.argv[vc.argv.index("--campaign-id") + 1] == "tune_2026_01"
        assert "--expected-cmd-sha" in vc.argv
        assert vc.argv[vc.argv.index("--expected-cmd-sha") + 1] == "abc123de"

    def test_skip_excludes_named_subcalls(self) -> None:
        calls = sp._build_subcalls(
            experiment_dir=Path("/exp"),
            cluster="c",
            profile="sge",
            campaign_id=None,
            expected_cmd_sha=None,
            force_export=False,
            notebooks_dir="notebooks",
            skip=["validate-campaign", "plan-throughput"],
        )
        assert [c.name for c in calls] == ["export-package"]


class TestOverallDerivation:
    """Composite ``overall`` derivation: fail > warn > pass precedence."""

    def test_all_succeed_overall_pass(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_run_async(
            monkeypatch,
            {
                "export-package": _ok_subresult(),
                "plan-throughput": _ok_subresult(),
                "validate-campaign": _ok_subresult({"overall": "pass"}),
            },
        )
        result = sp.submit_preflight(experiment_dir=tmp_path, cluster="c", profile="sge")
        assert result["overall"] == "pass"
        assert result["fanout_strategy"] == "asyncio"
        assert result["export_package"]["ok"] is True
        assert result["validate_campaign"]["ok"] is True

    def test_any_subcall_failure_overall_fail(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_run_async(
            monkeypatch,
            {
                "export-package": _ok_subresult(),
                "plan-throughput": _err_subresult("cluster_timeout"),
                "validate-campaign": _ok_subresult({"overall": "pass"}),
            },
        )
        result = sp.submit_preflight(experiment_dir=tmp_path, cluster="c", profile="sge")
        assert result["overall"] == "fail"
        # Parallel siblings' work is preserved (not collapsed to a single error).
        assert result["export_package"]["ok"] is True
        assert result["plan_throughput"]["ok"] is False
        assert result["plan_throughput"]["envelope"]["error_code"] == "cluster_timeout"

    def test_validate_campaign_warn_propagates_when_no_failures(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _patch_run_async(
            monkeypatch,
            {
                "export-package": _ok_subresult(),
                "plan-throughput": _ok_subresult(),
                "validate-campaign": _ok_subresult({"overall": "warn"}),
            },
        )
        result = sp.submit_preflight(experiment_dir=tmp_path, cluster="c", profile="sge")
        assert result["overall"] == "warn"

    def test_failure_dominates_validate_campaign_warn(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # When BOTH conditions hold, fail wins over warn (precedence pin).
        _patch_run_async(
            monkeypatch,
            {
                "export-package": _err_subresult(),
                "plan-throughput": _ok_subresult(),
                "validate-campaign": _ok_subresult({"overall": "warn"}),
            },
        )
        result = sp.submit_preflight(experiment_dir=tmp_path, cluster="c", profile="sge")
        assert result["overall"] == "fail"


class TestSkipBehavior:
    """``skip=[...]`` excludes the named sub-call from dispatch AND output."""

    def test_skip_validate_campaign_yields_null_slot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        ordered: list[str] = []
        _patch_run_async(
            monkeypatch,
            {
                "export-package": _ok_subresult(),
                "plan-throughput": _ok_subresult(),
            },
            record_order=ordered,
        )
        result = sp.submit_preflight(
            experiment_dir=tmp_path,
            cluster="c",
            profile="sge",
            skip=["validate-campaign"],
        )
        # Only the two non-skipped sub-calls dispatched.
        assert set(ordered) == {"export-package", "plan-throughput"}
        # Skipped slot is null (not an error SubResult) so a re-run targets
        # only the missing piece.
        assert result["validate_campaign"] is None
        # The non-skipped pair populate.
        assert result["export_package"] is not None
        assert result["plan_throughput"] is not None
        # No validate-campaign means no warn signal — overall stays pass.
        assert result["overall"] == "pass"


class TestFanoutStrategy:
    """Selecting ``sequential`` vs ``asyncio`` reaches the right code path."""

    def test_asyncio_is_default(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        seen: list[str] = []
        _patch_run_async(monkeypatch, {}, record_strategy=seen)
        sp.submit_preflight(experiment_dir=tmp_path, cluster="c", profile="sge")
        assert seen == ["asyncio"]

    def test_sequential_passed_through(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seen: list[str] = []
        _patch_run_async(monkeypatch, {}, record_strategy=seen)
        sp.submit_preflight(
            experiment_dir=tmp_path,
            cluster="c",
            profile="sge",
            fanout_strategy="sequential",
        )
        assert seen == ["sequential"]

    def test_invalid_strategy_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="unknown fanout_strategy"):
            sp.submit_preflight(
                experiment_dir=tmp_path,
                cluster="c",
                profile="sge",
                fanout_strategy="threads",  # not in the schema's enum
            )


class TestSynthErrorSubresult:
    """:func:`_synth_error_subresult` is the shape used for spawn/timeout/parse
    failures, exercised here directly (the async paths are integration-level)."""

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
        # Conforms to ErrorEnvelope in envelope.json: required fields present.
        assert env["ok"] is False
        assert env["error_code"] == "cluster_timeout"
        assert env["message"] == "probe timed out"
        assert env["category"] == "cluster"
        assert env["retry_safe"] is False
