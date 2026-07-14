"""Tests for the MCP server projection (``hpc_agent._kernel.extension.mcp_server``).

The registry is populated by the session-autouse fixture in ``conftest.py``;
these tests drive :class:`McpServer.handle` directly (no real stdio transport)
and inject a fake CLI runner so no subprocess is spawned.
"""

from __future__ import annotations

import io
import json

import pytest

import hpc_agent
from hpc_agent._kernel.extension import mcp_server as M
from hpc_agent._kernel.registry.primitive import get_registry

# Raw scheduler cancel / submit commands the worker fence denies. None of these
# is an hpc-agent primitive, so the MCP surface can never expose them — this set
# pins that invariant.
_FORBIDDEN_SURFACE = {"scancel", "qdel", "bkill", "qmod", "sbatch", "qsub"}


class FakeRunner:
    """Records argv and returns a canned ``(exit_code, stdout, stderr)``."""

    def __init__(self, *, exit_code: int = 0, stdout: str | None = None, stderr: str = "") -> None:
        self.calls: list[list[str]] = []
        self._exit_code = exit_code
        self._stdout = (
            stdout
            if stdout is not None
            else json.dumps({"ok": True, "idempotent": True, "data": {"hello": "world"}})
        )
        self._stderr = stderr

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        return self._exit_code, self._stdout, self._stderr


def _server(*, allow_mutations: bool = False, catalog: str = "full", runner=None) -> M.McpServer:
    return M.McpServer(
        registry=get_registry(),
        allow_mutations=allow_mutations,
        catalog=catalog,
        runner=runner or FakeRunner(),
    )


def _result(server: M.McpServer, method: str, params: dict | None = None) -> dict:
    resp = server.handle({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
    assert resp is not None
    assert "error" not in resp, resp.get("error")
    return resp["result"]


def _tool_names(server: M.McpServer) -> set[str]:
    return {t["name"] for t in _result(server, "tools/list")["tools"]}


# ─── handshake ──────────────────────────────────────────────────────────────


def test_initialize_reports_version_and_capabilities() -> None:
    server = _server()
    result = _result(server, "initialize", {"protocolVersion": "2025-06-18"})
    assert result["protocolVersion"] == "2025-06-18"  # echoes the client's version
    # serverInfo.version is the FINGERPRINTED version (``<version>[+<sha>]``)
    # because the instructions tell clients to compare it for skew and the
    # bare number cannot express skew between installs of the same release.
    # Backward-parseable: the prefix up to ``+`` is the plain version.
    from hpc_agent._build_info import full_version

    assert result["serverInfo"]["name"] == "hpc-agent"
    assert result["serverInfo"]["version"] == full_version()
    assert result["serverInfo"]["version"].split("+", 1)[0] == hpc_agent.__version__
    assert set(result["capabilities"]) == {"tools", "resources", "prompts"}
    # Version is surfaced in instructions so a client can detect skew.
    assert hpc_agent.__version__ in result["instructions"]


def test_initialize_falls_back_to_default_protocol_version() -> None:
    result = _result(_server(), "initialize", {})
    assert result["protocolVersion"] == M._PROTOCOL_VERSION


def test_notification_returns_no_response() -> None:
    # A request without "id" is a notification — no response is written.
    assert _server().handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method_is_method_not_found() -> None:
    resp = _server().handle({"jsonrpc": "2.0", "id": 7, "method": "does/not/exist"})
    assert resp["error"]["code"] == -32601


# ─── safety: the read/act boundary ───────────────────────────────────────────


def test_default_exposes_only_read_only_verbs() -> None:
    server = _server()
    names = _tool_names(server)
    registry = get_registry()
    # Every exposed tool is a query/validate primitive.
    for name in names:
        assert registry[name].verb in M._READ_ONLY_VERBS
    # A representative read-only primitive is present; a workflow is not.
    assert "summarize-submit-plan" in names
    assert "find" in names and "describe" in names
    assert "submit-flow" not in names


def test_allow_mutations_exposes_mutating_verbs() -> None:
    names = _tool_names(_server(allow_mutations=True))
    assert "submit-flow" in names  # verb="workflow"


def test_no_scheduler_cancel_or_submit_tool_ever() -> None:
    # Neither default nor mutation-enabled servers expose a raw scheduler
    # cancel/submit command — they are not registry primitives at all.
    for server in (_server(), _server(allow_mutations=True)):
        assert _tool_names(server).isdisjoint(_FORBIDDEN_SURFACE)


def test_forbidden_tool_call_is_invalid_params() -> None:
    # submit-s1 exists but is a mutating (workflow) verb gated off by default;
    # calling it without --allow-mutations is a contract error (-32602), not a
    # silent success. (submit-flow, the former example here, is now refused
    # OUTRIGHT by the blocking-verb fence regardless of the flag — see
    # test_mcp_refuses_blocking_no_detach_workflow — so it no longer exercises
    # the mutation gate.)
    resp = _server().handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "submit-s1"}}
    )
    assert resp["error"]["code"] == -32602
    assert "allow-mutations" in resp["error"]["message"]


