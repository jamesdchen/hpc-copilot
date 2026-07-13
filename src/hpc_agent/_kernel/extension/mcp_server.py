"""Model Context Protocol (MCP) server — a registry-projected surface.

This is the fourth agent surface (after slash commands, workflow skills,
and worker prompts): an MCP server that speaks JSON-RPC 2.0 over stdio.
It is an **additive projection** of the ``@primitive`` registry, not a
re-implementation of the CLI — the same design ``docs/reference/agent-surface.md``
anticipated ("MCP wrappers can be written on top of the CLI"). Two halves:

* **Discovery is projected.** ``tools/list`` walks the live registry
  (:func:`hpc_agent._kernel.registry.primitive.get_registry`) exactly like
  ``cli/parser.py`` walks it to build argparse. There is no second
  hand-maintained tool list, so the MCP surface cannot drift from the CLI —
  the CLI registry stays the single source of truth.
* **Invocation drives the CLI dispatch path — in-process by default.**
  ``tools/call`` writes the spec to a temp file and drives the SAME
  ``cli.dispatch.main`` code path the ``hpc-agent`` binary runs, then maps
  the JSON envelope back. The default runner is
  :func:`_in_process_cli_runner` (warm registry, ~40 ms/call vs ~1.2 s for a
  subprocess — measured 2026-07-04); :func:`_subprocess_cli_runner` stays
  injectable as the isolation fallback and the parity oracle. Every contract
  the CLI carries — exit-code → category, schema validation,
  journal/idempotency, the ``{ok, error_code, category, retry_safe,
  remediation}`` failure shape — is inherited verbatim, with zero
  re-implementation.

Safety posture (the reason this ships)
=======================================

The headless worker fence (:data:`hpc_agent._kernel.lifecycle.invoke._CLUSTER_OP_DENY_COMMANDS`)
re-imposes a no-``scancel`` / no-``ssh`` / no-exfil deny on three CLI config
surfaces because a shell-bearing worker *could* run those commands. An MCP
client has no shell — it can only call the verbs this server exposes. So the
deny boundary collapses to "which verbs are registered as tools", and:

* **Read-only by default.** Only ``query`` / ``validate`` primitives are
  exposed. Mutating verbs (``mutate`` / ``submit`` / ``workflow`` /
  ``scaffold``) require an explicit opt-in (``allow_mutations`` /
  ``--allow-mutations``).
* **No cancel/raw-submit verb exists in the registry at all** — ``scancel`` /
  ``qdel`` / ``sbatch`` / ``qsub`` are never hpc-agent primitives, so they are
  structurally unreachable through this surface regardless of the flag.
  ``tests/test_mcp_server.py`` pins this invariant.

Addressing the trade-offs MCP introduces
=========================================

* **Failure contract.** MCP collapses results into ``{content, isError}``;
  the CLI's machine-readable ``error_code`` / ``category`` / ``retry_safe`` and
  the exit code would be lost. We keep them: the full envelope (plus the
  process ``exit_code``) rides in ``structuredContent``, and ``isError`` is set
  from ``ok``/exit-code. A client that reads ``structuredContent`` recovers the
  same semantics it had over the CLI.
* **Context bloat vs. tiered discovery.** The CLI deliberately offers
  ``find`` → ``describe`` → invoke so a headless loop never dumps the whole
  catalog. ``catalog="tiered"`` mirrors that: it advertises only ``find`` +
  ``describe`` + a generic ``run-primitive`` tool, so the per-tool schemas of
  every primitive stay out of the model's context until pulled on demand.
  ``catalog="full"`` (default) exposes each read-only primitive as its own
  typed tool.
* **Version skew.** The server's package version is reported in
  ``serverInfo.version`` and called out in ``initialize`` ``instructions`` so a
  client can detect a daemon/package mismatch.

The protocol layer (JSON-RPC framing, dispatch) and the projection layer are
kept as pure functions / a plain class so both are unit-testable without a real
stdio transport: :class:`McpServer.handle` takes a request dict and returns a
response dict, and the subprocess runner is injectable.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

# The elicitation prompt + render-digest composition half lives in a separable,
# transport-free module; re-exported here so the historical ``mcp_server.<name>``
# import paths tests and prose pin keep resolving. The four names the server class
# reads directly (prompt render, overnight binding, response filter, capture
# markers) resolve from these bindings; the rest are pure re-exports (F401-marked).
# The elicitation POLICY constants (timeout, firing tool, authorship-evidence key,
# requested schema) stay in THIS module beside the class methods that read them.
from hpc_agent._kernel.extension.mcp_elicitation import (
    _DIFF_EMBED_MAX_BYTES,  # noqa: F401 — re-export
    _DIGEST_BLOCK_MAX_BYTES,  # noqa: F401 — re-export
    _OVERNIGHT_CONSENT_BLOCK,  # noqa: F401 — re-export
    _accepted_utterance,
    _overnight_consent_binding,
    _render_diff_body_lines,  # noqa: F401 — re-export
    _render_digest_block,  # noqa: F401 — re-export
    _render_elicitation_prompt,
    _render_overnight_consent_block,  # noqa: F401 — re-export
    _tier_trigger_headline,  # noqa: F401 — re-export
    _with_capture_markers,
)
from hpc_agent._kernel.extension.workflow_entries import WORKFLOW_ENTRIES_BY_PROMPT
from hpc_agent.cli._dispatch import CliShape, _leaf_verb

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import IO

    from hpc_agent._kernel.registry.primitive import PrimitiveMeta

# MCP protocol revision this server is written against. On ``initialize`` we
# echo back the client's requested version when it sends one (the broadest
# compatibility posture for a minimal server) and fall back to this otherwise.
_PROTOCOL_VERSION = "2025-06-18"

# ─── elicitation capability flag (harness-contract capability 1, second channel)
#
# The 2025-06-18 MCP revision adds server-initiated ELICITATION: the server sends
# an ``elicitation/create`` REQUEST to the client, the client renders a form, the
# human types a response, and the client returns it. For the harness contract this
# is a SECOND conforming utterance channel — the typed response travels
# client->server with the model never touching it (out-of-band satisfied), the
# server-side handler filters it (free-text only, per the clicked-option hazard)
# and ``append_utterance``s it.
#
# The bidirectional pump this needs is now BUILT (``docs/design/mcp-elicitation.md``
# D1): a server-originated request id namespace (:meth:`McpServer._next_outbound_id`
# — the collision-proof ``hpc-srv-<n>`` space), a single daemon stdin-reader thread
# feeding a :class:`queue.Queue` so a ``get(timeout=…)`` deadline can actually fire
# on Windows (the ``select()``-on-console-stdin gap that made the earlier "no
# threads" plan unimplementable), and :meth:`McpServer._request_from_client` — the
# blocking-with-timeout wait a tool handler uses, servicing interleaved client
# requests inline (elicitation suppressed for nested dispatch) so a waiting
# elicitation never head-of-line-blocks the session (conduct rule 11).
#
# What THIS flag reports is the honest thing a *separate-process probe* can verify:
# the SERVER code path exists. It says nothing about the client — client support is
# negotiated per session at ``initialize`` (:attr:`McpServer._client_elicitation`,
# from ``params["capabilities"]["elicitation"]``) and is "unknown from this probe"
# to ``harness-capabilities`` by design (D2). When a session's client does NOT
# declare elicitation, the channel DEGRADES to the hook path (capability 1's
# ``UserPromptSubmit`` utterance-capture), silently and honestly, exactly as the
# contract specifies for an absent capability. The elicitation HANDLER + firing
# site (the ``append-decision`` retry-once wrap) land in E4; this flag being True
# means the pump underneath them is real.
ELICITATION_SERVER_IMPLEMENTED: bool = True

# The read/act boundary, mirrored from the @primitive ``verb`` taxonomy
# (docs/architecture.md → "The decide / act boundary"). Only these are exposed
# without an explicit mutation opt-in.
_READ_ONLY_VERBS = frozenset({"query", "validate"})
_MUTATING_VERBS = frozenset({"mutate", "submit", "scaffold", "workflow"})

# The synthetic tool that preserves CLI-style tiered discovery under MCP.
_RUN_PRIMITIVE_TOOL = "run-primitive"

# The ``curated`` catalog's fixed extras: the recovery verbs (``doctor`` detects
# stalled/orphaned runs, ``kill`` the request→confirm kill, ``net-triage`` the
# bounded connectivity differential — the 2026-07-05 incident's missing tool)
# plus the ``submit-speculate`` opt-in touchpoint — AND the human-amplification
# LOOP surfaces the block verbs are driven through: ``block-drive`` (the chain
# driver the skills invoke — one stateless tick that parks or detaches, so it is
# MCP-safe, never a multi-hour blocking call) and ``append-decision`` (the
# greenlight commit). Without these two, an MCP-first agent reaches the block
# verbs but not the DRIVER or the COMMIT, so it drops to raw CLI for the core
# loop and hand-authors specs — reintroducing the finding-13/17 corruption class
# (run #6). These are stable human-amplification surfaces that are NOT blocks
# (their Result models carry no ``next_block``), so they are unioned in
# explicitly. Everything else in ``curated`` is DERIVED (see
# :func:`_declares_next_block`) — a block is any verb whose Result model declares
# a ``next_block`` field, so adding/removing that field moves a verb in/out of
# the curated set with no edit here.
_CURATED_EXTRA_VERBS = frozenset(
    {
        "doctor",
        "kill",
        "net-triage",
        "submit-speculate",
        "block-drive",
        "append-decision",
        # ``scope-lock`` is a stable human-amplification mutate that is NOT a
        # block (no ``next_block``), so it is unioned in explicitly — the run-#8
        # lesson: an MCP-unreachable verb gets hand-rolled. ``scope-status`` (a
        # pure read) stays OUT of the curated set.
        "scope-lock",
        # ``verify-reproduction`` is the reproduction receipt QUERY — a read verb
        # (no ``next_block``, so it does not derive in), unioned explicitly
        # because it is the sanctioned post-repro step ``reproduce-run``'s brief
        # directs the human to (the receipt is computed under a caller-owned
        # tolerance). Same run-#8 lesson: an MCP-unreachable verb gets
        # hand-rolled. (``reproduce-run`` itself needs NO entry — its Result
        # declares ``next_block``, so it DERIVES into the catalog.)
        "verify-reproduction",
        # The notebook-audit loop's agent_facing verbs (notebook-audit design
        # doc, Amendment 2 — run #10): the loop is HUMAN-sequenced (a
        # block-drive-style driver was REJECTED there), so none of these declare
        # ``next_block`` and none derive in — each is unioned explicitly. Run
        # #10 priced their MCP absence live: hand-authored spec JSONs, two
        # schema fumbles — the run-#8 unreachable-gets-hand-rolled class.
        # ``notebook-lint`` — the four structural checks, the loop's first tick.
        "notebook-lint",
        # ``notebook-audit-view`` — the audit loop's typed surface (the
        # verbatim-relay canonical view) — run #10 priced the CLI-spec fallback.
        "notebook-audit-view",
        # ``notebook-status`` — the per-section audit-state read the loop exits on.
        "notebook-status",
        # ``notebook-auto-clear`` — the CODE-attestor clearance mutate (recorded
        # roots only; the laundering guard enforces at invocation).
        "notebook-auto-clear",
        # ``notebook-record-receipt`` — the emitter's sha-bound render-receipt
        # journaling; unreachable, an emitter reaches for ``python -c``.
        "notebook-record-receipt",
        # ``notebook-draft-context`` — the deterministic drafting projection
        # (draft-context design): template slugs, resolved engines, call sites,
        # root inventories — the mechanized run-#10 drafting brief.
        # ``notebook-draft`` — the drafter-attribution record (multi-human MH5).
        "notebook-draft",
        "notebook-draft-context",
        # ``notebook-scaffold-template`` — the content-free template scaffold
        # that opens an audit.
        "notebook-scaffold-template",
        # ``notebook-record-config`` — the standalone audit's config seat (the
        # run-#10 rootless-canonical fix); unreachable it would be the next
        # hand-authored spec JSON.
        "notebook-record-config",
        # ``audit-preflight`` — the GO/NO-GO substrate-prereq brief the
        # notebook-audit skill runs FIRST (before drafting). Human-sequenced
        # like the rest of the audit loop (no ``next_block``), so it is
        # unioned explicitly — unreachable, the agent re-derives the checks
        # by hand (the exact prose-rot the verb mechanized away).
        "audit-preflight",
        # ``evidence-brief`` — the evidence-memory point digest the audit
        # onboarding relays VERBATIM when the human named scope tags. A pure
        # read with no ``next_block``; unreachable, the agent skips the
        # prior-evidence surface or hand-walks the stores.
        "evidence-brief",
        # ── the read-loop QUERY verbs the skills name MCP-direct ─────────────
        # These are pure reads the skills instruct the agent to call "DIRECT
        # through MCP — never a spec-file round-trip" (hpc-submit/hpc-status/
        # hpc-aggregate/hpc-campaign/hpc-notebook-audit SKILL §"Read-only QUERY
        # verbs go DIRECT through MCP"). None declares ``next_block`` (a read is
        # not a block), so none DERIVES in; each is unioned explicitly per the
        # run-#8 lesson — an MCP-unreachable verb gets hand-rolled (a Write +
        # Bash + Read spec-file round-trip for a value one MCP call returns, the
        # stale-relay class the rule-10 Stop hook exists to catch). The
        # reachability lint (scripts/lint_skill_mcp_reachability.py) enforces
        # that every verb a SKILL body names MCP-direct is curated-reachable.
        #
        # ``read-decisions`` — the decision-journal chain-coherence read named
        # MCP-direct at hpc-submit/hpc-status/hpc-aggregate/hpc-campaign/
        # hpc-notebook-audit SKILLs (the parallel-prep back-half preflight scan).
        "read-decisions",
        # ``verify-relay`` — the relay-integrity read named MCP-direct at
        # hpc-submit/hpc-status/hpc-aggregate/hpc-campaign SKILLs ("relay the
        # numbers `status-snapshot`/`verify-relay` report — never a figure you
        # remember").
        "verify-relay",
        # ``attention-queue`` — the fleet-wide needs-your-verdict digest named
        # MCP-direct at hpc-status SKILL ("read-only MCP, direct — no spec-file
        # round-trip"); its ``render`` is relayed VERBATIM, so a spec-file
        # round-trip is exactly the hand-rolled detour this entry closes.
        "attention-queue",
        # ``revise-resolved`` — the spec-delta re-resolve the hpc-submit SKILL
        # names MCP-direct ("call `revise-resolved` (MCP-direct) — NEVER
        # hand-write or hand-edit a spec JSON"). VERIFIED it declares NO
        # ``next_block`` (``_wire/workflows/revise_resolved.py::
        # ReviseResolvedResult``), so despite the SKILL's MCP-direct directive it
        # does NOT derive into the curated catalog — the honest fix is this
        # explicit union, not a phantom ``next_block``. (``retarget-run``, the
        # sibling recovery arm the same SKILL names MCP-direct, DOES declare
        # ``next_block`` and derives in — no entry needed.) Hand-rolling this one
        # is precisely the finding-4/10/13/17 spec-corruption class the verb
        # exists to make impossible.
        "revise-resolved",
        # ``poll-detached`` — the zero-SSH detached-lease liveness query (architect
        # memo §2, built by the sibling m-poll unit; wire ``_wire/queries/
        # poll_detached.py``, home ``ops/monitor/poll_detached.py``). m-poll has
        # MERGED, so this verb is now LIVE in the registry (the pin test guards on
        # its presence); it is a pure read (no ``next_block``, so no derivation),
        # unioned in explicitly like the other MCP-direct reads above.
        "poll-detached",
    }
)

# Curated-catalog decision for the attestation/dossier exporters (conformance-kit
# K10, "expose export-attestations beside export-dossier's posture"). Both
# ``export-dossier`` and ``export-attestations`` are read-only ``query`` verbs
# that declare NO ``next_block`` and are NOT in the extras above, so NEITHER
# derives into the curated catalog — the honest mirror is a recorded
# NON-EXPOSURE, not a new entry. They are HUMAN-run publish/export steps (a human
# exports a dossier or an in-toto attestation bundle after a run completes), not
# agent-loop touchpoints the way the block verbs and recovery opt-ins are; adding
# either to ``_CURATED_EXTRA_VERBS`` would advertise an export affordance the
# amplification loop never needs. Recorded here so the parity is deliberate and
# auditable rather than incidental: if one is ever curated, curate the other and
# say why.

# Read-only context resources, each backed by a CLI verb. The URI scheme is
# informational; the value is the argv driven through the same runner as tools.
_RESOURCES: dict[str, tuple[tuple[str, ...], str]] = {
    "hpc-agent://capabilities": (
        ("capabilities",),
        "Operations catalog + environment metadata (mirror of `hpc-agent capabilities`).",
    ),
    "hpc-agent://clusters": (
        ("clusters", "list"),
        "Configured cluster definitions (mirror of `hpc-agent clusters list`).",
    ),
}

# The four user-facing workflow slash commands, surfaced as MCP prompts.
# PROJECTED, not hand-listed (§6): the name set and each entry's
# start-the-driver instruction are sourced from the single workflow-entry table
# (:mod:`hpc_agent._kernel.extension.workflow_entries`) — the one canonical
# source the Claude-Code slash AND this MCP prompt both project from. Adding a
# workflow there surfaces it here with no edit; ``get_prompt`` still reads the
# packaged command ``.md`` for the human description body.
_PROMPT_NAMES = tuple(WORKFLOW_ENTRIES_BY_PROMPT)


# A runner takes an hpc-agent argv (without the leading binary) and returns
# ``(exit_code, stdout, stderr)``. Injected for testability.
CliRunner = Callable[["list[str]"], "tuple[int, str, str]"]


# ─── the bidirectional pump's queue sentinels (D1 item 6) ────────────────────
#
# The SOLE stdin reader is a daemon thread (:meth:`McpServer._reader_loop`) that
# only parses lines and enqueues them onto a :class:`queue.Queue`; it never
# dispatches. ``serve`` and :meth:`McpServer._request_from_client` both consume
# that queue, and ``Queue.get(timeout=…)`` is what makes the elicitation deadline
# real on Windows (a blocking ``readline`` has no deadline, and ``select()`` does
# not work on console/pipe stdin there). Two non-message signals ride the queue as
# module-singleton sentinels so a consumer can distinguish them from any dict:
_EOF = object()  # stdin closed — decline-equivalent during a wait, then shutdown
_PARSE_ERROR = object()  # a non-JSON line — the consumer emits a -32700 response

# Elicitation timeout (D3): a human may walk away mid-elicitation and the tool
# call must not wedge. 300 s is generous for an at-the-keyboard typed sentence
# while staying far under the runner ceiling (``_SUBPROCESS_RUNNER_TIMEOUT_SEC``).
_ELICITATION_TIMEOUT_SEC: float = 300.0

# ─── the elicitation firing site (E4, docs/design/mcp-elicitation.md D4/D5) ──
#
# The ONE v1 firing site is the sign-off path over MCP: ``append-decision`` is
# the only tool whose ok:false refusal can open an ``elicitation/create`` (D6 —
# no second firing site; ``scope-unlock`` and the plain greenlight stay
# hook-tier). The trigger is E2's machine-readable marker, the distinct
# ``authorship_evidence`` KEY inside the envelope's ``failure_features`` block —
# never the block's mere presence (``_spec_invalid_failure_features`` synthesizes
# a default block for EVERY spec_invalid) and never prose.
_ELICITATION_FIRING_TOOL = "append-decision"
_AUTHORSHIP_EVIDENCE_KEY = "authorship_evidence"

# The spec-conformant ``requestedSchema`` (D3): STRING fields ONLY — no ``enum``,
# no option list — so the clicked-option hazard (``answer_capture._is_clicked``)
# is closed BY CONSTRUCTION on the send side; there is nothing to click, only
# free text to type. Defense-in-depth on the receive side filters each returned
# string through ``state.utterances.is_harness_injected`` + non-empty anyway.
_ELICITATION_REQUESTED_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "utterance": {
            "type": "string",
            "description": "Type the sign-off in your own words.",
        }
    },
    "required": ["utterance"],
}


def _is_response(item: Any) -> bool:
    """True when *item* is a JSON-RPC RESPONSE to a server-originated request.

    A response carries an ``id`` and a ``result`` or ``error``, and — unlike a
    request/notification — no ``method`` (D1 item 3). Anything else the classifier
    routes elsewhere (a ``method`` dict to :meth:`McpServer.handle`, a malformed
    dict to a -32600).
    """
    return (
        isinstance(item, dict)
        and "method" not in item
        and "id" in item
        and ("result" in item or "error" in item)
    )


# Server-level cap on one tool call, enforced by BOTH runners: the subprocess
# runner kills its child on expiry, and the default in-process runner arms a
# SIGALRM backstop (:func:`_in_process_deadline`) over the same ceiling — so the
# wedge class (an unbounded piped/poll wait) cannot recur through EITHER runner.
# Generous: the blocking watch/wait invocations are refused at the seam (conduct
# rule 11 — :func:`_refuse_blocking_over_mcp` requires detach for
# submit-s2/s3/status-watch and refuses monitor-flow/verify-canary/submit-flow
# outright), so a legitimate call is minutes, not hours; 1 h still covers a slow
# stage/harvest with wide margin.
_SUBPROCESS_RUNNER_TIMEOUT_SEC: float = 3600.0

# Cadence of the mid-call liveness heartbeat (run-#12 finding 3). A module
# constant so a test can shrink it to observe a heartbeat without a real 15 s
# wait.
_HEARTBEAT_INTERVAL_SEC: float = 15.0


def _isolated_runner_argv(argv: list[str]) -> list[str]:
    """The argv the isolated runner executes (a seam so tests can substitute)."""
    return [sys.executable, "-m", "hpc_agent", *argv]


def _subprocess_cli_runner(argv: list[str]) -> tuple[int, str, str]:
    """Run ``python -m hpc_agent <argv...>`` in a SUBPROCESS and capture its envelope.

    ``sys.executable -m hpc_agent`` rather than a bare ``hpc-agent`` on PATH so
    the server drives the *same* interpreter/install it was launched from —
    no dependency on the console script being on PATH, and no version skew
    between the registry this process projected and the binary it invokes.

    This is the fully-isolated runner (its own process, its own cwd/globals). It
    is kept available for tests and callers that want process isolation, but the
    server DEFAULTS to :func:`_in_process_cli_runner` for latency (no per-call
    interpreter cold-start + registry walk). The two are parity-checked
    (``tests/test_mcp_server.py``): the same tool call through either yields an
    identical envelope + exit code.

    The wait is BOUNDED by :data:`_SUBPROCESS_RUNNER_TIMEOUT_SEC`, and the
    piped capture routes through ``infra.remote._capture_via_select`` — the
    S2-wedge-fix seam whose deadline can actually fire on Windows (kill on
    expiry, bounded post-kill drain). On expiry the child is killed and the
    call maps to exit 124 (the ``timeout(1)`` convention) with the deadline
    named on stderr.
    """
    from hpc_agent.infra.remote import _capture_via_select

    try:
        proc = _capture_via_select(
            _isolated_runner_argv(argv), timeout=_SUBPROCESS_RUNNER_TIMEOUT_SEC
        )
    except subprocess.TimeoutExpired:
        return (
            124,
            "",
            (
                f"hpc-agent {' '.join(argv)} exceeded the isolated runner's "
                f"{_SUBPROCESS_RUNNER_TIMEOUT_SEC:.0f}s deadline — child killed"
            ),
        )
    return proc.returncode, proc.stdout, proc.stderr


class _InProcessDeadlineExceeded(Exception):
    """Raised inside the in-process runner when its SIGALRM backstop fires."""


@contextlib.contextmanager
def _in_process_deadline(argv: list[str]) -> Iterator[None]:
    """Arm a SIGALRM backstop so a wedged in-process call cannot hold this
    single-threaded server forever (the same ceiling the subprocess runner
    enforces, :data:`_SUBPROCESS_RUNNER_TIMEOUT_SEC`, now applied to the DEFAULT
    runner too).

    The blocking verbs are already refused at the seam
    (:func:`_refuse_blocking_over_mcp`), so this only fires if an UNFENCED
    blocking verb reaches the default runner — a bug this backstops. A killable
    timeout cannot be built with an abandonable worker thread here: the
    in-process call writes to the server's REAL ``sys.stdout`` — the JSON-RPC
    channel — so a leaked worker would corrupt the transport (and hold journal
    locks). SIGALRM instead raises IN the wedged call, unwinding the
    ``redirect_stdout``/``redirect_stderr`` contexts (which restore the real
    streams) before control returns — no leaked thread, no corrupted transport,
    no per-call subprocess latency.

    Armed only on POSIX (``setitimer``/``SIGALRM``) from the main thread
    (``setitimer`` can be set nowhere else); elsewhere it is a no-op and the
    pre-existing unbounded behavior stands — the seam is the load-bearing guard
    either way. Any handler + pending timer already installed (e.g. a signal-mode
    ``pytest-timeout``) is saved and restored on exit.
    """
    can_arm = (
        hasattr(signal, "setitimer")
        and hasattr(signal, "SIGALRM")
        and threading.current_thread() is threading.main_thread()
        and _SUBPROCESS_RUNNER_TIMEOUT_SEC > 0
    )
    if not can_arm:
        yield
        return

    def _fire(_signum: int, _frame: Any) -> None:
        raise _InProcessDeadlineExceeded

    prev_handler = signal.signal(signal.SIGALRM, _fire)
    prev_delay, prev_interval = signal.setitimer(signal.ITIMER_REAL, _SUBPROCESS_RUNNER_TIMEOUT_SEC)
    try:
        yield
    finally:
        # Restore the prior timer + handler (best effort: elapsed time under a
        # ms-scale call is negligible against any outer second-scale deadline).
        signal.setitimer(signal.ITIMER_REAL, prev_delay, prev_interval)
        signal.signal(signal.SIGALRM, prev_handler)


def _in_process_cli_runner(argv: list[str]) -> tuple[int, str, str]:
    """Dispatch ``hpc-agent <argv...>`` IN-PROCESS via :func:`cli.dispatch.main`.

    The server process already holds the warm ``@primitive`` registry it
    projected; the subprocess runner re-paid Python cold-start + the full
    registry walk on every single tool call. This runner reuses the imported
    registry (``register_primitives`` is idempotent) and drives the SAME
    ``cli.dispatch.main(argv)`` code path, so the ``(exit_code, stdout-envelope,
    stderr)`` contract — and therefore the ``error_code`` / ``category`` /
    ``retry_safe`` / ``exit_code`` the envelope carries — is reproduced exactly,
    just without a subprocess.

    Parity mechanics: ``main`` prints the single-line JSON envelope to stdout and
    diagnostics to stderr and returns the int exit code; we capture both streams
    and the return value. ``argparse`` error paths raise ``SystemExit`` (its code
    is the exit code, ``None`` → 0); any other uncaught exception is mapped to
    exit code 1 with the traceback on stderr, matching a subprocess whose Python
    died on an uncaught exception. ``_subprocess_cli_runner`` remains the
    isolated fallback for tests / callers that want a separate process.

    State-leak audit (§7)
    ---------------------
    This runner reuses the process's module-global ``@primitive`` registry
    across every tool call, so cross-call global-state leakage was audited. It
    is CLEAN, for structural reasons:

    * **The registry is populated idempotently, never mutated per call.**
      ``register_primitives()`` is idempotent and runs once at ``build_server``;
      ``main``/verb dispatch only *reads* it. No verb registers, edits, or
      unregisters a primitive at invocation, so the shared registry cannot drift
      between calls.
    * **Per-call state is local, not module-global.** ``argv``, the captured
      ``StringIO`` stdout/stderr, and ``code`` are all locals rebound every call;
      nothing is stashed on a module attribute or default-argument object.
    * **stdout/stderr are restored deterministically.**
      ``contextlib.redirect_stdout``/``redirect_stderr`` are context managers
      that restore the real streams on exit — including on the ``SystemExit`` /
      broad-``Exception`` paths (the ``with`` unwinds before the ``except``
      body runs), so a crashing verb cannot leave the process's streams
      redirected into a dead ``StringIO`` for the next call.
    * **Durable state (journal / filesystem / cwd) is the CLI's own contract**,
      identical whether the verb runs in-process or in a subprocess — it is
      *intended* cross-call state (the decision journal is how blocks chain),
      not a leak. The subprocess runner shares it too.

    Net: the only state shared between calls is the read-only registry and the
    intended durable journal/filesystem; both are the same under the subprocess
    runner, which is why the parity test holds across a mutating + a workflow
    verb, not just ``find``.
    """
    import io
    import traceback

    from hpc_agent.cli.dispatch import main as _cli_main

    out, err = io.StringIO(), io.StringIO()
    try:
        # ``_in_process_deadline`` is the OUTERMOST context: it is entered first
        # and exited last, so a SIGALRM raised mid-call unwinds the redirect
        # contexts (restoring the real streams) before the timer is disarmed.
        with (
            _in_process_deadline(argv),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = _cli_main(list(argv))
    except _InProcessDeadlineExceeded:
        # The SIGALRM backstop fired: a call blew past the runner ceiling (should
        # be impossible — every blocking verb is refused at the seam). The
        # redirect contexts restored the real streams as the exception unwound,
        # so the process/transport is uncorrupted. Map to 124 like the
        # subprocess runner's timeout.
        return (
            124,
            "",
            (
                f"hpc-agent {' '.join(argv)} exceeded the in-process runner's "
                f"{_SUBPROCESS_RUNNER_TIMEOUT_SEC:.0f}s deadline — interrupted"
            ),
        )
    except SystemExit as exc:  # argparse / explicit sys.exit inside a verb
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    except Exception:  # noqa: BLE001 — parity: an uncaught crash is exit 1 + traceback on stderr
        err.write(traceback.format_exc())
        code = 1
    return int(code), out.getvalue(), err.getvalue()


# Conduct rule 11 (proving-run-3 finding (h)): the in-process dispatch is
# SYNCHRONOUS and single-threaded, so one blocking tools/call (a canary/array
# watch, a wait-to-terminal poll) wedges the whole server and every later call
# queues behind it — observed twice in run #3 (26-min and 20-min head-of-line
# stalls; an abandoned agent turn does NOT kill the server-side call). The
# blocking invocations are refused HERE, at the seam, with the detached path
# named — never left to skill prose.
# ``status-watch`` joined this set on 2026-07-07 (connection-broker.md): it is now
# detach-by-contract too — its monitor poll runs in a durable detached worker, so
# a blocking (detach=false) invocation over this synchronous server would wedge it
# exactly like an S2/S3/S4 watch.
# ``aggregate-run`` / ``aggregate-flow`` / ``campaign-run`` joined 2026-07-08
# (run-#10 F-K): a live ``aggregate-run`` call held the synchronous server for
# 20+ minutes with zero observability (no log, no lease). Each is now
# detach-by-contract — the combine SSH + rsync pull (or a whole campaign
# iteration) runs in a durable detached worker.
_DETACH_REQUIRED_VERBS = frozenset(
    {
        "submit-s2",
        "submit-s3",
        "submit-s4",
        "status-watch",
        "aggregate-run",
        "aggregate-flow",
        "campaign-run",
    }
)

# ``wait-detached`` is refused OUTRIGHT, not detach-gated: it is itself the
# blocking wait (a local pid-lease block that runs "potentially many
# minutes/hours" — ``ops/monitor/wait_detached.py``), so there is no
# ``detach=true`` remedy — it is not a submit that can be handed to a worker.
# The curated catalog already excludes it (its Result declares no ``next_block``
# and it is not a curated extra), but the DEFAULT ``full`` catalog and
# ``tiered`` expose it (it is ``agent_facing`` ``verb="query"``); without this
# seam refusal a client calling it there wedges the synchronous server for the
# whole wait (proving-run-3 head-of-line class). The MCP-safe alternatives are
# named in the refusal: ``poll-detached`` for an instant snapshot, or running
# ``wait-detached`` via backgrounded Bash OUTSIDE this server.
_BLOCKING_WAIT_VERBS = frozenset({"wait-detached"})

# Blocking WORKFLOW verbs whose spec has NO ``detach`` escape hatch (unlike the
# ``_DETACH_REQUIRED_VERBS``, whose specs carry ``detach``). Each is a
# poll-to-terminal loop that would hold this synchronous server for its whole
# budget — the same proving-run-3 head-of-line class — but cannot be
# detach-gated because their specs are ``extra='forbid'`` with no ``detach``
# field to set:
#   * ``monitor-flow``  — polls a run to terminal or ``wall_clock_budget_seconds``
#     (DEFAULT 86400 = 24h).
#   * ``verify-canary``  — waits on a 1-task canary to terminal (30-min default
#     poll, ``wait_budget_sec`` raisable arbitrarily).
#   * ``submit-flow``    — runs submit→(canary/record) synchronously; its canary
#     leg blocks the same way and it has no detach field to grow into.
# They are refused OUTRIGHT (like ``_BLOCKING_WAIT_VERBS``), each naming the
# MCP-safe alternative that IS detachable/instant. Curated excludes them; this
# backstops the ``full``/``tiered`` catalogs (all three are ``verb='workflow'``,
# invocable there under ``--allow-mutations``).
_BLOCKING_NO_DETACH_ALTERNATIVES: dict[str, str] = {
    "monitor-flow": (
        "for an instant read call `status-snapshot`; to watch to terminal set "
        '{"detach": true} on `status-watch` (the monitor poll runs in a '
        "detached worker) and run `hpc-agent wait-detached` via backgrounded "
        "Bash to be woken"
    ),
    "verify-canary": (
        'launch the canary via `submit-s2` with {"detach": true} (the '
        "sanctioned S2 detached canary) and run `hpc-agent wait-detached` via "
        "backgrounded Bash to be woken at completion"
    ),
    "submit-flow": (
        "drive the campaign through the block chain instead: `submit-s1` / "
        "`block-drive` return a brief immediately; for a detached submit+watch "
        'set {"detach": true} on `submit-s2`'
    ),
}


def _refuse_blocking_over_mcp(name: str, arguments: Mapping[str, Any]) -> None:
    """Raise ``_Invalid`` for tool calls that would block the server.

    ``submit-s2``/``submit-s3``/``submit-s4``/``status-watch``/``aggregate-run``/
    ``aggregate-flow``/``campaign-run`` must carry ``spec.detach == true`` (the
    detached worker + ``wait-detached`` is the sanctioned wait; the S4 /
    aggregate harvest's combine + rsync pull + breaker wait-and-retry — the
    status-watch monitor poll — and a full ``campaign-run`` submit→monitor→
    aggregate iteration can hold the line for many minutes on a throttled host).
    The detached path returns a pid handle immediately; ``wait-detached`` (via
    backgrounded Bash) wakes the caller once.

    ``wait-detached`` itself is refused outright (:data:`_BLOCKING_WAIT_VERBS`):
    it is the blocking wait, with no ``detach`` escape hatch, so over this
    synchronous server any invocation wedges the line. Curated already excludes
    it; this backstops the ``full``/``tiered`` catalogs where it is otherwise
    invocable.

    The ``monitor-flow`` / ``verify-canary`` / ``submit-flow`` workflow verbs are
    likewise refused outright (:data:`_BLOCKING_NO_DETACH_ALTERNATIVES`): they are
    poll-to-terminal loops with no ``detach`` field in their (``extra='forbid'``)
    specs, so — unlike the detach-gated verbs above — there is no ``detach=true``
    remedy; each refusal names the detachable/instant MCP-safe path instead.
    """
    if name in _BLOCKING_WAIT_VERBS:
        raise _Invalid(
            f"{name} is a BLOCKING local wait on a detached worker's lease pid "
            "(potentially many minutes/hours) with no detach=true remedy — over "
            "this synchronous server it wedges every later tool call "
            "(head-of-line; an abandoned turn does not stop it). For an instant "
            "status read call `poll-detached`; to be woken at completion run "
            "`hpc-agent wait-detached` via backgrounded Bash OUTSIDE this server."
        )
    alt = _BLOCKING_NO_DETACH_ALTERNATIVES.get(name)
    if alt is not None:
        raise _Invalid(
            f"{name} is a BLOCKING poll-to-terminal workflow with no detach=true "
            "remedy — over this synchronous server it wedges every later tool "
            "call (head-of-line; an abandoned turn does not stop it). Instead: "
            f"{alt}."
        )
    spec = arguments.get("spec")
    spec_dict = spec if isinstance(spec, dict) else {}
    if name in _DETACH_REQUIRED_VERBS and not spec_dict.get("detach"):
        raise _Invalid(
            f"{name} without detach=true is a BLOCKING scheduler watch — over "
            "this synchronous server it wedges every later tool call "
            "(head-of-line; an abandoned turn does not stop it). Set "
            '{"detach": true} in the spec; the block returns a pid handle '
            "immediately. Then run `hpc-agent wait-detached` via backgrounded "
            "Bash to be woken when the brief is ready."
        )


class _Invalid(Exception):
    """Maps to JSON-RPC ``-32602`` (invalid params) — a client contract error."""


class _MethodNotFound(Exception):
    """Maps to JSON-RPC ``-32601`` (method not found)."""


# ─── schema / projection helpers (pure) ────────────────────────────────────


def _load_input_schema(basename: str | None) -> dict[str, Any] | None:
    """Return the packaged ``schemas/<basename>.input.json`` as a dict, or None."""
    if not basename:
        return None
    try:
        from importlib.resources import files

        text = (files("hpc_agent.schemas") / f"{basename}.input.json").read_text(encoding="utf-8")
        loaded = json.loads(text)
    except (FileNotFoundError, ModuleNotFoundError, OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _arg_property(arg: Any) -> dict[str, Any]:
    """JSON-Schema property for one :class:`hpc_agent.cli._dispatch.CliArg`."""
    if arg.action in ("store_true", "store_false"):
        prop: dict[str, Any] = {"type": "boolean"}
    elif arg.type is int:
        prop = {"type": "integer"}
    else:
        prop = {"type": "string"}
    if arg.choices:
        prop["enum"] = list(arg.choices)
    if arg.help:
        prop["description"] = arg.help
    return prop


def _tool_input_schema(name: str, shape: CliShape) -> dict[str, Any]:
    """Build the MCP ``inputSchema`` for a primitive from its :class:`CliShape`.

    ``--spec`` primitives embed the packaged wire schema under a ``spec``
    object property (so a client gets the full typed contract); each
    ``CliArg`` becomes a property; ``experiment_dir`` is optional.
    """
    props: dict[str, Any] = {}
    required: list[str] = []
    if shape.experiment_dir_arg:
        props["experiment_dir"] = {
            "type": "string",
            "description": "Path to the experiment repo (default: the server's working directory).",
        }
    if shape.spec_arg:
        spec_schema = _load_input_schema(shape.schema_ref.input if shape.schema_ref else None)
        props["spec"] = spec_schema or {"type": "object", "description": "JSON spec object."}
        if shape.spec_required:
            required.append("spec")
    for arg in shape.args:
        key = arg.attr_name()
        props[key] = _arg_property(arg)
        # Positional args (flag without a leading dash) are always required by
        # argparse; a flag is required only when it declares so.
        if not arg.flag.startswith("-") or arg.required:
            required.append(key)
    schema: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    # The spec sub-object may legitimately carry extra keys; only the top
    # level is closed.
    schema["additionalProperties"] = False
    return schema


def _declares_next_block(meta: PrimitiveMeta) -> bool:
    """True when *meta*'s primitive returns a Result model declaring ``next_block``.

    The structural definition of a **block** (design §2/§3): a verb whose Result
    model carries the machine-computed ``next_block`` field. Derived — never a
    hardcoded verb list — so the curated MCP catalog tracks exactly the block
    verbs N marked, and adding/removing a ``next_block`` field on a Result model
    moves the verb in/out of the curated set automatically.

    Resolution is deliberately narrow to stay robust under ``from __future__
    import annotations``: the return annotation is a bare model NAME string
    (e.g. ``"SubmitBlockResult"``) resolved ONLY against the function's own
    module globals — NEVER ``typing.get_type_hints`` over the whole signature,
    which would choke on ``TYPE_CHECKING``-only param annotations (``Path``) that
    the block funcs use. A non-name return (``dict[str, Any]``) resolves to None
    → not a block.
    """
    func = meta.func
    ret = getattr(func, "__annotations__", {}).get("return")
    model: Any = None
    if isinstance(ret, str):
        model = getattr(func, "__globals__", {}).get(ret)
    elif isinstance(ret, type):
        model = ret
    fields = getattr(model, "model_fields", None)
    return isinstance(fields, dict) and "next_block" in fields


def _tool_definition(name: str, meta: PrimitiveMeta) -> dict[str, Any]:
    """Project one primitive into an MCP tool definition."""
    shape = meta.cli
    assert isinstance(shape, CliShape)  # callers filter on this
    return {
        "name": name,
        "title": name,
        "description": (meta.description or shape.help or name).strip(),
        "inputSchema": _tool_input_schema(name, shape),
        "annotations": {
            "title": name,
            "readOnlyHint": meta.verb in _READ_ONLY_VERBS,
            "destructiveHint": meta.verb in ("mutate", "submit"),
            "idempotentHint": bool(meta.idempotent),
        },
    }


def allowed_primitives(
    registry: Mapping[str, PrimitiveMeta], *, allow_mutations: bool
) -> dict[str, PrimitiveMeta]:
    """The primitives this server may expose/invoke, gated by the safety policy.

    Always includes ``query`` / ``validate`` primitives that have a CLI shape.
    Includes mutating verbs only when *allow_mutations* is set. This is the one
    place the read/act boundary is enforced; both catalog modes and the
    ``tools/call`` guard consult it, so a tiered-mode ``run-primitive`` can
    never reach a verb the policy forbids.
    """
    out: dict[str, PrimitiveMeta] = {}
    for prim_name, meta in registry.items():
        if not isinstance(meta.cli, CliShape):
            continue
        if meta.verb in _READ_ONLY_VERBS or (allow_mutations and meta.verb in _MUTATING_VERBS):
            out[prim_name] = meta
    return out


def _build_invocation(
    name: str, shape: CliShape, arguments: Mapping[str, Any], spec_path: str | None
) -> list[str]:
    """Render the ``hpc-agent`` argv for a tool call (binary name excluded)."""
    argv: list[str] = []
    if shape.group:
        argv.append(shape.group)
    argv.append(_leaf_verb(name, shape))

    positionals: list[str] = []
    optionals: list[str] = []
    for arg in shape.args:
        key = arg.attr_name()
        if key not in arguments or arguments[key] is None:
            continue
        value = arguments[key]
        if not arg.flag.startswith("-"):
            positionals.append(str(value))
        elif arg.action in ("store_true", "store_false"):
            if value:
                optionals.append(arg.flag)
        else:
            optionals.extend([arg.flag, str(value)])

    if shape.experiment_dir_arg and arguments.get("experiment_dir"):
        optionals.extend(["--experiment-dir", str(arguments["experiment_dir"])])
    if shape.spec_arg and spec_path is not None:
        optionals.extend(["--spec", spec_path])

    return argv + positionals + optionals


def _parse_envelope(stdout: str) -> dict[str, Any] | None:
    """Parse the single-line JSON envelope from CLI stdout (last non-blank line)."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _tool_result(exit_code: int, stdout: str, stderr: str) -> dict[str, Any]:
    """Map a CLI ``(exit_code, stdout, stderr)`` into an MCP tool result.

    Preserves the CLI's machine-readable failure contract: the full envelope
    (``error_code`` / ``category`` / ``retry_safe`` / ``remediation`` / ``data``)
    plus the process ``exit_code`` ride in ``structuredContent``, and
    ``isError`` is true on a non-zero exit or an ``ok:false`` envelope.
    """
    envelope = _parse_envelope(stdout)
    ok = isinstance(envelope, dict) and envelope.get("ok") is True
    is_error = exit_code != 0 or not ok
    if isinstance(envelope, dict):
        structured = dict(envelope)
    else:
        structured = {
            "ok": False,
            "error_code": "internal",
            "category": "internal",
            "retry_safe": False,
            "message": "hpc-agent produced no parseable JSON envelope.",
            "raw_stdout": stdout[-2000:],
            "raw_stderr": stderr[-2000:],
        }
    structured["exit_code"] = exit_code
    return {
        "content": [{"type": "text", "text": json.dumps(structured, sort_keys=True)}],
        "structuredContent": structured,
        "isError": is_error,
    }


