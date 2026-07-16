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


def _fast_dispatch_enabled(verb: str | None = None) -> bool:
    """Whether the single-verb fast path may serve *verb* (opt-out + plugin gate).

    Disabled by ``HPC_AGENT_NO_FAST_CLI=1`` (a field kill switch for the
    central path). Otherwise the fast path serves *verb* UNLESS an installed
    plugin can reshape THIS verb's CLI — in which case it must take the full
    :func:`build_parser` walk that honours the reshaping. ``verb`` is the leaf
    verb being dispatched (``argv[0]`` on the fast path); ``None`` asks the
    coarse question "is the fast path available for *some* verb" (used by tests
    and the kill-switch/no-plugin baselines).

    Per-verb granularity (latency rank 13). The plugin contract
    (``_kernel/registry/plugins.py``) has exactly ONE hook handed the argparse
    subparsers, ``register_cli`` (``plugins.register_plugin_cli`` — the sole
    seam that can override or extend a core verb's parser). A plugin that
    implements only the primitive-registration hook (``primitive_modules``)
    contributes NEW verbs absent from ``VERB_MODULE_MAP``; they miss the fast
    path and fall through on their own. The remaining hooks
    (``slash_command_assets`` / ``schema_assets`` / ``worker_prompt_assets`` /
    ``run_setup_actions``) never touch the parser. So the only guard that can
    fire is "a ``register_cli`` plugin reshapes a core verb", and
    :func:`hpc_agent._kernel.registry.plugins.cli_reshaping_verdict` reduces the
    loaded set to exactly that:

    * ``conservative`` — an UNDECLARED ``register_cli`` plugin (no manifest
      ``reshapes_core_verbs``): it could touch anything, so EVERY verb takes the
      full walk. This is the pre-manifest behaviour, preserved for back-compat.
    * ``reshaped`` — the verbs DECLARED-reshaped by manifest-carrying plugins;
      only those fall back. An add-only plugin declaring ``reshapes_core_verbs=()``
      (e.g. ``hpc-agent-notebook-render``, which only ADDS a ``render``
      subcommand) contributes nothing here, so core verbs stay fast — the win.

    The verdict is read through
    :func:`hpc_agent.cli._fast_path_cache.cached_cli_reshaping_verdict`, which
    caches it across subprocesses keyed on the installed-distribution set so the
    ``entry_points()`` scan is not re-paid every invocation.

    ``HPC_AGENT_DISABLE_PLUGINS=1`` short-circuits to allow — with plugins
    disabled none can reshape anything (``load_plugins`` honours the same var
    and returns ``()``). Any metadata/load error → full path (``False``): the
    byte-identical fallback is preserved by construction.
    """
    import os

    if os.environ.get("HPC_AGENT_NO_FAST_CLI") == "1":
        return False
    if os.environ.get("HPC_AGENT_DISABLE_PLUGINS") == "1":
        return True
    try:
        from hpc_agent.cli._fast_path_cache import cached_cli_reshaping_verdict

        conservative, reshaped = cached_cli_reshaping_verdict()
        if conservative:
            return False
        # A specific verb only falls back when a plugin declares it reshaped;
        # the coarse (verb is None) question answers "is the path available at
        # all" — True whenever no plugin forces the wholesale disable.
        return verb not in reshaped
    except Exception:  # noqa: BLE001 — a metadata hiccup must not break the CLI
        return False


# Discovery verbs whose handler reads the WHOLE operations catalog. They ARE
# fast-path-eligible (``CliShape.fast_path_safe``) but only via BAKED HYDRATION:
# their handlers rebuild the whole-truth catalog from the shipped
# ``operations.json`` bake (never the partial live registry — premortem A1), so
# the fast path may serve them only when that bake is trustworthy
# (``baked_catalog_usable`` — content-keyed on the build fingerprint). A source
# checkout (untrusted bake) or an unreadable bake steers them to the full walk,
# byte-identical.
_BAKED_HYDRATION_VERBS: frozenset[str] = frozenset({"describe", "find"})


