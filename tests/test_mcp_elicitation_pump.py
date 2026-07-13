"""E1 — the bidirectional pump (``docs/design/mcp-elicitation.md`` D1/D2/D3).

Drives the server-originated request path through the fake-client DUPLEX harness
(:mod:`tests._mcp_harness`): outbound/inbound id correlation, interleaved
client-request servicing during a wait, timeout → decline-equivalent, late
response dropped silently, id-namespace non-collision, the depth cap, EOF during
a wait, and absent-transport structural unavailability. No real stdio, no
subprocess, no network.
"""

from __future__ import annotations

import queue
from typing import Any, cast

import pytest

from hpc_agent._kernel.extension import mcp_server as M
from hpc_agent._kernel.registry.primitive import get_registry
from tests._mcp_harness import FakeMcpClient, _LineSink, make_eliciting_server


def _plain_server() -> M.McpServer:
    return M.McpServer(
        registry=get_registry(),
        allow_mutations=True,
        catalog="curated",
        runner=lambda _argv: (0, "{}", ""),
    )


# ─── outbound id namespace (D1 item 1) ───────────────────────────────────────


def test_outbound_id_is_monotonic_hpc_srv_namespace() -> None:
    server = _plain_server()
    ids = [server._next_outbound_id() for _ in range(3)]
    assert ids == ["hpc-srv-1", "hpc-srv-2", "hpc-srv-3"]
    # A distinct string space: none of these can equal a typical integer
    # client-chosen id, and the prefix is reserved to the server.
    assert all(i.startswith("hpc-srv-") for i in ids)


# ─── D1 item 5 — absent transport ⇒ elicitation structurally unavailable ─────


def test_absent_transport_returns_decline_equivalent() -> None:
    # A server that never ran ``serve`` has no transport/queue threaded on, so
    # the wait primitive declines immediately — every direct-``handle`` test
    # stays valid unchanged.
    server = _plain_server()
    assert server._transport is None and server._msg_queue is None
    assert server._request_from_client("elicitation/create", {"message": "x"}, 0.5) is None


# ─── D2 — capability stored at initialize ────────────────────────────────────


def test_initialize_stores_client_elicitation_true() -> None:
    server = _plain_server()
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"capabilities": {"elicitation": {}}},
        }
    )
    assert server._client_elicitation is True


def test_initialize_absent_capability_is_false() -> None:
    server = _plain_server()
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert server._client_elicitation is False
    # A capabilities object WITHOUT elicitation is also false.
    server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {"capabilities": {"roots": {}}},
        }
    )
    assert server._client_elicitation is False


# ─── correlation: outbound request ↔ inbound response (D1 items 1-4) ─────────


def test_outbound_inbound_correlation_accept() -> None:
    server = make_eliciting_server()
    with FakeMcpClient(server) as client:
        client.initialize()
        client.send(
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {"name": "elicit-test", "arguments": {}},
            }
        )
        # The server-originated request: correct shape + id namespace.
        req = client.recv()
        assert req["method"] == "elicitation/create"
        assert req["id"] == "hpc-srv-1"
        assert req["params"] == {"message": "type your sign-off"}
        # Answer it; the pump routes the response to the waiter.
        client.send(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"action": "accept", "content": {"utterance": "I approve run x"}},
            }
        )
        resp = client.recv()
        assert resp["id"] == 10
        assert resp["result"]["structuredContent"]["elicited"] is True
    assert server.last_response["result"]["action"] == "accept"


# ─── interleaved client requests are serviced DURING a wait (D3) ─────────────


def test_interleaved_request_serviced_during_wait() -> None:
    server = make_eliciting_server()
    with FakeMcpClient(server) as client:
        client.initialize()
        client.send(
            {
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {"name": "elicit-test", "arguments": {}},
            }
        )
        req = client.recv()  # the elicitation/create — server now blocked in the wait
        assert req["id"] == "hpc-srv-1"
        # A ping arrives while the elicitation is in flight → serviced inline.
        client.send({"jsonrpc": "2.0", "id": 21, "method": "ping"})
        pong = client.recv()
        assert pong["id"] == 21 and pong["result"] == {}
        # Now finish the elicitation.
        client.send(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"action": "accept", "content": {"utterance": "ok"}},
            }
        )
        resp = client.recv()
        assert resp["id"] == 20


def test_nested_elicitation_is_suppressed_during_wait() -> None:
    # A re-entrant tool call that would itself elicit takes the degrade path
    # (D3) — the depth cap is never even reached.
    server = make_eliciting_server()
    with FakeMcpClient(server) as client:
        client.initialize()
        client.send(
            {
                "jsonrpc": "2.0",
                "id": 30,
                "method": "tools/call",
                "params": {"name": "elicit-test", "arguments": {}},
            }
        )
        req = client.recv()
        # Second elicit-test WHILE the first is pending.
        client.send(
            {
                "jsonrpc": "2.0",
                "id": 31,
                "method": "tools/call",
                "params": {"name": "elicit-test", "arguments": {}},
            }
        )
        nested = client.recv()
        assert nested["id"] == 31
        assert nested["result"]["structuredContent"]["degraded"] == "suppressed"
        # The outer elicitation still completes normally.
        client.send(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"action": "accept", "content": {"utterance": "ok"}},
            }
        )
        assert client.recv()["id"] == 30
    assert server.suppressed_calls == 1


