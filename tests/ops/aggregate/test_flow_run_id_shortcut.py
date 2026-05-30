"""Tests for the ``--run-id`` CLI shortcut on ``aggregate-flow``.

The canonical authoring path is ``--spec <file>``. ``--run-id <id>`` is
a 1-field shortcut for the common case where every other
``AggregateFlowSpec`` field is at its default — agents and humans avoid
writing a trivial 1-key JSON file just to pass a run identifier.

Contract:

* ``--run-id X`` is equivalent to a spec file ``{"run_id": "X"}``.
* ``--run-id`` and ``--spec`` are mutually exclusive (raises SpecInvalid).
* Neither flag raises SpecInvalid mentioning both options.

These tests drive the dispatcher end-to-end so the parser wiring +
arg_pre + dispatcher's ``spec_required=False`` branch are all exercised.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.cli._dispatch import dispatch_primitive
from hpc_agent.cli.dispatch import main as cli_main
from hpc_agent.ops.aggregate_flow import _aggregate_flow_arg_pre

# ───────────────────────────────────────────────────────────────────
# Unit tests for the arg_pre hook — keep them away from the dispatcher
# so they don't pay the spec-file IO / output-schema-validation cost.
# ───────────────────────────────────────────────────────────────────


def test_arg_pre_run_id_only_synthesizes_spec() -> None:
    """``--run-id X`` alone must synthesize ``AggregateFlowSpec(run_id=X)``."""
    ns = argparse.Namespace(spec=None, run_id="r_shortcut_test")
    extra = _aggregate_flow_arg_pre(ns)
    assert "spec" in extra, "arg_pre must override the dispatcher's None spec"
    spec = extra["spec"]
    assert isinstance(spec, AggregateFlowSpec)
    assert spec.run_id == "r_shortcut_test"
    # Every other field must equal the spec defaults — the shortcut's
    # whole point is "every other field defaulted."
    assert spec == AggregateFlowSpec(run_id="r_shortcut_test")


def test_arg_pre_spec_only_returns_empty() -> None:
    """When ``--spec`` is set, arg_pre is a no-op — dispatcher already loaded it."""
    ns = argparse.Namespace(spec=Path("/tmp/spec.json"), run_id=None)
    extra = _aggregate_flow_arg_pre(ns)
    assert extra == {}


def test_arg_pre_both_raises_ambiguous() -> None:
    """Passing both --spec and --run-id is an authoring error: pick one."""
    from hpc_agent import errors

    ns = argparse.Namespace(spec=Path("/tmp/spec.json"), run_id="r_xxx")
    with pytest.raises(errors.SpecInvalid) as exc_info:
        _aggregate_flow_arg_pre(ns)
    msg = str(exc_info.value).lower()
    # Wording: surface 'ambiguous' / 'not both' and 'pick one' actionable hint.
    assert "ambiguous" in msg or "not both" in msg
    assert "--spec" in str(exc_info.value)
    assert "--run-id" in str(exc_info.value)


def test_arg_pre_neither_raises_actionable() -> None:
    """Missing both flags must mention both options in the error message."""
    from hpc_agent import errors

    ns = argparse.Namespace(spec=None, run_id=None)
    with pytest.raises(errors.SpecInvalid) as exc_info:
        _aggregate_flow_arg_pre(ns)
    # The error message must mention BOTH options so the caller knows
    # they have a choice — the old "--spec is required" wording hid the
    # shortcut.
    assert "--spec" in str(exc_info.value)
    assert "--run-id" in str(exc_info.value)


# ───────────────────────────────────────────────────────────────────
# End-to-end via the dispatcher: prove the parser wiring + arg_pre +
# spec_required=False branch all work together. We stub the primitive
# body so neither the SSH layer nor the output-schema validator fires.
# ───────────────────────────────────────────────────────────────────


def _capture_emit(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Patch ``_emit`` to capture envelopes."""
    captured: list[dict] = []
    monkeypatch.setattr(
        "hpc_agent.cli._helpers._emit",
        lambda payload: captured.append(payload),
    )
    return captured


