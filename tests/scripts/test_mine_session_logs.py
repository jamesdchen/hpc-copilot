"""Tests for ``scripts/mine_session_logs.py`` — the transcript forensics walker.

Pins the three classification lanes over a synthetic Claude Code transcript:
human turns (harness-injected user turns dropped via the SAME public filter
the capture hook uses), exact-matched harness verbs (``mcp__hpc-agent__*`` +
``hpc-agent`` CLI invocations), and off-script cluster mutations — including
the ``!``-prefix ``<bash-input>`` hand-submit class that motivated the tool
(2026-07-27) and the log-tail-watcher false-positive class the first live walk
surfaced (a grep PATTERN containing ``qsub`` is not a mutation).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "mine_session_logs", REPO_ROOT / "scripts" / "mine_session_logs.py"
)
assert _SPEC is not None and _SPEC.loader is not None
mine = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("mine_session_logs", mine)
_SPEC.loader.exec_module(mine)


def _user(text: str, ts: str) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"content": text}}


def _tool_use(name: str, tool_input: dict, ts: str) -> dict:
    return {
        "type": "assistant",
        "timestamp": ts,
        "message": {"content": [{"type": "tool_use", "name": name, "input": tool_input}]},
    }


def _write_transcript(path: Path, records: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\ngarbage-not-json\n", encoding="utf-8"
    )


def test_walker_classifies_all_three_lanes(tmp_path: Path) -> None:
    transcript = tmp_path / "sess-abc.jsonl"
    _write_transcript(
        transcript,
        [
            _user("I sign off on feature-construction", "2026-07-27T08:00:00Z"),
            # Harness-injected user turns are NOT human utterances.
            _user(
                "<task-notification>\n<task-id>x</task-id>\n</task-notification>",
                "2026-07-27T08:01:00Z",
            ),
            _user(
                "<local-command-stdout>Login successful</local-command-stdout>",
                "2026-07-27T08:02:00Z",
            ),
            # Harness verbs: MCP tool + CLI invocation in Bash.
            _tool_use("mcp__hpc-agent__submit-s2", {"cluster": "carc"}, "2026-07-27T08:03:00Z"),
            _tool_use(
                "Bash",
                {"command": 'cd "/c/x" && hpc-agent wait-detached --spec s.json'},
                "2026-07-27T08:04:00Z",
            ),
            # The log-tail watcher class: qsub inside a grep PATTERN, no ssh —
            # NOT a mutation (first live walk's false-positive class).
            _tool_use(
                "Bash",
                {"command": 'until grep -qE "qsub|submitted|[fatal]" /c/logs/w.log; do :; done'},
                "2026-07-27T08:05:00Z",
            ),
            # A genuine model-composed off-script mutation (ssh + qdel).
            _tool_use(
                "Bash",
                {"command": "ssh.exe -o BatchMode=yes u@dtn \"bash -lc 'qdel 111 222'\""},
                "2026-07-27T08:06:00Z",
            ),
            # The 07-27 class: the human hand-submits via a !-prefix bash turn.
            _user(
                "<bash-input>ssh usc-discovery sbatch --array=1-500 job.slurm</bash-input>",
                "2026-07-27T08:07:00Z",
            ),
        ],
    )
    events = mine.walk_transcript(transcript)
    kinds = [(e.kind, e.detail) for e in events]

    humans = [d for k, d in kinds if k == "human"]
    assert humans == ["I sign off on feature-construction"]  # injected turns dropped

    verbs = [d for k, d in kinds if k == "harness_verb"]
    assert "submit-s2" in verbs  # MCP prefix stripped, exact-matched
    assert any("wait-detached" in d for d in verbs)  # CLI invocation

    off = [d for k, d in kinds if k == "off_script"]
    assert len(off) == 2
    assert any("qdel 111 222" in d for d in off)  # model-composed
    assert any("sbatch --array=1-500" in d for d in off)  # human bash-input
    assert not any("until grep" in d for d in off)  # watcher class excluded

    # The torn tail line never kills the walk (append-only file, live session).
    assert all(e.session == "sess-abc" for e in events)


def test_markdown_report_surfaces_off_script_section(tmp_path: Path) -> None:
    transcript = tmp_path / "sess-md.jsonl"
    _write_transcript(
        transcript,
        [
            _user("kick off the backtest", "2026-07-27T09:00:00Z"),
            _user(
                "<bash-input>ssh h2 qsub -t 1-400 wrapper.sh</bash-input>",
                "2026-07-27T09:01:00Z",
            ),
        ],
    )
    report = mine.render_markdown(mine.walk_transcript(transcript))
    assert "OFF-SCRIPT cluster mutations: 1" in report
    assert "qsub -t 1-400" in report
    assert "no journal" in report  # the section names WHY these rows matter


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
