"""The fixture-repo builder — HPC_JOURNAL_DIR honored + repo isolation (K1).

The builder must land the journal namespace under whatever HPC_JOURNAL_DIR
resolves (never the developer's real ~/.claude/hpc), and two distinct
experiment dirs must be isolated (distinct <repo_hash>/ namespaces) so an
utterance written to one is invisible to the other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hpc_agent.conformance.fixture_repo import claim_fixture_repo, journal_home
from hpc_agent.state.utterances import append_utterance, read_utterances, utterances_path

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_honors_hpc_journal_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "j"
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(home))
    exp = claim_fixture_repo(tmp_path / "exp")

    assert journal_home() == home
    # the claimed namespace (and thus the utterance log path) lives UNDER home
    ns = utterances_path(exp).parent
    assert ns.is_dir()
    assert home in ns.parents


def test_claim_makes_writes_land(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "j"))
    exp = claim_fixture_repo(tmp_path / "exp")
    # no-scaffold contract: the append lands only because the namespace exists
    record = append_utterance(exp, "20 seeds at 1M samples")
    assert record is not None
    assert [r["text"] for r in read_utterances(exp)] == ["20 seeds at 1M samples"]


def test_repos_are_isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "j"))
    exp1 = claim_fixture_repo(tmp_path / "exp1")
    exp2 = claim_fixture_repo(tmp_path / "exp2")

    assert utterances_path(exp1).parent != utterances_path(exp2).parent

    append_utterance(exp1, "only in one")
    assert [r["text"] for r in read_utterances(exp1)] == ["only in one"]
    assert read_utterances(exp2) == []


def test_unclaimed_repo_is_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A dir the builder did NOT claim leaves the write a clean no-op
    (no-scaffold) — proving the claim is what enables the namespace."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "j"))
    unclaimed = tmp_path / "unclaimed"
    unclaimed.mkdir()
    assert append_utterance(unclaimed, "dropped") is None
    assert read_utterances(unclaimed) == []
