"""Tests for the ``PostToolUse`` AskUserQuestion answer-capture hook (run #5).

The second capture gap: selector answers never pass ``UserPromptSubmit``, so
a human who TYPED the sweep into the question tool was invisible to the
authorship gate. Covers the typed-vs-clicked line (a click on an
agent-authored option label is NEVER logged — the laundering channel), the
multi-select composed-of-labels case, annotation notes, the no-scaffold rule,
the silent stdout contract, and fail-open on garbage payloads.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from hpc_agent._kernel.hooks import answer_capture
from hpc_agent.state.utterances import read_utterances, utterances_path


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _scaffold_namespace(exp: Path) -> None:
    from hpc_agent.state.run_record import journal_dir

    journal_dir(exp)


def _payload(exp: Path, *, questions: list[dict], answers: dict, **extra: object) -> dict:
    return {
        "tool_name": "AskUserQuestion",
        "cwd": str(exp),
        "tool_input": {"questions": questions, "answers": answers, **extra},
    }


_SWEEP_Q = [
    {
        "question": "What sweep shape?",
        "header": "Sweep",
        "options": [{"label": "items_x_seeds"}, {"label": "cartesian_product"}],
    }
]


def test_typed_other_answer_is_captured(tmp_path: Path) -> None:
    """The run #5 shape: the human typed concrete values into the selector's
    free-text field — that IS human-authored evidence for the gate."""
    _scaffold_namespace(tmp_path)
    typed = "20 seeds, n_samples=1000000"
    records = answer_capture.capture(_payload(tmp_path, questions=_SWEEP_Q, answers={"q": typed}))
    assert len(records) == 1
    assert read_utterances(tmp_path)[0]["text"] == typed


def test_clicked_option_label_is_never_captured(tmp_path: Path) -> None:
    """A click on an agent-authored label carries no human authorship — logging
    it would let a fabricated option launder its tokens into the trust anchor."""
    _scaffold_namespace(tmp_path)
    out = answer_capture.capture(
        _payload(tmp_path, questions=_SWEEP_Q, answers={"q": "items_x_seeds"})
    )
    assert out == []
    assert read_utterances(tmp_path) == []


def test_multiselect_composed_of_labels_is_skipped_but_mixed_is_captured(
    tmp_path: Path,
) -> None:
    _scaffold_namespace(tmp_path)
    # All parts are offered labels → a multi-select click → skipped.
    clicked = _payload(
        tmp_path, questions=_SWEEP_Q, answers={"q": "items_x_seeds, cartesian_product"}
    )
    assert answer_capture.capture(clicked) == []
    # Any typed residue → the whole answer is captured verbatim.
    mixed = "items_x_seeds, with seeds 0 through 19"
    answer_capture.capture(_payload(tmp_path, questions=_SWEEP_Q, answers={"q": mixed}))
    assert [r["text"] for r in read_utterances(tmp_path)] == [mixed]


def test_annotation_notes_are_captured(tmp_path: Path) -> None:
    _scaffold_namespace(tmp_path)
    payload = _payload(
        tmp_path,
        questions=_SWEEP_Q,
        answers={"q": "items_x_seeds"},
        annotations={"q": {"notes": "use 1M samples per seed"}},
    )
    answer_capture.capture(payload)
    assert [r["text"] for r in read_utterances(tmp_path)] == ["use 1M samples per seed"]


def test_never_scaffolds_a_journal_namespace(tmp_path: Path) -> None:
    home = tmp_path / "journal"
    out = answer_capture.capture(
        _payload(tmp_path / "somerepo", questions=_SWEEP_Q, answers={"q": "typed value 42"})
    )
    assert out == []
    assert not home.exists() or not any(home.iterdir())


def test_ignores_other_tools_and_malformed_payloads(tmp_path: Path) -> None:
    _scaffold_namespace(tmp_path)
    assert answer_capture.capture(None) == []
    assert answer_capture.capture({"tool_name": "Bash", "cwd": str(tmp_path)}) == []
    assert answer_capture.capture({"tool_name": "AskUserQuestion", "cwd": str(tmp_path)}) == []
    non_string_answer = {
        "tool_name": "AskUserQuestion",
        "cwd": str(tmp_path),
        "tool_input": {"answers": {"q": 42}},
    }
    assert answer_capture.capture(non_string_answer) == []
    assert not utterances_path(tmp_path).exists()


def test_main_writes_log_and_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _scaffold_namespace(tmp_path)
    payload = _payload(tmp_path, questions=_SWEEP_Q, answers={"q": "seeds 0 through 19"})
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))

    assert answer_capture.main() == 0
    assert capsys.readouterr().out == ""
    assert read_utterances(tmp_path)[0]["text"] == "seeds 0 through 19"


def test_main_is_a_clean_noop_on_garbage_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert answer_capture.main() == 0
    assert capsys.readouterr().out == ""


# --- MT4: HPC_ACTOR attribution (seam onto MT1's ``append_utterance(actor=)``) ---


def _spy_append(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple, dict]]:
    """Spy over ``append_utterance`` (imported at call time inside ``capture``)."""
    calls: list[tuple[tuple, dict]] = []

    def _spy(*args: object, **kwargs: object) -> dict:
        calls.append((args, kwargs))
        return {"ts": "t", "sha256": "s", "text": str(args[1]) if len(args) > 1 else ""}

    monkeypatch.setattr("hpc_agent.state.utterances.append_utterance", _spy)
    return calls


def test_capture_passes_actor_kwarg_when_hpc_actor_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_ACTOR", "bob")
    calls = _spy_append(monkeypatch)

    answer_capture.capture(_payload(tmp_path, questions=_SWEEP_Q, answers={"q": "typed value 42"}))

    assert len(calls) == 1
    _args, kwargs = calls[0]
    assert kwargs == {"actor": "bob"}


def test_capture_omits_actor_kwarg_when_hpc_actor_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HPC_ACTOR", raising=False)
    calls = _spy_append(monkeypatch)

    answer_capture.capture(_payload(tmp_path, questions=_SWEEP_Q, answers={"q": "typed value 42"}))

    assert len(calls) == 1
    _args, kwargs = calls[0]
    assert "actor" not in kwargs


def test_capture_omits_actor_kwarg_when_hpc_actor_invalid_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_ACTOR", "bad/slug here")
    calls = _spy_append(monkeypatch)

    answer_capture.capture(_payload(tmp_path, questions=_SWEEP_Q, answers={"q": "typed value 42"}))

    assert len(calls) == 1
    _args, kwargs = calls[0]
    assert "actor" not in kwargs


def test_capture_unset_env_is_byte_identical_end_to_end(tmp_path: Path) -> None:
    _scaffold_namespace(tmp_path)
    records = answer_capture.capture(
        _payload(tmp_path, questions=_SWEEP_Q, answers={"q": "typed value 42"})
    )
    assert len(records) == 1
    assert utterances_path(tmp_path).name == "utterances.jsonl"
    assert read_utterances(tmp_path)[0]["text"] == "typed value 42"