# ─── tools/call: the failure contract is preserved ───────────────────────────


def test_call_tool_success_passes_spec_and_unwraps_data() -> None:
    runner = FakeRunner()
    server = _server(runner=runner)
    result = _result(
        server,
        "tools/call",
        {"name": "summarize-submit-plan", "arguments": {"spec": {"k": "v"}}},
    )
    assert result["isError"] is False
    assert result["structuredContent"]["data"] == {"hello": "world"}
    assert result["structuredContent"]["exit_code"] == 0
    # The spec was written to a temp file and passed via --spec.
    argv = runner.calls[0]
    assert argv[0] == "summarize-submit-plan"
    assert "--spec" in argv


def test_call_tool_error_preserves_error_code_category_and_exit_code() -> None:
    failed = json.dumps(
        {
            "ok": False,
            "error_code": "ssh_unreachable",
            "category": "network",
            "retry_safe": True,
            "message": "boom",
        }
    )
    server = _server(runner=FakeRunner(exit_code=2, stdout=failed))
    result = _result(
        server, "tools/call", {"name": "summarize-submit-plan", "arguments": {"spec": {}}}
    )
    assert result["isError"] is True
    sc = result["structuredContent"]
    assert sc["error_code"] == "ssh_unreachable"
    assert sc["category"] == "network"
    assert sc["retry_safe"] is True
    assert sc["exit_code"] == 2


def test_call_tool_non_json_stdout_is_error() -> None:
    server = _server(runner=FakeRunner(exit_code=3, stdout="traceback: boom"))
    result = _result(server, "tools/call", {"name": "find", "arguments": {"query": "x"}})
    assert result["isError"] is True
    assert result["structuredContent"]["exit_code"] == 3


# ─── tiered catalog (context-bloat mitigation) ───────────────────────────────


def test_tiered_catalog_exposes_only_explorers_and_runner() -> None:
    server = _server(catalog="tiered")
    assert _tool_names(server) == {"find", "describe", M._RUN_PRIMITIVE_TOOL}


def test_run_primitive_routes_to_underlying_primitive() -> None:
    runner = FakeRunner()
    server = _server(catalog="tiered", runner=runner)
    result = _result(
        server,
        "tools/call",
        {
            "name": M._RUN_PRIMITIVE_TOOL,
            "arguments": {"name": "find", "arguments": {"query": "submit"}},
        },
    )
    assert result["isError"] is False
    assert runner.calls[0] == ["find", "submit"]


def test_run_primitive_respects_the_safety_gate() -> None:
    # Even via the generic runner, a gated verb is refused.
    resp = _server(catalog="tiered").handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": M._RUN_PRIMITIVE_TOOL, "arguments": {"name": "submit-flow"}},
        }
    )
    assert resp["error"]["code"] == -32602


# ─── argv construction ───────────────────────────────────────────────────────


def test_build_invocation_positional_and_flag() -> None:
    meta = get_registry()["find"]
    argv = M._build_invocation("find", meta.cli, {"query": "x", "limit": 5}, None)
    assert argv == ["find", "x", "--limit", "5"]


