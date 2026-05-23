"""Registry-driven CLI dispatcher.

This module defines :class:`CliShape` (the per-primitive CLI declaration
that lives on the ``@primitive`` decorator in ``atoms/<x>.py``) and
:func:`dispatch_primitive` (the generic adapter that reads a primitive's
``CliShape`` from the registry, builds kwargs from an
``argparse.Namespace``, invokes the primitive, and emits a JSON
envelope on stdout).

Eight hooks cover ~82% of adapter shapes (see the migration plan for
the classification):

* ``spec_arg`` — inject ``--spec`` and load+validate+model_validate.
* ``experiment_dir_arg`` — inject ``--experiment-dir`` (default cwd).
* ``dry_run_arg`` + ``dry_run_passthrough_keys`` — inject ``--dry-run``
  and short-circuit to a "what would run" envelope.
* ``requires_ssh`` — gate the call via :func:`_require_ssh_agent`.
* ``args`` — per-primitive flag declarations (``CliArg``).
* ``arg_pre`` — pre-call hook to transform args into kwargs
  (e.g. parse ``--extra-env "k=v,k=v"`` into a dict).
* ``result_post`` — post-call hook to project the primitive's
  return value into the envelope ``data`` dict.
* ``handler`` — Tier 2 escape hatch; the dispatcher delegates wholly
  to a hand-written ``cmd_*`` function.

A primitive whose adapter doesn't fit even with rich hooks should
declare ``handler=cmd_<x>`` and the hand-written body lives in
``cli/<module>.py``.
"""

from __future__ import annotations