def _try_fast_dispatch(argv: list[str]) -> int | None:
    """Dispatch a single known ungrouped verb without the full registry walk.

    Returns the process exit code on the fast path, or ``None`` to signal the
    caller to fall back to the full ``register_primitives`` + ``build_parser``
    path. Falls back (returns ``None``) for: an empty argv, a leading global
    flag (``--version`` / top-level ``--help``), a verb absent from the
    generated map (grouped verbs, Tier-3 ``run`` / ``mcp-serve``, unknown
    verbs), an installed CLI-shaping (``register_cli``) plugin, a discovery
    verb whose baked catalog is not trustworthy (``describe`` / ``find`` in a
    source checkout) OR whose core-only bake would miss a plugin's
    ``primitive_modules`` verbs, ``describe --schema`` (resolves an arbitrary
    target verb's meta the fast path has not imported), or any stale-map miss.
    Every fallback path yields byte-identical behaviour to before — only speed
    differs.
    """
    if not argv or argv[0].startswith("-"):
        return None
    if not _fast_dispatch_enabled(argv[0]):
        return None
    if argv[0] in _BAKED_HYDRATION_VERBS:
        # ``describe --schema <verb>`` loads the input schema of an ARBITRARY
        # target verb via its registry meta, which the single-verb fast path
        # never imported — take the full walk so the meta is present.
        if "--schema" in argv:
            return None
        from hpc_agent._kernel.registry.plugins import plugin_contributes_primitive_modules
        from hpc_agent._kernel.registry.primitive import baked_catalog_usable

        if not baked_catalog_usable():
            # Untrusted / unreadable bake — resolving off the partial live
            # registry would be wrong-but-plausible (A1). Full walk instead.
            return None
        if plugin_contributes_primitive_modules():
            # The bake is CORE-ONLY; a plugin adding primitive_modules puts verbs
            # in the full walk's catalog that the bake cannot carry. Serving
            # ``describe`` / ``find`` off it would MISS those verbs — non-byte-
            # identical to the full walk. Fall back so the discovery surface is
            # the whole truth. (Scoped to describe/find only — core verbs never
            # enter this branch, so the hot submit-preflight path is unaffected.)
            return None
    from hpc_agent.cli._verb_module_map import VERB_MODULE_MAP

    entry = VERB_MODULE_MAP.get(argv[0])
    if entry is None:
        return None
    primitive_name, module_name = entry

    from hpc_agent._kernel.registry.primitive import register_single_module
    from hpc_agent.cli.parser import build_single_verb_parser

    try:
        register_single_module(module_name)
        parser = build_single_verb_parser(primitive_name)
    except ImportError:
        # The OTHER staleness mode (docstring: "any stale-map miss falls back"):
        # the verb's defining module was renamed/deleted while the verb still
        # exists elsewhere, so register_single_module's bare import_module raises
        # ModuleNotFoundError. Degrade to the full registry walk (which discovers
        # modules by package walk, not the map) instead of letting the traceback
        # escape main() as a non-envelope crash for a verb the walk dispatches
        # fine (#59).
        return None
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
        import os as _os
        from pathlib import Path

        from hpc_agent._kernel.lifecycle.crash_disclosure import log_has_fatal_marker
        from hpc_agent.state.block_terminal import read_terminal, record_terminal

        experiment_dir = Path.cwd()
        if read_terminal(experiment_dir, run_id, block) is not None:
            return
        # Honest terminal (run-#13 finding 2): never assert the log discloses
        # something the write path cannot guarantee. Read the worker log's tail
        # (bounded) and say what it ACTUALLY contains — a flushed ``[fatal]``
        # block, or none at all (a hard kill / unflushed buffers), naming the last
        # log line so the reader sees where the worker really stopped.
        disclosed, last_line = log_has_fatal_marker(_os.environ.get("HPC_DETACHED_LOG"))
        if disclosed:
            message = (
                f"detached {block} worker for run {run_id} exited {exit_code} "
                "before recording a terminal — the worker log carries the "
                "disclosed failure ([fatal] block present); re-invoke the block "
                "(recorded-terminal replay keeps it idempotent)"
            )
        else:
            tail = f"; last log line: {last_line!r}" if last_line else ""
            message = (
                f"detached {block} worker for run {run_id} exited {exit_code} "
                "WITHOUT disclosure in its log (no [fatal] block — hard kill or "
                f"unflushed buffers){tail}; re-invoke the block (recorded-terminal "
                "replay keeps it idempotent)"
            )
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
                "log_disclosed": disclosed,
                "message": message,
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
    from hpc_agent._kernel.lifecycle.crash_disclosure import (
        emit_fatal_block,
        install_crash_faulthandler,
    )
    from hpc_agent._kernel.lifecycle.heartbeat import detached_heartbeat, last_heartbeat_line

    # Worker crash disclosure (run-#13 finding 2): a detached worker died exit-2
    # (a VPN drop severed its scp child) and flushed NOTHING to its log — no
    # traceback, no exit code — so the terminal's "the log carries the disclosed
    # failure" was a lie. Arm faulthandler so a hard signal (segfault/abort — the
    # paths no ``except`` can catch) dumps a native traceback to the log, and flush
    # a ``[fatal]`` block on every catchable exit path below. No-ops outside a
    # detached worker, so the foreground CLI console stays clean.
    with detached_heartbeat():
        install_crash_faulthandler()
        try:
            rc = _dispatch_main(argv)
        except SystemExit as exc:
            if exc.code not in (0, None):
                emit_fatal_block(exc=exc, last_stage=last_heartbeat_line())
            raise
        except Exception as exc:
            emit_fatal_block(exc=exc, last_stage=last_heartbeat_line())
            _record_detached_failure_terminal(3)
            raise
        if rc != 0:
            emit_fatal_block(exit_code=rc, last_stage=last_heartbeat_line())
            _record_detached_failure_terminal(rc)
        return rc


def _dispatch_main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page (cp1252) whose codec
    # cannot encode the ``→`` and box-drawing characters in our --help
    # text and catalog tables, raising UnicodeEncodeError on print_help().
    # Force UTF-8 on the std streams up front. stdin is included for one-shot
    # CLI use (piped spec text must decode as UTF-8, run-#12 finding 13) —
    # but this loop must NEVER run against mcp-serve's real stdin: that is
    # the JSON-RPC transport with a reader thread blocked in ``readline()``,
    # and a reconfigure-under-read returns a false EOF on Windows, killing
    # the server after the in-flight call (regression 17243a17). Over MCP the
    # in-process runner swaps stdin out (``_shield_real_stdin``) and the
    # session-level UTF-8 reconfigure happens once in ``cmd_mcp_serve``.
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
    # help/version, grouped/Tier-3/unknown verbs, an installed CLI-shaping
    # plugin, or any stale-map miss, so behaviour is byte-identical and only
    # speed differs.
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