def test_build_invocation_prepends_group() -> None:
    meta = get_registry()["clusters-list"]
    argv = M._build_invocation("clusters-list", meta.cli, {}, None)
    assert argv[:2] == ["clusters", "list"]


def test_tool_schema_embeds_spec_as_required() -> None:
    meta = get_registry()["summarize-submit-plan"]
    schema = M._tool_input_schema("summarize-submit-plan", meta.cli)
    assert "spec" in schema["properties"]
    assert "spec" in schema["required"]


def test_trace_is_exposed_read_only_with_flag_schema() -> None:
    # The `trace` query verb is auto-exposed in the default (read-only)
    # catalog, and its flags project into the tool inputSchema — including the
    # `--format` enum — so an MCP client can pull the execution DAG.
    server = _server()
    assert "trace" in _tool_names(server)
    meta = get_registry()["trace"]
    assert meta.verb in M._READ_ONLY_VERBS
    schema = M._tool_input_schema("trace", meta.cli)
    assert {"campaign_id", "run_id", "trace_format"} <= set(schema["properties"])
    assert schema["properties"]["trace_format"]["enum"] == ["dag", "flat", "dot"]


def test_trace_argv_renders_campaign_and_format() -> None:
    meta = get_registry()["trace"]
    argv = M._build_invocation(
        "trace", meta.cli, {"campaign_id": "camp", "trace_format": "dot"}, None
    )
    assert argv == ["trace", "--campaign-id", "camp", "--format", "dot"]


# ─── resources & prompts ──────────────────────────────────────────────────────


def test_resources_list_and_read() -> None:
    runner = FakeRunner(stdout=json.dumps({"ok": True, "data": {}}))
    server = _server(runner=runner)
    uris = {r["uri"] for r in _result(server, "resources/list")["resources"]}
    assert "hpc-agent://capabilities" in uris
    assert "hpc-agent://clusters" in uris
    read = _result(server, "resources/read", {"uri": "hpc-agent://capabilities"})
    assert read["contents"][0]["mimeType"] == "application/json"
    assert runner.calls[0] == ["capabilities"]


def test_resources_read_unknown_uri_is_invalid() -> None:
    resp = _server().handle(
        {"jsonrpc": "2.0", "id": 3, "method": "resources/read", "params": {"uri": "bogus://x"}}
    )
    assert resp["error"]["code"] == -32602


def test_prompts_list_and_get() -> None:
    server = _server()
    names = {p["name"] for p in _result(server, "prompts/list")["prompts"]}
    assert {"submit-hpc", "monitor-hpc", "aggregate-hpc", "campaign-hpc"} <= names
    got = _result(server, "prompts/get", {"name": "submit-hpc"})
    text = got["messages"][0]["content"]["text"]
    assert isinstance(text, str) and text.strip()


def test_prompts_get_unknown_is_invalid() -> None:
    resp = _server().handle(
        {"jsonrpc": "2.0", "id": 4, "method": "prompts/get", "params": {"name": "nope"}}
    )
    assert resp["error"]["code"] == -32602


# ─── transport smoke test ─────────────────────────────────────────────────────


def test_serve_loop_writes_one_response_per_request() -> None:
    server = _server()
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})  # no response
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})
        + "\n"
    )
    stdout = io.StringIO()
    server.serve(stdin, stdout)
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    # initialize + ping → two responses; the notification produced none.
    assert len(lines) == 2
    assert json.loads(lines[0])["id"] == 1
    assert json.loads(lines[1])["id"] == 2


def test_serve_loop_reports_parse_error() -> None:
    stdout = io.StringIO()
    _server().serve(io.StringIO("not json\n"), stdout)
    assert json.loads(stdout.getvalue())["error"]["code"] == -32700


def test_invalid_catalog_rejected() -> None:
    with pytest.raises(ValueError, match="catalog"):
        M.McpServer(registry=get_registry(), catalog="weird")


# ─── conduct rule 11: blocking invocations are refused at the MCP seam ───────


def _call(server, name: str, arguments: dict) -> dict:
    resp = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 7100,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert resp is not None
    return resp


