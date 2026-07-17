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


def test_missing_third_party_dep_discloses_and_proceeds(tmp_path: Path, capsys) -> None:
    """run-14 finding #6: an import of a dependency ABSENT from the local
    interpreter but supplied by the cluster env (e.g. pandas on the native-Windows
    control-plane venv) is an HONEST DISCLOSURE, not a refusal. The local smoke
    cannot judge the cluster env; the cluster canary still verifies it. Previously
    this REFUSED and forced the human to opt out of a useful check."""
    _write_tasks_py(tmp_path)
    ex = _write_script(
        tmp_path,
        "needs_a_cluster_dep.py",
        "import a_third_party_pkg_not_in_this_repo_xyz\n",
    )
    # No raise — the missing module is not one of the repo's own modules, so it is
    # treated as a cluster-env dependency and disclosed.
    sf._smoke_one_executor(tmp_path, spec=_spec(), executor=ex, result_dir_template=_TEMPLATE)
    err = capsys.readouterr().err
    assert "a_third_party_pkg_not_in_this_repo_xyz" in err
    assert "cluster canary will verify" in err
    # The disclosure names the interpreter it actually ran under (FIX C: pinned to
    # sys.executable, never PATH's python3).
    assert sys.executable in err


def test_import_error_of_own_repo_module_refuses(tmp_path: Path) -> None:
    """Teeth: a ModuleNotFoundError naming one of the repo's OWN packages (a broken
    sub-import inside the user's code) is something the local smoke CAN judge — the
    cluster would fail identically — so the stage is still refused, and the opt-out
    is named."""
    _write_tasks_py(tmp_path)
    pkg = tmp_path / "mypkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    ex = _write_script(tmp_path, "uses_pkg.py", "import mypkg.does_not_exist_sub\n")
    with pytest.raises(errors.SpecInvalid) as ei:
        sf._smoke_one_executor(tmp_path, spec=_spec(), executor=ex, result_dir_template=_TEMPLATE)
    msg = str(ei.value)
    assert "part of the experiment repo" in msg
    assert "mypkg" in msg
    assert "pre_stage_smoke=false" in msg  # opt-out still named


def test_syntax_error_still_refuses(tmp_path: Path) -> None:
    """Teeth: a genuine SyntaxError is something the local smoke CAN judge (Python
    compiles the whole module before running any line), so it still refuses even
    with the run-14 finding-#6 disclosure path in place — a syntax error is never a
    missing-dependency disclosure."""
    _write_tasks_py(tmp_path)
    ex = _write_script(tmp_path, "syntaxbad.py", "def f(:\n    pass\n")
    with pytest.raises(errors.SpecInvalid) as ei:
        sf._smoke_one_executor(tmp_path, spec=_spec(), executor=ex, result_dir_template=_TEMPLATE)
    assert "SyntaxError" in str(ei.value)


def test_missing_module_discriminator_helpers(tmp_path: Path) -> None:
    """Unit-pin the gate's interpretation helpers: repo-own vs third-party, and the
    plain-ImportError (no 'No module named') → None → refuse path."""
    (tmp_path / "mymod.py").write_text("")
    (tmp_path / "mypkg").mkdir()
    assert sf._module_shipped_in_repo(tmp_path, "mymod")
    assert sf._module_shipped_in_repo(tmp_path, "mypkg")
    assert not sf._module_shipped_in_repo(tmp_path, "pandas")

    assert (
        sf._missing_top_level_module("ModuleNotFoundError: No module named 'pandas.core'")
        == "pandas"
    )
    assert sf._missing_top_level_module('No module named "torch"') == "torch"
    # A plain ImportError names no missing module → None → the refuse path, never a
    # cluster-env disclosure.
    assert sf._missing_top_level_module("ImportError: cannot import name 'x' from 'y'") is None
    assert sf._missing_top_level_module("") is None


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


def test_win32_resolution_never_invokes_bare_python3(monkeypatch) -> None:
    """Rung 3 of the local-interpreter resolution (run-14 finding #6): a CLUSTER-
    shaped bare ``python3``/``python`` executor is pinned to the quoted control-
    plane interpreter (``sys.executable``) — NEVER left as a bare ``python3`` PATH
    lookup, which on the native-Windows box resolves a foreign/absent interpreter
    (run #11: msys64's python, no hpc_agent). Pin the CONSTRUCTED command under a
    win32-reported platform.

    (Rung 1 — an experiment-repo venv named by the spec/sidecar — is intentionally
    absent: no spec/sidecar field names a LOCAL interpreter. The only interpreter a
    sidecar names is ``env_python``, a CLUSTER path unusable on the local box. So
    resolution is rung 2 (sys.executable) with rung 3 as the win32 guarantee.)"""
    import sys as _sys

    from hpc_agent.ops.validate.dry_run_local import _localize_interpreter

    monkeypatch.setattr(_sys, "platform", "win32", raising=False)
    for cluster_cmd in (
        "python3 -m hpc_agent.executor_cli run-registered train.py",
        "python train.py --seed $SEED",
    ):
        built = _localize_interpreter(cluster_cmd)
        assert not built.startswith("python3 ")  # never a bare PATH lookup
        assert not built.startswith("python ")
        assert built.startswith(f'"{_sys.executable}" ')  # always the pinned interpreter
