"""Pre-stage local task-0 smoke gate (notebook-audit Addendum 4, item 7).

The gate wires ``ops/validate/dry_run_local`` into the submit flow BEFORE any
transport/staging: a broken executor (run #11's units bug) is caught locally in
seconds instead of first at the REMOTE canary after an expensive stage.

Semantics under test (one per test so a refactor that breaks one path can't
silently take out the others):

* a nonzero exit within the timeout REFUSES the stage, relaying the executor's
  own stderr tail verbatim;
* an import failure REFUSES but flags a possible local-env artifact and names
  the opt-out field;
* a timeout is NOT a failure — the gate discloses "inconclusive" and proceeds;
* the ``pre_stage_smoke=false`` opt-out skips a would-be refusal;
* a crash in the smoke runner itself is fail-open (disclose + proceed).

Executors are tiny temp python scripts (as ``test_dry_run_local`` does), invoked
via the quoted ``sys.executable`` so the shell-string contract is cross-platform
(no POSIX single-quoting), and run with ``cwd=experiment_dir`` so the relative
script name resolves.
"""

from __future__ import annotations

import json
import textwrap
from typing import TYPE_CHECKING, Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec
from hpc_agent.ops import submit_flow as sf

if TYPE_CHECKING:
    from pathlib import Path

# The quoted interpreter tolerates a space in the path (e.g. the uv-tool venv)
# under BOTH cmd.exe and /bin/sh, so these tests run on native Windows too.
import sys

_EXE = f'"{sys.executable}"'


def _write_tasks_py(exp: Path) -> None:
    """A stub .hpc/tasks.py exposing total()/resolve(i) with one runnable id."""
    target = exp / ".hpc" / "tasks.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def total(): return 1\ndef resolve(i): return {'seed': 1}\n")


def _write_script(exp: Path, name: str, body: str) -> str:
    """Drop a tiny executor script into *exp* and return the quoted command."""
    (exp / name).write_text(textwrap.dedent(body))
    return f"{_EXE} {name}"


def _write_sidecar(exp: Path, run_id: str, *, executor: str, template: str) -> None:
    """Write a minimal per-run sidecar the gate reads for the smoke inputs."""
    from hpc_agent.state.runs import run_sidecar_path

    path = run_sidecar_path(exp, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"executor": executor, "result_dir_template": template}))


def _spec(**over: Any) -> SubmitFlowSpec:
    base: dict[str, Any] = dict(
        profile="p",
        cluster="c",
        ssh_target="u@h",
        remote_path="/r",
        job_name="j",
        run_id="run-1",
        total_tasks=100,
        backend="sge",
        script=".hpc/templates/cpu_array.sh",
        job_env={"EXECUTOR": "python3 .hpc/_hpc_dispatch.py", "HPC_CMD_SHA": "sha-abc"},
    )
    base.update(over)
    return SubmitFlowSpec(**base)


_TEMPLATE = "results/seed_{seed}"


# ── refusal paths ──────────────────────────────────────────────────────


def test_nonzero_exit_refuses_stage_with_stderr_verbatim(tmp_path: Path) -> None:
    _write_tasks_py(tmp_path)
    ex = _write_script(
        tmp_path,
        "boom.py",
        """
        import sys
        sys.stderr.write("BOOM_MARKER_XYZ: units bug\\n")
        sys.exit(1)
        """,
    )
    with pytest.raises(errors.SpecInvalid) as ei:
        sf._smoke_one_executor(tmp_path, spec=_spec(), executor=ex, result_dir_template=_TEMPLATE)
    msg = str(ei.value)
    # The executor's own stderr is relayed verbatim; core never interprets it.
    assert "BOOM_MARKER_XYZ" in msg
    assert "exited 1" in msg
    # And the actionable opt-out is named.
    assert "pre_stage_smoke=false" in msg


def test_import_error_refuses_but_flags_local_env_artifact(tmp_path: Path) -> None:
    _write_tasks_py(tmp_path)
    ex = _write_script(
        tmp_path,
        "badimport.py",
        "import a_module_that_does_not_exist_xyz\n",
    )
    with pytest.raises(errors.SpecInvalid) as ei:
        sf._smoke_one_executor(tmp_path, spec=_spec(), executor=ex, result_dir_template=_TEMPLATE)
    msg = str(ei.value)
    assert "ModuleNotFoundError" in msg  # stderr tail relayed verbatim
    # An import failure MAY be a cluster-only dependency — the refusal says so
    # and names the field to skip the gate.
    assert "local-env artifact" in msg
    assert "pre_stage_smoke=false" in msg


# ── proceed paths ──────────────────────────────────────────────────────


def test_timeout_is_inconclusive_and_proceeds(tmp_path: Path, capsys) -> None:
    _write_tasks_py(tmp_path)
    ex = _write_script(
        tmp_path,
        "slow.py",
        "import time\ntime.sleep(30)\n",
    )
    # A sleep past the 1s cap is a TIMEOUT — not a failure (long tasks are
    # normal). No raise; a disclosure is surfaced.
    sf._smoke_one_executor(
        tmp_path,
        spec=_spec(pre_stage_smoke_timeout_sec=1),
        executor=ex,
        result_dir_template=_TEMPLATE,
    )
    err = capsys.readouterr().err
    assert "inconclusive" in err
    assert "timeout after 1s" in err