def test_mcp_refuses_submit_s2_without_detach() -> None:
    """A blocking canary watch over the synchronous server = head-of-line wedge
    (proving-run-3: 26-min and 20-min stalls). Refused with the detached path
    named."""
    server = _server(allow_mutations=True)
    resp = _call(server, "submit-s2", {"spec": {"detach": False}})
    assert "error" in resp
    assert "detach" in resp["error"]["message"]
    assert "wait-detached" in resp["error"]["message"]


def test_mcp_allows_submit_s2_with_detach() -> None:
    runner = FakeRunner()
    server = _server(allow_mutations=True, runner=runner)
    resp = _call(server, "submit-s2", {"spec": {"detach": True}})
    assert "error" not in resp
    assert runner.calls, "detached invocation must reach the runner"


def test_mcp_refuses_submit_s4_without_detach() -> None:
    """The S4 harvest (combine SSH + rsync pull + breaker wait-and-retry) can
    hold the synchronous server for minutes — detach is required, like S2/S3."""
    server = _server(allow_mutations=True)
    resp = _call(server, "submit-s4", {"spec": {"detach": False}})
    assert "error" in resp
    assert "detach" in resp["error"]["message"]
    assert "wait-detached" in resp["error"]["message"]


def test_mcp_allows_submit_s4_with_detach() -> None:
    runner = FakeRunner()
    server = _server(allow_mutations=True, runner=runner)
    resp = _call(server, "submit-s4", {"spec": {"detach": True}})
    assert "error" not in resp
    assert runner.calls, "detached invocation must reach the runner"


def test_mcp_refuses_blocking_status_watch() -> None:
    """status-watch is now detach-by-contract (connection-broker.md 2026-07-07):
    a blocking (detach=false / absent) invocation over the synchronous server is
    refused with the detached-path named — the same rule as submit-s2/s3/s4."""
    server = _server(allow_mutations=True)
    resp = _call(server, "status-watch", {"spec": {"detach": False}})
    assert "error" in resp
    assert "detach" in resp["error"]["message"]
    assert "wait-detached" in resp["error"]["message"]


def test_mcp_allows_status_watch_with_detach() -> None:
    runner = FakeRunner()
    server = _server(allow_mutations=True, runner=runner)
    resp = _call(server, "status-watch", {"spec": {"detach": True, "monitor": {"run_id": "r"}}})
    assert "error" not in resp
    assert runner.calls, "detached invocation must reach the runner"


def test_mcp_refuses_aggregate_run_without_detach() -> None:
    """run-#10 F-K: a synchronous aggregate-run (combine SSH + rsync pull) held the
    server for 20+ minutes with zero observability — detach is required, like S4."""
    server = _server(allow_mutations=True)
    resp = _call(server, "aggregate-run", {"spec": {"detach": False}})
    assert "error" in resp
    assert "detach" in resp["error"]["message"]
    assert "wait-detached" in resp["error"]["message"]


def test_mcp_allows_aggregate_run_with_detach() -> None:
    runner = FakeRunner()
    server = _server(allow_mutations=True, runner=runner)
    resp = _call(server, "aggregate-run", {"spec": {"detach": True}})
    assert "error" not in resp
    assert runner.calls, "detached invocation must reach the runner"


def test_mcp_refuses_aggregate_flow_without_detach() -> None:
    """aggregate-flow's default detach is OFF (composed atom), but a DIRECT blocking
    MCP invocation is still refused — the seam reads the raw spec, not the default."""
    server = _server(allow_mutations=True)
    resp = _call(server, "aggregate-flow", {"spec": {"run_id": "r"}})
    assert "error" in resp
    assert "detach" in resp["error"]["message"]
    assert "wait-detached" in resp["error"]["message"]


def test_mcp_allows_aggregate_flow_with_detach() -> None:
    runner = FakeRunner()
    server = _server(allow_mutations=True, runner=runner)
    resp = _call(server, "aggregate-flow", {"spec": {"detach": True, "run_id": "r"}})
    assert "error" not in resp
    assert runner.calls, "detached invocation must reach the runner"


