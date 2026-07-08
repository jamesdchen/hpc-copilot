"""T7 — the determinism-fingerprint ``needs_verdict`` attention kind.

New item kind ``reproduction-needs-verdict`` (class VERDICT): a ledger sample
whose RECORDED classifier verdict is ``needs_verdict`` (T1 stamped it at append)
and whose ``content_sha`` no committed ``reproduction-verdict`` decision has yet
named. Amendment 2 — verdict-on-demand: it parks as a leverage-ZERO standing
item (fan-out 0, aging by the sample ``ts``); it does NOT route on creation.

The collector re-implements no envelope math: it reads the recorded sample
verdict and joins the run journal's ``reproduction-verdict`` records.

TOY VOCABULARY ONLY: a widget-metric reproduction. Never harxhar / quant words.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.ops import attention_queue as aq
from hpc_agent.state import determinism as det
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.fingerprint_store import fingerprint_path

_CMD_SHA = "a" * 64
_ORIG_RUN = "widget-run-orig"
_REPRO_RUN = "widget-run-repro"
_IDENTITY = {"cmd_sha": _CMD_SHA, "tasks_py_sha": "t" * 64, "executor": "widget_exec.py"}
_SHA = "c" * 64
_TS = "2026-07-01T00:00:00Z"
_NOW = "2026-07-08T00:00:00Z"


def _sample(
    *,
    verdict: str,
    content_sha: str = _SHA,
    ts: str = _TS,
    scale: str = "main",
    run_ids: tuple[str, str] = (_ORIG_RUN, _REPRO_RUN),
    a: float = 1.0,
    b: float = 1.004,
) -> dict:
    """A toy widget-reproduction sample record (validated by the T1 model)."""
    per_key = det.diff_metrics({"widget_rate": a}, {"widget_rate": b})
    return det.build_sample_record(
        ts=ts,
        content_sha=content_sha,
        identity=_IDENTITY,
        source="verify-reproduction",
        run_ids=list(run_ids),
        cluster="widgetcluster",
        scale=scale,
        verdict=verdict,
        per_key=per_key,
    )


def _write_sample(exp: Path, record: dict) -> None:
    append_jsonl_line(fingerprint_path(exp, _CMD_SHA), record)


def _answer(exp: Path, *, content_sha: str = _SHA, accept: bool, run_id: str = _REPRO_RUN) -> None:
    """The human's ordinary reproduction-verdict record on the repro RUN scope."""
    append_decision(
        exp,
        scope_kind="run",
        scope_id=run_id,
        block="reproduction-verdict",
        response="y" if accept else "not this one",
        resolved={"accept": accept, "content_sha": content_sha},
    )


# ── the standing item ─────────────────────────────────────────────────────────


def test_unanswered_needs_verdict_yields_one_item(tmp_path: Path) -> None:
    _write_sample(tmp_path, _sample(verdict="needs_verdict"))

    collection = aq.collect_reproduction_verdicts(tmp_path, now=_NOW)
    assert len(collection.items) == 1
    item = collection.items[0]
    assert item.kind == aq.REPRODUCTION_NEEDS_VERDICT
    assert item.item_class == aq.VERDICT
    assert item.scope_kind == "run"
    assert item.scope_id == _REPRO_RUN  # the repro run holds the verdict record
    assert item.block == "reproduction-verdict"
    assert item.since == _TS  # ages honestly by the sample's own ts
    assert item.unblocks == 0  # Amendment 2: leverage-zero standing item


def test_fanout_is_zero_end_to_end(tmp_path: Path) -> None:
    _write_sample(tmp_path, _sample(verdict="needs_verdict"))
    items = aq.collect_items(tmp_path, now=_NOW).items
    verdicts = [i for i in items if i.kind == aq.REPRODUCTION_NEEDS_VERDICT]
    assert len(verdicts) == 1
    assert verdicts[0].unblocks == 0  # no encoded edge → falls through the class order
    # And the kind carries no fan-out dispatch entry.
    assert aq._fanout_for(verdicts[0], tmp_path) == 0