# ─── timeout → decline-equivalent (D3) ───────────────────────────────────────


def test_timeout_declines() -> None:
    server = make_eliciting_server(elicit_timeout=0.3)
    with FakeMcpClient(server) as client:
        client.initialize()
        client.send(
            {
                "jsonrpc": "2.0",
                "id": 40,
                "method": "tools/call",
                "params": {"name": "elicit-test", "arguments": {}},
            }
        )
        req = client.recv()
        assert req["method"] == "elicitation/create"
        # Never answer — the deadline fires and the call returns as decline.
        resp = client.recv(timeout=5.0)
        assert resp["id"] == 40
        assert resp["result"]["structuredContent"]["elicited"] is False
    assert server.last_response is None
    # The pending slot was cleared on timeout.
    assert server._pending_id is None


# ─── a late response for a timed-out id is dropped silently ──────────────────


def test_late_response_dropped_silently() -> None:
    server = make_eliciting_server(elicit_timeout=0.3)
    with FakeMcpClient(server) as client:
        client.initialize()
        client.send(
            {
                "jsonrpc": "2.0",
                "id": 50,
                "method": "tools/call",
                "params": {"name": "elicit-test", "arguments": {}},
            }
        )
        req = client.recv()
        resp = client.recv(timeout=5.0)  # the decline (timeout)
        assert resp["id"] == 50
        # The human answers LATE, after the slot was cleared: dropped, no crash.
        client.send(
            {
                "jsonrpc": "2.0",
                "id": req["id"],
                "result": {"action": "accept", "content": {"utterance": "too late"}},
            }
        )
        # The server is still alive and serving — a ping proves it.
        client.send({"jsonrpc": "2.0", "id": 51, "method": "ping"})
        assert client.recv(timeout=5.0)["id"] == 51


# ─── id-namespace non-collision with client-chosen ids ───────────────────────


def test_id_namespace_non_collision_with_client_ids() -> None:
    # A client REQUEST whose id string collides with the server's outbound space
    # is still classified as a request (it has ``method``), not mistaken for the
    # pending elicitation response — the classification is method-keyed and the
    # response match is exact-id on top of the reserved string space.
    server = make_eliciting_server()
    with FakeMcpClient(server) as client:
        client.initialize()
        client.send(
            {
                "jsonrpc": "2.0",
                "id": 60,
                "method": "tools/call",
                "params": {"name": "elicit-test", "arguments": {}},
            }
        )
        req = client.recv()
        assert req["id"] == "hpc-srv-1"
        # A client request that (mis)uses the reserved id string: serviced as a
        # request, does NOT satisfy the wait.
        client.send({"jsonrpc": "2.0", "id": "hpc-srv-1", "method": "ping"})
        pong = client.recv()
        assert pong["id"] == "hpc-srv-1" and pong["result"] == {}
        # The elicitation is still pending; the REAL response completes it.
        client.send(
            {
                "jsonrpc": "2.0",
                "id": "hpc-srv-1",
                "result": {"action": "accept", "content": {"utterance": "ok"}},
            }
        )
        assert client.recv()["id"] == 60
    assert server.last_response["result"]["action"] == "accept"


# ─── depth cap: one elicitation in flight (D3, invariant not queue) ──────────


def test_depth_cap_is_asserted() -> None:
    server = _plain_server()
    # Simulate a transport being present with a slot already occupied.
    server._transport = cast("Any", _LineSink())
    server._msg_queue = queue.Queue()
    server._pending_id = "hpc-srv-1"
    with pytest.raises(AssertionError, match="depth cap"):
        server._request_from_client("elicitation/create", {"message": "x"}, 0.5)


# ─── EOF during a wait → decline + normal shutdown ───────────────────────────


def test_eof_during_wait_declines_and_shuts_down() -> None:
    server = make_eliciting_server()
    client = FakeMcpClient(server)
    client.__enter__()
    try:
        client.initialize()
        client.send(
            {
                "jsonrpc": "2.0",
                "id": 70,
                "method": "tools/call",
                "params": {"name": "elicit-test", "arguments": {}},
            }
        )
        req = client.recv()
        assert req["method"] == "elicitation/create"
        # stdin closes mid-wait: the pump declines, then the loop shuts down.
        client.stdin.close()
        resp = client.recv(timeout=5.0)
        assert resp["id"] == 70
        assert resp["result"]["structuredContent"]["elicited"] is False
    finally:
        client._thread.join(timeout=5.0)
    assert not client._thread.is_alive()
    assert server.last_response is None


# ─── the serve loop drops an UNEXPECTED top-level response ───────────────────


def test_top_level_unexpected_response_dropped() -> None:
    server = _plain_server()
    with FakeMcpClient(server) as client:
        client.initialize()
        # A response with no matching in-flight request (none is) → dropped.
        client.send({"jsonrpc": "2.0", "id": "hpc-srv-99", "result": {"action": "accept"}})
        # Server keeps serving.
        client.send({"jsonrpc": "2.0", "id": 80, "method": "ping"})
        assert client.recv(timeout=5.0)["id"] == 80


def test_serve_still_reports_parse_error() -> None:
    server = _plain_server()
    with FakeMcpClient(server) as client:
        client.send_raw("not json at all")
        err = client.recv(timeout=5.0)
        assert err["error"]["code"] == -32700
