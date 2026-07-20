"""K8 self-conformance — run the KIT AGAINST ITSELF for the two reference adapters.

The self-conformance leg (``docs/design/conformance-kit.md`` D-K5): a real
``pytest`` run of the kit's capability + negotiation + canonicalization modules,
loaded with each shipped reference adapter, must produce the honest verdict —

* ``hpc_agent.conformance.adapters.claude_code:build`` → FULLY conforming (all
  three capabilities pass, zero skips, the ``conforming: harness contract v1`` line);
* ``hpc_agent.conformance.adapters.notebook_render:build`` → honestly PARTIAL
  (capability 1 passes; relay-enforcement and backgrounding SKIP with their
  contract-named degraded tiers; zero failures; the ``partial: utterance-log`` line).

Both run OFFLINE, in-process against the hook cores / render stack — no live
harness, no network (the conformance-lane boundary). The modules are addressed by
FILE PATH (not ``--pyargs``) so the package ``conftest.py`` registers
``--harness-adapter`` before option parsing (the K2/K4 standalone idiom); the
child cwd is the repo root so the ``tests`` package (the elicitation rig) and the
kit conftest resolve. The notebook leg ``importorskip``s the render stack the CI
notebook job installs, and puts the plugin src on the child ``PYTHONPATH``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# The kit modules the self-run certifies against — the three capability batteries
# plus negotiation (declared==detected==behaved) and canonicalization.
_KIT_MODULES = (
    "test_capability_utterance_log.py",
    "test_capability_relay.py",
    "test_capability_backgrounding.py",
    "test_negotiation.py",
    "test_canonicalization.py",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _plugin_src() -> Path:
    return _repo_root() / "examples" / "plugins" / "hpc-agent-notebook-render" / "src"


def _child_env(*, extra_paths: tuple[Path, ...] = ()) -> dict[str, str]:
    """The child ``PYTHONPATH`` = ``src`` (+ any extra roots) prepended to the parent.

    The stop-hook append activation markers are SCRUBBED: set in some dev shells
    (the ``test_wave_c_adapters.py`` env-var note), they flip the reference Stop
    seam from the REJECTOR (block-once with a reason — the shape the relay kit
    certifies) into the COMPLETER, mis-shaping every contradiction triple. The
    completer has its own scoped-activation kit
    (``test_capability_stop_hook_append.py``); the self-run certifies the
    default posture. Same scrub as ``test_capability_decision_rendezvous.py``.
    """
    env = dict(os.environ)
    env.pop("HPC_STOP_HOOK_APPEND", None)
    env.pop("HPC_STOP_HOOK_APPEND_ON_BLOCK", None)
    roots = [str(_repo_root() / "src"), *(str(p) for p in extra_paths)]
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join([*roots, existing]) if existing else os.pathsep.join(roots)
    return env


def _run_kit(
    adapter: str, *, extra_paths: tuple[Path, ...] = ()
) -> subprocess.CompletedProcess[str]:
    """Run the kit modules against *adapter* in a subprocess; return the result."""
    root = _repo_root()
    module_args = [str(root / "src" / "hpc_agent" / "conformance" / name) for name in _KIT_MODULES]
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            *module_args,
            "--harness-adapter",
            adapter,
            "-q",
        ],
        cwd=str(root),
        env=_child_env(extra_paths=extra_paths),
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )


def test_claude_code_self_run_is_fully_conforming() -> None:
    """The full Claude Code reference certifies as CONFORMING — all three
    capabilities pass in-process, zero skips, and the report stamps the verdict."""
    proc = _run_kit("hpc_agent.conformance.adapters.claude_code:build")
    assert proc.returncode == 0, f"claude_code self-run failed:\n{proc.stdout}\n{proc.stderr}"
    out = proc.stdout
    assert "[conformance] harness: claude-code" in out
    assert "conforming: harness contract v1 (kit hpc-agent" in out
    # A conforming run rounds nothing up: no partial line, no skipped capability.
    assert "partial:" not in out
    assert "skipped:" not in out


def test_notebook_render_self_run_is_partial() -> None:
    """The notebook-render reference certifies as PARTIAL: capability 1 passes;
    relay-enforcement and backgrounding SKIP with their contract-named tiers; the
    run has zero failures (a partial is honest, never a failure)."""
    # The plugin ships in-repo (put on the child PYTHONPATH below); its render stack
    # is what the CI notebook job installs — skip when that stack is absent.
    pytest.importorskip("jupytext", reason="render stack not installed")
    pytest.importorskip("nbformat")
    pytest.importorskip("nbdime")
    if not _plugin_src().is_dir():
        pytest.skip("notebook-render plugin source not present")

    proc = _run_kit(
        "hpc_agent.conformance.adapters.notebook_render:build",
        extra_paths=(_plugin_src(),),
    )
    assert proc.returncode == 0, f"notebook_render self-run failed:\n{proc.stdout}\n{proc.stderr}"
    out = proc.stdout
    assert "[conformance] harness: notebook-render" in out
    assert "partial: utterance-log (kit hpc-agent" in out
    # The skips are listed WITH their contract-named degraded tiers, verbatim.
    assert "skipped: relay-enforcement — degraded tier: verb-only relay-audit posture" in out
    assert (
        "skipped: backgrounding — degraded tier: "
        "synchronous in-turn execution; correctness unaffected" in out
    )
    # Honest partial: never rounded up to conforming.
    assert "conforming: harness contract" not in out
