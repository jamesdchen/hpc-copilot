"""Sub-skill return file primitives — ``emit-skill-return`` / ``fetch-skill-return``.

WS2 of the determinism migration. Replaces the Skill-tool-result-as-chat-message
return mechanism (which fires an end-of-turn signal that stalls parent skills
mid-procedure) with an atomic file write into
``<experiment_dir>/.hpc/_returns/<skill>.json`` that the parent reads after the
sub-skill returns.

Per-skill schemas live in ``hpc_agent/schemas/skill_returns/<skill>.json``; each
is a ``oneOf: [Success, Error]`` with Success required-fields derived from the
sub-skill's final-step "Return …" contract and Error inheriting the standard
:class:`hpc_agent.schemas.envelope.json` ``ErrorEnvelope`` shape.

The pair is symmetric:

* **emit-skill-return** — the sub-skill, as its FINAL tool call: write the
  envelope to ``<exp>/.hpc/_returns/<skill>.staged.json`` first, then invoke
  this verb. It validates the staged file against the per-skill schema and
  atomically renames to ``<skill>.json``. Schema-fail surfaces as a
  ``spec_invalid`` envelope naming the schema path + failing JSON path; the
  staged file is preserved for debugging.
* **fetch-skill-return** — the parent skill, immediately after the
  ``Skill(<sub>)`` tool call returns. It reads ``<exp>/.hpc/_returns/<skill>.json``,
  re-validates, prints the envelope to stdout, and (by default) deletes it.
  A missing file surfaces as a typed ``precondition_failed`` envelope
  with ``error_class_raw == "skill_return_missing"`` so the parent can branch
  on that specific failure shape without parsing remediation prose.

The chosen schema-dir convention (``schemas/skill_returns/<skill>.json``) keeps
each sub-skill's return contract next to its peers and out of the per-primitive
``schemas/*.output.json`` namespace.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
from importlib.resources import as_file
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.cli._helpers import (
    EXIT_OK,
    _emit,
    _err,
)

# Single source of truth for every skill that emits a return envelope.
# A skill name appearing here must have a matching schema file at
# ``hpc_agent/schemas/skill_returns/<name>.json``. The CLI verbs reject any
# other ``--skill`` value with ``spec_invalid: unknown_skill``.
_KNOWN_SKILLS: tuple[str, ...] = (
    "hpc-wrap-entry-point",
    "hpc-classify-axis",
    "hpc-build-executor",
    "hpc-status",
    "hpc-aggregate",
)

# Skill names are lowercase letters / digits / hyphens — mirrors the
# ``describe`` verb's name rule. Used to reject path-traversal attempts
# in ``--skill`` before we touch the filesystem.
_SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _validate_skill_name(skill: str) -> str | None:
    """Return a remediation string if *skill* is invalid; ``None`` if OK."""
    if not _SKILL_NAME_RE.match(skill):
        return (
            f"skill name {skill!r} must be lowercase letters, digits, and "
            "hyphens — a sub-skill that emits a return envelope"
        )
    if skill not in _KNOWN_SKILLS:
        return (
            f"skill name {skill!r} is not a registered sub-skill. "
            f"Known: {', '.join(_KNOWN_SKILLS)}."
        )
    return None


def _returns_dir(experiment_dir: Path) -> Path:
    """Return ``<experiment>/.hpc/_returns`` — created on demand by the emitter."""
    return experiment_dir.expanduser().resolve() / ".hpc" / "_returns"


def _staged_path(experiment_dir: Path, skill: str) -> Path:
    return _returns_dir(experiment_dir) / f"{skill}.staged.json"


def _committed_path(experiment_dir: Path, skill: str) -> Path:
    return _returns_dir(experiment_dir) / f"{skill}.json"


def _schema_resource_name(skill: str) -> str:
    """Return the schema basename — e.g. ``"hpc-classify-axis.json"``."""
    return f"{skill}.json"


def _load_skill_schema(skill: str) -> tuple[dict[str, Any], str]:
    """Load the per-skill schema and return ``(schema_dict, displayed_path)``.

    *displayed_path* is what we show in error remediations — the schema's
    canonical on-disk location, not the importlib.resources internal handle.
    """
    pkg = _resource_files("hpc_agent.schemas") / "skill_returns"
    schema_res = pkg / _schema_resource_name(skill)
    schema_text = schema_res.read_text(encoding="utf-8")
    schema = json.loads(schema_text)
    # ``as_file`` materializes a stable filesystem path for editor / human use
    # — when the package is installed as a zip, this is the temp-extract; in
    # a source/editable install it's the in-tree path. Either way it points
    # at the file the operator should open to read the contract.
    with as_file(schema_res) as path_obj:
        displayed = (
            f"hpc_agent/schemas/skill_returns/{_schema_resource_name(skill)} (resolved: {path_obj})"
        )
    return schema, displayed


def _validate_envelope(envelope: Any, schema: dict[str, Any]) -> tuple[bool, str, str]:
    """Validate *envelope* against *schema*.

    Returns ``(ok, message, json_path)``. ``ok=True`` → empty message + path;
    ``ok=False`` → ``message`` is the jsonschema validator message and
    ``json_path`` is the absolute path inside the envelope where validation
    failed (``"<root>"`` for top-level mismatches).
    """
    try:
        import jsonschema
    except ImportError:
        # Hard dep per pyproject.toml; absence is a packaging bug, not a
        # silent no-op. Surface as a validation failure so the parent
        # doesn't act on an unvalidated envelope.
        return (False, "jsonschema not installed (packaging error)", "<root>")

    from hpc_agent._kernel.contract.schema import schema_registry

    validator = jsonschema.Draft202012Validator(schema, registry=schema_registry())
    try:
        validator.validate(envelope)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        return (False, exc.message, path)
    return (True, "", "")


def _atomic_rename(src: Path, dst: Path) -> None:
    """Rename *src* over *dst* atomically on POSIX and Windows.

    ``Path.replace`` is the cross-platform atomic-on-same-filesystem rename
    (``os.replace`` on both). We never cross filesystems here — staged and
    committed both live under ``.hpc/_returns/``.
    """
    os.replace(src, dst)


# ─── emit-skill-return ──────────────────────────────────────────────────────


def _cmd_emit_skill_return(args: argparse.Namespace) -> int:
    skill: str = args.skill
    experiment_dir: Path = Path(args.experiment_dir).expanduser()

    name_err = _validate_skill_name(skill)
    if name_err is not None:
        return _err(
            error_code="spec_invalid",
            message=name_err,
            category="user",
            retry_safe=False,
        )

    staged = _staged_path(experiment_dir, skill)
    committed = _committed_path(experiment_dir, skill)

    if not staged.exists():
        return _err(
            error_code="precondition_failed",
            message=(
                f"staged return envelope not found at {staged}. The sub-skill "
                f"must Write the envelope JSON to {staged.name} BEFORE invoking "
                f"`hpc-agent emit-skill-return --skill {skill}`."
            ),
            category="user",
            retry_safe=False,
            remediation=(
                "Order of operations: (1) Write the envelope to "
                f"<experiment_dir>/.hpc/_returns/{skill}.staged.json; "
                f"(2) Bash `hpc-agent emit-skill-return --skill {skill} "
                "--experiment-dir <exp>` as the FINAL tool call."
            ),
        )

    try:
        envelope = json.loads(staged.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _err(
            error_code="spec_invalid",
            message=f"staged envelope at {staged} is not valid JSON: {exc}",
            category="user",
            retry_safe=False,
            remediation=(
                "Rewrite the staged file with valid JSON. The staged file is "
                "preserved as-is for debugging — re-invoke this verb after fixing."
            ),
        )

    try:
        schema, schema_display = _load_skill_schema(skill)
    except (FileNotFoundError, OSError) as exc:
        # Should never happen — _KNOWN_SKILLS and the schemas dir must stay
        # in lock-step (tests/cli/test_skill_returns.py pins this).
        return _err(
            error_code="internal",
            message=f"return-envelope schema for skill {skill!r} not found in package data: {exc}",
            category="internal",
            retry_safe=False,
        )

    ok, msg, json_path = _validate_envelope(envelope, schema)
    if not ok:
        return _err(
            error_code="spec_invalid",
            message=(
                f"staged envelope for skill {skill!r} failed schema validation "
                f"at {json_path}: {msg}"
            ),
            category="user",
            retry_safe=False,
            remediation=(
                f"Inspect the schema: {schema_display}. "
                f"Failing JSON path inside the envelope: {json_path}. "
                f"The staged file is preserved at {staged} for debugging."
            ),
        )

    # Schema OK → atomic-rename. ``Path.replace`` (os.replace) is atomic on
    # the same filesystem on both POSIX and Windows.
    _atomic_rename(staged, committed)

    _emit(
        {
            "ok": True,
            "idempotent": True,
            "data": {
                "skill": skill,
                "path": str(committed),
                "validated": True,
            },
        }
    )
    return EXIT_OK


@primitive(
    name="emit-skill-return",
    verb="mutate",
    side_effects=[SideEffect("filesystem", "<experiment_dir>/.hpc/_returns/")],
    idempotent=True,
    idempotency_key="experiment_dir",
    cli=CliShape(
        help=(
            "Validate the staged sub-skill return envelope at "
            "<experiment_dir>/.hpc/_returns/<skill>.staged.json against the "
            "per-skill schema, then atomically rename to "
            "<skill>.json. Use as the sub-skill's FINAL tool call to "
            "hand off to the parent skill without firing an end-of-turn "
            "chat message."
        ),
        experiment_dir_arg=True,
        args=(
            CliArg(
                "--skill",
                required=True,
                help=(
                    "Sub-skill name (one of: hpc-wrap-entry-point, "
                    "hpc-classify-axis, hpc-build-executor, hpc-status, "
                    "hpc-aggregate)."
                ),
            ),
        ),
        handler=_cmd_emit_skill_return,
    ),
    agent_facing=True,
)
def emit_skill_return(*, skill: str, experiment_dir: str | Path) -> dict[str, Any]:
    """Python-side entrypoint mirroring the CLI verb.

    Validates ``<experiment_dir>/.hpc/_returns/<skill>.staged.json`` and
    renames it to ``<skill>.json``. Returns the same data dict the CLI
    emits as ``envelope.data`` on success. Raises :class:`ValueError` on
    schema failure (CLI surfaces it as a ``spec_invalid`` envelope).
    """
    exp = Path(experiment_dir).expanduser()
    name_err = _validate_skill_name(skill)
    if name_err is not None:
        raise ValueError(name_err)
    staged = _staged_path(exp, skill)
    committed = _committed_path(exp, skill)
    if not staged.exists():
        raise FileNotFoundError(f"staged return envelope not found at {staged}")
    envelope = json.loads(staged.read_text(encoding="utf-8"))
    schema, _ = _load_skill_schema(skill)
    ok, msg, json_path = _validate_envelope(envelope, schema)
    if not ok:
        raise ValueError(f"envelope failed schema validation at {json_path}: {msg}")
    _atomic_rename(staged, committed)
    return {"skill": skill, "path": str(committed), "validated": True}


# ─── fetch-skill-return ─────────────────────────────────────────────────────


def _cmd_fetch_skill_return(args: argparse.Namespace) -> int:
    skill: str = args.skill
    experiment_dir: Path = Path(args.experiment_dir).expanduser()
    clear: bool = not getattr(args, "no_clear", False)

    name_err = _validate_skill_name(skill)
    if name_err is not None:
        return _err(
            error_code="spec_invalid",
            message=name_err,
            category="user",
            retry_safe=False,
        )

    committed = _committed_path(experiment_dir, skill)
    if not committed.exists():
        # Typed "missing" envelope — the parent skill can branch on
        # ``failure_features.error_class_raw == "skill_return_missing"``
        # without parsing the remediation prose.
        return _err(
            error_code="precondition_failed",
            message=(
                f"no return envelope at {committed}. The sub-skill "
                f"{skill!r} did not run, or it crashed before emitting its "
                "final envelope."
            ),
            category="user",
            retry_safe=False,
            remediation=(
                "Re-invoke the sub-skill; on its final step it MUST run "
                f"`hpc-agent emit-skill-return --skill {skill}` to commit "
                "the staged envelope. Check whether a "
                f".staged.json sibling exists at "
                f"{_staged_path(experiment_dir, skill)} — its presence "
                "means the sub-skill staged the envelope but the emit "
                "verb either was never called or failed validation."
            ),
            failure_features={
                "error_class_raw": "skill_return_missing",
            },
        )

    try:
        envelope = json.loads(committed.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _err(
            error_code="spec_invalid",
            message=f"return envelope at {committed} is not valid JSON: {exc}",
            category="internal",
            retry_safe=False,
        )

    try:
        schema, schema_display = _load_skill_schema(skill)
    except (FileNotFoundError, OSError) as exc:
        return _err(
            error_code="internal",
            message=f"return-envelope schema for skill {skill!r} not found in package data: {exc}",
            category="internal",
            retry_safe=False,
        )

    ok, msg, json_path = _validate_envelope(envelope, schema)
    if not ok:
        # The emitter validates on the way in, so a fail here means the
        # file was hand-edited or the schema bumped. Refuse rather than
        # surface a possibly-invalid payload to the parent.
        return _err(
            error_code="spec_invalid",
            message=(
                f"return envelope at {committed} for skill {skill!r} no "
                f"longer matches its schema at {json_path}: {msg}"
            ),
            category="internal",
            retry_safe=False,
            remediation=(
                f"Inspect the schema: {schema_display}. "
                f"Failing JSON path inside the envelope: {json_path}. "
                "The file may have been hand-edited or the per-skill schema "
                "bumped — re-invoke the sub-skill to regenerate it."
            ),
        )

    # Print the validated envelope verbatim — this IS the envelope the
    # sub-skill emitted; the parent reads it from this stdout.
    print(json.dumps(envelope, sort_keys=True), flush=True)

    if clear:
        # Don't bury a successful read on a Windows file-lock race; the
        # next emit overwrites via atomic rename anyway.
        with contextlib.suppress(OSError):
            committed.unlink()
    return EXIT_OK


@primitive(
    name="fetch-skill-return",
    verb="query",
    side_effects=[SideEffect("filesystem", "<experiment_dir>/.hpc/_returns/")],
    idempotent=True,
    cli=CliShape(
        help=(
            "Read, re-validate, and emit the committed sub-skill return "
            "envelope at <experiment_dir>/.hpc/_returns/<skill>.json — the "
            "parent skill's seam after the Skill(<sub>) tool call returns. "
            "Deletes the file after reading unless --no-clear is set."
        ),
        experiment_dir_arg=True,
        args=(
            CliArg(
                "--skill",
                required=True,
                help=(
                    "Sub-skill name (one of: hpc-wrap-entry-point, "
                    "hpc-classify-axis, hpc-build-executor, hpc-status, "
                    "hpc-aggregate)."
                ),
            ),
            CliArg(
                "--no-clear",
                action="store_true",
                help=(
                    "Leave the committed envelope on disk after reading "
                    "(default: delete). Useful when the same envelope "
                    "must be inspected by multiple consumers."
                ),
            ),
        ),
        handler=_cmd_fetch_skill_return,
    ),
    agent_facing=True,
)
def fetch_skill_return(
    *, skill: str, experiment_dir: str | Path, clear: bool = True
) -> dict[str, Any]:
    """Python-side entrypoint mirroring the CLI verb.

    Returns the validated envelope dict. Raises :class:`FileNotFoundError`
    when no committed envelope exists and :class:`ValueError` on a schema
    mismatch.
    """
    exp = Path(experiment_dir).expanduser()
    name_err = _validate_skill_name(skill)
    if name_err is not None:
        raise ValueError(name_err)
    committed = _committed_path(exp, skill)
    if not committed.exists():
        raise FileNotFoundError(f"no return envelope at {committed}")
    envelope: dict[str, Any] = json.loads(committed.read_text(encoding="utf-8"))
    schema, _ = _load_skill_schema(skill)
    ok, msg, json_path = _validate_envelope(envelope, schema)
    if not ok:
        raise ValueError(f"envelope failed schema validation at {json_path}: {msg}")
    if clear:
        with contextlib.suppress(OSError):
            committed.unlink()
    return envelope


__all__ = [
    "emit_skill_return",
    "fetch_skill_return",
]
