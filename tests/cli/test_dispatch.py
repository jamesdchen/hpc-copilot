"""Exhaustive tests for the registry-driven CLI dispatcher.

Covers every hook in :class:`hpc_agent.cli._dispatch.CliShape` —
spec_arg, experiment_dir_arg, dry_run_arg + passthrough,
requires_ssh, args, arg_pre, result_post, handler — plus
:func:`cli_to_invocation_string` and verb-group nesting. Each test
registers a synthetic primitive with the desired shape, exercises the
dispatcher (or the parser), and asserts the envelope on stdout or the
returned int.

The tests build a *fresh* synthetic registry via ``_reset_for_tests``
so they don't pollute the real registry and don't depend on the
@primitive decoration order. Each test re-imports the production
modules at the end so subsequent unrelated tests see the registered
production primitives.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import pytest
from pydantic import BaseModel, Field

from hpc_agent import errors
from hpc_agent._internal import primitive as _prim_mod
from hpc_agent._internal.primitive import primitive, register_primitives
from hpc_agent.cli._dispatch import (
    CliArg,
    CliShape,
    SchemaRef,
    cli_to_invocation_string,
    dispatch_primitive,
)


@pytest.fixture(autouse=True)
def _fresh_registry() -> Any:
    """Save-and-restore the primitive registry around each test.

    Each test registers a small handful of synthetic primitives via
    ``@primitive(name="syn-...", cli=CliShape(...))``. We snapshot the
    production registry (populated by the session-scoped autouse
    fixture in ``tests/conftest.py``) and pop synthetic names on
    teardown. We *don't* use ``_reset_for_tests`` here: that helper
    wipes the registry and clears the registration latch, but
    ``register_primitives`` reads ``sys.modules`` (cached imports) and
    cannot re-run @primitive decorators, so the registry would stay
    empty for every test in subsequent modules.
    """
    register_primitives()
    snapshot = set(_prim_mod._REGISTRY.keys())
    yield
    for name in list(_prim_mod._REGISTRY.keys()):
        if name not in snapshot:
            del _prim_mod._REGISTRY[name]


def _capsys_envelope(captured) -> dict[str, Any]:
    """Return the parsed JSON envelope on stdout (must be exactly one line)."""
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected one envelope, got: {captured.out!r}"
    return json.loads(lines[0])


# ─── args-based dispatch ───────────────────────────────────────────────────


def test_args_based_dispatch_emits_ok_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    @primitive(
        name="syn-add",
        verb="query",
        cli=CliShape(
            help="Add two ints.",
            args=(
                CliArg("--lhs", type=int, required=True),
                CliArg("--rhs", type=int, required=True),
            ),
        ),
    )
    def syn_add(*, lhs: int, rhs: int) -> dict[str, int]:
        return {"sum": lhs + rhs}

    ns = argparse.Namespace(lhs=2, rhs=3)
    rc = dispatch_primitive("syn-add", ns)

    assert rc == 0
    env = _capsys_envelope(capsys.readouterr())
    assert env == {"ok": True, "idempotent": True, "data": {"sum": 5}}


def test_experiment_dir_arg_injects_kwarg(capsys: pytest.CaptureFixture[str], tmp_path) -> None:
    @primitive(
        name="syn-cwd",
        verb="query",
        cli=CliShape(help="Echo the experiment dir.", experiment_dir_arg=True),
    )
    def syn_cwd(*, experiment_dir) -> dict[str, str]:
        return {"path": str(experiment_dir)}

    ns = argparse.Namespace(experiment_dir=tmp_path)
    assert dispatch_primitive("syn-cwd", ns) == 0
    env = _capsys_envelope(capsys.readouterr())
    assert env["data"] == {"path": str(tmp_path)}


# ─── spec_arg dispatch ─────────────────────────────────────────────────────


class _Spec(BaseModel):
    name: str
    count: int = Field(ge=1)


def test_spec_arg_loads_and_model_validates(capsys: pytest.CaptureFixture[str], tmp_path) -> None:
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps({"name": "alpha", "count": 7}), encoding="utf-8")

    @primitive(
        name="syn-spec",
        verb="query",
        cli=CliShape(
            help="Run with a typed spec.",
            spec_arg=True,
            spec_model=_Spec,
        ),
    )
    def syn_spec(*, spec: _Spec) -> dict[str, Any]:
        assert isinstance(spec, _Spec)
        return {"name": spec.name, "doubled": spec.count * 2}

    ns = argparse.Namespace(spec=spec_file)
    assert dispatch_primitive("syn-spec", ns) == 0
    env = _capsys_envelope(capsys.readouterr())
    assert env["data"] == {"name": "alpha", "doubled": 14}


def test_spec_arg_rejects_missing_spec_with_user_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    @primitive(
        name="syn-required",
        verb="query",
        cli=CliShape(help="Spec-required.", spec_arg=True, spec_model=_Spec),
    )
    def syn_required(*, spec: _Spec) -> dict[str, Any]:
        return {"name": spec.name}

    ns = argparse.Namespace(spec=None)
    rc = dispatch_primitive("syn-required", ns)
    assert rc == 1  # EXIT_USER_ERROR
    env = _capsys_envelope(capsys.readouterr())
    assert env["ok"] is False
    assert env["category"] == "user"


def test_spec_arg_rejects_invalid_model(capsys: pytest.CaptureFixture[str], tmp_path) -> None:
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps({"name": "alpha", "count": 0}), encoding="utf-8")

    @primitive(
        name="syn-invalid",
        verb="query",
        cli=CliShape(help="Bad spec.", spec_arg=True, spec_model=_Spec),
    )
    def syn_invalid(*, spec: _Spec) -> dict[str, Any]:
        return {"name": spec.name}

    ns = argparse.Namespace(spec=spec_file)
    rc = dispatch_primitive("syn-invalid", ns)
    assert rc == 1
    env = _capsys_envelope(capsys.readouterr())
    assert env["ok"] is False


# ─── dry_run_passthrough_keys ──────────────────────────────────────────────


def test_dry_run_passthrough_emits_shape_without_calling_primitive(
    capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps({"name": "alpha", "count": 7}), encoding="utf-8")
    called: list[bool] = []

    @primitive(
        name="syn-dryrun",
        verb="query",
        cli=CliShape(
            help="Dry-run passthrough.",
            spec_arg=True,
            spec_model=_Spec,
            dry_run_arg=True,
            dry_run_passthrough_keys=("name", "count"),
        ),
    )
    def syn_dryrun(*, spec: _Spec) -> dict[str, Any]:
        called.append(True)
        return {"name": spec.name}

    ns = argparse.Namespace(spec=spec_file, dry_run=True)
    rc = dispatch_primitive("syn-dryrun", ns)
    assert rc == 0
    assert called == [], "primitive must not be called on dry-run"
    env = _capsys_envelope(capsys.readouterr())
    assert env["data"] == {"dry_run": True, "name": "alpha", "count": 7}


# ─── arg_pre ───────────────────────────────────────────────────────────────


def test_arg_pre_merges_extra_kwargs(capsys: pytest.CaptureFixture[str]) -> None:
    def _arg_pre(ns: argparse.Namespace) -> dict[str, Any]:
        kv = {}
        for tok in (ns.extra_env or "").split(","):
            if "=" in tok:
                k, _, v = tok.partition("=")
                kv[k.strip()] = v.strip()
        return {"extra_env": kv}

    @primitive(
        name="syn-arg-pre",
        verb="query",
        cli=CliShape(
            help="Custom env parsing.",
            args=(CliArg("--extra-env", type=str, default=""),),
            arg_pre=_arg_pre,
        ),
    )
    def syn_arg_pre(*, extra_env: dict[str, str]) -> dict[str, Any]:
        return {"env": extra_env}

    ns = argparse.Namespace(extra_env="A=1,B=2")
    assert dispatch_primitive("syn-arg-pre", ns) == 0
    env = _capsys_envelope(capsys.readouterr())
    assert env["data"] == {"env": {"A": "1", "B": "2"}}


# ─── result_post ───────────────────────────────────────────────────────────


def test_result_post_projects_return_value(capsys: pytest.CaptureFixture[str]) -> None:
    @primitive(
        name="syn-post",
        verb="query",
        cli=CliShape(
            help="Project list of objects.",
            result_post=lambda result: {"items": [{"k": x} for x in result]},
        ),
    )
    def syn_post() -> list[int]:
        return [1, 2, 3]

    ns = argparse.Namespace()
    assert dispatch_primitive("syn-post", ns) == 0
    env = _capsys_envelope(capsys.readouterr())
    assert env["data"] == {"items": [{"k": 1}, {"k": 2}, {"k": 3}]}


# ─── requires_ssh ──────────────────────────────────────────────────────────


def test_requires_ssh_gates_without_auth_sock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    @primitive(
        name="syn-ssh",
        verb="query",
        cli=CliShape(help="Needs SSH agent.", requires_ssh=True),
    )
    def syn_ssh() -> dict[str, Any]:
        raise AssertionError("must not be called without SSH gate")

    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    ns = argparse.Namespace()
    rc = dispatch_primitive("syn-ssh", ns)
    assert rc == 2  # EXIT_CLUSTER_ERROR (ssh_unreachable → network → cluster)
    env = _capsys_envelope(capsys.readouterr())
    assert env["ok"] is False
    assert env["error_code"] == "ssh_unreachable"


# ─── handler (Tier 2 escape hatch) ─────────────────────────────────────────


def test_handler_replaces_default_dispatch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _custom(ns: argparse.Namespace) -> int:
        from hpc_agent.cli._helpers import EXIT_OK, _ok

        _ok({"custom": True, "x": ns.x}, name="syn-handler")
        return EXIT_OK

    @primitive(
        name="syn-handler",
        verb="query",
        cli=CliShape(
            help="Hand-written handler.",
            args=(CliArg("--x", type=int, required=True),),
            handler=_custom,
        ),
    )
    def syn_handler() -> dict[str, Any]:
        raise AssertionError("dispatcher must delegate to handler instead")

    ns = argparse.Namespace(x=42)
    assert dispatch_primitive("syn-handler", ns) == 0
    env = _capsys_envelope(capsys.readouterr())
    assert env["data"] == {"custom": True, "x": 42}


# ─── signature-based kwarg filtering (CLI-only flags) ─────────────────────


def test_cli_only_flags_dropped_when_primitive_doesnt_accept_them(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A CliArg whose attr_name doesn't match a primitive kwarg is dropped.

    Pattern: a primitive's Python signature is ``recall_campaigns(roots, *,
    spec)`` but the CLI exposes ``--root``, ``--limit``, ``--task-kind``,
    etc., builds them into a payload in ``arg_pre``, and re-maps them
    under ``roots=`` and ``spec=``. The raw flag values must NOT be
    forwarded to the primitive (else TypeError on unknown kwarg).
    """

    def _arg_pre(ns: argparse.Namespace) -> dict[str, Any]:
        return {
            "roots": [ns.root] if ns.root else [],
            "spec": {"limit": ns.limit, "operator": ns.operator},
        }

    @primitive(
        name="syn-filter",
        verb="query",
        cli=CliShape(
            help="Re-mapped flags.",
            args=(
                CliArg("--root", type=str, default=None),
                CliArg("--limit", type=int, default=10),
                CliArg("--operator", type=str, default=None),
            ),
            arg_pre=_arg_pre,
        ),
    )
    def syn_filter(roots: list[str], *, spec: dict[str, Any]) -> dict[str, Any]:
        return {"roots": roots, "limit": spec["limit"]}

    ns = argparse.Namespace(root="/tmp/x", limit=20, operator="me")
    assert dispatch_primitive("syn-filter", ns) == 0
    env = _capsys_envelope(capsys.readouterr())
    assert env["data"] == {"roots": ["/tmp/x"], "limit": 20}


