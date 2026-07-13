"""A fake-client DUPLEX harness for the MCP bidirectional pump.

Paired in-memory streams — no real stdio, no subprocess, no network (the
plugins-CI offline posture) — that drive :meth:`McpServer.serve` on a background
thread while the test acts as the MCP CLIENT: it sends requests, reads responses,
and (the whole point of the bidirectional upgrade) answers server-ORIGINATED
``elicitation/create`` requests.

Reusable on purpose: the conformance kit (``docs/design/conformance-kit.md``,
the cross-plan reuse ledger) consumes this exact rig for its
elicitation-channel assertions, so it lives here as an importable module
(``from tests._mcp_harness import FakeMcpClient, RecordingElicitServer``) beside
the other root-level shared helpers (``tests/_subprocess.py`` etc.), not inside a
single test file.

Why real blocking streams and not ``io.StringIO``: the pump's deadline is only
real because the sole stdin reader is a daemon thread feeding a ``queue.Queue``
that ``serve`` / ``_request_from_client`` consume with ``get(timeout=…)``. A
``StringIO`` hits EOF the instant it is exhausted, so it cannot model a client
that has sent nothing yet while a human deliberates. :class:`_BlockingLinePipe`
blocks its line iterator until the client writes or closes — the property the
timeout path is tested against.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import TYPE_CHECKING, Any

from hpc_agent._kernel.extension import mcp_server as M

if TYPE_CHECKING:
    from collections.abc import Mapping


class _BlockingLinePipe:
    """Server stdin: the client writes whole lines; the reader thread blocks
    iterating them until a line is available or the pipe is closed (EOF)."""

    def __init__(self) -> None:
        self._q: queue.Queue[str | None] = queue.Queue()

    def write(self, s: str) -> int:  # the client's send side
        self._q.put(s)
        return len(s)

    def flush(self) -> None:  # pragma: no cover - stream protocol
        pass

    def close(self) -> None:
        self._q.put(None)  # EOF sentinel for the iterator

    def __iter__(self) -> _BlockingLinePipe:
        return self

    def __next__(self) -> str:
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item


class _LineSink:
    """Server stdout: the server writes framed JSON; the client reads one line
    (one JSON-RPC message) at a time, blocking with a timeout."""

    def __init__(self) -> None:
        self._q: queue.Queue[str] = queue.Queue()
        self._buf = ""

    def write(self, s: str) -> int:  # the server's write side
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._q.put(line)
        return len(s)

    def flush(self) -> None:  # pragma: no cover - stream protocol
        pass

    def get_line(self, timeout: float = 5.0) -> str:
        return self._q.get(timeout=timeout)

    def try_get_line(self, timeout: float = 0.25) -> str | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None


class FakeMcpClient:
    """Drives a real :class:`McpServer` over paired in-memory streams.

    Usage as a context manager starts the ``serve`` thread and tears it down
    (closing stdin → EOF → the loop exits) on exit::

        with FakeMcpClient(server) as client:
            client.initialize()
            client.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", ...})
            req = client.recv()  # the server-originated elicitation/create
            client.send({"jsonrpc": "2.0", "id": req["id"], "result": {...}})
            resp = client.recv()  # the tools/call response
    """

    def __init__(self, server: M.McpServer) -> None:
        self._server = server
        self.stdin = _BlockingLinePipe()  # server reads this
        self.stdout = _LineSink()  # server writes this
        self._thread = threading.Thread(
            target=server.serve, args=(self.stdin, self.stdout), name="fake-mcp-serve", daemon=True
        )

    def __enter__(self) -> FakeMcpClient:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- client → server --------------------------------------------------
    def send(self, message: Mapping[str, Any]) -> None:
        self.stdin.write(json.dumps(dict(message)) + "\n")

    def send_raw(self, line: str) -> None:
        """Send a raw (possibly non-JSON) line — for the parse-error path."""
        self.stdin.write(line + "\n")

    # -- server → client --------------------------------------------------
    def recv(self, timeout: float = 5.0) -> dict[str, Any]:
        parsed = json.loads(self.stdout.get_line(timeout=timeout))
        assert isinstance(parsed, dict)
        return parsed

    def recv_maybe(self, timeout: float = 0.25) -> dict[str, Any] | None:
        line = self.stdout.try_get_line(timeout=timeout)
        return None if line is None else json.loads(line)

    # -- convenience ------------------------------------------------------
    def initialize(self, *, elicitation: bool = True) -> dict[str, Any]:
        caps: dict[str, Any] = {"elicitation": {}} if elicitation else {}
        self.send(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": caps},
            }
        )
        return self.recv()

    def close(self) -> None:
        self.stdin.close()
        self._thread.join(timeout=5.0)


class RecordingElicitServer(M.McpServer):
    """An :class:`McpServer` whose ``elicit-test`` tool fires ONE server-originated
    elicitation — the seam E4's real ``append-decision`` retry-once wrap will
    occupy. It records the raw response the pump returned so a test can assert
    correlation / decline / drop outcomes without E4's handler existing yet.

    It mirrors E4's re-entrancy rule: when a nested ``elicit-test`` arrives while
    an elicitation is already in flight (``self._elicitation_suppressed``), it
    takes the DEGRADE path instead of opening a second (cap-violating) request.
    """

    def __init__(
        self, *args: Any, elicit_timeout: float = M._ELICITATION_TIMEOUT_SEC, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.elicit_timeout = elicit_timeout
        self.elicit_params: dict[str, Any] = {"message": "type your sign-off"}
        self.last_response: Any = "__unset__"
        self.suppressed_calls = 0

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name == "elicit-test":
            if self._elicitation_suppressed:
                # Nested elicitation while one is in flight → degrade (D3).
                self.suppressed_calls += 1
                return _ok_result({"elicited": False, "degraded": "suppressed"})
            resp = self._request_from_client(
                "elicitation/create", self.elicit_params, self.elicit_timeout
            )
            self.last_response = resp
            return _ok_result({"elicited": resp is not None})
        return super().call_tool(name, arguments)


def _ok_result(payload: dict[str, Any]) -> dict[str, Any]:
    structured = {"ok": True, **payload}
    return {
        "content": [{"type": "text", "text": json.dumps(structured, sort_keys=True)}],
        "structuredContent": structured,
        "isError": False,
    }


def make_eliciting_server(
    *, elicit_timeout: float = M._ELICITATION_TIMEOUT_SEC
) -> RecordingElicitServer:
    """A curated-catalog :class:`RecordingElicitServer` over the live registry."""
    from hpc_agent._kernel.registry.primitive import get_registry

    return RecordingElicitServer(
        registry=get_registry(),
        allow_mutations=True,
        catalog="curated",
        runner=lambda _argv: (0, "{}", ""),
        elicit_timeout=elicit_timeout,
    )
