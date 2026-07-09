"""Conformance kit K4 — capability 1, the out-of-band human-utterance log.

Certifies a CANDIDATE harness's utterance-log provider against the normative
write API (``docs/internals/harness-contract.md`` §2 + capability 1). Every
write goes through :meth:`~hpc_agent.conformance.adapter.HarnessAdapter.write_utterance`
(the harness's own human-input channel, filters included); every read goes
through :func:`hpc_agent.state.utterances.read_utterances` — the kit NEVER writes
the log directly. The assertion battery (D-K3):

* frozen 3-field record schema + sorted-keys byte-shape + append-only oldest-first;
* the byte cap with the sha over the FULL raw text; codepoint-boundary truncation;
* no-scaffold — a write into an unclaimed namespace is a clean no-op with ZERO
  footprint;
* provenance: harness-injection tags (derived FROM the exported filter regex so a
  filter extension auto-extends the fixtures) are refused; a tag quoted mid-text
  lands; a clicked option is not logged, typed free text is (``answer_question``);
* the consumer-defined authorship-gate pass, BOTH directions — an adapter-written
  utterance stating a sweep GRANTS ``append-decision``'s ``task_generator`` at the
  full-strength tier, and a value the utterances never stated is REFUSED (the
  guard-can-fire leg that makes this a TCK, not a lint).

Every module-level ``check_*`` is a pure ``(adapter, repo) -> None`` assertion
reused by the mirror unit test (``tests/conformance_kit/…``) so the module is
proven green against the built-in reference adapter without a pytest subprocess;
the ``test_*`` wrappers bind them to the kit's ``harness_adapter`` /
``fixture_repo`` / ``require_utterance_log`` fixtures.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

import pytest

from hpc_agent.state.utterances import (
    HARNESS_INJECTION_RE,
    MAX_UTTERANCE_BYTES,
    read_utterances,
    utterances_path,
)

if TYPE_CHECKING:
    from pathlib import Path

    from hpc_agent.conformance.adapter import HarnessAdapter


# --- helpers -----------------------------------------------------------------


def _raw_lines(repo: Path) -> list[str]:
    """The on-disk utterance-log lines (non-blank), oldest-first — never creating."""
    path = utterances_path(repo)
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def injection_tags() -> list[str]:
    """The harness-injection tag names, DERIVED from the exported filter regex.

    The fixtures are never a hard-coded tag list: a filter extension
    (a new alternative in :data:`~hpc_agent.state.utterances.HARNESS_INJECTION_RE`)
    auto-extends the provenance assertions here (the enforcement-map rule).
    """
    match = re.search(r"<\(\?:([^)]+)\)", HARNESS_INJECTION_RE.pattern)
    assert match, "HARNESS_INJECTION_RE shape changed — cannot derive injection tags"
    return [tag.strip() for tag in match.group(1).split("|") if tag.strip()]


def _supports_answer_question(adapter: HarnessAdapter) -> bool:
    return callable(getattr(adapter, "answer_question", None))


# --- pure assertion battery (reused by the mirror unit test) -----------------


def check_schema_and_append_only(adapter: HarnessAdapter, repo: Path) -> None:
    """Frozen ``{ts, sha256, text}`` schema, sorted-keys byte-shape, append-only."""
    inputs = ["first human utterance: 20 seeds", "second human utterance"]
    for text in inputs:
        adapter.write_utterance(repo, text)

    records = read_utterances(repo)
    # append-only, oldest-first — the reader returns them in write order
    assert [r["text"] for r in records] == inputs

    lines = _raw_lines(repo)
    assert len(lines) == len(inputs)
    for line, rec, source in zip(lines, records, inputs, strict=True):
        # exactly the three frozen fields — the writer adds nothing else
        assert set(rec) == {"ts", "sha256", "text"}
        # sorted-keys byte-shape: the on-disk line IS json.dumps(sort_keys=True)
        assert line == json.dumps(
            {"ts": rec["ts"], "sha256": rec["sha256"], "text": rec["text"]},
            sort_keys=True,
        )
        # sha256 digests the full raw text (uncapped here → equals the input)
        assert rec["sha256"] == hashlib.sha256(source.encode("utf-8")).hexdigest()


def check_byte_cap_full_text_sha(adapter: HarnessAdapter, repo: Path) -> None:
    """Stored text capped at the byte limit; sha256 over the FULL raw text."""
    big = "x" * (MAX_UTTERANCE_BYTES + 904)
    adapter.write_utterance(repo, big)
    rec = read_utterances(repo)[-1]
    assert len(rec["text"].encode("utf-8")) <= MAX_UTTERANCE_BYTES  # capped
    assert rec["sha256"] == hashlib.sha256(big.encode("utf-8")).hexdigest()  # FULL text
    assert big.startswith(rec["text"])  # a clean prefix of the original


def check_codepoint_truncation(adapter: HarnessAdapter, repo: Path) -> None:
    """A multi-byte codepoint straddling the cap decodes cleanly (never mid-cut)."""
    # 4095 ASCII bytes, then 3-byte euro signs: the cap at 4096 lands INSIDE the
    # first euro, so a byte-cut would leave a mangled partial codepoint.
    text = "a" * (MAX_UTTERANCE_BYTES - 1) + "€" * 40
    adapter.write_utterance(repo, text)
    stored = read_utterances(repo)[-1]["text"]
    assert len(stored.encode("utf-8")) <= MAX_UTTERANCE_BYTES
    assert "�" not in stored  # no replacement char — never a mid-codepoint cut
    assert text.startswith(stored)  # a clean codepoint-boundary prefix
    # the straddling codepoint was dropped WHOLE, exactly the reference truncation
    assert stored == text.encode("utf-8")[:MAX_UTTERANCE_BYTES].decode("utf-8", "ignore")


def check_no_scaffold(adapter: HarnessAdapter, repo: Path) -> None:
    """A write into an UNCLAIMED namespace leaves zero footprint and never raises."""
    unclaimed = repo.parent / "unclaimed-repo"
    unclaimed.mkdir(exist_ok=True)
    namespace = utterances_path(unclaimed).parent
    assert not namespace.exists()  # the reader/writer must never scaffold it

    adapter.write_utterance(unclaimed, "this must vanish — no namespace claimed")

    assert not namespace.exists()  # still no directory created
    assert read_utterances(unclaimed) == []  # clean no-op


def check_injection_filter(adapter: HarnessAdapter, repo: Path) -> None:
    """Text OPENING with an injection tag is refused; a quoted tag mid-text lands."""
    tags = injection_tags()
    assert tags, "no injection tags derived — provenance assertion cannot fire"

    for tag in tags:
        before = len(read_utterances(repo))
        adapter.write_utterance(repo, f"<{tag}> harness-injected content, not human-typed")
        assert len(read_utterances(repo)) == before, (
            f"an injected <{tag}> turn was logged as a human utterance"
        )

    # a human merely QUOTING a tag mid-sentence still lands (the anchor is ^)
    quoted = "please note the <system-reminder> tag appears mid sentence"
    adapter.write_utterance(repo, quoted)
    assert read_utterances(repo)[-1]["text"] == quoted


def check_clicked_vs_typed(adapter: HarnessAdapter, repo: Path) -> None:
    """A clicked offered label is not logged; typed free text is (``_is_clicked``)."""
    labels = ["Interpret as converged", "Run one more wave"]

    before = len(read_utterances(repo))
    adapter.answer_question(repo, labels, "Interpret as converged")  # a pure click
    assert len(read_utterances(repo)) == before, "a clicked option label was logged"

    typed = "actually, use 20 seeds at 1M samples each"
    adapter.answer_question(repo, labels, typed)  # free text the human typed
    assert typed in [r["text"] for r in read_utterances(repo)]


def check_authorship_gate_grants_from_utterances(adapter: HarnessAdapter, repo: Path) -> None:
    """The consumer pass (grant leg): an adapter-written sweep GRANTS the gate.

    The load-bearing TCK assertion — the candidate harness's records satisfy the
    REAL consumer (``ops/decision/journal.py``'s human-authorship gate over
    ``_harness_human_texts``): a bare ``y`` commits a ``task_generator`` whose
    tokens derive from a log the harness wrote.
    """
    from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
    from hpc_agent.ops.decision.journal import append_decision

    adapter.write_utterance(repo, "use 20 seeds at 1M samples each")
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "run",
            "scope_id": "k4-gate-grant",
            "block": "s1",
            "response": "y",
            "proposal": "task_generator: items_x_seeds, 20 seeds (0-19), samples=1_000_000",
            "resolved": {
                "task_generator": {"kind": "items_x_seeds", "seeds": 20, "samples": 1_000_000}
            },
        }
    )
    out = append_decision(experiment_dir=repo, spec=spec)
    assert out.record.resolved["task_generator"]["seeds"] == 20


def check_authorship_gate_refuses_fabrication(adapter: HarnessAdapter, repo: Path) -> None:
    """The consumer pass (refuse leg): a value the utterances never stated is REFUSED.

    The declared==behaved leg — the gate is only meaningful because it can also
    FIRE (engineering-principles). With the utterance log present the AGENT-
    authored ``response`` carries no authorship weight, so a fabricated quote
    cannot launder tokens the human never typed.
    """
    from hpc_agent import errors
    from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
    from hpc_agent.ops.decision.journal import append_decision

    adapter.write_utterance(repo, "hello, please check the cluster status")
    spec = AppendDecisionInput.model_validate(
        {
            "scope_kind": "run",
            "scope_id": "k4-gate-refuse",
            "block": "s1",
            # a fabricated quote stating 20 / 1M — in journal-response mode this
            # would PASS; the harness-captured log must outrank it.
            "response": "use 20 seeds at 1M samples",
            "proposal": "task_generator: items_x_seeds, 20 seeds (0-19), samples=1_000_000",
            "resolved": {
                "task_generator": {"kind": "items_x_seeds", "seeds": 20, "samples": 1_000_000}
            },
        }
    )
    with pytest.raises(errors.SpecInvalid) as excinfo:
        append_decision(experiment_dir=repo, spec=spec)
    message = str(excinfo.value)
    assert "task_generator is human-authored" in message
    assert "harness-captured" in message  # names the evidence source consulted


# --- pytest wrappers (bind the battery to the kit fixtures) ------------------


def test_record_schema_and_append_only(
    harness_adapter: HarnessAdapter, fixture_repo: Path, require_utterance_log: None
) -> None:
    check_schema_and_append_only(harness_adapter, fixture_repo)


def test_byte_cap_and_full_text_sha(
    harness_adapter: HarnessAdapter, fixture_repo: Path, require_utterance_log: None
) -> None:
    check_byte_cap_full_text_sha(harness_adapter, fixture_repo)


def test_codepoint_boundary_truncation(
    harness_adapter: HarnessAdapter, fixture_repo: Path, require_utterance_log: None
) -> None:
    check_codepoint_truncation(harness_adapter, fixture_repo)


def test_no_scaffold_into_unclaimed_namespace(
    harness_adapter: HarnessAdapter, fixture_repo: Path, require_utterance_log: None
) -> None:
    check_no_scaffold(harness_adapter, fixture_repo)


def test_harness_injection_tags_refused(
    harness_adapter: HarnessAdapter, fixture_repo: Path, require_utterance_log: None
) -> None:
    check_injection_filter(harness_adapter, fixture_repo)


def test_clicked_option_excluded_typed_text_logged(
    harness_adapter: HarnessAdapter, fixture_repo: Path, require_utterance_log: None
) -> None:
    if not _supports_answer_question(harness_adapter):
        pytest.skip(
            "adapter does not implement the optional answer_question channel "
            "(clicked-vs-typed provenance) — capability 1 core assertions unaffected"
        )
    check_clicked_vs_typed(harness_adapter, fixture_repo)


def test_authorship_gate_grants_from_utterances(
    harness_adapter: HarnessAdapter, fixture_repo: Path, require_utterance_log: None
) -> None:
    check_authorship_gate_grants_from_utterances(harness_adapter, fixture_repo)


def test_authorship_gate_refuses_fabrication(
    harness_adapter: HarnessAdapter, fixture_repo: Path, require_utterance_log: None
) -> None:
    check_authorship_gate_refuses_fabrication(harness_adapter, fixture_repo)
