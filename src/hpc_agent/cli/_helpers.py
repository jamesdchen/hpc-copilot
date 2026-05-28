"""Shared helpers for the ``hpc-agent`` CLI surface — the adapter contract.

This module is the **public adapter contract** for both host-internal CLI
modules (``hpc_agent.cli.*``) and external plugins. Plugins import these
symbols (``_ok``, ``_err``, ``_load_spec``, ``_require_ssh_agent``, …)
to build their own CLI subcommands — see ``hpc-agent-pro``'s
``register_cli`` for the pattern. The underscore prefix is historical;
**these are the extension SDK and rename will require a release**.

The helpers split into two boundaries that frame every cmd_*:

* **Input boundary** — ``_load_spec``, ``_validate_against_schema``,
  ``_require_ssh_agent``, ``_add_experiment_dir`` /
  ``_add_run_id`` / ``_add_spec_and_dry_run``. argparse args + JSON spec
  files → validated Python kwargs.
* **Output boundary** — ``_emit``, ``_ok``, ``_err``, ``_err_from_hpc``,
  ``EXIT_OK`` / ``EXIT_USER_ERROR`` / ``EXIT_CLUSTER_ERROR`` /
  ``EXIT_INTERNAL``. Primitive return value → JSON envelope on stdout.

A cmd_* is "input boundary → primitive → output boundary." The 80% of
adapters that fit this exact shape are candidates for the future
registry-driven dispatcher (``_dispatch.py``); the other 20% have real
branching logic and stay hand-written.
"""

from __future__ import annotations

import argparse
import functools
import json
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent.infra.ssh_agent import agent_available, agent_detail

EXIT_OK = 0
EXIT_USER_ERROR = 1
EXIT_CLUSTER_ERROR = 2
EXIT_INTERNAL = 3

# error_code → exit code mapping. Stable contract; documented in docs/reference/cli-spec.md.
_EXIT_CODE_BY_CATEGORY = {
    "user": EXIT_USER_ERROR,
    "cluster": EXIT_CLUSTER_ERROR,
    "network": EXIT_CLUSTER_ERROR,
    "internal": EXIT_INTERNAL,
}


# ─── envelope helpers ──────────────────────────────────────────────────────


def _emit(envelope: dict[str, Any]) -> None:
    """Print a single-line JSON envelope to stdout."""
    print(json.dumps(envelope, sort_keys=True), flush=True)


@functools.cache
def _meta_idempotent(name: str) -> bool:
    """Look up a primitive's idempotency declaration from the catalog.

    B4 rewire: callers used to hardcode ``_ok(idempotent=True/False, ...)``
    which forked the truth between the @primitive decorator (consumed by
    docs / lint) and the runtime envelope (consumed by caller policy).
    Routing through the catalog collapses both to the decorator.

    Cached because the catalog walks every primitive's frontmatter on
    first call. Falls back to True on miss (consistent with the
    pre-B4 default for query-style commands; the cross-validation test
    in tests/test_idempotency.py guards against silent drift).
    """
    try:
        from hpc_agent._kernel.registry.operations import operations_catalog

        for entry in operations_catalog():
            if entry.get("name") == name:
                return bool(entry.get("idempotent", True))
    except (LookupError, KeyError, FileNotFoundError):
        # Narrow catch so programmer errors (e.g. registry queried
        # before register_primitives()) surface in main() rather than
        # being silently coerced to ``idempotent=True``.
        pass
    return True


def _ok(
    data: dict[str, Any],
    *,
    idempotent: bool | None = None,
    name: str | None = None,
    partial_errors: list[dict[str, str]] | None = None,
) -> None:
    """Emit an ok-true envelope.

    *idempotent* (B4 rewire): preferred spelling is to pass *name* — the
    primitive's catalog name — and let the envelope read the
    ``idempotent`` flag from ``operations_catalog()``. The legacy
    ``idempotent=True/False`` kwarg is still honoured for callsites that
    don't have a primitive mapping (e.g. cmd_aggregate which wraps a
    pure mapreduce reduce). When both are supplied, the explicit kwarg
    wins so callers can opt out of the catalog lookup if needed.

    *partial_errors*: optional list of ``{code, detail}`` dicts surfaced
    at the top level of the envelope — distinct from any per-primitive
    error list that lives inside the ``data`` block.
    Used by primitives like ``inspect-cluster`` whose underlying data
    source can be partially degraded (qhost timed out, sacct
    unavailable) without the operation as a whole failing.
    """
    if idempotent is None:
        idempotent = _meta_idempotent(name) if name else True
    if name:
        from hpc_agent._kernel.contract.schema import validate_output

        validate_output(data, name)
    env: dict[str, Any] = {"ok": True, "idempotent": idempotent, "data": data}
    if partial_errors:
        env["partial_errors"] = list(partial_errors)
    _emit(env)


def _err(
    *,
    error_code: str,
    message: str,
    category: str,
    retry_safe: bool,
    remediation: str | None = None,
) -> int:
    payload = {
        "ok": False,
        "error_code": error_code,
        "message": message,
        "category": category,
        "retry_safe": retry_safe,
    }
    if remediation is not None:
        payload["remediation"] = remediation
    _emit(payload)
    return _EXIT_CODE_BY_CATEGORY.get(category, EXIT_INTERNAL)


