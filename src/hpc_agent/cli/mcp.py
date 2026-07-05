"""``hpc-agent mcp-serve`` — expose the primitive registry as an MCP server.

A Tier 3 verb (no ``@primitive`` backing): it does not emit the one-shot JSON
envelope every other verb does — it speaks the Model Context Protocol
(JSON-RPC 2.0) on stdout for the lifetime of the process, so an MCP client
(Claude, Codex, Gemini, any MCP-speaking harness) can drive hpc-agent as
native tools/resources/prompts. The projection and protocol live in
:mod:`hpc_agent._kernel.extension.mcp_server`; this module is just the CLI
entry point + the safety-flag surface.

The default is read-only: only ``query`` / ``validate`` primitives are exposed
as tools. ``--allow-mutations`` additionally exposes the mutating verbs
(submit / aggregate / scaffold); the registry has no scheduler cancel/submit
verb, so those remain unreachable regardless. ``--catalog tiered`` advertises
only ``find`` / ``describe`` / ``run-primitive`` to keep per-tool schemas out of
the model's context for large catalogs.

``register(sub)`` is invoked from
:func:`hpc_agent.cli.parser._register_tier3_modules`.
"""

from __future__ import annotations

import argparse
import sys

from hpc_agent.cli._helpers import EXIT_OK


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    """Run the MCP server on stdio until the client closes the connection.

    Diagnostics go to stderr; stdout is reserved for the JSON-RPC stream.
    Returns ``EXIT_OK`` on a clean EOF.
    """
    from hpc_agent._kernel.extension.mcp_server import build_server

    catalog = getattr(args, "catalog", "full")
    if catalog not in ("full", "tiered", "curated"):
        catalog = "full"
    server = build_server(
        allow_mutations=bool(getattr(args, "allow_mutations", False)),
        catalog=catalog,
    )
    print(
        f"hpc-agent mcp-serve: ready "
        f"(catalog={catalog}, mutations={'on' if server._allow_mutations else 'off'})",
        file=sys.stderr,
        flush=True,
    )
    server.serve(sys.stdin, sys.stdout)
    return EXIT_OK


def register(sub: argparse._SubParsersAction) -> None:
    """Register the ``mcp-serve`` subcommand on *sub*."""
    p = sub.add_parser(
        "mcp-serve",
        help=(
            "Serve the hpc-agent primitive registry over the Model Context "
            "Protocol (JSON-RPC 2.0 on stdio). Read-only by default; speaks MCP, "
            "not the one-shot JSON envelope."
        ),
    )
    p.add_argument(
        "--allow-mutations",
        action="store_true",
        help=(
            "Also expose mutating verbs (submit / aggregate / scaffold / workflow) "
            "as tools. Off by default: only query/validate verbs are reachable. "
            "Scheduler cancel/raw-submit are never registry primitives, so they "
            "stay unreachable either way."
        ),
    )
    p.add_argument(
        "--catalog",
        choices=["full", "tiered", "curated"],
        default="full",
        help=(
            "full (default): one typed tool per read-only primitive. tiered: "
            "expose only find/describe/run-primitive so per-tool schemas stay out "
            "of the model's context (mirrors the CLI's find->describe->invoke flow). "
            "curated: the human-amplification block verbs (those returning a "
            "next_block), the loop driver block-drive + the greenlight commit "
            "append-decision, plus the recovery/opt-in verbs (doctor, kill, "
            "net-triage, submit-speculate) — the surface install-commands registers."
        ),
    )
    p.set_defaults(func=cmd_mcp_serve)


__all__ = ["cmd_mcp_serve", "register"]
