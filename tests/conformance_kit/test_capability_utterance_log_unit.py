"""Mirror unit test for K4 — the capability-1 battery against the reference.

Proves the conformance module
(``hpc_agent.conformance.test_capability_utterance_log``) runs GREEN against the
built-in reference adapter (``hpc_agent.conformance.reference_adapter``) two ways:

* directly — each pure ``check_*`` assertion is exercised in-process against a
  freshly-claimed fixture repo (fast, precise diagnostics);
* end-to-end — a real ``pytest --pyargs`` subprocess loading the kit conftest,
  fixtures, and skip machinery with ``--harness-adapter …:build`` exits 0
  (proves the fixture wiring + report hook, not just the assertion bodies).

The reference is capability-1 ONLY, so it also documents the honest-partial
posture: it declares ``utterance-log`` (and the optional ``answer_question``
channel) and nothing else.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING, cast

import pytest

from hpc_agent.conformance import test_capability_utterance_log as k4
from hpc_agent.conformance.adapter import (
    CAP_UTTERANCE_LOG,
    declared_capabilities,
)
from hpc_agent.conformance.fixture_repo import claim_fixture_repo
from hpc_agent.conformance.reference_adapter import ReferenceAdapter, build

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A claimed, isolated fixture repo honoring HPC_JOURNAL_DIR (the kit idiom)."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return cast("Path", claim_fixture_repo(tmp_path / "experiment"))


# --- the built-in reference passes every capability-1 assertion --------------


def test_reference_schema_and_append_only(repo: Path) -> None:
    k4.check_schema_and_append_only(build(), repo)


def test_reference_byte_cap_full_text_sha(repo: Path) -> None:
    k4.check_byte_cap_full_text_sha(build(), repo)


def test_reference_codepoint_truncation(repo: Path) -> None:
    k4.check_codepoint_truncation(build(), repo)


def test_reference_no_scaffold(repo: Path) -> None:
    k4.check_no_scaffold(build(), repo)


def test_reference_injection_filter(repo: Path) -> None:
    k4.check_injection_filter(build(), repo)


def test_reference_clicked_vs_typed(repo: Path) -> None:
    k4.check_clicked_vs_typed(build(), repo)


def test_reference_authorship_gate_grants(repo: Path) -> None:
    k4.check_authorship_gate_grants_from_utterances(build(), repo)


def test_reference_authorship_gate_refuses(repo: Path) -> None:
    k4.check_authorship_gate_refuses_fabrication(build(), repo)


# --- the fixtures/derivations the battery depends on -------------------------


def test_injection_tags_derived_from_exported_regex() -> None:
    """Tags are DERIVED from the exported filter (never a hard-coded list), and
    cover the two named cases in the plan."""
    tags = k4.injection_tags()
    assert "task-notification" in tags
    assert "system-reminder" in tags
    # every derived tag really opens an injected turn the reference drops
    from hpc_agent.state.utterances import is_harness_injected

    for tag in tags:
        assert is_harness_injected(f"<{tag}> content")


def test_reference_declares_only_utterance_log() -> None:
    """Honest-partial: the minimal reference declares capability 1 and no more."""
    assert declared_capabilities(ReferenceAdapter()) == frozenset({CAP_UTTERANCE_LOG})
    assert callable(ReferenceAdapter().answer_question)


# --- end-to-end: the real kit run wires up and passes ------------------------


def test_kit_module_runs_green_via_pytest_subprocess(tmp_path: Path) -> None:
    """A real ``pytest`` run of the K4 module against the reference adapter
    exits 0 — exercising the conftest fixtures, skip machinery, and the
    conformance report hook, not merely the assertion bodies.

    The module is addressed by FILE PATH (not ``--pyargs``) so the package
    ``conftest.py`` is discovered along the arg path early enough to register
    ``--harness-adapter`` before option parsing (the K2 standalone idiom)."""
    repo_root = _repo_root()
    module_path = (
        repo_root / "src" / "hpc_agent" / "conformance" / "test_capability_utterance_log.py"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            str(module_path),
            "--harness-adapter",
            "hpc_agent.conformance.reference_adapter:build",
            "-q",
        ],
        cwd=str(repo_root),
        env={**_child_env(repo_root)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"kit run failed:\nSTDOUT\n{proc.stdout}\nSTDERR\n{proc.stderr}"
    # the report hook fired for the certified reference
    assert "harness conformance" in proc.stdout


def _repo_root() -> Path:
    from pathlib import Path

    return Path(__file__).resolve().parents[2]


def _child_env(repo_root: Path) -> dict[str, str]:
    import os

    env = dict(os.environ)
    src = str(repo_root / "src")
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src + (os.pathsep + existing if existing else "")
    return env