def _err_from_hpc(exc: errors.HpcError) -> int:
    remediation = exc.remediation
    # No hard pre-flight agent gate any more: ``ssh_run`` uses
    # ``BatchMode=yes`` so a missing/usable-auth failure fails fast on its
    # own (no hang), and a precheck would reject valid IdentityFile-based
    # auth that needs no agent at all (submit-flow has always relied on
    # this). Instead, when an SSH op DOES fail and no agent is reachable,
    # append the agent state — the user keeps the actionable hint the old
    # precheck gave without the false negative. ``agent_detail()`` also
    # describes the Windows named-pipe agent (which never sets
    # SSH_AUTH_SOCK).
    if isinstance(exc, errors.SshUnreachable) and not agent_available():
        hint = (
            f"No SSH agent reachable ({agent_detail()}). If you authenticate "
            "via an IdentityFile in ~/.ssh/config this is fine; otherwise load "
            "a key — Unix/macOS: `ssh-add ~/.ssh/<key>` (and forward "
            "SSH_AUTH_SOCK into spawned envs); Windows: `Start-Service "
            "ssh-agent; ssh-add ~/.ssh/<key>`."
        )
        remediation = f"{remediation} {hint}" if remediation else hint
    return _err(
        error_code=exc.error_code,
        message=str(exc),
        category=exc.category,
        retry_safe=exc.retry_safe,
        remediation=remediation,
    )


# ─── shared option helpers ─────────────────────────────────────────────────


def _add_experiment_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=Path.cwd(),
        help="Path to the experiment repo (default: current working directory).",
    )


def _add_run_id(parser: argparse.ArgumentParser) -> None:
    """Add the canonical ``--run-id`` argument (always required)."""
    parser.add_argument("--run-id", required=True)


def _add_spec_and_dry_run(
    parser: argparse.ArgumentParser,
    *,
    schema_hint: str,
    dry_run_help: str,
) -> None:
    """Add the ``--spec`` (required) + ``--dry-run`` pair used by the
    workflow-flow subcommands (``submit-flow``, ``monitor-flow``,
    ``aggregate-flow``).

    *schema_hint* is the schema filename mentioned in the spec help
    (e.g. ``"schemas/submit_flow.input.json"``); *dry_run_help* lets
    each subcommand explain what dry-run skips.
    """
    parser.add_argument(
        "--spec",
        type=Path,
        required=True,
        help=f"JSON spec file ({schema_hint})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=dry_run_help,
    )


def _load_spec(spec_path: Path | None, *, schema_name: str | None = None) -> dict[str, Any]:
    """Load and (optionally) JSON-Schema-validate ``--spec`` input.

    Validation is opt-in via *schema_name* so callers without a matching
    schema (e.g. ad-hoc dicts) still work, but every CLI subcommand that
    has one in ``hpc_agent/schemas/<name>.input.json`` should pass
    it.  Validation failures map to ``SpecInvalid`` with the schema
    field path in the message — far more useful to a calling agent than
    the Python ``int("abc")`` traceback we used to surface.
    """
    if spec_path is None:
        return {}
    try:
        loaded = json.loads(spec_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise errors.SpecInvalid(f"--spec file not found: {spec_path}") from exc
    except json.JSONDecodeError as exc:
        raise errors.SpecInvalid(f"--spec is not valid JSON ({spec_path}): {exc}") from exc
    if not isinstance(loaded, dict):
        raise errors.SpecInvalid(f"--spec must be a JSON object; got {type(loaded).__name__}")
    if schema_name is not None:
        _validate_against_schema(loaded, schema_name)
    return loaded


def _validate_against_schema(payload: Any, schema_name: str) -> None:
    """Validate *payload* against ``hpc_agent/schemas/<schema_name>.input.json``.

    Raises :class:`errors.SpecInvalid` on schema mismatch.  When the
    ``jsonschema`` library is unavailable (older installs that haven't
    picked up the runtime dep), this falls back to a no-op so the CLI
    keeps working — schema validation is defence in depth, not the only
    line of defence (``submit_and_record`` etc. still validate inputs).

    Cross-file ``$ref`` (rare post-Pydantic-migration — most
    schemas are now self-contained with constraints inlined from
    :mod:`hpc_agent._wire._shared`) resolves through the
    shared registry in :mod:`hpc_agent._kernel.contract.schema`.
    """
    try:
        import jsonschema  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        # Warn once so missing-dep installs (minimal venv, broken pip
        # state) don't silently bypass the defence-in-depth layer. The
        # Pydantic-driven inner validation still runs.
        import warnings as _warnings

        _warnings.warn(
            "jsonschema not installed; skipping wire-schema validation. "
            "Install with `pip install hpc-agent[<extras>]` or `pip install jsonschema>=4.18`.",
            RuntimeWarning,
            stacklevel=2,
        )
        return
    # Search core first, then known plugin schema roots. A pro-only
    # primitive (e.g. ``predict_queue_wait``, ``run_pre_submit_gates``)
    # has its schema under ``hpc_agent_pro.schemas/``; the bare
    # ``hpc_agent.schemas`` lookup would silently no-op and the
    # defence-in-depth layer would never fire for any pro primitive.
    schema_text: str | None = None
    for pkg in ("hpc_agent.schemas", "hpc_agent_pro.schemas"):
        try:
            schema_text = (_resource_files(pkg) / f"{schema_name}.input.json").read_text(
                encoding="utf-8"
            )
            break
        except (FileNotFoundError, ModuleNotFoundError):
            continue
    if schema_text is None:
        return
    schema = json.loads(schema_text)
    from hpc_agent._kernel.contract.schema import validate as _validate

    try:
        _validate(payload, schema)
    except jsonschema.ValidationError as exc:
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        raise errors.SpecInvalid(
            f"--spec failed schema {schema_name}.input.json at {path}: {exc.message}"
        ) from exc
