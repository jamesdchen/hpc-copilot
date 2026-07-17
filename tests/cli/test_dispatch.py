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
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from hpc_agent import errors
from hpc_agent._kernel.registry import primitive as _prim_mod
from hpc_agent._kernel.registry.primitive import primitive, register_primitives
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
    envelope: dict[str, Any] = json.loads(lines[0])
    return envelope


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


def test_spec_arg_accepts_explicit_empty_object_for_all_optional_model(
    capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """A supplied ``--spec`` containing a literal ``{}`` is a VALID spec for an
    all-optional model — it must not misfire the "--spec is required" guard.

    The doctor-install repro (2026-07-04): ``hpc-agent doctor-install --spec
    <file with {}>`` was rejected with "--spec is required" because the loaded
    empty dict is falsy — the guard keys on the path now, not the dict.
    """

    class _AllOptional(BaseModel):
        notify: bool = True
        interval_minutes: int = 15

    spec_file = tmp_path / "spec.json"
    spec_file.write_text("{}", encoding="utf-8")

    @primitive(
        name="syn-all-optional",
        verb="query",
        cli=CliShape(help="All-optional spec.", spec_arg=True, spec_model=_AllOptional),
    )
    def syn_all_optional(*, spec: _AllOptional) -> dict[str, Any]:
        return {"notify": spec.notify, "interval_minutes": spec.interval_minutes}

    ns = argparse.Namespace(spec=spec_file)
    assert dispatch_primitive("syn-all-optional", ns) == 0
    env = _capsys_envelope(capsys.readouterr())
    assert env["data"] == {"notify": True, "interval_minutes": 15}


def test_spec_arg_explicit_empty_object_still_rejects_required_fields(
    capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """An explicit ``{}`` against a model WITH required fields fails with the
    real field error (model validation), not the misleading "--spec is
    required"."""
    spec_file = tmp_path / "spec.json"
    spec_file.write_text("{}", encoding="utf-8")

    @primitive(
        name="syn-empty-required",
        verb="query",
        cli=CliShape(help="Required fields.", spec_arg=True, spec_model=_Spec),
    )
    def syn_empty_required(*, spec: _Spec) -> dict[str, Any]:
        return {"name": spec.name}

    ns = argparse.Namespace(spec=spec_file)
    rc = dispatch_primitive("syn-empty-required", ns)
    assert rc == 1
    env = _capsys_envelope(capsys.readouterr())
    assert env["ok"] is False
    assert "--spec is required" not in env["message"]
    assert "name" in env["message"]  # the real missing-field diagnosis


def _register_syn_inline() -> None:
    @primitive(
        name="syn-inline",
        verb="query",
        cli=CliShape(help="Spec via file path.", spec_arg=True, spec_model=_Spec),
    )
    def syn_inline(*, spec: _Spec) -> dict[str, Any]:
        return {"name": spec.name}


@pytest.mark.parametrize(
    "inline",
    [
        '{"run_id": "pi-estimation"}',
        '  ["a", "b"]',
    ],
)
def test_spec_arg_inline_json_gets_friendly_spec_invalid(
    capsys: pytest.CaptureFixture[str], inline: str
) -> None:
    """Inline JSON passed to ``--spec`` yields the ``spec_invalid`` envelope,
    never a raw ``internal`` OSError.

    The proving-run-3 papercut (2026-07-04): on Windows,
    ``Path('{"run_id": ...}').read_text()`` raises OSError(22) — not
    FileNotFoundError — because ``"`` and ``:`` are invalid path characters,
    and the unclassified OSError escaped ``_load_spec`` as an ``internal``
    envelope for ``wait-detached`` while ``submit-s1`` happened to hit the
    friendly branch. One loader now classifies brace/bracket-leading args
    before touching the filesystem, on every platform.
    """
    _register_syn_inline()
    ns = argparse.Namespace(spec=Path(inline))
    rc = dispatch_primitive("syn-inline", ns)
    assert rc == 1  # EXIT_USER_ERROR, never EXIT_INTERNAL
    env = _capsys_envelope(capsys.readouterr())
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    assert env["category"] == "user"
    assert "FILE PATH" in env["message"]
    assert "run_id" in env["message"] or '"a"' in env["message"]  # echoes the arg
    assert "Write the JSON to a file" in env["message"]


def test_spec_arg_inline_json_message_truncates_long_payloads(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_syn_inline()
    inline = json.dumps({"run_id": "x" * 500})
    ns = argparse.Namespace(spec=Path(inline))
    assert dispatch_primitive("syn-inline", ns) == 1
    env = _capsys_envelope(capsys.readouterr())
    assert env["error_code"] == "spec_invalid"
    assert "x" * 80 in env["message"]
    assert "x" * 200 not in env["message"]  # echoed preview is capped ~100 chars


def test_spec_arg_nonexistent_path_still_reports_file_not_found(
    capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """An ordinary missing path keeps the pinned ``file not found`` diagnosis."""
    _register_syn_inline()
    missing = tmp_path / "no-such-spec.json"
    ns = argparse.Namespace(spec=missing)
    rc = dispatch_primitive("syn-inline", ns)
    assert rc == 1
    env = _capsys_envelope(capsys.readouterr())
    assert env["error_code"] == "spec_invalid"
    assert "file not found" in env["message"]
    assert "no-such-spec.json" in env["message"]


def test_spec_arg_unreadable_path_is_user_error_not_internal(
    capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """Any other OSError from the spec path (here: a directory) maps to
    ``spec_invalid``, never an ``internal`` envelope."""
    _register_syn_inline()
    ns = argparse.Namespace(spec=tmp_path)  # a directory, not a file
    rc = dispatch_primitive("syn-inline", ns)
    assert rc == 1
    env = _capsys_envelope(capsys.readouterr())
    assert env["error_code"] == "spec_invalid"
    assert env["category"] == "user"


def test_spec_arg_utf16_file_is_user_error_not_internal(
    capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """A UTF-16 spec file (the classic PowerShell ``... > spec.json`` redirection
    on Windows writes UTF-16LE with a BOM) is a user file-encoding error, so it
    maps to ``spec_invalid`` with an encoding hint — never an ``internal``
    envelope from an uncaught UnicodeDecodeError."""
    _register_syn_inline()
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps({"run_id": "pi-estimation"}), encoding="utf-16")
    ns = argparse.Namespace(spec=spec_file)
    rc = dispatch_primitive("syn-inline", ns)
    assert rc == 1  # EXIT_USER_ERROR, never EXIT_INTERNAL
    env = _capsys_envelope(capsys.readouterr())
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    assert env["category"] == "user"
    assert "UTF-8" in env["message"]


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


def test_requires_ssh_is_not_a_hard_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``requires_ssh`` is declarative metadata, not a pre-flight gate.

    The dispatcher used to short-circuit a ``requires_ssh`` primitive with
    ``ssh_unreachable`` when no agent was reachable. That blocked valid
    IdentityFile-based auth, so the hard gate was removed — ``ssh_run`` uses
    ``BatchMode=yes`` (fails fast at the real connection) and a genuine auth
    failure is enriched with agent state in ``_err_from_hpc``. This test
    proves the primitive is now actually dispatched (here it raises its own
    error to prove it was reached) instead of being gated before it runs.
    """

    @primitive(
        name="syn-ssh",
        verb="query",
        cli=CliShape(help="Declares SSH.", requires_ssh=True),
    )
    def syn_ssh() -> dict[str, Any]:
        raise errors.SpecInvalid("primitive was reached — no hard SSH gate")

    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    rc = dispatch_primitive("syn-ssh", argparse.Namespace())
    env = _capsys_envelope(capsys.readouterr())
    assert env["ok"] is False
    # Reached the primitive (its own spec_invalid), NOT short-circuited
    # with a pre-flight ssh_unreachable.
    assert env["error_code"] == "spec_invalid"
    assert rc == 1  # spec_invalid → user → exit 1


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


def test_pydantic_validation_error_maps_to_spec_invalid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A raw pydantic ``ValidationError`` from a verb → spec_invalid / exit 1.

    ``pydantic.ValidationError`` does NOT subclass ``ValueError``; the fast-path
    latency work deferred its import out of ``dispatch`` module scope into the
    ``_invoke_parsed`` generic handler (isinstance branch). This guard fires if
    that restructure ever regresses the mapping (a raw ValidationError falling
    through to the internal / exit-3 last-resort clause).
    """
    from hpc_agent.cli import dispatch

    class _Model(BaseModel):
        n: int

    def _boom(_args: argparse.Namespace) -> int:
        _Model(n="not-an-int")  # type: ignore[arg-type]  # raises pydantic ValidationError
        return 0

    ns = argparse.Namespace(func=_boom)
    rc = dispatch._invoke_parsed(ns)
    assert rc == 1
    env = _capsys_envelope(capsys.readouterr())
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"


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

    # Phase 3: legacy fallback is gone. Tier 3 modules are still
    # registered though; that's fine — they live under their own
    # top-level verbs and don't collide with the synthetic ``syngrp``.
    from hpc_agent.cli.parser import build_parser

    parser = build_parser()

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
