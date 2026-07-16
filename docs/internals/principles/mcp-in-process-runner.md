---
slug: mcp-in-process-runner
order: 9
title: "The MCP in-process runner: never touch the real transport streams"
scope: "In-process dispatch swaps out all three real stdio streams; session stream reconfig happens once, before the reader thread exists."
---

# The MCP in-process runner: never touch the real transport streams

Over `mcp-serve`, verbs dispatch IN-PROCESS (`_in_process_cli_runner`) inside
the server whose real `sys.stdin`/`sys.stdout` ARE the JSON-RPC transport,
with a dedicated reader thread permanently blocked in `readline()` on stdin.
Any per-dispatch code that touches those real streams corrupts the session:
stdout writes garble the framing (already guarded by the redirect contexts),
and a `sys.stdin.reconfigure()` racing the blocked cross-thread `readline`
returns a FALSE EOF on Windows — the reader dies, `serve()` exits cleanly
after the in-flight call, and the SECOND `tools/call` of every session meets
a dead server with no traceback anywhere (regression 17243a17: the per-verb
UTF-8 mojibake fix fired per-dispatch; the 2026-07-16 `notebook-record-config`
"Connection closed" incident). The rule: in-process dispatch runs with ALL
three real streams swapped out (`redirect_stdout`/`redirect_stderr` +
`_shield_real_stdin`), and any session-level stream reconfiguration happens
exactly once in `cmd_mcp_serve`, before the reader thread exists.

## Enforcement map

| Rule | Enforced by | Fires when |
|---|---|---|
| In-process dispatch over `mcp-serve` never touches the real `sys.stdin` (the JSON-RPC transport): the runner swaps it for an empty buffer for the call's duration (`_shield_real_stdin`, restored on every unwind path), so `cli.dispatch`'s per-dispatch UTF-8 reconfigure — and any verb stdin read — sees the shield, never the transport; the session UTF-8 reconfigure (run-12 finding 13, cp1252 mojibake) runs once, pre-thread, in `cmd_mcp_serve` | `tests/test_mcp_server.py::test_in_process_runner_shields_real_stdin` (fire path: booby-trapped stdin whose `reconfigure`/`readline` raise survives a real dispatch untouched and is restored) + `::test_serve_survives_sequential_tool_calls_over_real_pipes` (@slow, end-to-end: real subprocess, a verb call then a second request still answered, server alive — load-bearing on Windows, where the false EOF exists) | the runner stops swapping the real stdin out for the dispatch's duration, per-dispatch code regains access to a real transport stream, or the serve-start reconfigure moves after thread spawn |