def test_evidence_is_verbatim_sample_fields(tmp_path: Path) -> None:
    record = _sample(verdict="needs_verdict")
    _write_sample(tmp_path, record)

    item = aq.collect_reproduction_verdicts(tmp_path, now=_NOW).items[0]
    ev = item.evidence
    assert ev["content_sha"] == _SHA
    assert ev["source"] == "verify-reproduction"
    assert ev["scale"] == "main"
    assert ev["cluster"] == "widgetcluster"
    assert ev["run_ids"] == [_ORIG_RUN, _REPRO_RUN]
    # The deviation is lifted verbatim from the record — never re-derived here.
    assert ev["per_key"] == record["per_key"]
    assert item.cluster == "widgetcluster"


# ── answered → no item ────────────────────────────────────────────────────────


def test_accepted_verdict_answers_the_item(tmp_path: Path) -> None:
    _write_sample(tmp_path, _sample(verdict="needs_verdict"))
    _answer(tmp_path, accept=True)
    assert aq.collect_reproduction_verdicts(tmp_path, now=_NOW).items == []


def test_rejected_verdict_answers_the_item(tmp_path: Path) -> None:
    _write_sample(tmp_path, _sample(verdict="needs_verdict"))
    _answer(tmp_path, accept=False)  # reject also closes the standing item
    assert aq.collect_reproduction_verdicts(tmp_path, now=_NOW).items == []


def test_verdict_naming_a_different_sha_does_not_answer(tmp_path: Path) -> None:
    _write_sample(tmp_path, _sample(verdict="needs_verdict"))
    _answer(tmp_path, content_sha="d" * 64, accept=True)  # names a different sample
    # Token-exact join: a verdict for another sha leaves this one standing.
    assert len(aq.collect_reproduction_verdicts(tmp_path, now=_NOW).items) == 1


# ── other verdict classes are not this kind ───────────────────────────────────


def test_auto_cleared_sample_yields_no_item(tmp_path: Path) -> None:
    _write_sample(tmp_path, _sample(verdict="auto_cleared", a=1.0, b=1.0))
    assert aq.collect_reproduction_verdicts(tmp_path, now=_NOW).items == []


def test_mismatch_sample_yields_no_item(tmp_path: Path) -> None:
    _write_sample(tmp_path, _sample(verdict="mismatch"))
    assert aq.collect_reproduction_verdicts(tmp_path, now=_NOW).items == []


# ── fail-open + skip accounting ───────────────────────────────────────────────


def test_no_ledger_yields_nothing(tmp_path: Path) -> None:
    collection = aq.collect_reproduction_verdicts(tmp_path, now=_NOW)
    assert collection.items == []
    assert collection.skipped == []


def test_torn_ledger_line_is_skip_counted_not_fatal(tmp_path: Path) -> None:
    # A good needs_verdict line plus one torn line: the item still surfaces and
    # the malformed line is DISCLOSED in skip accounting (no-silent-caps).
    _write_sample(tmp_path, _sample(verdict="needs_verdict"))
    with fingerprint_path(tmp_path, _CMD_SHA).open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")

    collection = aq.collect_reproduction_verdicts(tmp_path, now=_NOW)
    assert len(collection.items) == 1
    assert len(collection.skipped) == 1
    assert "malformed" in collection.skipped[0]["reason"]


def test_unreadable_repro_journal_surfaces_the_item(tmp_path: Path, monkeypatch) -> None:
    _write_sample(tmp_path, _sample(verdict="needs_verdict"))

    def _boom(*_a, **_k):  # the repro-journal read blows up
        raise OSError("torn journal")

    monkeypatch.setattr(
        "hpc_agent.state.decision_journal.read_decisions", _boom, raising=True
    )
    # Fail-open: an unreadable journal reads NOT-answered → the item still surfaces.
    assert len(aq.collect_reproduction_verdicts(tmp_path, now=_NOW).items) == 1


# ── route-through: the recorded verdict + the journal join, no envelope math ──


def test_route_through_no_reimplemented_envelope_math() -> None:
    src = inspect.getsource(aq.collect_reproduction_verdicts)
    # Routes the tolerant read through the ONE store symbol and the answered join.
    assert "read_samples(" in src
    assert "_needs_verdict_answered(" in src
    # The recorded sample verdict IS T1's classifier output — never re-reduced here.
    for forbidden in ("reduce_envelope", "classify(", "diff_metrics", "_reduce_key"):
        assert forbidden not in src, f"collector must not re-implement envelope math: {forbidden}"

    join_src = inspect.getsource(aq._needs_verdict_answered)
    assert "read_decisions(" in join_src  # the answered test routes through the journal
