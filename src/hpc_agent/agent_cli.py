"""Command-line interface — the agent surface.

Phase-3 thin shell. The bulk of CLI behaviour now lives in
:mod:`hpc_agent.cli`:

* Per-primitive CLI shape lives on the ``@primitive`` decorator in
  ``atoms/<x>.py`` as a :class:`hpc_agent.cli._dispatch.CliShape`. The
  registry walk in :func:`hpc_agent.cli.parser.build_parser`
  auto-registers each verb.
* Tier-3 verbs (``run``, ``capabilities``, ``install-commands``,
  ``setup``, ``describe``) have no ``@primitive`` backing and live in
  ``cli/<module>.py`` with a ``register(sub)`` function aggregated by
  :func:`hpc_agent.cli.parser._register_tier3_modules`.
* Tier-2 hand-written ``cmd_*`` adapters (``cmd_status``, ``cmd_submit``,
  ``cmd_aggregate``, etc.) live next to their domain in
  ``cli/<module>.py`` and are reached via :class:`CliShape.handler`.

Conventions are unchanged:

- Stdout is exclusively a single-line JSON envelope. Exception:
  ``capabilities --full`` emits a plain-text ``llms-full`` dump.
- Stderr carries free-form diagnostic prose; do not parse it as JSON.
- Exit codes: 0 success, 1 user error, 2 cluster/network error, 3 internal.
- Every subcommand accepts ``--experiment-dir`` (defaults to CWD).
- Subcommands with non-trivial inputs accept ``--spec path/to/spec.json``.

The full schema for each subcommand is documented in
``docs/reference/cli-spec.md`` and shipped as JSON Schema files under
``hpc_agent/schemas/``.
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys

from pydantic import ValidationError

from hpc_agent import errors

# ─── adapter SDK ───────────────────────────────────────────────────────────
#
# Helpers + EXIT codes live in :mod:`hpc_agent.cli._helpers` (the
# canonical home). Re-exported here so existing imports keep working —
# the ``hpc-agent-pro`` plugin and a handful of tests do
# ``from hpc_agent.agent_cli import _ok, _load_spec, ...``. New code
# imports from ``hpc_agent.cli._helpers`` directly.
from hpc_agent.cli._helpers import (  # noqa: F401 — re-exported public surface
    _EXIT_CODE_BY_CATEGORY,
    EXIT_CLUSTER_ERROR,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_USER_ERROR,
    _add_experiment_dir,
    _add_run_id,
    _add_spec_and_dry_run,
    _emit,
    _err,
    _err_from_hpc,
    _load_spec,
    _meta_idempotent,
    _ok,
    _require_ssh_agent,
    _validate_against_schema,
)

# ─── per-domain cmd_* re-exports (back-compat for tests + plugins) ─────────
#
# Every legacy ``cmd_*`` adapter moved to its per-domain module under
# ``hpc_agent.cli/<module>.py``. We re-export the symbols here so
# existing call sites (``from hpc_agent.agent_cli import cmd_setup``)
# keep resolving. New code imports from ``hpc_agent.cli.<module>``
# directly.
from hpc_agent.cli.aggregate import cmd_aggregate  # noqa: E402, F401
from hpc_agent.cli.lifecycle import (  # noqa: E402, F401
    _preempted_summary_from_sidecar,
    cmd_status,
)
from hpc_agent.cli.recover import (  # noqa: E402, F401
    _VALID_RESUBMIT_CATEGORIES,
    cmd_resubmit,
)
from hpc_agent.cli.setup import (  # noqa: E402, F401
    cmd_capabilities,
    cmd_describe,
    cmd_install_commands,
    cmd_setup,
)
from hpc_agent.cli.spawn import cmd_run  # noqa: E402, F401
from hpc_agent.cli.submit import (  # noqa: E402, F401
    cmd_submit,
    cmd_submit_flow,
    cmd_submit_flow_batch,
)

# Helper re-exports for legacy import paths in tests + the pro plugin.
from hpc_agent.ops.monitor.list_in_flight import _last_status_age_seconds  # noqa: E402, F401

# Back-compat shims for tests that still reach for a ``cmd_*`` name on
# the legacy module. Tier 1 primitives flow through the dispatcher; the
# shim is a one-line delegation so the test surface keeps working.


def cmd_logs(args: argparse.Namespace) -> int:
    """Dispatcher shim — back-compat for tests that call ``cli.cmd_logs``."""
    from hpc_agent.cli._dispatch import dispatch_primitive

    return dispatch_primitive("logs", args)


def _live_subcommands() -> list[str]:
    """Derive the subcommand list from the actual argparse tree.

    Walks the parser the dispatcher would build and returns the sorted
    set of top-level subcommand names. Used by
    :func:`hpc_agent.cli.setup.cmd_capabilities` to assemble the
    ``capabilities`` envelope and by tests that introspect the verb
    surface.
    """
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return sorted(action.choices)
    return []


def build_parser() -> argparse.ArgumentParser:
    """Public entry point — delegates to the registry-driven orchestrator.

    Every verb is registered via either the registry walk (CliShape on
    a ``@primitive`` decorator) or a Tier 3 module's ``register(sub)``.
    The legacy hand-written ``add_parser`` body is gone.
    """
    from hpc_agent.cli.parser import build_parser as _build_parser

    return _build_parser()


# ─── verb-group argv preprocessor ──────────────────────────────────────────
#
# ``hpc-agent build build-executor <args>`` strips the ``build`` prefix
# before argparse sees it. The flat form (``hpc-agent build-executor
# <args>``) keeps working — both routes hit the same handler.

_VERB_GROUPS: dict[str, frozenset[str]] = {
    "validate": frozenset({"validate-campaign"}),
    "build": frozenset(
        {
            "axes-init",
            "build-executor",
            "build-submit-spec",
            "build-tasks-py",
            "build-template",
        }
    ),
}


def _print_group_help(group: str) -> None:
    """List the subcommands belonging to a verb group, one per line."""
    members = sorted(_VERB_GROUPS[group])
    print(f"hpc-agent {group} <subcommand>", file=sys.stderr)
    print(f"\nSubcommands ({len(members)}):", file=sys.stderr)
    for cmd in members:
        print(f"  hpc-agent {group} {cmd}", file=sys.stderr)
    print(
        "\nFlat form also works: ``hpc-agent <subcommand>``. "
        "Pass ``--help`` to any subcommand for arguments.",
        file=sys.stderr,
    )


def _strip_verb_group(argv: list[str]) -> list[str]:
    """If argv[0] names a verb group, strip it (or print group help)."""
    if not argv or argv[0] not in _VERB_GROUPS:
        return argv
    group = argv[0]
    if len(argv) == 1 or argv[1] in {"-h", "--help"}:
        _print_group_help(group)
        raise SystemExit(0)
    if argv[1] in _VERB_GROUPS[group]:
        return argv[1:]
    print(
        f"hpc-agent: {argv[1]!r} is not in the {group!r} group.",
        file=sys.stderr,
    )
    _print_group_help(group)
    raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page (cp1252) whose codec
    # cannot encode the ``→`` and box-drawing characters in our --help
    # text and catalog tables, raising UnicodeEncodeError on print_help().
    # Force UTF-8 on the std streams up front.
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                _reconfigure(encoding="utf-8")

    # Populate the primitive registry once before any subcommand dispatch.
    from hpc_agent._kernel.registry.primitive import register_primitives

    register_primitives()
    if argv is None:
        argv = sys.argv[1:]
    argv = _strip_verb_group(argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        rc: int = args.func(args)
        return rc
    except errors.HpcError as exc:
        return _err_from_hpc(exc)
    except subprocess.TimeoutExpired as exc:
        return _err_from_hpc(
            errors.ClusterTimeout(
                f"scheduler subprocess timed out after {exc.timeout}s: {exc.cmd!r}"
            )
        )
    except ValidationError as exc:
        # pydantic v2 ``ValidationError`` does NOT subclass ``ValueError``;
        # without this clause a malformed --spec would fall through to the
        # generic handler and be mislabelled internal / exit 3.
        return _err_from_hpc(errors.SpecInvalid(str(exc)))
    except ValueError as exc:
        return _err_from_hpc(errors.SpecInvalid(str(exc)))
    except Exception as exc:  # noqa: BLE001 — last-resort envelope
        return _err_from_hpc(errors.HpcError(f"{type(exc).__name__}: {exc}"))


if __name__ == "__main__":
    sys.exit(main())