import argparse
import dataclasses
import inspect
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent.cli._helpers import (
    EXIT_OK,
    _err_from_hpc,
    _load_spec,
    _ok,
    _require_ssh_agent,
    _validate_against_schema,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@dataclasses.dataclass(frozen=True)
class CliArg:
    """One argparse argument declaration carried on a primitive's :class:`CliShape`.

    Mirrors the subset of ``argparse.ArgumentParser.add_argument`` we
    actually use across the CLI surface. ``dest`` defaults to the flag
    name with leading ``--`` stripped and ``-`` replaced by ``_``
    (the argparse default), so most CliArg declarations omit it.
    """

    flag: str
    type: Any = str
    default: Any = None
    required: bool = False
    help: str = ""
    action: str | None = None
    nargs: str | int | None = None
    choices: tuple[str, ...] | None = None
    dest: str | None = None

    def add_to(self, parser: argparse.ArgumentParser) -> None:
        """Add this argument to *parser* via ``parser.add_argument``."""
        kwargs: dict[str, Any] = {}
        if self.action is not None:
            # store_true / store_false don't take type / default kwargs.
            kwargs["action"] = self.action
            if self.default is not None:
                kwargs["default"] = self.default
        else:
            kwargs["type"] = self.type
            kwargs["default"] = self.default
        if self.required and not self.flag.startswith("-"):
            # Positional args don't take required=.
            pass
        elif self.required:
            kwargs["required"] = True
        if self.help:
            kwargs["help"] = self.help
        if self.nargs is not None:
            kwargs["nargs"] = self.nargs
        if self.choices is not None:
            kwargs["choices"] = list(self.choices)
        if self.dest is not None and self.flag.startswith("-"):
            kwargs["dest"] = self.dest
        parser.add_argument(self.flag, **kwargs)

    def attr_name(self) -> str:
        """Return the ``argparse.Namespace`` attribute for this arg."""
        if self.dest is not None:
            return self.dest
        # argparse derives dest from the longest flag, stripping leading
        # dashes and replacing remaining dashes with underscores.
        bare = self.flag.lstrip("-")
        return bare.replace("-", "_")


@dataclasses.dataclass(frozen=True)
class SchemaRef:
    """Schema reference for spec-loading and (future) output validation.

    ``input`` is the basename under ``hpc_agent/schemas/`` (no
    extension) — e.g. ``"submit_flow"`` resolves to
    ``schemas/submit_flow.input.json``. The dispatcher passes this to
    :func:`_validate_against_schema` after :func:`_load_spec`.
    """

    input: str | None = None
    output: str | None = None


@dataclasses.dataclass(frozen=True)
class CliShape:
    """Per-primitive CLI declaration; consumed by :func:`dispatch_primitive`.

    Lives on the ``@primitive`` decorator in ``atoms/<x>.py`` — the
    decorator metadata is the single source of truth (rather than
    forking a string description into a hand-written ``cmd_*``).
    """

    help: str
    # Standard flag injectors.
    spec_arg: bool = False
    experiment_dir_arg: bool = False
    dry_run_arg: bool = False
    requires_ssh: bool = False
    # Spec-loading details (companion to spec_arg).
    schema_ref: SchemaRef | None = None
    spec_model: Any = None  # Pydantic model class; model_validate applied
    spec_required: bool = True
    spec_kwarg: str = "spec"  # kwarg name to pass the loaded spec under
    # Per-primitive args.
    args: tuple[CliArg, ...] = ()
    # Dry-run short-circuit: when --dry-run is set, emit an envelope of
    # {key: kwargs[key], ..., "dry_run": True} and exit without calling
    # the primitive. The keys must be resolvable via spec attributes
    # (when spec_arg is set) or kwarg names.
    dry_run_passthrough_keys: tuple[str, ...] = ()
    # Rich hooks.
    arg_pre: Callable[[argparse.Namespace], dict[str, Any]] | None = None
    result_post: Callable[[Any], dict[str, Any]] | None = None
    # Tier 2 escape hatch.
    handler: Callable[[argparse.Namespace], int] | None = None
    # Verb-group support: when set, the primitive's CLI parser is nested
    # under a parent ``hpc-agent <group> ...`` subparser. The leaf verb
    # is the primitive name with ``f"{group}-"`` stripped, unless
    # ``verb`` overrides it.
    group: str | None = None
    verb: str | None = None
    # Whether the primitive accepts experiment_dir as a positional arg.
    # Most accept it as a keyword (the dispatcher always passes kwarg),
    # but a few atoms have signatures like ``f(experiment_dir, *, spec)``
    # which still work via kwarg call.


def _leaf_verb(primitive_name: str, shape: CliShape) -> str:
    """Return the verb name for *primitive_name* — the leaf under its group, if any."""
    if shape.verb is not None:
        return shape.verb
    if shape.group is not None and primitive_name.startswith(f"{shape.group}-"):
        return primitive_name[len(shape.group) + 1 :]
    return primitive_name


def cli_to_invocation_string(name: str, cli: Any) -> str | None:
    """Render a primitive's ``cli=`` declaration as a shell invocation string.

    Consumers that historically read ``meta.cli`` as a string (the
    operations catalog, the markdown frontmatter generator, the
    catalog-table renderer) flow through this so they don't have to
    branch on ``isinstance(cli, CliShape)``. Returns ``None`` when no
    CLI is declared, the legacy string verbatim when ``cli`` is a str,
    or a synthesized ``hpc-agent <group?> <verb> [flags]`` string for a
    :class:`CliShape`.
    """
    if cli is None:
        return None
    if isinstance(cli, str):
        return cli
    if not isinstance(cli, CliShape):
        return str(cli)
    parts = ["hpc-agent"]
    if cli.group:
        parts.append(cli.group)
    parts.append(_leaf_verb(name, cli))
    if cli.spec_arg:
        parts.append("--spec <path>")
    if cli.experiment_dir_arg:
        parts.append("[--experiment-dir <dir>]")
    if cli.dry_run_arg:
        parts.append("[--dry-run]")
    for arg in cli.args:
        token = arg.flag if arg.flag.startswith("-") else f"<{arg.flag}>"
        if arg.required and arg.flag.startswith("-"):
            parts.append(f"{token} <{arg.attr_name()}>")
        elif arg.flag.startswith("-"):
            if arg.action in {"store_true", "store_false"}:
                parts.append(f"[{token}]")
            else:
                parts.append(f"[{token} <{arg.attr_name()}>]")
        else:
            parts.append(token)
    return " ".join(parts)


def _build_kwargs(
    name: str, shape: CliShape, ns: argparse.Namespace, func: Any
) -> dict[str, Any]:
    """Build the kwarg dict to pass to *func*.

    The build order is: standard injectors (``experiment_dir`` from
    ``experiment_dir_arg``), then raw per-arg values pulled off the
    argparse namespace (so the primitive sees them as-is for the
    common case where a ``--foo-bar`` flag maps directly to a ``foo_bar``
    kwarg), then ``spec_arg`` loading, then ``arg_pre`` overrides
    (so a CliArg's raw value can be re-mapped to a different kwarg
    name or transformed into a richer Python type).

    Finally, kwargs are filtered to parameters the primitive's
    signature actually accepts. CLI-only flags (e.g. a primitive that
    expects ``roots`` but exposes ``--root`` on the CLI) are dropped
    after ``arg_pre`` has re-mapped them under the right kwarg name.
    The filter is skipped when the primitive declares ``**kwargs``.
    """
    kwargs: dict[str, Any] = {}
    if shape.experiment_dir_arg:
        kwargs["experiment_dir"] = ns.experiment_dir
    for arg in shape.args:
        attr = arg.attr_name()
        if hasattr(ns, attr):
            kwargs[attr] = getattr(ns, attr)
    if shape.spec_arg:
        spec_value = _load_and_model_validate_spec(name, shape, ns)
        kwargs[shape.spec_kwarg] = spec_value
    if shape.arg_pre is not None:
        extra = shape.arg_pre(ns)
        if extra:
            kwargs.update(extra)
    return _filter_to_signature(kwargs, func)


def _filter_to_signature(kwargs: dict[str, Any], func: Any) -> dict[str, Any]:
    """Drop kwargs the primitive doesn't accept (CLI-only flags consumed by arg_pre)."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return kwargs
    params = sig.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    accepted = {n for n, p in params.items() if p.kind is not inspect.Parameter.VAR_POSITIONAL}
    return {k: v for k, v in kwargs.items() if k in accepted}


def _load_and_model_validate_spec(
    name: str, shape: CliShape, ns: argparse.Namespace
) -> Any:
    """Load ``--spec`` from disk, optionally schema-validate, optionally model_validate."""
    spec_path: Path | None = getattr(ns, "spec", None)
    schema_name = shape.schema_ref.input if shape.schema_ref else None
    raw = _load_spec(spec_path, schema_name=None)
    if not raw and shape.spec_required:
        raise errors.SpecInvalid(f"--spec is required for `{name}`")
    if not isinstance(raw, dict):
        raise errors.SpecInvalid(
            f"--spec for `{name}` must be a JSON object; got {type(raw).__name__}"
        )
    if schema_name is not None:
        _validate_against_schema(raw, schema_name)
    if shape.spec_model is None:
        return raw
    try:
        return shape.spec_model.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError shape
        raise errors.SpecInvalid(str(exc)) from exc


def _coerce_result(result: Any) -> dict[str, Any]:
    """Project a primitive return value into the envelope ``data`` dict.

    Handles Pydantic models (``model_dump``), workflow result classes
    (``to_envelope_data``), and plain dicts. Anything else is wrapped as
    ``{"value": result}`` so the envelope shape stays uniform.
    """
    if isinstance(result, dict):
        return result
    if hasattr(result, "to_envelope_data"):
        return result.to_envelope_data()  # type: ignore[no-any-return]
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")  # type: ignore[no-any-return]
    return {"value": result}


def _emit_dry_run(name: str, shape: CliShape, kwargs: dict[str, Any]) -> int:
    """Emit the standard dry-run envelope and exit OK.

    Resolves each key in ``shape.dry_run_passthrough_keys`` from either
    the spec (when ``spec_arg`` is set) or the kwargs dict.
    """
    payload: dict[str, Any] = {"dry_run": True}
    spec_obj = kwargs.get(shape.spec_kwarg) if shape.spec_arg else None
    for key in shape.dry_run_passthrough_keys:
        if spec_obj is not None and hasattr(spec_obj, key):
            payload[key] = getattr(spec_obj, key)
        elif spec_obj is not None and isinstance(spec_obj, dict) and key in spec_obj:
            payload[key] = spec_obj[key]
        elif key in kwargs:
            payload[key] = kwargs[key]
    _ok(payload, name=name)
    return EXIT_OK


def dispatch_primitive(name: str, ns: argparse.Namespace) -> int:
    """Generic dispatcher — reads the registry, executes the primitive.

    1. Look up the primitive in the registry.
    2. If ``cli.handler`` is set → delegate (Tier 2 path).
    3. If ``cli.requires_ssh`` → gate via :func:`_require_ssh_agent`.
    4. Build kwargs (spec, experiment_dir, args, arg_pre).
    5. If ``--dry-run`` and ``dry_run_passthrough_keys`` → emit shape, exit.
    6. Call the primitive.
    7. Project result via ``result_post`` (or :func:`_coerce_result`),
       emit ``_ok`` envelope tagged with the primitive name.
    """
    from hpc_agent._internal.primitive import get_meta

    meta = get_meta(name)
    shape = meta.cli
    if not isinstance(shape, CliShape):
        raise TypeError(
            f"dispatch_primitive called for {name!r} but its cli= is "
            f"{type(shape).__name__}, not CliShape"
        )

    if shape.handler is not None:
        return shape.handler(ns)

    if shape.requires_ssh:
        rc = _require_ssh_agent()
        if rc is not None:
            return rc

    try:
        kwargs = _build_kwargs(name, shape, ns, meta.func)
    except errors.HpcError as exc:
        return _err_from_hpc(exc)

    if shape.dry_run_arg and getattr(ns, "dry_run", False) and shape.dry_run_passthrough_keys:
        return _emit_dry_run(name, shape, kwargs)

    try:
        result = meta.func(**kwargs)
    except errors.HpcError as exc:
        return _err_from_hpc(exc)

    data = shape.result_post(result) if shape.result_post is not None else _coerce_result(result)
    _ok(data, name=name)
    return EXIT_OK


__all__ = [
    "CliArg",
    "CliShape",
    "SchemaRef",
    "_leaf_verb",
    "cli_to_invocation_string",
    "dispatch_primitive",
]