def test_smoke_runner_exception_is_fail_open(tmp_path: Path, capsys, monkeypatch) -> None:
    _write_tasks_py(tmp_path)
    # The smoke runner itself blowing up must NEVER block a submit.
    from hpc_agent.ops.validate import dry_run_local as drl

    def _boom(*a, **k):
        raise RuntimeError("smoke infra exploded")

    monkeypatch.setattr(drl, "dry_run_local", _boom)
    sf._smoke_one_executor(
        tmp_path,
        spec=_spec(),
        executor=f"{_EXE} -c pass",
        result_dir_template=_TEMPLATE,
    )
    err = capsys.readouterr().err
    assert "smoke runner errored" in err


def test_healthy_executor_passes_silently(tmp_path: Path, capsys) -> None:
    _write_tasks_py(tmp_path)
    ex = _write_script(tmp_path, "ok.py", "raise SystemExit(0)\n")
    sf._smoke_one_executor(tmp_path, spec=_spec(), executor=ex, result_dir_template=_TEMPLATE)
    # A green smoke emits no disclosure and no refusal.
    assert capsys.readouterr().err == ""


# ── the gate: opt-out + sidecar read ───────────────────────────────────


def test_gate_refuses_from_sidecar_executor(tmp_path: Path) -> None:
    """End-to-end through the gate: it reads the executor off the sidecar."""
    _write_tasks_py(tmp_path)
    ex = _write_script(tmp_path, "boom.py", "raise SystemExit(1)\n")
    spec = _spec()
    _write_sidecar(tmp_path, spec.run_id, executor=ex, template=_TEMPLATE)
    with pytest.raises(errors.SpecInvalid):
        sf._pre_stage_smoke_gate(tmp_path, [spec], [0])


def test_opt_out_skips_a_would_be_refusal(tmp_path: Path) -> None:
    """``pre_stage_smoke=false`` skips the gate — the same broken executor that
    refuses above is waved through, proving the opt-out actually fires."""
    _write_tasks_py(tmp_path)
    ex = _write_script(tmp_path, "boom.py", "raise SystemExit(1)\n")
    spec = _spec(pre_stage_smoke=False)
    _write_sidecar(tmp_path, spec.run_id, executor=ex, template=_TEMPLATE)
    # No raise: the gate never ran the broken executor.
    sf._pre_stage_smoke_gate(tmp_path, [spec], [0])


def test_refusal_discloses_pinned_interpreter(tmp_path: Path) -> None:
    """FIX C: a BARE ``python3`` executor token is pinned to ``sys.executable``
    for the LOCAL smoke (an import sanity check), and the refusal DISCLOSES which
    interpreter actually ran — so a local-interpreter mismatch is not mistaken for
    a cluster bug."""
    import sys

    _write_tasks_py(tmp_path)
    (tmp_path / "boom.py").write_text("raise SystemExit(1)\n")
    with pytest.raises(errors.SpecInvalid) as ei:
        sf._smoke_one_executor(
            tmp_path, spec=_spec(), executor="python3 boom.py", result_dir_template=_TEMPLATE
        )
    msg = str(ei.value)
    assert "pinned to sys.executable" in msg
    assert sys.executable in msg


def test_smoke_interpreter_disclosure_extracts_first_token() -> None:
    """The disclosure names the interpreter from the finding's command evidence,
    handling both the quoted-path form and a bare token."""
    from types import SimpleNamespace

    quoted = SimpleNamespace(evidence={"command": '"C:\\a b\\python.exe" -m x'})
    assert "C:\\a b\\python.exe" in sf._smoke_interpreter_disclosure(quoted)

    bare = SimpleNamespace(evidence={"command": "/usr/bin/python3 x.py"})
    assert "/usr/bin/python3" in sf._smoke_interpreter_disclosure(bare)

    # No command evidence → empty disclosure (never crashes).
    assert sf._smoke_interpreter_disclosure(SimpleNamespace(evidence={})) == ""


def test_localize_interpreter_substitutes_bare_python() -> None:
    """Cluster-shaped ``python3 -m ...`` must run under THIS interpreter
    locally (run #11: PATH's python3 was msys64's, no hpc_agent, every smoke
    refused); an explicit path is respected verbatim."""
    import sys

    from hpc_agent.ops.validate.dry_run_local import _localize_interpreter

    assert _localize_interpreter("python3 -m hpc_agent.executor_cli run-registered x.py") == (
        f'"{sys.executable}" -m hpc_agent.executor_cli run-registered x.py'
    )
    assert _localize_interpreter("python x.py").startswith(f'"{sys.executable}" ')
    assert _localize_interpreter("/usr/bin/python3 x.py") == "/usr/bin/python3 x.py"
    assert _localize_interpreter("bash run.sh") == "bash run.sh"
