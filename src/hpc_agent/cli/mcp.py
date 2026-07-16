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

Persistent SSH engine — pinned explicit here (memo §3)
------------------------------------------------------
Since the latency-audit rank-3 flip (2026-07-16) the persistent asyncssh engine
is default-ON process-wide (:func:`hpc_agent.infra.ssh_engine.engine_enabled` —
an unset ``HPC_SSH_ENGINE`` now selects it). ``mcp-serve`` is still special: it
is the one verb that runs as a long-lived process, and so the one place a
persistent SSH connection AMORTISES across many commands — the outsourced command
channel's idle sweeper + slot-held-per-in-flight-op invariant
(:mod:`hpc_agent.infra.ssh_engine` header, invariant 2 — the 2026-07-16 run-14
correction: a slot is held around each connect + command window and freed when
the connection goes idle, so a warm-idle connection holds none) were hardened
*from mcp-serve incidents* (the run-#10 F-B residual: an mcp-serve holding its
per-host slot until process exit — now closed by that per-command release). So
:func:`cmd_mcp_serve` (and nowhere else — no
import-time side effect) PINS the engine value explicitly via ``setdefault`` — no
longer to *turn it on* (the default already is) but to make the effective engine
visible in every downstream env echo, honouring a user-preset value, and pinning
``native`` under ``HPC_MCP_NO_SSH_ENGINE=1`` so that opt-out still disables it.

Honest degradation — the engine is never load-bearing (verified today): ANY
engine trouble raises :class:`hpc_agent.infra.ssh_engine.EngineUnavailable` — a
disabled engine, an **unimportable asyncssh** (``ssh_engine.py`` raises it when
the import fails), a breaker-refused/failed connect, a wedged command, a dead
channel — and the ssh seam catches exactly that
(``except ssh_engine.EngineUnavailable:`` in
:func:`hpc_agent.infra.remote`) and falls straight through to the one-shot
path. Turning the engine on here can therefore never be *worse* than leaving it
off; the worst case is a cold one-shot handshake per command, i.e. today's
behaviour.

**One exception, by design (F55).** Fall-through is only harmless when the
command had NOT yet reached the remote host. A failure AFTER dispatch (a
per-command timeout while ``conn.run`` was in flight, a torn connection mid-run)
raises ``EngineUnavailable(dispatched=True)``; for a command the caller marked
NON-idempotent (a ``qsub``/``sbatch`` submit — see ``remote.non_idempotent_remote``),
the seam does NOT re-execute it one-shot, because the remote half may still be
running and a re-run would duplicate the array. Such a command surfaces its
failure instead of self-healing. Idempotent read surfaces (status polls, pulls)
keep the unconditional fall-through, so "never worse than off" still holds for
them — the exception is scoped to exactly the commands where a silent
re-execution would be a correctness bug, not a slowdown.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys

from hpc_agent.cli._helpers import EXIT_OK

# Opt-out for the mcp-serve engine default. Distinct from the engine's own
# opt-in (HPC_SSH_ENGINE): this one only suppresses *our* setdefault injection.
NO_SSH_ENGINE_ENV = "HPC_MCP_NO_SSH_ENGINE"


def _enable_ssh_engine_default() -> str:
    """Default the persistent SSH engine ON for the long-lived mcp-serve process.

    Returns the engine disposition for the stderr ready line, one of:

    * ``"off"`` — the engine is not enabled: opted out via
      ``HPC_MCP_NO_SSH_ENGINE=1`` AND no independent ``HPC_SSH_ENGINE=asyncssh``.
    * ``"user-set"`` — ``HPC_SSH_ENGINE`` was already set; ``setdefault`` is a
      no-op, so the user's choice (asyncssh *or* native/off) wins.
    * ``"on"`` — env was unset; we make ``asyncssh`` explicit.

    Since the latency-audit rank-3 flip (2026-07-16) the engine is default-ON
    process-wide (:func:`ssh_engine.engine_enabled` — an unset env now selects
    asyncssh), so mcp-serve's ``setdefault`` no longer *turns the engine on* —
    it only PINS the value so every downstream env echo (doctor / status /
    net-triage) discloses the effective engine instead of the silent default.
    The ``HPC_MCP_NO_SSH_ENGINE=1`` opt-out therefore can no longer just skip
    the injection (unset would still be ON): with no explicit user value it now
    pins ``native`` so the opt-out genuinely disables the engine, preserving its
    documented contract. A user-preset ``HPC_SSH_ENGINE`` still wins in either
    branch.

    The label reflects the EFFECTIVE disposition, not the injection decision:
    the opt-out does not disable an engine the operator turned on independently
    via ``HPC_SSH_ENGINE=asyncssh`` (the ssh seam reads that env, not our opt-out).
    Reporting ``off`` while the engine is genuinely on would defeat the line's
    purpose ("why is MCP slow must be a measurement, not a mystery"), so under
    the opt-out we report the engine's real state.
    """
    from hpc_agent.infra import ssh_engine

    preset = ssh_engine.ENGINE_ENV in os.environ
    if os.environ.get(NO_SSH_ENGINE_ENV, "").strip() == "1":
        if preset:
            return "user-set" if ssh_engine.engine_enabled() else "off"
        # No user value + engine default-ON: pin native so the opt-out actually
        # disables the engine (leaving the env unset would keep it on).
        os.environ[ssh_engine.ENGINE_ENV] = "native"
        return "off"
    os.environ.setdefault(ssh_engine.ENGINE_ENV, "asyncssh")
    return "user-set" if preset else "on"


def cmd_mcp_serve(args: argparse.Namespace) -> int:
    """Run the MCP server on stdio until the client closes the connection.

    Diagnostics go to stderr; stdout is reserved for the JSON-RPC stream.
    Returns ``EXIT_OK`` on a clean EOF.
    """
    from hpc_agent._kernel.extension.mcp_server import build_server

    engine_state = _enable_ssh_engine_default()

    # Run-12 finding 13 (cp1252 mojibake) is enforced HERE, exactly once, while
    # the process is still single-threaded: once ``serve()`` starts, the stdin
    # reader thread is permanently blocked in ``readline()``, and a
    # reconfigure-under-read returns a false EOF on Windows (the second-call
    # connection-closed class, regression 17243a17). The per-dispatch
    # reconfigure in ``cli.dispatch`` never sees the real streams over MCP —
    # the in-process runner shields them (``_shield_real_stdin`` + redirects).
    for _stream in (sys.stdin, sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                _reconfigure(encoding="utf-8")

    catalog = getattr(args, "catalog", "full")
    if catalog not in ("full", "tiered", "curated"):
        catalog = "full"
    server = build_server(
        allow_mutations=bool(getattr(args, "allow_mutations", False)),
        catalog=catalog,
    )
    print(
        f"hpc-agent mcp-serve: ready "
        f"(catalog={catalog}, mutations={'on' if server._allow_mutations else 'off'}, "
        f"engine={engine_state})",
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


__all__ = ["cmd_mcp_serve", "register", "NO_SSH_ENGINE_ENV"]
