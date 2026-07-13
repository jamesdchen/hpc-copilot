"""The whole-feature ACCEPTANCE test for the data trace (``docs/design/data-trace.md``).

One end-to-end contract test that drives the LANDED core substrate exactly as the
design's "Acceptance for the whole feature" paragraph specifies:

    a toy pipeline emits -> ingests -> renders all four views -> a planted
    divergence localizes via trace-diff -> the journaled trace_sha matches a
    recompute -> a rootless/knob-less run digests exactly per its sidecar context.

The pipeline is pure-stdlib synthetic stages over CSV-shaped dicts —
``load -> filter -> join -> reduce`` — with DECLARED drops and a tracked label
(``units_space``). TOY vocabulary only (widgets / readings — never quant): the
core-test enforcement pin (toy fixtures only in core tests).

Every stage-exit record is built via T1's :func:`make_record` with the
``row_count`` / ``col_set`` / ``label_chain`` / ``value_sketch`` / ``digest``
atoms; emission writes the T2 transport file (``_trace.jsonl``); T1's
:func:`ingest_trace` moves it into the store and journals ONE ``data-trace`` sha
record; T5 :func:`trace_render` renders the four views; T6 :func:`trace_diff`
localizes a planted divergence; and the T3 classifier
(:func:`classify_digests`) decides digests per the recorded-before-it-starts
context with no cluster in sight.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from pathlib import Path

from hpc_agent._wire.queries.trace_diff import TraceDiffSpec, TraceKey
from hpc_agent._wire.queries.trace_render import TraceRenderSpec
from hpc_agent.execution.mapreduce.data_trace_contract import TRACE_TRANSPORT_FILENAME
from hpc_agent.ops.trace_diff_op import trace_diff
from hpc_agent.ops.trace_render_op import trace_render
from hpc_agent.state import data_trace as dt
from hpc_agent.state.data_trace_classifier import (
    SMALL_ARRAY_DIGEST_THRESHOLD,
    DigestContext,
    classify_digests,
    digest_availability,
)
from hpc_agent.state.decision_journal import read_decisions

# --- the toy pipeline (pure stdlib; widgets / readings — never quant) ---------

_WIDGETS: list[dict] = [
    {"widget_id": 1, "name": "alpha", "qty": 5},
    {"widget_id": 2, "name": "bravo", "qty": 1},
    {"widget_id": 3, "name": "charlie", "qty": 8},
    {"widget_id": 4, "name": "delta", "qty": 2},
    {"widget_id": 5, "name": "echo", "qty": 9},
]
#: widget_id -> reading (the inner-join right side)
_READINGS: dict[int, float] = {1: 10.0, 3: 30.0, 5: 50.0}

_LABEL = "units_space"


def _digest(frame: list[dict]) -> str:
    """A content sha of a frame (pure stdlib) — the toy's ``digest`` atom."""
    payload = json.dumps(frame, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sketch(values: list[float]) -> dict:
    """A ``value_sketch`` bundle over a numeric column (fixed q05/q50/q95)."""
    vs = sorted(float(v) for v in values)
    n = len(vs)

    def q(p: float) -> float:
        return vs[min(n - 1, round(p * (n - 1)))] if n else 0.0

    return {
        "min": vs[0],
        "max": vs[-1],
        "mean": statistics.fmean(vs),
        "std": statistics.pstdev(vs) if n > 1 else 0.0,
        "quantiles": {"q05": q(0.05), "q50": q(0.5), "q95": q(0.95)},
    }


def _cols(frame: list[dict]) -> dict:
    return {"columns": sorted(frame[0].keys())} if frame else {"columns": []}


def build_pipeline_records(threshold: int, *, with_digests: bool = True) -> list[dict]:
    """Run the toy pipeline and return its per-stage trace records.

    ``load -> filter(qty >= threshold) -> join(readings) -> reduce(sum)``. The
    filter's threshold is the KNOB the planted divergence turns. ``with_digests``
    models the T3 classifier's decision: ON emits the ``digest`` atom at every
    stage, OFF emits none (the degradation surface T3 discloses).
    """
    load_frame = list(_WIDGETS)
    kept = [w for w in load_frame if w["qty"] >= threshold]
    joined = [
        {**w, "reading": _READINGS[w["widget_id"]]} for w in kept if w["widget_id"] in _READINGS
    ]
    total = sum(r["reading"] for r in joined)
    reduced = [{"total_reading": total}]

    def digest_atom(frame: list[dict]) -> dict:
        return {"digest": _digest(frame)} if with_digests else {}

    load = dt.make_record(
        "load",
        0,
        {
            "row_count": {"rows": len(load_frame), "dropped": 0},
            "col_set": _cols(load_frame),
            "value_sketch": {"qty": _sketch([w["qty"] for w in load_frame])},
            "label_chain": {_LABEL: "raw"},
            **digest_atom(load_frame),
        },
    )
    filt = dt.make_record(
        "filter",
        1,
        {
            "row_count": {"rows": len(kept), "dropped": len(load_frame) - len(kept)},
            "col_set": _cols(kept),
            "value_sketch": {"qty": _sketch([w["qty"] for w in kept])},
            "label_chain": {_LABEL: "raw"},
            **digest_atom(kept),
        },
    )
    join = dt.make_record(
        "join",
        2,
        {
            "row_count": {"rows": len(joined), "dropped": len(kept) - len(joined)},
            "col_set": _cols(joined),
            "value_sketch": {"reading": _sketch([r["reading"] for r in joined])},
            "label_chain": {_LABEL: "norm"},
            **digest_atom(joined),
        },
    )
    # A12 G-c: the reduce stage's trace is CORE-shaped counts-only (no sketch);
    # the label rides through so the chain stays continuous.
    reduce = dt.make_record(
        "reduce",
        3,
        {
            "row_count": {"rows": len(reduced), "dropped": len(joined) - len(reduced)},
            "col_set": _cols(reduced),
            "label_chain": {_LABEL: "norm"},
            **digest_atom(reduced),
        },
    )
    return [load, filt, join, reduce]


def _emit_transport(out_dir: Path, records: list[dict]) -> Path:
    """Step 1: EMIT the per-stage trace to the T2 transport file (``_trace.jsonl``)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / TRACE_TRANSPORT_FILENAME
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    return path


# --- the whole-feature acceptance flow ---------------------------------------


def test_data_trace_whole_feature_acceptance(tmp_path: Path) -> None:
    exp = tmp_path

    # (1) EMIT — a per-stage trace to the T2 transport file, records via make_record.
    records_a = build_pipeline_records(threshold=3)
    assert [r["stage"] for r in records_a] == ["load", "filter", "join", "reduce"]
    transport_a = _emit_transport(exp / "out-a", records_a)
    assert transport_a.name == TRACE_TRANSPORT_FILENAME

    # (2) INGEST — via T1's ingest_trace: journals one data-trace record, stores the file.
    summary = dt.ingest_trace(exp, "run", "run-a", 0, transport_a)
    assert not transport_a.exists()  # the in-flight packet is disposable post-ingest
    store_path = dt.trace_store_path(exp, "run", "run-a", 0)
    assert store_path.exists()

    decisions = read_decisions(exp, "run", "run-a")
    dt_records = [d for d in decisions if d["block"] == dt.DATA_TRACE_BLOCK]
    assert len(dt_records) == 1
    resolved = dt_records[0]["resolved"]
    assert resolved["scope"] == "run" and resolved["id"] == "run-a"
    assert resolved["stage_count"] == 4

    # (3) RENDER — all four views. Assert content facts, not full goldens.
    rendered = trace_render(
        experiment_dir=exp, spec=TraceRenderSpec(scope_kind="run", scope_id="run-a")
    )
    assert rendered.present is True
    assert rendered.stage_count == 4

    # (3a) waterfall carries the DECLARED drops (filter dropped 2 of 5; conserved).
    waterfall = {r.stage: r for r in rendered.waterfall}
    assert waterfall["filter"].dropped == 2
    assert (waterfall["filter"].rows_in, waterfall["filter"].rows_out) == (5, 3)
    assert waterfall["filter"].expected == 3  # 5 - 2, conservation holds
    # A conserved pipeline flags nothing.
    assert [f for f in rendered.flags if f.rule == "row_conservation"] == []

    # (3b) the label chain renders across every stage (the units ledger, unbroken).
    assert rendered.label_chains[_LABEL] == [
        "load=raw",
        "filter=raw",
        "join=norm",
        "reduce=norm",
    ]
    assert [f for f in rendered.flags if f.rule == "label_chain_break"] == []

    # (3c) lineage shows the col deltas — 'reading' is born at the join.
    lineage = {r.stage: r for r in rendered.feature_lineage}
    assert lineage["join"].added == ["reading"]
    assert rendered.feature_births["reading"] == "join"
    assert rendered.feature_births["widget_id"] == "load"

    # (3d) the sketch table renders the declared columns.
    sketch = {(r.stage, r.column): r for r in rendered.sketch}
    assert ("load", "qty") in sketch and sketch[("load", "qty")].mean is not None
    assert ("join", "reading") in sketch

    # All four view headers are present in the deterministic markdown.
    for header in ("## Row waterfall", "## Label chains", "## Feature lineage", "## Sketch"):
        assert header in rendered.render

    # (4) PLANTED DIVERGENCE — run again with a changed filter (threshold 6 drops
    #     DIFFERENT rows at the filter stage). trace-diff localizes the FIRST
    #     divergence exactly at (stage=filter, atom=row_count).
    records_b = build_pipeline_records(threshold=6)
    transport_b = _emit_transport(exp / "out-b", records_b)
    dt.ingest_trace(exp, "run", "run-b", 0, transport_b)

    diff = trace_diff(
        exp,
        spec=TraceDiffSpec(
            a=TraceKey(scope_kind="run", scope_id="run-a"),
            b=TraceKey(scope_kind="run", scope_id="run-b"),
        ),
    )
    assert diff.clean is False
    assert diff.first_divergence is not None
    fd = diff.first_divergence
    assert (fd.stage, fd.seq, fd.atom) == ("filter", 1, "row_count")
    assert "row_count" in fd.detail
    # The load stage — identical input — is byte-clean upstream of the divergence.
    load_stage = next(s for s in diff.stages if s["stage"] == "load")
    assert load_stage["divergences"] == []

    # (5) The journaled trace_sha matches a RECOMPUTE (records_sha over the store).
    stored = dt.read_trace(exp, "run", "run-a", 0)
    recomputed = dt.records_sha(stored)
    assert recomputed == summary["trace_sha"] == resolved["trace_sha"]
    assert rendered.trace_sha == recomputed

    # (6) A rootless/knob-less context digests EXACTLY per its sidecar context —
    #     pure classifier, no cluster (drive T3 over the fixtures' contexts).
    _assert_digest_classification()


def _assert_digest_classification() -> None:
    # local-gauntlet context -> digests ON (an identity question: "did my cheap-kill
    # see what I think?"). Emitting WITH digests, every stage is digested -> present.
    local = classify_digests(DigestContext(is_local=True))
    assert local.digests_on is True and "local" in local.triggers
    on_records = build_pipeline_records(threshold=3, with_digests=True)
    avail_on = digest_availability(on_records)
    assert avail_on.present is True
    assert avail_on.disclosure() is None

    # a big-array, non-canary, non-reproduction, cluster context -> digests OFF.
    # Emitting WITHOUT digests, verification DEGRADES and DISCLOSES (never fabricates).
    big = classify_digests(
        DigestContext(
            task_count=SMALL_ARRAY_DIGEST_THRESHOLD * 50,
            is_canary=False,
            reproduces=False,
            is_local=False,
        )
    )
    assert big.digests_on is False and big.triggers == ()
    off_records = build_pipeline_records(threshold=3, with_digests=False)
    avail_off = digest_availability(off_records)
    assert avail_off.present is False
    disclosure = avail_off.disclosure()
    assert disclosure is not None and "unrecorded" in disclosure

    # the OVERRIDE wins over the classifier signal (and is disclosed as exercised).
    override = classify_digests(DigestContext(is_local=True, override="force_off"))
    assert override.digests_on is False  # would be ON by 'local', force_off wins
    assert override.override_exercised is True
    assert override.override == "force_off"
