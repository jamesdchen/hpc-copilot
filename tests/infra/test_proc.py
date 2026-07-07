"""One-definition property for the PID-liveness probe (audit 2026-07-07 #1).

Two byte-divergent hand-rolled ``_pid_alive`` copies (``detached.py`` and
``ssh_slots.py``) were collapsed onto a single ``infra/proc.py`` definition over
``psutil``. These tests pin the collapse so a second copy cannot silently
regrow: both former call sites resolve to the one probe, and neither module
re-implements a win32/POSIX probe of its own.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

from hpc_agent._kernel.lifecycle import detached
from hpc_agent.infra import proc, ssh_slots

_PROBE_TOKENS = ("OpenProcess", "GetExitCodeProcess", "GetLastError")


def test_ssh_slots_pid_alive_is_the_shared_probe() -> None:
    # Pure alias: ssh_slots._pid_alive IS proc.pid_alive — a regrown copy would
    # rebind this name to a fresh function and break the identity.
    assert ssh_slots._pid_alive is proc.pid_alive


def test_detached_pid_alive_forwards_to_the_shared_probe() -> None:
    # detached keeps a thin wrapper (the monkeypatch seam tests depend on), but
    # it imports and forwards to the one definition — no second implementation.
    assert detached.proc_pid_alive is proc.pid_alive


def _module_path(mod: object) -> Path:
    src = getattr(mod, "__file__", None)
    assert src is not None
    return Path(src)


def test_no_second_hand_rolled_probe_regrew() -> None:
    # The win32 ctypes probe substrate must live ONLY in proc.py. If either
    # former call site re-grows an OpenProcess/GetLastError probe (or an
    # os.kill liveness probe), this fires.
    for mod in (detached, ssh_slots):
        path = _module_path(mod)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # Only flag a real ctypes attribute access — docstrings that name the
        # tokens historically are fine.
        probe_attrs = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in _PROBE_TOKENS
        }
        assert not probe_attrs, (
            f"{path.name} re-grew a hand-rolled win32 probe {sorted(probe_attrs)} "
            "— the PID probe has ONE home (infra/proc.py)"
        )
        kill_probe = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "kill"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            for node in ast.walk(tree)
        )
        assert not kill_probe, f"{path.name} re-grew an os.kill probe"


def test_probe_semantics_preserved_across_the_seam() -> None:
    # The pid<=0 guard and a live-self probe agree at every surface.
    assert proc.pid_alive(os.getpid()) is True
    assert proc.pid_alive(0) is False
    assert proc.pid_alive(-1) is False
    # detached's wrapper matches its underlying definition.
    assert detached._pid_alive(os.getpid()) is True
    assert detached._pid_alive(0) is False
    assert detached._pid_alive(-1) is False
    # ssh_slots' alias is the same object, so it agrees by construction.
    assert ssh_slots._pid_alive(os.getpid()) is True