def test_var_keyword_primitive_receives_all_kwargs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Filtering is skipped when the primitive declares ``**kwargs``."""

    @primitive(
        name="syn-varkw",
        verb="query",
        cli=CliShape(
            help="Variadic.",
            args=(CliArg("--alpha", type=int, default=1),),
        ),
    )
    def syn_varkw(**kwargs: Any) -> dict[str, Any]:
        return {"received": sorted(kwargs.keys())}

    ns = argparse.Namespace(alpha=5)
    assert dispatch_primitive("syn-varkw", ns) == 0
    env = _capsys_envelope(capsys.readouterr())
    assert env["data"] == {"received": ["alpha"]}


# ─── HpcError propagation ─────────────────────────────────────────────────


def test_hpc_error_routed_to_err_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    @primitive(
        name="syn-raises",
        verb="query",
        cli=CliShape(help="Always raises."),
    )
    def syn_raises() -> dict[str, Any]:
        raise errors.SpecInvalid("nope")

    ns = argparse.Namespace()
    rc = dispatch_primitive("syn-raises", ns)
    assert rc == 1
    env = _capsys_envelope(capsys.readouterr())
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    assert "nope" in env["message"]


# ─── verb-group registration via the parser ────────────────────────────────


def test_verb_group_nests_under_parent_in_parser() -> None:
    # We don't decorate here because the parser walks the live registry.
    # Build a tiny registry with one grouped primitive, then call the
    # parser to verify nesting.
    @primitive(
        name="syngrp-status",
        verb="query",
        cli=CliShape(
            help="Grouped status.",
            args=(CliArg("--id", type=str, required=True),),
            group="syngrp",
        ),
    )
    def syngrp_status(*, id: str) -> dict[str, Any]:
        return {"id": id}

    # We need the legacy fallback to be a no-op (no extra add_parser).
    # Patch _register_legacy_subcommands to a noop for this test so the
    # registry-only walk drives the parser.
    import hpc_agent.agent_cli as _ac

    real_legacy = _ac._register_legacy_subcommands
    _ac._register_legacy_subcommands = lambda sub, **_: None
    try:
        from hpc_agent.cli.parser import build_parser

        parser = build_parser()
    finally:
        _ac._register_legacy_subcommands = real_legacy

    # Top-level should have a "syngrp" verb whose nested subparser owns "status".
    sub_actions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert sub_actions, "expected at least one _SubParsersAction"
    top_choices = sub_actions[0].choices
    assert "syngrp" in top_choices
    parent = top_choices["syngrp"]
    nested = [a for a in parent._actions if isinstance(a, argparse._SubParsersAction)]
    assert nested, "verb group missing nested subparser"
    assert "status" in nested[0].choices


# ─── cli_to_invocation_string ──────────────────────────────────────────────


def test_invocation_string_preserves_legacy_string() -> None:
    assert cli_to_invocation_string("foo", "hpc-agent foo --bar") == "hpc-agent foo --bar"


def test_invocation_string_synthesizes_from_cli_shape() -> None:
    shape = CliShape(
        help="X",
        spec_arg=True,
        schema_ref=SchemaRef(input="thing"),
        experiment_dir_arg=True,
        dry_run_arg=True,
        args=(
            CliArg("--run-id", required=True),
            CliArg("--flag", action="store_true"),
        ),
    )
    rendered = cli_to_invocation_string("do-it", shape)
    assert rendered is not None
    assert rendered.startswith("hpc-agent do-it")
    assert "--spec <path>" in rendered
    assert "[--experiment-dir <dir>]" in rendered
    assert "[--dry-run]" in rendered
    assert "--run-id <run_id>" in rendered
    assert "[--flag]" in rendered


def test_invocation_string_for_grouped_primitive() -> None:
    shape = CliShape(help="X", group="campaign")
    assert cli_to_invocation_string("campaign-status", shape) == ("hpc-agent campaign status")


def test_invocation_string_none_for_python_only() -> None:
    assert cli_to_invocation_string("internal-thing", None) is None
