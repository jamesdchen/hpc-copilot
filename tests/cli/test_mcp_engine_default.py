"""``mcp-serve`` defaults the persistent SSH engine ON — and only there (memo §3).

``mcp-serve`` is the sole long-lived hpc-agent process, so it is the one place a
held asyncssh connection amortises (the engine's idle sweeper + slot-held-while-
open invariant were hardened *from* mcp-serve incidents —
``hpc_agent.infra.ssh_engine`` header). These pin the four behaviours the memo
names:

* default-when-unset → the engine env is set to ``asyncssh`` and reported ``on``;
* user-preset env wins (``setdefault`` semantics) → reported ``user-set``;
* opt-out wins (``HPC_MCP_NO_SSH_ENGINE=1``) → env untouched, reported ``off``;
* no effect outside mcp-serve → importing/registering never touches the env.

Plus the honest-degradation cite: an *unimportable* asyncssh raises
:class:`EngineUnavailable`, which :func:`hpc_agent.infra.remote` catches and
routes to the one-shot path — so defaulting the engine on here can never be
worse than leaving it off.
"""

from __future__ import annotations

import argparse
import io

import pytest

from hpc_agent.cli import mcp as mcp_cli
from hpc_agent.infra import ssh_engine


class _FakeServer:
    """Stand-in for the MCP server: records that serve() ran; no I/O."""

    _allow_mutations = False

    def serve(self, stdin: object, stdout: object) -> None:  # noqa: D401
        return None


@pytest.fixture()
def _clean_engine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each case from a known-empty engine + opt-out env."""
    monkeypatch.delenv(ssh_engine.ENGINE_ENV, raising=False)
    monkeypatch.delenv(mcp_cli.NO_SSH_ENGINE_ENV, raising=False)


def _run_serve(monkeypatch: pytest.MonkeyPatch) -> str:
    """Drive cmd_mcp_serve with build_server stubbed; return the stderr ready line."""
    monkeypatch.setattr(
        "hpc_agent._kernel.extension.mcp_server.build_server",
        lambda **kw: _FakeServer(),
    )
    buf = io.StringIO()
    monkeypatch.setattr("sys.stderr", buf)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("sys.stdout", io.StringIO())
    rc = mcp_cli.cmd_mcp_serve(argparse.Namespace(catalog="full", allow_mutations=False))
    assert rc == 0
    return buf.getvalue()


def test_default_when_unset_turns_engine_on(
    _clean_engine_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env unset → we default HPC_SSH_ENGINE=asyncssh and report engine=on."""
    ready = _run_serve(monkeypatch)
    assert ssh_engine.engine_enabled() is True
    import os

    assert os.environ[ssh_engine.ENGINE_ENV] == "asyncssh"
    assert "engine=on" in ready


def test_user_preset_env_wins(_clean_engine_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A user-set value survives (setdefault is a no-op) and reports user-set.

    Uses ``native`` to prove we do not clobber a deliberate opt-*out* via the
    engine's own knob — the value is unchanged, not forced to asyncssh.
    """
    monkeypatch.setenv(ssh_engine.ENGINE_ENV, "native")
    ready = _run_serve(monkeypatch)
    import os

    assert os.environ[ssh_engine.ENGINE_ENV] == "native"
    assert ssh_engine.engine_enabled() is False
    assert "engine=user-set" in ready


def test_user_preset_asyncssh_also_reports_user_set(
    _clean_engine_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even when the preset equals our default, presence → user-set, not on."""
    monkeypatch.setenv(ssh_engine.ENGINE_ENV, "asyncssh")
    ready = _run_serve(monkeypatch)
    assert "engine=user-set" in ready


def test_opt_out_wins(_clean_engine_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """HPC_MCP_NO_SSH_ENGINE=1 → env untouched, engine stays off, reports off."""
    monkeypatch.setenv(mcp_cli.NO_SSH_ENGINE_ENV, "1")
    ready = _run_serve(monkeypatch)
    assert ssh_engine.ENGINE_ENV not in __import__("os").environ
    assert "engine=off" in ready


def test_no_effect_outside_mcp_serve(
    _clean_engine_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Importing the module and running register() must not touch the engine env.

    The default lives entirely inside cmd_mcp_serve — no import-time side
    effect, no leak into any other verb's dispatch.
    """
    import os

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    mcp_cli.register(sub)
    assert ssh_engine.ENGINE_ENV not in os.environ


def test_helper_disposition_matrix(
    _clean_engine_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_enable_ssh_engine_default returns the three dispositions directly."""
    assert mcp_cli._enable_ssh_engine_default() == "on"

    monkeypatch.setenv(ssh_engine.ENGINE_ENV, "asyncssh")
    assert mcp_cli._enable_ssh_engine_default() == "user-set"

    monkeypatch.delenv(ssh_engine.ENGINE_ENV, raising=False)
    monkeypatch.setenv(mcp_cli.NO_SSH_ENGINE_ENV, "1")
    assert mcp_cli._enable_ssh_engine_default() == "off"


def test_unimportable_asyncssh_falls_back_to_one_shot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Honest-degradation cite: engine ON + asyncssh unimportable → one-shot.

    ``engine_ssh_run`` raises :class:`EngineUnavailable` when asyncssh cannot be
    imported; :func:`hpc_agent.infra.remote` catches exactly that and routes to
    the one-shot path. This proves defaulting the engine on in mcp-serve is
    never worse than leaving it off, even on a box without the ssh extra.
    """
    import builtins

    monkeypatch.setenv(ssh_engine.ENGINE_ENV, "asyncssh")
    assert ssh_engine.engine_enabled() is True

    real_import = builtins.__import__

    def _no_asyncssh(name: str, *a: object, **k: object) -> object:
        if name == "asyncssh" or name.startswith("asyncssh."):
            raise ImportError("no asyncssh in this test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_asyncssh)

    with pytest.raises(ssh_engine.EngineUnavailable):
        ssh_engine.engine_ssh_run("echo hi", ssh_target="user@host", timeout=5)
