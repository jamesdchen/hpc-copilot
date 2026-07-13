"""Tests for the remote-read-ack lint (spec B3′, run-12 finding 24).

Pins these invariants (mirrors ``test_lint_no_raw_ssh.py``):

1. The real tree passes — every ssh consumer that reads a remote ``.stdout``
   is either ack-routed (module references an ack helper) or carries a cited
   ALLOWLIST entry today.
2. The lint can actually FIRE: a function that calls ``ssh_run(...)`` and reads
   ``.stdout`` with NO ack helper and NO ALLOWLIST entry is reported.
3. Ack-routed reads do NOT fire: a module that references ``split_ack`` /
   ``wrap_with_ack`` / ``scheduler_query_ran`` is trusted as ack-aware.
4. The cited ALLOWLIST exempts a ``path::function``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_remote_read_ack", REPO_ROOT / "scripts" / "lint_remote_read_ack.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_remote_read_ack"] = lint
_SPEC.loader.exec_module(lint)


def test_real_tree_is_clean() -> None:
    """No ssh consumer reads a remote .stdout un-ack-gated on the current tree."""
    assert lint.main() == 0


def _module(tmp_path: Path, body: str, *, name: str = "demo.py") -> Path:
    """Write *body* as a module under a synthetic scan root and return the root."""
    root = tmp_path / "src"
    p = root / "hpc_agent" / "ops" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return root


# A function that calls ssh_run and reads .stdout with no ack routing.
_UNGUARDED = (
    "from hpc_agent.infra import remote\n"
    "\n"
    "def read_state(ssh_target):\n"
    "    proc = remote.ssh_run('squeue', ssh_target=ssh_target)\n"
    "    return proc.stdout.splitlines()\n"
)

# The same read, routed through split_ack (ack-aware module).
_GUARDED = (
    "from hpc_agent.infra import remote\n"
    "from hpc_agent.infra.ssh_validation import split_ack\n"
    "\n"
    "def read_state(ssh_target):\n"
    "    proc = remote.ssh_run('squeue', ssh_target=ssh_target)\n"
    "    clean, rc = split_ack(proc.stdout or '', '__ACK__=')\n"
    "    return clean\n"
)


def test_lint_rule_fires_on_synthetic_input(tmp_path: Path, capsys) -> None:
    """A synthetic ungated ssh_run().stdout read is reported by lint_file."""
    root = _module(tmp_path, _UNGUARDED)
    path = root / "hpc_agent" / "ops" / "demo.py"
    findings = lint.lint_file(path)
    assert findings, "expected a finding for the un-ack-gated read"
    assert any("remote-read not ack-gated" in msg for _, msg in findings)
    # and it fails the whole run
    assert lint.main(root) == 1
    out = capsys.readouterr().out
    assert "read_state" in out


def test_ack_routed_module_produces_no_finding(tmp_path: Path) -> None:
    """A module that routes the read through split_ack is detected clean."""
    root = _module(tmp_path, _GUARDED)
    path = root / "hpc_agent" / "ops" / "demo.py"
    assert lint.lint_file(path) == []
    assert lint.main(root) == 0


def test_scheduler_query_ran_also_counts_as_ack(tmp_path: Path) -> None:
    """The composed helper ``scheduler_query_ran`` (attribute form) also clears."""
    body = (
        "from hpc_agent.infra import remote\n"
        "\n"
        "def read_state(backend_cls, ssh_target):\n"
        "    proc = remote.ssh_run('squeue', ssh_target=ssh_target)\n"
        "    clean, ok = backend_cls.scheduler_query_ran(proc.stdout)\n"
        "    return clean\n"
    )
    root = _module(tmp_path, body)
    assert lint.main(root) == 0


def test_local_subprocess_stdout_without_ssh_run_is_clean(tmp_path: Path) -> None:
    """A .stdout read with no ssh_run call in the function is not a candidate."""
    body = (
        "import subprocess\n"
        "\n"
        "def read_local():\n"
        "    cp = subprocess.run(['echo', 'ok'], capture_output=True, text=True)\n"
        "    return cp.stdout\n"
    )
    root = _module(tmp_path, body)
    assert lint.main(root) == 0


def test_allowlist_exempts_a_path_function(tmp_path: Path, monkeypatch) -> None:
    root = _module(tmp_path, _UNGUARDED)
    key = "hpc_agent/ops/demo.py::read_state"
    assert lint.main(root) == 1  # fires without the exemption
    monkeypatch.setattr(lint, "ALLOWLIST", frozenset({key}))
    assert lint.main(root) == 0  # cited exemption clears it


def test_ssh_validation_module_is_skipped(tmp_path: Path) -> None:
    """The helper-defining module is skipped even if it reads a bare .stdout."""
    root = tmp_path / "src"
    p = root / "hpc_agent" / "infra" / "ssh_validation.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    # A candidate-shaped body: calls ssh_run and reads .stdout, no ack routing.
    p.write_text(_UNGUARDED, encoding="utf-8")
    assert lint.lint_file(p) == []
    assert lint.main(root) == 0