def test_dispatcher_run_id_shortcut_synthesizes_spec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """End-to-end: ``--run-id X`` makes the dispatcher hand the primitive
    an ``AggregateFlowSpec(run_id=X)`` with every other field defaulted.

    Stub out the primitive body so the test stays a CLI-wiring assertion,
    not an integration test of the workflow itself.
    """
    captured_kwargs: dict = {}

    def _fake_aggregate_flow(**kwargs):
        captured_kwargs.update(kwargs)
        # Return a minimal-but-schema-valid envelope.
        from hpc_agent.ops.aggregate_flow import AggregateFlowResult

        return AggregateFlowResult(
            run_id=kwargs["spec"].run_id,
            combined_waves=[],
            failed_waves=[],
            waves_combined_this_call=[],
            combiner_dir_local=str(tmp_path / "_combiner"),
            aggregated_metrics={},
        )

    # Patch the registry entry's underlying callable so the dispatcher
    # picks it up. ``PrimitiveMeta`` is frozen, so swap the entry in the
    # registry dict with a replaced copy (restored by monkeypatch teardown).
    import dataclasses as _dc

    from hpc_agent._kernel.registry import primitive as _reg

    original = _reg._REGISTRY["aggregate-flow"]
    monkeypatch.setitem(
        _reg._REGISTRY,
        "aggregate-flow",
        _dc.replace(original, func=_fake_aggregate_flow),
    )

    _capture_emit(monkeypatch)

    ns = argparse.Namespace(
        experiment_dir=tmp_path,
        spec=None,
        run_id="r_shortcut_test",
        dry_run=False,
    )
    rc = dispatch_primitive("aggregate-flow", ns)
    assert rc == 0
    spec = captured_kwargs.get("spec")
    assert isinstance(spec, AggregateFlowSpec)
    assert spec.run_id == "r_shortcut_test"
    assert spec == AggregateFlowSpec(run_id="r_shortcut_test")


def test_dispatcher_spec_file_path_still_works(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The canonical ``--spec <file>`` path still loads + validates the spec."""
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps({"run_id": "r_shortcut_test"}), encoding="utf-8")

    captured_kwargs: dict = {}

    def _fake_aggregate_flow(**kwargs):
        captured_kwargs.update(kwargs)
        from hpc_agent.ops.aggregate_flow import AggregateFlowResult

        return AggregateFlowResult(
            run_id=kwargs["spec"].run_id,
            combined_waves=[],
            failed_waves=[],
            waves_combined_this_call=[],
            combiner_dir_local=str(tmp_path / "_combiner"),
            aggregated_metrics={},
        )

    import dataclasses as _dc

    from hpc_agent._kernel.registry import primitive as _reg

    original = _reg._REGISTRY["aggregate-flow"]
    monkeypatch.setitem(
        _reg._REGISTRY,
        "aggregate-flow",
        _dc.replace(original, func=_fake_aggregate_flow),
    )

    _capture_emit(monkeypatch)

    ns = argparse.Namespace(
        experiment_dir=tmp_path,
        spec=spec_file,
        run_id=None,
        dry_run=False,
    )
    rc = dispatch_primitive("aggregate-flow", ns)
    assert rc == 0
    spec = captured_kwargs.get("spec")
    assert isinstance(spec, AggregateFlowSpec)
    assert spec.run_id == "r_shortcut_test"
    # The synthesized-from-file spec must equal the one --run-id would
    # have produced — that's the equivalence guarantee.
    assert spec == AggregateFlowSpec(run_id="r_shortcut_test")


# ───────────────────────────────────────────────────────────────────
# End-to-end via the top-level CLI entry point: drive the actual
# argparse + dispatcher + error envelope path for the failure cases.
# These don't need the primitive to run — they fail before it would.
# ───────────────────────────────────────────────────────────────────


def test_cli_both_run_id_and_spec_raises_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """At the top-level CLI: --spec X --run-id Y → SpecInvalid envelope."""
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps({"run_id": "r_xxxxxxxx"}), encoding="utf-8")

    captured = _capture_emit(monkeypatch)
    rc = cli_main(
        [
            "aggregate-flow",
            "--experiment-dir",
            str(tmp_path),
            "--spec",
            str(spec_file),
            "--run-id",
            "r_xxxxxxxx",
        ]
    )
    assert rc != 0
    env = captured[-1]
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    msg = env["message"].lower()
    assert "ambiguous" in msg or "not both" in msg
    assert "--spec" in env["message"] and "--run-id" in env["message"]


def test_cli_neither_run_id_nor_spec_raises_actionable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """At the top-level CLI: no --spec, no --run-id → SpecInvalid mentioning both."""
    captured = _capture_emit(monkeypatch)
    rc = cli_main(
        [
            "aggregate-flow",
            "--experiment-dir",
            str(tmp_path),
        ]
    )
    assert rc != 0
    env = captured[-1]
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    assert "--spec" in env["message"]
    assert "--run-id" in env["message"]