def _read_command_md(name: str) -> str | None:
    """Return the body of a bundled ``slash_commands/commands/<name>.md`` file."""
    try:
        from importlib.resources import files

        body = (files("hpc_agent.slash_commands") / "commands" / f"{name}.md").read_text(
            encoding="utf-8"
        )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return None
    return body


def _strip_frontmatter(body: str) -> tuple[dict[str, str], str]:
    """Split a leading ``---`` YAML-ish frontmatter block from a markdown body.

    Returns ``(frontmatter_kv, remaining_body)``. The parse is intentionally
    minimal (``key: value`` lines only) — enough to lift a ``description:`` for
    the prompt listing without a YAML dependency.
    """
    if not body.startswith("---"):
        return {}, body
    close = body.find("\n---", 3)
    if close == -1:
        return {}, body
    front = body[3:close]
    rest = body[close + 4 :]
    kv: dict[str, str] = {}
    for line in front.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            kv[key.strip()] = val.strip().strip("'\"")
    return kv, rest.lstrip("\n")


# ─── the server ─────────────────────────────────────────────────────────────


class McpServer:
    """Projects the primitive registry as an MCP server over JSON-RPC 2.0."""

    def __init__(
        self,
        *,
        registry: Mapping[str, PrimitiveMeta],
        allow_mutations: bool = False,
        catalog: str = "full",
        runner: CliRunner | None = None,
    ) -> None:
        if catalog not in ("full", "tiered", "curated"):
            raise ValueError(f"catalog must be 'full', 'tiered', or 'curated', got {catalog!r}")
        self._registry = registry
        self._allow_mutations = allow_mutations
        self._catalog = catalog
        # Default to the WARM in-process runner (reuses this process's registry;
        # no per-call interpreter cold-start). ``_subprocess_cli_runner`` (full
        # process isolation) is no longer the default but stays injectable as the
        # isolation fallback + the parity oracle the in-process runner is checked
        # against (tests/test_mcp_curated.py).
        self._runner = runner or _in_process_cli_runner
        # ── bidirectional pump state (D1) ──────────────────────────────────
        # The outbound write stream + the reader thread's message queue are
        # threaded on by ``serve`` before its loop and are ``None`` otherwise —
        # so any embedding that never calls ``serve`` (every direct-``handle``
        # unit test) has NO transport, and elicitation is structurally
        # unavailable there (:meth:`_request_from_client` returns the
        # decline-equivalent ``None`` immediately). D1 item 5.
        self._transport: IO[str] | None = None
        self._msg_queue: queue.Queue[Any] | None = None
        # Monotonic counter behind the ``hpc-srv-<n>`` outbound id namespace
        # (D1 item 1) — a distinct string space that can never collide with a
        # client-chosen id.
        self._outbound_counter: int = 0
        # The pending-response slot (D1 item 2): the id of the ONE
        # server-originated request currently in flight, or ``None``. Size ≤ 1
        # (D3) — an invariant asserted in :meth:`_request_from_client`, never a
        # queue, because dispatch is single-threaded (the reader thread only
        # enqueues).
        self._pending_id: str | None = None
        # True while a client request is being dispatched INSIDE an elicitation
        # wait: a nested tool call that would itself elicit takes the degrade
        # path instead (D3 re-entrancy). E4 reads this at the firing site.
        self._elicitation_suppressed: bool = False
        # Per-session client capability, negotiated at ``initialize`` (D2) — set
        # from ``params["capabilities"]["elicitation"]``; elicitation fires only
        # when this is true (the gate check lands in E4).
        self._client_elicitation: bool = False
        # ADAPTIVE DEGRADATION (notebook-audit item 12 / Addendum 7, run #11): a
        # client can DECLARE elicitation at ``initialize`` yet render no popup, so
        # a refusal becomes a silent 300s stall (all journal locks probed free).
        # When an elicitation times out with NO response of ANY kind (silence —
        # NOT a human DECLINE, which IS a response), the channel is marked dark and
        # every later authorship refusal this session degrades to the hook path
        # IMMEDIATELY (the same plain-refusal path a never-declaring client takes).
        # A capability declaration is a claim, not a proof; it is re-probed next
        # session (this is per-session state, reset on construction).
        self._client_elicitation_dark: bool = False

    # -- projection ---------------------------------------------------------

    def _allowed(self) -> dict[str, PrimitiveMeta]:
        return allowed_primitives(self._registry, allow_mutations=self._allow_mutations)

    def _cli_primitives(self) -> dict[str, PrimitiveMeta]:
        """All primitives with a CLI shape, regardless of the mutation gate.

        The mutation-independent base for the ``curated`` catalog: curated is
        itself a deliberate allowlist (its block verbs are all inherently
        ``workflow``-typed), so it is NOT re-gated by ``--allow-mutations`` — see
        :meth:`_curated_metas`.
        """
        return {
            name: meta for name, meta in self._registry.items() if isinstance(meta.cli, CliShape)
        }

    def _curated_metas(self) -> dict[str, PrimitiveMeta]:
        """The curated catalog's verb→meta map: derived blocks ∪ the fixed extras.

        A block is any verb whose Result model declares ``next_block``
        (:func:`_declares_next_block`) — derived, not hardcoded — unioned with
        the stable recovery/opt-in extras (:data:`_CURATED_EXTRA_VERBS`).

        Computed off :meth:`_cli_primitives`, NOT :meth:`_allowed`: the
        ``--allow-mutations ∩ curated`` intersection was vestigial (design §7).
        Curated is already the boundary — an authored allowlist of inherently
        mutating block verbs — and the verb-level guards (greenlight gate, drift
        guard, idempotency) still fire at invocation regardless of the flag, so
        gating the *listing* on ``allow_mutations`` only mis-hid the read-only
        ``workflow`` blocks (``status-snapshot`` / ``aggregate-check`` /
        ``campaign-watch``). ``full``/``tiered`` stay gated by :meth:`_allowed`.
        """
        base = self._cli_primitives()
        names = {name for name, meta in base.items() if _declares_next_block(meta)}
        names |= {v for v in _CURATED_EXTRA_VERBS if v in base}
        return {name: base[name] for name in sorted(names)}

    def _curated_names(self) -> list[str]:
        """The curated catalog's verb set (keys of :meth:`_curated_metas`)."""
        return list(self._curated_metas())

    def _invocable(self) -> dict[str, PrimitiveMeta]:
        """Primitives this server may INVOKE, per the active catalog's boundary.

        ``curated`` is its own allowlist (design §7 — see :meth:`_curated_metas`),
        so its block verbs are callable regardless of ``--allow-mutations``; the
        verb-level guards enforce. ``full``/``tiered`` invoke only what the
        read/act policy (:meth:`_allowed`) permits. Keeping the *call* gate and
        the *listing* on the same set means curated never advertises a tool it
        would then refuse to run.
        """
        if self._catalog == "curated":
            return self._curated_metas()
        return self._allowed()

    def list_tools(self) -> list[dict[str, Any]]:
        allowed = self._allowed()
        if self._catalog == "curated":
            # A small, human-amplification-shaped surface: the block verbs
            # (derived from a ``next_block`` Result field) plus the recovery /
            # opt-in extras — each as its own typed tool. NOT gated by
            # ``allow_mutations`` (design §7 — curated is itself the allowlist).
            curated = self._curated_metas()
            return [_tool_definition(name, meta) for name, meta in curated.items()]
        if self._catalog == "tiered":
            # Mirror the CLI's find → describe → invoke discovery: advertise
            # only the explorers plus a generic invoker, keeping every
            # primitive's per-tool schema out of the model's context until
            # pulled on demand via `describe`.
            tools: list[dict[str, Any]] = []
            for explorer in ("find", "describe"):
                if explorer in allowed:
                    tools.append(_tool_definition(explorer, allowed[explorer]))
            tools.append(self._run_primitive_definition())
            return tools
        return [_tool_definition(name, meta) for name, meta in sorted(allowed.items())]

    def _run_primitive_definition(self) -> dict[str, Any]:
        verbs = "query/validate" if not self._allow_mutations else "any registered"
        return {
            "name": _RUN_PRIMITIVE_TOOL,
            "title": "run an hpc-agent primitive by name",
            "description": (
                "Invoke any exposed hpc-agent primitive by name. Use `find` to "
                "search the catalog and `describe <name>` to fetch a primitive's "
                f"contract first. Only {verbs} primitives are reachable."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The primitive's wire name."},
                    "arguments": {
                        "type": "object",
                        "description": 'The primitive\'s arguments (e.g. {"spec": {...}}).',
                    },
                },
                "required": ["name"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": not self._allow_mutations},
        }

    # -- tools/call ---------------------------------------------------------

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name == _RUN_PRIMITIVE_TOOL:
            inner = arguments.get("name")
            if not isinstance(inner, str):
                raise _Invalid(f"{_RUN_PRIMITIVE_TOOL} requires a string 'name'")
            inner_args = arguments.get("arguments") or {}
            if not isinstance(inner_args, dict):
                raise _Invalid(f"{_RUN_PRIMITIVE_TOOL} 'arguments' must be an object")
            return self.call_tool(inner, inner_args)

        _refuse_blocking_over_mcp(name, arguments)
        meta = self._invocable().get(name)
        if meta is None:
            # Either the primitive does not exist or the safety policy forbids
            # it (a mutating verb without ``allow_mutations`` under the
            # full/tiered catalogs; the curated catalog is its own allowlist and
            # does not gate on the flag). Both are client contract errors, not
            # tool failures.
            raise _Invalid(
                f"tool {name!r} is not available. It is either unknown or a "
                "mutating verb that requires the server to be started with "
                "--allow-mutations."
            )
        shape = meta.cli
        assert isinstance(shape, CliShape)

        result = self._invoke_cli(name, shape, arguments)
        # The elicitation firing site (D4 + the D6 amendment, user-ruled 2026-07-09):
        # the ``append-decision`` sign-off popup is the PRIMARY read-and-sign channel,
        # not a retry-only fallback. When the authorship gate would refuse (no
        # matching human utterance) the server ELICITS FIRST and the append proceeds
        # with the typed utterance — the model NEVER sees the interim refusal (this
        # `call_tool` is atomic; the CLI runs, the popup collects the sign-off, the
        # invocation re-runs, and only the final verdict returns). An utterance that
        # ALREADY passes the gate (ok:true) returns straight through — no popup on a
        # valid append. The FALLBACK (the plain refusal → hook path) is taken exactly
        # when elicitation is unavailable: an undeclared or declared-but-dark client,
        # no transport, or a suppressed nested dispatch — :meth:`_elicitation_applies`
        # gates all of it, so a client without the channel behaves byte-for-byte as
        # before this promotion.
        if self._elicitation_applies(name, result):
            return self._elicit_then_retry(name, shape, arguments, result)
        return result

    def _invoke_cli(
        self, name: str, shape: CliShape, arguments: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Drive one CLI invocation for *name* and map it to an MCP tool result.

        The transport-free core of :meth:`call_tool`: writes the spec temp file,
        renders the argv, runs the injected runner (with per-call telemetry), and
        maps ``(exit_code, stdout, stderr)`` through :func:`_tool_result`. The E4
        retry re-runs THIS (never :meth:`call_tool`), so a retry can never itself
        re-enter the elicitation firing site — the retry-once bound is structural.
        """
        spec_path: str | None = None
        spec = arguments.get("spec")
        try:
            if shape.spec_arg and spec is not None:
                fd, spec_path = tempfile.mkstemp(prefix="hpc-mcp-spec-", suffix=".json")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(spec, fh)
            argv = _build_invocation(name, shape, arguments, spec_path)
            started = time.perf_counter()
            # Mid-call liveness (run-#12 finding 3): a long verb showed the
            # human NOTHING between dispatch and result — three separate
            # "is it hung?" investigations in one night. One stderr line every
            # ~15s on the same tail-able surface as the per-call telemetry;
            # the daemon timer dies with the call.
            #
            # Capture the REAL stderr handle HERE, before the runner runs: the
            # default ``_in_process_cli_runner`` wraps dispatch in
            # ``contextlib.redirect_stderr``, which rebinds ``sys.stderr``
            # PROCESS-WIDE across every thread for the call's duration. A
            # heartbeat that resolved ``sys.stderr`` at write time would land in
            # that captured StringIO (then be discarded by ``_tool_result``) —
            # swallowing exactly the lines this feature exists to emit. Binding
            # the pre-redirect handle keeps the heartbeat on the tail-able MCP
            # log where the human is watching.
            real_err = sys.stderr
            _hb_stop = threading.Event()

            def _heartbeat() -> None:
                while not _hb_stop.wait(_HEARTBEAT_INTERVAL_SEC):
                    real_err.write(
                        f"[mcp] {name} still running ({int(time.perf_counter() - started)}s)\n"
                    )

            threading.Thread(target=_heartbeat, daemon=True).start()
            try:
                exit_code, stdout, stderr = self._runner(argv)
            finally:
                _hb_stop.set()
            # Per-call telemetry (2026-07-04): "why is MCP slow" must be a
            # measurement, not a mystery. One stderr line per tools/call —
            # stderr rides the harness's MCP log, never the JSON-RPC channel.
            sys.stderr.write(
                f"[mcp] {name} {int((time.perf_counter() - started) * 1000)}ms exit={exit_code}\n"
            )
        finally:
            if spec_path is not None:
                with contextlib.suppress(OSError):
                    os.unlink(spec_path)
        return _tool_result(exit_code, stdout, stderr)

    # -- elicitation firing site (E4) ---------------------------------------

    def _elicitation_applies(self, name: str, result: Mapping[str, Any]) -> bool:
        """Whether *result* is an authorship refusal this session may re-elicit.

        Every leg is required (D4 step 2): the tool is ``append-decision`` (D6 —
        the sole firing site), the client negotiated elicitation at initialize
        (:attr:`_client_elicitation`, D2) AND that channel has not gone dark
        (:attr:`_client_elicitation_dark` — a prior elicitation this session timed
        out with no response of any kind, so the declaration is treated as unproven
        and every later refusal degrades to the hook path; item 12 / Addendum 7),
        a live transport exists, no elicitation is already in flight and none is
        suppressed (nested dispatch takes the degrade path), and the envelope is
        ``ok:false`` carrying E2's distinct ``authorship_evidence`` KEY in
        ``failure_features`` — never the block's mere presence (the synthesized
        spec_invalid default), never prose.
        """
        if name != _ELICITATION_FIRING_TOOL:
            return False
        if not self._client_elicitation or self._client_elicitation_dark:
            return False
        if self._transport is None or self._msg_queue is None:
            return False
        if self._elicitation_suppressed or self._pending_id is not None:
            return False
        structured = result.get("structuredContent")
        if not isinstance(structured, dict) or structured.get("ok") is not False:
            return False
        features = structured.get("failure_features")
        return isinstance(features, dict) and _AUTHORSHIP_EVIDENCE_KEY in features

    def _elicit_then_retry(
        self,
        name: str,
        shape: CliShape,
        arguments: Mapping[str, Any],
        refusal: dict[str, Any],
    ) -> dict[str, Any]:
        """Elicit a typed sign-off (the PRIMARY channel), append it, re-run once (D4 + D6).

        This is the promoted primary read-and-sign path (D6 amendment, 2026-07-09):
        the popup fires BEFORE any refusal reaches the model, collects the human's
        typed sign-off, and the append proceeds with it — the re-run is the mechanism
        that lands the now-present utterance, not a second user-visible attempt.

        Sends ``elicitation/create`` with the code-rendered prompt (D5) and the
        free-text-only schema (D3), filters the response
        (:func:`_accepted_utterance`), and on captured text calls
        :func:`state.utterances.append_utterance` — the server process is
        harness-side code, the contract's specified handler. Then re-runs the
        IDENTICAL CLI invocation EXACTLY once; the gate re-checks against the
        now-present utterance and a second refusal stands (never a loop). Any
        non-capture outcome (decline/cancel/timeout/EOF/injected/empty, or a
        no-op append) returns the ORIGINAL *refusal* unchanged — no utterance
        appended, never a JSON-RPC error.

        Wait disclosure (item 12 leg b / Addendum 10's no-black-box contract): the
        wait is not dead air. One ``[mcp]`` line at OPEN names the tool + deadline
        and one at CLOSE names the outcome (answered / declined / timed-out-dark),
        landing on the same tail-able stderr surface the per-call telemetry uses
        (the harness's MCP log, never the JSON-RPC channel).

        Adaptive degradation (item 12 leg a): a ``None`` from the wait is SILENCE
        (timeout / EOF — the transport is present, :meth:`_elicitation_applies`
        guaranteed it), which is DISTINCT from a human DECLINE (a real response
        whose filtered text is empty). Silence marks the channel dark so the rest
        of this session skips elicitation; a decline leaves it live.
        """
        from pathlib import Path

        exp = arguments.get("experiment_dir")
        experiment_dir = Path(exp) if isinstance(exp, str) and exp else Path.cwd()

        timeout = _ELICITATION_TIMEOUT_SEC
        sys.stderr.write(
            f"[mcp] waiting on human elicitation ({timeout:.0f}s timeout) for {name}\n"
        )
        response = self._request_from_client(
            "elicitation/create",
            {
                # E-render: the popup carries the code-computed render digest for a
                # notebook sign-off (bytes read off disk here, model suspended).
                "message": _render_elicitation_prompt(arguments, experiment_dir),
                "requestedSchema": _ELICITATION_REQUESTED_SCHEMA,
            },
            timeout,
        )
        if response is None:
            # SILENCE — no response of any kind within the deadline (or EOF). The
            # client declared elicitation but rendered nothing (run #11): mark the
            # channel dark so subsequent authorship refusals return the plain
            # refusal immediately, and log the close outcome.
            self._client_elicitation_dark = True
            sys.stderr.write(
                f"[mcp] elicitation channel DARK for {name}: no response within "
                f"{timeout:.0f}s — degrading to the hook path for the rest of this "
                "session (timed-out-dark)\n"
            )
            return refusal
        text = _accepted_utterance(response)
        if text is None:
            # A real response arrived (decline / cancel / injected / empty): the
            # human saying no is a valid outcome, not a fault — the channel stays
            # LIVE, never marked dark.
            sys.stderr.write(
                f"[mcp] elicitation for {name}: human response, no sign-off (declined)\n"
            )
            return refusal
        sys.stderr.write(f"[mcp] elicitation for {name}: sign-off captured (answered)\n")

        from hpc_agent.state.utterances import append_utterance

        # ``experiment_dir`` was resolved above (shared with the render-digest read).
        # For an overnight standing consent the capture is BOUND to the coverage the
        # popup named (USER RULING 3, docs/design/bound-capture.md): the gate then
        # matches this exact binding instead of word-overlapping the chat stream. A
        # non-overnight sign-off binds nothing (``None``) — byte-identical to before.
        bound = _overnight_consent_binding(arguments)
        record = append_utterance(experiment_dir, text, bound=bound)
        if record is None:
            # Fail-open (no namespace / unwritable log): nothing was recorded, so
            # a retry would re-refuse identically — return the original refusal.
            return refusal

        retried = self._invoke_cli(name, shape, arguments)
        # On capture the RESULT carries the fingerprint, NEVER the text (D5): the
        # model learns the gate's verdict from the retried envelope, and the
        # sha256 of the recorded utterance — not the human's words.
        return _with_capture_markers(retried, record["sha256"])

    # -- resources ----------------------------------------------------------

    def list_resources(self) -> list[dict[str, Any]]:
        return [
            {
                "uri": uri,
                "name": uri.rsplit("/", 1)[-1],
                "description": description,
                "mimeType": "application/json",
            }
            for uri, (_argv, description) in _RESOURCES.items()
        ]

    def read_resource(self, uri: Any) -> dict[str, Any]:
        entry = _RESOURCES.get(uri) if isinstance(uri, str) else None
        if entry is None:
            raise _Invalid(f"unknown resource uri {uri!r}")
        argv, _description = entry
        _exit_code, stdout, stderr = self._runner(list(argv))
        text = stdout.strip() or stderr.strip()
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": text}]}

    # -- prompts ------------------------------------------------------------

    def list_prompts(self) -> list[dict[str, Any]]:
        # The prompt SET is projected from the workflow-entry table (§6); the
        # human description body is still lifted from the packaged command .md,
        # falling back to the entry's start-the-driver instruction when the .md
        # is not installed — the table stays the source of truth either way.
        prompts: list[dict[str, Any]] = []
        for name in _PROMPT_NAMES:
            entry = WORKFLOW_ENTRIES_BY_PROMPT[name]
            body = _read_command_md(name)
            if body is None:
                desc = f"/{name} — start the {entry.name} workflow"
                prompts.append({"name": name, "description": desc})
                continue
            front, _rest = _strip_frontmatter(body)
            prompts.append({"name": name, "description": front.get("description", f"/{name}")})
        return prompts

    def get_prompt(self, name: Any, _arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in _PROMPT_NAMES:
            raise _Invalid(f"unknown prompt {name!r}")
        entry = WORKFLOW_ENTRIES_BY_PROMPT[str(name)]
        # The MESSAGE BODY is ALWAYS the canonical entry's executable
        # ``start_instruction`` (§6): it drives ``block-drive`` + commits via
        # ``append-decision`` — the curated verbs an MCP-only client actually
        # has. The packaged slash ``.md`` body is authored for Claude-Code: it
        # instructs the agent to invoke the Skill tool, run Bash, and call
        # CronCreate, none of which exist over MCP ("An MCP client has no shell"
        # — module docstring). Serving that verbatim dead-ends the client or
        # pushes it to hand-author specs (the finding-13/17 spec-corruption
        # class). So the ``.md`` only supplies the human-readable ``description``;
        # the table stays the source of truth for what to RUN.
        body = _read_command_md(str(name))
        description = f"/{name} — start the {entry.name} workflow"
        if body is not None:
            front, _rest = _strip_frontmatter(body)
            description = front.get("description", description)
        return {
            "description": description,
            "messages": [
                {"role": "user", "content": {"type": "text", "text": entry.start_instruction}}
            ],
        }

    # -- JSON-RPC -----------------------------------------------------------

    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        # Per-session elicitation negotiation (D2): the 2025-06-18 revision
        # declares client elicitation support as a ``capabilities.elicitation``
        # object (``{}`` when supported). Presence of the KEY is the signal —
        # not truthiness, since the declared value is an empty object. Absent →
        # the channel degrades to the hook path, silently and honestly. These
        # params were previously discarded; this is a store, not new plumbing.
        client_caps = params.get("capabilities")
        self._client_elicitation = isinstance(client_caps, dict) and "elicitation" in client_caps
        if self._catalog == "tiered":
            catalog_note = (
                "Tiered catalog: use the `find` and `describe` tools to discover "
                "primitives, then `run-primitive` to invoke one."
            )
        elif self._catalog == "curated":
            catalog_note = (
                "Curated catalog: the human-amplification block verbs (each "
                "returns a next_block suggestion), the loop driver `block-drive` "
                "and the greenlight commit `append-decision`, plus the "
                "recovery/opt-in verbs (doctor, kill, net-triage, submit-speculate) "
                "are exposed as typed tools. Drive the submit/aggregate/campaign "
                "loops via `block-drive` and commit each `y` via `append-decision` "
                "— do not hand-author specs on the CLI."
            )
        else:
            catalog_note = "Each read-only primitive is exposed as its own typed tool."
        mutation_note = (
            "Mutating verbs (submit/aggregate/scaffold) ARE exposed."
            if self._allow_mutations
            else "Read-only (query/validate) verbs only; mutating verbs are gated off."
        )
        # Fingerprinted version (``0.10.65+g<sha>``): the version *number*
        # alone cannot express skew between installs of the same release, and
        # the instructions explicitly tell clients to compare versions — so
        # serverInfo carries the commit identity too. Lazy import; resolved
        # once per server (initialize), fail-open to the bare number.
        from hpc_agent._build_info import full_version

        server_version = full_version()
        return {
            "protocolVersion": requested if isinstance(requested, str) else _PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"listChanged": False},
                "prompts": {"listChanged": False},
            },
            "serverInfo": {"name": "hpc-agent", "version": server_version},
            "instructions": (
                f"hpc-agent {server_version} MCP server. Tools mirror the "
                "`hpc-agent` CLI registry (`hpc-agent capabilities`); each tool "
                "result carries the full CLI envelope in structuredContent "
                "(error_code, category, retry_safe, exit_code). "
                f"{mutation_note} {catalog_note} Compare serverInfo.version against "
                "your client's expected hpc-agent version to detect skew."
            ),
        }

    def _dispatch(self, method: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.list_tools()}
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str):
                raise _Invalid("tools/call requires a string 'name'")
            arguments = params.get("arguments") or {}
            if not isinstance(arguments, dict):
                raise _Invalid("tools/call 'arguments' must be an object")
            return self.call_tool(name, arguments)
        if method == "resources/list":
            return {"resources": self.list_resources()}
        if method == "resources/read":
            return self.read_resource(params.get("uri"))
        if method == "prompts/list":
            return {"prompts": self.list_prompts()}
        if method == "prompts/get":
            return self.get_prompt(params.get("name"), params.get("arguments") or {})
        raise _MethodNotFound()

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def handle(self, request: Any) -> dict[str, Any] | None:
        """Dispatch one JSON-RPC request; return its response, or None for a notification."""
        if not isinstance(request, dict):
            return self._error(None, -32600, "invalid request")
        is_notification = "id" not in request
        req_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return None if is_notification else self._error(req_id, -32602, "params must be object")
        try:
            result = self._dispatch(method, params)
        except _Invalid as exc:
            return None if is_notification else self._error(req_id, -32602, str(exc))
        except _MethodNotFound:
            if is_notification:
                return None
            return self._error(req_id, -32601, f"method not found: {method}")
        except Exception as exc:  # noqa: BLE001 — any handler bug becomes a JSON-RPC error, not a crash
            if is_notification:
                return None
            return self._error(req_id, -32603, f"internal error: {exc}")
        return None if is_notification else {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _reader_loop(self, stdin: IO[str], q: queue.Queue[Any]) -> None:
        """The SOLE stdin reader (D1 item 6): parse lines, enqueue, never dispatch.

        Runs on one daemon thread. It reads newline-delimited lines, parses each
        as JSON, and pushes the result onto *q* — a parsed message dict, or
        :data:`_PARSE_ERROR` for a non-JSON line. On stdin close it pushes
        :data:`_EOF` exactly once (the ``finally`` guarantees it even if the
        iterator raises). It touches NO handler and NO registry, so dispatch
        stays single-threaded and the state-leak / re-entrancy analyses hold.
        """
        try:
            for raw in stdin:
                line = raw.strip()
                if not line:
                    continue
                try:
                    q.put(json.loads(line))
                except json.JSONDecodeError:
                    q.put(_PARSE_ERROR)
        finally:
            q.put(_EOF)

    def serve(self, stdin: IO[str], stdout: IO[str]) -> None:
        """Run the JSON-RPC loop as a consumer of the reader thread's queue.

        The blocking ``readline`` loop is gone: one daemon thread
        (:meth:`_reader_loop`) is the sole stdin reader, and this loop consumes
        the queue it feeds. That indirection is what lets an in-flight
        elicitation impose a real deadline (:meth:`_request_from_client` calls
        ``Queue.get(timeout=…)`` on the SAME queue). The transport (``stdout``)
        and the queue are threaded onto the instance for the wait primitive's
        duration, and cleared on exit so a later direct-``handle`` embedding sees
        no stale transport (D1 item 5).
        """
        q: queue.Queue[Any] = queue.Queue()
        self._transport = stdout
        self._msg_queue = q
        reader = threading.Thread(
            target=self._reader_loop, args=(stdin, q), name="mcp-stdin-reader", daemon=True
        )
        reader.start()
        try:
            while True:
                item = q.get()
                if item is _EOF:
                    break
                self._consume_message(item, stdout)
        finally:
            self._transport = None
            self._msg_queue = None

    def _consume_message(self, item: Any, stdout: IO[str]) -> None:
        """Classify one dequeued message and act (top-level, no elicitation in flight).

        Message-kind dispatch (D1 item 3): a :data:`_PARSE_ERROR` sentinel emits
        a parse-error response; a dict with ``"method"`` is a request/
        notification handled by :meth:`handle`; a dict that is a RESPONSE
        (``"id"`` + ``"result"``/``"error"``, no ``"method"``) can only be a late
        or unknown server-request response at the top level (no wait is in
        flight here — the wait primitive drains the queue itself while blocked),
        so it is dropped silently with a ``[mcp]`` telemetry line.
        """
        if item is _PARSE_ERROR:
            self._write(stdout, self._error(None, -32700, "parse error"))
            return
        if isinstance(item, dict) and "method" in item:
            response = self.handle(item)
            if response is not None:
                self._write(stdout, response)
            return
        if _is_response(item):
            sys.stderr.write(f"[mcp] dropped unexpected response id={item.get('id')!r}\n")
            return
        # Neither a request/notification nor a recognizable response.
        self._write(stdout, self._error(None, -32600, "invalid request"))

    def _next_outbound_id(self) -> str:
        """The next collision-proof server-originated request id (D1 item 1)."""
        self._outbound_counter += 1
        return f"hpc-srv-{self._outbound_counter}"

    def _request_from_client(
        self, method: str, params: Mapping[str, Any], timeout_s: float = _ELICITATION_TIMEOUT_SEC
    ) -> dict[str, Any] | None:
        """Send a server-originated request and block for its response (D1 item 4).

        Writes the outbound request under a fresh ``hpc-srv-<n>`` id, then
        consumes the reader thread's queue with a real ``Queue.get(timeout=…)``
        deadline until the matching response arrives. Returns the raw JSON-RPC
        response dict on a match, or ``None`` for every decline-equivalent
        outcome (no transport, timeout, or EOF) — the caller (E4) maps ``None``
        to the gate's ordinary refusal.

        While blocked it services interleaved client REQUESTS inline, with
        elicitation SUPPRESSED for that nested dispatch (D3) — so a waiting
        elicitation never head-of-line-blocks the session, and a nested tool
        call that would itself elicit takes the degrade path. A response for any
        other id (a late one for a timed-out request) is dropped silently.
        """
        # Absent transport ⇒ elicitation is structurally unavailable (D1 item 5):
        # every direct-``handle`` test and any non-``serve`` embedding lands here.
        transport = self._transport
        q = self._msg_queue
        if transport is None or q is None:
            return None
        # Depth cap (D3): at most one elicitation in flight. An invariant, not a
        # queue — dispatch is single-threaded, so a second concurrent request
        # cannot arise; this asserts that structural fact.
        assert self._pending_id is None, "elicitation depth cap violated (one in flight)"

        out_id = self._next_outbound_id()
        self._pending_id = out_id
        deadline = time.monotonic() + timeout_s
        try:
            self._write(
                transport,
                {"jsonrpc": "2.0", "id": out_id, "method": method, "params": dict(params)},
            )
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    sys.stderr.write(
                        f"[mcp] elicitation {out_id} timed out after {timeout_s:.0f}s — decline\n"
                    )
                    return None
                try:
                    item = q.get(timeout=remaining)
                except queue.Empty:
                    sys.stderr.write(
                        f"[mcp] elicitation {out_id} timed out after {timeout_s:.0f}s — decline\n"
                    )
                    return None
                if item is _EOF:
                    # stdin closed mid-wait: decline-equivalent, then let the
                    # serve loop shut down by re-enqueuing the sentinel.
                    q.put(_EOF)
                    sys.stderr.write(
                        f"[mcp] EOF during elicitation {out_id} — decline + shutdown\n"
                    )
                    return None
                if item is _PARSE_ERROR:
                    self._write(transport, self._error(None, -32700, "parse error"))
                    continue
                if isinstance(item, dict) and "method" in item:
                    self._dispatch_interleaved(item, transport)
                    continue
                if _is_response(item):
                    if item.get("id") == out_id:
                        return dict(item)
                    sys.stderr.write(f"[mcp] dropped unexpected response id={item.get('id')!r}\n")
                    continue
                self._write(transport, self._error(None, -32600, "invalid request"))
        finally:
            self._pending_id = None

    def _dispatch_interleaved(self, item: dict[str, Any], transport: IO[str]) -> None:
        """Dispatch a client request that arrived DURING an elicitation wait (D3).

        Elicitation is suppressed for the nested handle so a re-entrant tool call
        that would elicit takes the degrade path instead of trying to open a
        second server-originated request under the (capped) pending slot.
        """
        prev = self._elicitation_suppressed
        self._elicitation_suppressed = True
        try:
            response = self.handle(item)
        finally:
            self._elicitation_suppressed = prev
        if response is not None:
            self._write(transport, response)

    @staticmethod
    def _write(stdout: IO[str], message: dict[str, Any]) -> None:
        stdout.write(json.dumps(message) + "\n")
        stdout.flush()


def build_server(
    *, allow_mutations: bool = False, catalog: str = "full", runner: CliRunner | None = None
) -> McpServer:
    """Construct an :class:`McpServer` over the live primitive registry.

    Calls :func:`register_primitives` (idempotent) so the registry is populated
    before it is projected.
    """
    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives

    register_primitives()
    return McpServer(
        registry=get_registry(),
        allow_mutations=allow_mutations,
        catalog=catalog,
        runner=runner,
    )


__all__ = [
    "ELICITATION_SERVER_IMPLEMENTED",
    "CliRunner",
    "McpServer",
    "allowed_primitives",
    "build_server",
]
