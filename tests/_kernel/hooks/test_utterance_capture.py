"""Tests for the ``UserPromptSubmit`` utterance-capture hook (proving run #4).

The human-authorship gate's evidence upgrade: the harness — not the agent —
appends each human prompt to ``<journal home>/<repo_hash>/utterances.jsonl``.
Covers the append record shape (ts + sha256 + text), the no-scaffold rule
(a prompt in a repo with no journal namespace leaves zero footprint), the
per-entry size cap (text capped, sha256 over the FULL raw prompt), the
silent stdout contract, and fail-open on garbage payloads.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from hpc_agent._kernel.hooks import utterance_capture
from hpc_agent.state.utterances import (
    MAX_UTTERANCE_BYTES,
    read_utterances,
    utterances_path,
)


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _scaffold_namespace(exp: Path) -> None:
    """Make *exp* an hpc repo — the way real state writes do."""
    from hpc_agent.state.run_record import journal_dir

    journal_dir(exp)


def test_capture_appends_ts_sha_and_text(tmp_path: Path) -> None:
    _scaffold_namespace(tmp_path)
    prompt = "use 20 seeds at 1M samples each"

    record = utterance_capture.capture({"cwd": str(tmp_path), "prompt": prompt})
    assert record is not None

    logged = read_utterances(tmp_path)
    assert len(logged) == 1
    entry = logged[0]
    assert entry["text"] == prompt
    assert entry["sha256"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    assert entry["ts"]  # auto-stamped ISO

    # Append-only: a second prompt adds a second line.
    utterance_capture.capture({"cwd": str(tmp_path), "prompt": "y"})
    assert len(read_utterances(tmp_path)) == 2


def test_capture_never_scaffolds_a_journal_namespace(tmp_path: Path) -> None:
    """No-scaffold (finding g): a prompt typed in a non-hpc repo must not
    create ~/.claude/hpc/<repo_hash>/ — the hook is installed user-globally."""
    home = tmp_path / "journal"
    out = utterance_capture.capture({"cwd": str(tmp_path / "somerepo"), "prompt": "hello"})
    assert out is None
    assert not home.exists() or not any(home.iterdir())


def test_size_cap_truncates_text_but_hashes_full_prompt(tmp_path: Path) -> None:
    _scaffold_namespace(tmp_path)
    prompt = "x" * (MAX_UTTERANCE_BYTES * 3)

    utterance_capture.capture({"cwd": str(tmp_path), "prompt": prompt})
    entry = read_utterances(tmp_path)[0]
    assert len(entry["text"].encode("utf-8")) <= MAX_UTTERANCE_BYTES
    # The fingerprint still covers the whole raw prompt.
    assert entry["sha256"] == hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def test_capture_ignores_empty_and_malformed_payloads(tmp_path: Path) -> None:
    _scaffold_namespace(tmp_path)
    assert utterance_capture.capture(None) is None
    assert utterance_capture.capture({"cwd": str(tmp_path)}) is None
    assert utterance_capture.capture({"cwd": str(tmp_path), "prompt": "   "}) is None
    assert utterance_capture.capture({"cwd": str(tmp_path), "prompt": 42}) is None
    assert not utterances_path(tmp_path).exists()


def test_capture_drops_harness_injected_payloads(tmp_path: Path) -> None:
    """Proving run #5: a background-task ``<task-notification>`` fired the
    hook and landed in the log as a "human utterance" — agent-influenced
    text inside the gate's trust anchor. A prompt OPENING with a harness
    tag is dropped; a human prompt quoting one mid-text still lands."""
    _scaffold_namespace(tmp_path)
    injected = [
        "<task-notification>\n<task-id>bg1</task-id>\n</task-notification>",
        "  <system-reminder>context stuff</system-reminder>",
        "<local-command-caveat>Caveat: ...</local-command-caveat>",
        "<command-name>/clear</command-name>",
    ]
    for prompt in injected:
        assert utterance_capture.capture({"cwd": str(tmp_path), "prompt": prompt}) is None
    assert read_utterances(tmp_path) == []

    quoting = "why did I get a <task-notification> about 20 seeds?"
    assert utterance_capture.capture({"cwd": str(tmp_path), "prompt": quoting}) is not None
    assert read_utterances(tmp_path)[0]["text"] == quoting


def test_main_writes_log_and_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UserPromptSubmit stdout is injected into model context — the capture
    must be silent (its whole point is an out-of-band record)."""
    _scaffold_namespace(tmp_path)
    payload = {"cwd": str(tmp_path), "prompt": "estimate pi via monte carlo"}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert utterance_capture.main() == 0
    assert capsys.readouterr().out == ""
    assert read_utterances(tmp_path)[0]["text"] == "estimate pi via monte carlo"


def test_main_is_a_clean_noop_on_garbage_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert utterance_capture.main() == 0
    assert capsys.readouterr().out == ""
