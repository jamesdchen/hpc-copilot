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


def _fast_dispatch_enabled() -> bool:
    """Whether the single-verb fast path may run (opt-out + plugin gate).

    Disabled by ``HPC_AGENT_NO_FAST_CLI=1`` (a field kill switch for the
    central path) and whenever any ``hpc_agent.plugins`` entry point is
    installed — a plugin may override or extend a core verb via
    ``register_cli``, which only the full :func:`build_parser` walk honours, so
    a plugin's mere presence forces every verb onto the full path. The
    entry-point scan is cheap and short-circuits under
    ``HPC_AGENT_DISABLE_PLUGINS=1``.
    """
    import os

    if os.environ.get("HPC_AGENT_NO_FAST_CLI") == "1":
        return False
    if os.environ.get("HPC_AGENT_DISABLE_PLUGINS") == "1":
        return True
    from importlib.metadata import entry_points

    try:
        return not list(entry_points(group="hpc_agent.plugins"))
    except Exception:  # noqa: BLE001 — a metadata hiccup must not break the CLI
        return False


def _try_fast_dispatch(argv: list[str]) -> int | None:
    """Dispatch a single known ungrouped verb without the full registry walk.

    Returns the process exit code on the fast path, or ``None`` to signal the
    caller to fall back to the full ``register_primitives`` + ``build_parser``
    path. Falls back (returns ``None``) for: an empty argv, a leading global
    flag (``--version`` / top-level ``--help``), a verb absent from the
    generated map (grouped verbs, Tier-3 ``run`` / ``mcp-serve``, unknown
    verbs), an installed plugin, or any stale-map miss. Every fallback path
    yields byte-identical behaviour to before — only speed differs.
    """
    if not argv or argv[0].startswith("-"):
        return None
    if not _fast_dispatch_enabled():
        return None
    from hpc_agent.cli._verb_module_map import VERB_MODULE_MAP

    entry = VERB_MODULE_MAP.get(argv[0])
    if entry is None:
        return None
    primitive_name, module_name = entry

    from hpc_agent._kernel.registry.primitive import register_single_module
    from hpc_agent.cli.parser import build_single_verb_parser

    register_single_module(module_name)
    parser = build_single_verb_parser(primitive_name)
    if parser is None:
        # Stale map (module no longer defines the verb / shape changed): fall
        # back. register_single_module already imported the module, which the
        # full walk would import anyway (cached), so this is correct, just not
        # the saved-import win.
        return None
    args = parser.parse_args(argv)
    return _invoke_parsed(args)


def _invoke_parsed(args: argparse.Namespace) -> int:
    """Run ``args.func`` under the uniform error→envelope translation.

    Shared by the fast path and the full path so a primitive raising
    ``HpcError`` / ``ValidationError`` / an unguarded exception maps to the
    same exit code and JSON envelope regardless of how the parser was built.
    """
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
    except Exception as exc:  # noqa: BLE001 — last-resort envelope
        # Spec-input validation sites across ``ops/``, ``meta/``,
        # ``_kernel/contract``, ``state/``, ``infra/`` raise typed
        # ``errors.SpecInvalid`` (caught by the ``HpcError`` clause
        # above with exit 1). Any ``ValueError`` reaching here is now
        # an unguarded internal bug (a stray ``int("garbage")`` from a
        # buggy parser), so let it surface as exit 3 instead of being
        # mis-classified as a user error.
        return _err_from_hpc(errors.HpcError(f"{type(exc).__name__}: {exc}"))


def _record_detached_failure_terminal(exit_code: int) -> None:
    """A detached worker that exits non-zero leaves a terminal record.

    Run-#12 finding 17 leg 3: six workers died on a local refusal with NO
    recorded terminal — the driver saw silence, the doctor could only alert,
    and diagnosis took an hour. The spawn env self-identifies the worker
    (``HPC_DETACHED_RUN_ID``/``_BLOCK``, set by ``_spawn_detached``; the
    worker's cwd is the experiment dir by the same contract). Best-effort:
    fires only in a marked worker, never overwrites a terminal the block
    already recorded (that one carries the real result), never raises.
    """
    import os

    run_id = os.environ.get("HPC_DETACHED_RUN_ID")
    block = os.environ.get("HPC_DETACHED_BLOCK")
    if not run_id or not block:
        return
    try:
        from pathlib import Path

        from hpc_agent.state.block_terminal import read_terminal, record_terminal

        experiment_dir = Path.cwd()
        if read_terminal(experiment_dir, run_id, block) is not None:
            return
        record_terminal(
            experiment_dir,
            run_id=run_id,
            block=block,
            cmd_sha="",
            result_dump={
                "ok": False,
                "detached_failure": True,
                "error_code": "detached_worker_exit",
                "exit_code": exit_code,
                "message": (
                    f"detached {block} worker for run {run_id} exited "
                    f"{exit_code} before recording a terminal — the worker "
                    "log carries the disclosed failure; re-invoke the block "
                    "(recorded-terminal replay keeps it idempotent)"
                ),
            },
        )
    except Exception:  # noqa: BLE001 — the exit path must never gain a new crash
        pass


def main(argv: list[str] | None = None) -> int:
    # A DETACHED worker heartbeats liveness into its captured log while the verb
    # runs (run-#12 findings 3/16/27, the >10s-progress discipline): a 0-byte log
    # for minutes of legitimate scp/rsync work is indistinguishable from a
    # frozen-at-birth freeze without it. No-op unless this process is a detached
    # worker (HPC_DETACHED_RUN_ID set) and HPC_DETACH_HEARTBEAT_SEC > 0. Started
    # before the verb runs, stopped in the CM's finally AFTER the verb returns —
    # so the wait-first loop never emits a line after the final envelope.
    from hpc_agent._kernel.lifecycle.heartbeat import detached_heartbeat

    with detached_heartbeat():
        try:
            rc = _dispatch_main(argv)
        except Exception:
            _record_detached_failure_terminal(3)
            raise
        if rc != 0:
            _record_detached_failure_terminal(rc)
        return rc


def _dispatch_main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page (cp1252) whose codec
    # cannot encode the ``→`` and box-drawing characters in our --help
    # text and catalog tables, raising UnicodeEncodeError on print_help().
    # Force UTF-8 on the std streams up front — INCLUDING stdin: mcp-serve
    # reads JSON-RPC lines from it, and a cp1252-decoded UTF-8 em-dash
    # corrupts human text INSIDE the server before any file is written
    # (run-#12 finding 13: the journaled goal's "â€"" mojibake).
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                _reconfigure(encoding="utf-8")

    if argv is None:
        argv = sys.argv[1:]
    argv = _strip_verb_group(argv)

    # Single-verb fast path: import ONLY the module that defines a known
    # ungrouped verb instead of the full ~100-module registry walk (~half the
    # cold-start cost). Returns None — falling through to the full path — for
    # help/version, grouped/Tier-3/unknown verbs, an installed plugin, or any
    # stale-map miss, so behaviour is byte-identical and only speed differs.
    fast = _try_fast_dispatch(argv)
    if fast is not None:
        return fast

    # Full path: populate the whole registry, build the complete parser.
    from hpc_agent._kernel.registry.primitive import register_primitives

    register_primitives()
    parser = build_parser()
    args = parser.parse_args(argv)
    return _invoke_parsed(args)


if __name__ == "__main__":
    sys.exit(main())