def test_mcp_refuses_campaign_run_without_detach() -> None:
    """A whole campaign iteration (submit→monitor→aggregate) over the synchronous
    server = a minutes-to-hours head-of-line wedge — detach is required."""
    server = _server(allow_mutations=True)
    resp = _call(server, "campaign-run", {"spec": {"detach": False}})
    assert "error" in resp
    assert "detach" in resp["error"]["message"]
    assert "wait-detached" in resp["error"]["message"]


def test_mcp_allows_campaign_run_with_detach() -> None:
    runner = FakeRunner()
    server = _server(allow_mutations=True, runner=runner)
    resp = _call(server, "campaign-run", {"spec": {"detach": True}})
    assert "error" not in resp
    assert runner.calls, "detached invocation must reach the runner"


def test_mcp_refuses_wait_detached_on_full_catalog() -> None:
    """``wait-detached`` is the blocking wait itself (no detach remedy). It is
    invocable on the DEFAULT ``full`` catalog (agent_facing query), so over the
    synchronous server it would wedge the line — refused outright at the seam,
    naming the MCP-safe reads (``poll-detached`` / backgrounded Bash). It never
    reaches the runner."""
    runner = FakeRunner()
    server = _server(catalog="full", runner=runner)
    resp = _call(server, "wait-detached", {"spec": {"run_id": "r1"}})
    assert "error" in resp
    assert "poll-detached" in resp["error"]["message"]
    assert "wait-detached" in resp["error"]["message"]
    assert not runner.calls, "refused blocking wait must not reach the runner"


def test_mcp_refuses_wait_detached_via_tiered_run_primitive() -> None:
    """The tiered catalog routes calls through ``run-primitive``; the blocking-
    wait refusal fires on the inner name, so ``wait-detached`` is wedge-proof
    there too."""
    runner = FakeRunner()
    server = _server(catalog="tiered", runner=runner)
    resp = _call(
        server,
        "run-primitive",
        {"name": "wait-detached", "arguments": {"spec": {"run_id": "r1"}}},
    )
    assert "error" in resp
    assert "poll-detached" in resp["error"]["message"]
    assert not runner.calls, "refused blocking wait must not reach the runner"


# ─── curated reachability of the MCP-direct read/recovery verbs ──────────────


def _curated_names(**kw) -> set[str]:
    return {t["name"] for t in _server(catalog="curated", **kw).list_tools()}


def test_curated_exposes_the_mcp_direct_read_loop_verbs() -> None:
    """The read-loop QUERY verbs the SKILLs name "DIRECT through MCP", plus the
    (MCP-direct)-tagged ``revise-resolved`` mutate, are curated-reachable — the
    coupling ``scripts/lint_skill_mcp_reachability.py`` enforces. None declares a
    ``next_block`` (a read/spec-delta is not a block), so this is the explicit
    union added to ``_CURATED_EXTRA_VERBS``, not derivation."""
    names = _curated_names()
    assert {"read-decisions", "verify-relay", "attention-queue", "revise-resolved"} <= names
    # All four are also members of the module constant (the listing derives from it).
    assert {"read-decisions", "verify-relay", "attention-queue", "revise-resolved"} <= (
        M._CURATED_EXTRA_VERBS
    )


def test_revise_resolved_is_an_extra_not_a_derived_block() -> None:
    """revise-resolved is curated ONLY via the explicit extra, never via
    ``_declares_next_block`` — the memo's verified verdict: ``ReviseResolvedResult``
    declares no ``next_block``. (retarget-run, the sibling recovery arm, DOES
    declare one and derives in — so it needs no extra entry.)"""
    reg = get_registry()
    assert "revise-resolved" in reg
    assert M._declares_next_block(reg["revise-resolved"]) is False
    assert "revise-resolved" in M._CURATED_EXTRA_VERBS
    # retarget-run derives (declares next_block) and is NOT hand-listed.
    assert M._declares_next_block(reg["retarget-run"]) is True
    assert "retarget-run" not in M._CURATED_EXTRA_VERBS


