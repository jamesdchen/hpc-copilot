"""``hpc-agent`` CLI entry point — argparse orchestration + dispatch.

This module is the canonical home for the top-level CLI orchestrator:

* :func:`main` — the ``hpc-agent`` console-script entry point
  (registered via ``pyproject.toml [project.scripts]``).
* :func:`build_parser` — public alias for the registry-driven argparse
  tree, delegating to :func:`hpc_agent.cli.parser.build_parser`.
* :func:`_strip_verb_group` / :func:`_print_group_help` — argv
  preprocessing for verb groups (``hpc-agent build build-executor`` →
  ``hpc-agent build-executor``).
* :func:`_live_subcommands` — introspects the live argparse tree, used
  by ``capabilities`` to enumerate verbs.

Stdout/stderr/exit-code conventions:

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
from hpc_agent.cli._helpers import _err_from_hpc


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