def test_poll_detached_extra_tolerates_sibling_absence() -> None:
    """poll-detached is a curated extra a SIBLING unit (m-poll) builds. It is a
    listed extra, but the curated LISTING guards on registry presence — an
    unbuilt extra is filtered out (``_curated_metas`` unions ``v in base`` only),
    so its absence never breaks the catalog, and it appears the moment it lands."""
    assert "poll-detached" in M._CURATED_EXTRA_VERBS
    reg = get_registry()
    names = _curated_names()
    if "poll-detached" in reg:
        assert "poll-detached" in names
    else:
        assert "poll-detached" not in names


# ─── isolated runner deadline (src subprocess-timeout discipline) ────────────


def test_subprocess_cli_runner_deadline_fires(monkeypatch) -> None:
    """The isolated runner's server-level cap kills a hanging child (exit 124).

    Fire path for the bound that closed the last ``_GRANDFATHERED`` entry in
    ``tests/contracts/test_src_subprocess_timeout_discipline.py``: a synthetic
    hanging child under an injected sub-second cap is killed (via the
    ``infra.remote.capture_via_select`` wedge-safe seam) rather than awaited,
    and the call maps to exit 124 with the deadline named on stderr.
    """
    import sys
    import time

    monkeypatch.setattr(M, "_SUBPROCESS_RUNNER_TIMEOUT_SEC", 0.5)
    monkeypatch.setattr(
        M,
        "_isolated_runner_argv",
        lambda argv: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    start = time.monotonic()
    code, out, err = M._subprocess_cli_runner(["find"])
    elapsed = time.monotonic() - start
    assert code == 124
    assert out == ""
    assert "deadline" in err
    assert "find" in err
    assert elapsed < 30, "child was awaited, not killed"


# ─── F01: blocking WORKFLOW verbs with no detach field refused outright ───────


@pytest.mark.parametrize(
    ("verb", "alt_hint"),
    [
        ("monitor-flow", "status-snapshot"),
        ("verify-canary", "submit-s2"),
        ("submit-flow", "submit-s1"),
    ],
)
def test_mcp_refuses_blocking_no_detach_workflow(verb: str, alt_hint: str) -> None:
    """``monitor-flow`` / ``verify-canary`` / ``submit-flow`` are poll-to-terminal
    workflows with NO ``detach`` field in their ``extra='forbid'`` specs, so —
    unlike ``submit-s2`` / ``status-watch`` — no ``{"detach": true}`` remedy
    exists. Over the synchronous server they wedge the line for up to 24h
    (proving-run-3 head-of-line class). Refused OUTRIGHT at the seam, each naming
    its MCP-safe alternative, and NEVER reaching the runner — the fire path for
    the extended fence."""
    runner = FakeRunner()
    server = _server(allow_mutations=True, catalog="full", runner=runner)
    resp = _call(server, verb, {"spec": {"run_id": "r1"}})
    assert "error" in resp
    msg = resp["error"]["message"]
    assert "detach=true" in msg and "wedges" in msg
    assert alt_hint in msg
    assert not runner.calls, "refused blocking workflow must not reach the runner"


def test_mcp_refuses_monitor_flow_via_tiered_run_primitive() -> None:
    """The tiered catalog routes through ``run-primitive``; the outright refusal
    fires on the INNER name, so the blocking workflow is wedge-proof there too."""
    runner = FakeRunner()
    server = _server(allow_mutations=True, catalog="tiered", runner=runner)
    resp = _call(
        server,
        "run-primitive",
        {"name": "monitor-flow", "arguments": {"spec": {}}},
    )
    assert "error" in resp
    assert "status-snapshot" in resp["error"]["message"]
    assert not runner.calls, "refused blocking workflow must not reach the runner"


# ─── F01: the DEFAULT in-process runner is now deadline-bounded too ───────────


def test_in_process_runner_deadline_interrupts_wedged_call(monkeypatch) -> None:
    """The default ``_in_process_cli_runner`` now enforces the same server-level
    ceiling the subprocess runner does — via a SIGALRM backstop that raises IN
    the wedged call (no leaked thread, no corrupted JSON-RPC stdout, no per-call
    subprocess latency). Fire path: a synthetic dispatch that sleeps past an
    injected sub-second deadline is INTERRUPTED and mapped to exit 124, not
    awaited."""
    import time

    import hpc_agent.cli.dispatch as _dispatch

    if not hasattr(M.signal, "setitimer"):  # pragma: no cover - POSIX-only backstop
        pytest.skip("setitimer/SIGALRM unavailable on this platform")

    monkeypatch.setattr(M, "_SUBPROCESS_RUNNER_TIMEOUT_SEC", 0.1)

    def _slow_main(argv: list[str]) -> int:
        time.sleep(30)
        return 0

    monkeypatch.setattr(_dispatch, "main", _slow_main)
    start = time.monotonic()
    code, out, err = M._in_process_cli_runner(["find"])
    elapsed = time.monotonic() - start
    assert code == 124
    assert out == ""
    assert "deadline" in err and "find" in err
    assert elapsed < 10, "the wedged call was interrupted, not awaited"


# ─── F02: the mid-call heartbeat survives the in-process runner's redirect ────


def test_heartbeat_reaches_pre_redirect_stderr(monkeypatch) -> None:
    """The default in-process runner wraps dispatch in
    ``contextlib.redirect_stderr``, which rebinds ``sys.stderr`` PROCESS-WIDE
    across every thread for the call. A heartbeat that resolved ``sys.stderr`` at
    WRITE time would land in that captured StringIO (then be discarded) —
    resurrecting the 'is it hung?' blindness it exists to fix. It must bind the
    REAL (pre-redirect) handle at thread start. Fire path: a runner that mimics
    the redirect while several heartbeat intervals elapse must still deliver the
    line to the pre-redirect stderr, and NOT into the redirect capture."""
    import contextlib as _ctx
    import io as _io
    import time

    monkeypatch.setattr(M, "_HEARTBEAT_INTERVAL_SEC", 0.02)

    swallowed: dict[str, str] = {}

    def _redirecting_runner(argv: list[str]) -> tuple[int, str, str]:
        captured = _io.StringIO()
        with _ctx.redirect_stderr(captured):
            time.sleep(0.12)  # several heartbeat intervals under the redirect
        swallowed["text"] = captured.getvalue()
        return 0, json.dumps({"ok": True, "data": {}}), ""

    real_err = _io.StringIO()
    monkeypatch.setattr(M.sys, "stderr", real_err)
    server = _server(runner=_redirecting_runner)
    shape = get_registry()["find"].cli
    server._invoke_cli("find", shape, {})

    assert "[mcp] find still running" in real_err.getvalue(), (
        "heartbeat must reach the pre-redirect stderr"
    )
    assert "still running" not in swallowed.get("text", ""), (
        "heartbeat must NOT be swallowed by the runner's redirect_stderr"
    )


# ─── F04: prompts/get serves the EXECUTABLE start_instruction, not slash body ─


def test_get_prompt_serves_executable_start_instruction_not_slash_body() -> None:
    """An MCP-only client has no Skill tool / Bash / CronCreate, so the packaged
    slash ``.md`` body (which instructs exactly those) is unusable. Every
    ``prompts/get`` must return the entry's executable ``start_instruction``
    (``block-drive`` + ``append-decision`` — curated verbs the client HAS) as the
    message body, and use the ``.md`` only for the human ``description``."""
    from hpc_agent._kernel.extension.workflow_entries import WORKFLOW_ENTRIES_BY_PROMPT

    server = _server()
    for name, entry in WORKFLOW_ENTRIES_BY_PROMPT.items():
        got = _result(server, "prompts/get", {"name": name})
        text = got["messages"][0]["content"]["text"]
        assert text == entry.start_instruction
        assert "block-drive" in text
        assert "Skill tool" not in text  # the slash-only affordance must not leak

    # When the slash .md IS installed (package data — the normal case), prove we
    # deliberately did NOT serve it as the body.
    body = M._read_command_md("submit-hpc")
    if body is not None:
        assert body.strip() != WORKFLOW_ENTRIES_BY_PROMPT["submit-hpc"].start_instruction
