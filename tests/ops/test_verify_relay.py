"""Direct-atom tests for the ``verify-relay`` primitive (conduct rule 10).

Seeds a tmp experiment dir with a decision journal + run sidecar (+ a
RunRecord for the state-word cases, mirroring the fixtures in
``test_decision_journal_primitives.py``), then drives the primitive with a
draft relay and asserts on the audit verdict. Covers: a clean relay passing, a
wrong number flagged with its nearest source value, a wrong state word, a wrong
run-id, conversational numbers not flagged, and the missing-sources /
unverifiable policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_agent._wire.queries.verify_relay import VerifyRelayInput, VerifyRelayResult
from hpc_agent.ops.decision.journal.verify_relay import verify_relay
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord
from hpc_agent.state.runs import write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

RUN_ID = "run-1"


def _seed_journal(tmp_path: Path, **evidence: object) -> None:
    append_decision(
        tmp_path,
        scope_kind="run",
        scope_id=RUN_ID,
        block="submit-s1",
        response="y",
        evidence_digest=dict(evidence) or {"canary": "green", "core_hours": 128},
    )


def _seed_sidecar(tmp_path: Path, *, task_count: int = 10) -> None:
    write_run_sidecar(
        tmp_path,
        run_id=RUN_ID,
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-03T00:00:00+00:00",
        executor="python3 .hpc/_hpc_dispatch.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=task_count,
        tasks_py_sha="b" * 64,
    )


def _seed_record(tmp_path: Path, *, status: str, job_ids: list[str] | None = None) -> None:
    upsert_run(
        tmp_path,
        RunRecord(
            run_id=RUN_ID,
            profile="p",
            cluster="hoffman2",
            ssh_target="u@h",
            remote_path="/remote",
            job_name="j",
            job_ids=job_ids if job_ids is not None else ["13610902"],
            total_tasks=10,
            submitted_at="2026-07-03T00:00:00+00:00",
            experiment_dir=str(tmp_path),
            status=status,
        ),
    )


def _run(tmp_path: Path, relay: str) -> VerifyRelayResult:
    return verify_relay(
        experiment_dir=tmp_path,
        spec=VerifyRelayInput(run_id=RUN_ID, relay_text=relay),
    )


# ── clean relay ────────────────────────────────────────────────────────────────


def test_clean_relay_passes(tmp_path: Path) -> None:
    _seed_journal(tmp_path, canary="green", core_hours=128)
    _seed_sidecar(tmp_path, task_count=10)
    _seed_record(tmp_path, status="failed")

    out = _run(
        tmp_path,
        "Run run-1 has failed. It consumed 128 core-hours across 10 tasks; the canary was green.",
    )
    assert out.clean is True
    assert out.mismatches == []
    assert out.claims_checked >= 3  # run-id, 128, 10, failed, canary-green
    assert "decision_journal" in out.sources_consulted
    assert "run_sidecar" in out.sources_consulted
    assert "run_record" in out.sources_consulted


# ── wrong number ───────────────────────────────────────────────────────────────


def test_wrong_number_flagged_with_nearest_source_value(tmp_path: Path) -> None:
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path, task_count=10)

    out = _run(tmp_path, "The run consumed 256 core-hours.")
    assert out.clean is False
    num = [m for m in out.mismatches if m.kind == "number"]
    assert len(num) == 1
    assert num[0].claim == "256"
    assert num[0].nearest_source_value == "128"


def test_truncated_decimal_tolerated_but_rounding_flagged(tmp_path: Path) -> None:
    _seed_journal(tmp_path, metric=3.1411)
    _seed_sidecar(tmp_path)

    # Pure truncation passes.
    ok = _run(tmp_path, "The metric is 3.14.")
    assert [m for m in ok.mismatches if m.kind == "number"] == []
    # A rounding that changes a digit is flagged.
    bad = _run(tmp_path, "The metric is 3.15.")
    assert [m for m in bad.mismatches if m.kind == "number"]


# ── wrong state ────────────────────────────────────────────────────────────────


def test_wrong_state_word_flagged(tmp_path: Path) -> None:
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="failed")

    out = _run(tmp_path, "The job is still running.")
    assert out.clean is False
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert state[0].claim.lower() == "running"
    assert state[0].nearest_source_value == "failed"


# ── wrong run-id ───────────────────────────────────────────────────────────────


def test_wrong_run_id_flagged(tmp_path: Path) -> None:
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "Results for run-2 are ready.")
    assert out.clean is False
    rid = [m for m in out.mismatches if m.kind == "run_id"]
    assert len(rid) == 1
    assert rid[0].claim == "run-2"
    assert rid[0].nearest_source_value == RUN_ID


def test_wrong_job_id_flagged(tmp_path: Path) -> None:
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="in_flight", job_ids=["13610902"])

    out = _run(tmp_path, "Scheduler job 99999999 is queued.")
    rid = [m for m in out.mismatches if m.kind == "run_id"]
    assert len(rid) == 1
    assert rid[0].claim == "99999999"


# ── path-segment false positives: a bracketed [<experiment_dir>] Windows path ───


def test_windows_path_in_relay_not_flagged_as_run_id(tmp_path: Path) -> None:
    """A doctor fleet proposal embeds the stalled run's ``[<experiment_dir>]``.
    A Windows path (``...\\experiments-2026\\run-12345``) is split by _IDENT_RE
    on its backslashes into bare id-shaped segments; each abuts a separator and
    must be defanged as a path fragment, not flagged as an unknown run-id."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path, task_count=10)

    relay = "Run run-1: driver stalled [C:\\Users\\james\\experiments-2026\\run-12345] — re-arm?"
    out = _run(tmp_path, relay)
    # The two path segments (experiments-2026, run-12345) must not surface.
    assert [m for m in out.mismatches if m.kind == "run_id"] == []
    assert out.clean is True
    claims = {m.claim for m in out.mismatches}
    assert "experiments-2026" not in claims
    assert "run-12345" not in claims


def test_prose_run_id_still_flags_beside_bracketed_path(tmp_path: Path) -> None:
    """No loss of detection: the path defang must not blanket-exempt run-ids.
    A genuinely wrong run-id named in PROSE (no abutting separator) beside a
    clean bracketed path still flags, and ONLY it — both directions pinned."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path, task_count=10)

    relay = (
        "Run run-1 stalled [C:\\Users\\james\\experiments-2026\\run-12345]; "
        "the prior attempt run-88888 diverged."
    )
    out = _run(tmp_path, relay)
    assert [m.claim for m in out.mismatches if m.kind == "run_id"] == ["run-88888"]


# ── conversational numbers ──────────────────────────────────────────────────────


def test_conversational_numbers_not_flagged(tmp_path: Path) -> None:
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path, task_count=10)

    relay = (
        "The plan has three steps:\n"
        "1. Stage the code.\n"
        "2. Submit the canary.\n"
        "3. Watch the array.\n"
        "Check back in ~2 minutes."
    )
    out = _run(tmp_path, relay)
    # No numeric/unverifiable mismatch from the list markers or the ~2.
    assert [m for m in out.mismatches if m.kind in ("number", "unverifiable")] == []


# ── missing sources / unverifiable policy ───────────────────────────────────────


def test_missing_sources_conversational_only_is_clean(tmp_path: Path) -> None:
    # No journal, no sidecar, no record — nothing to contradict.
    out = _run(tmp_path, "The run is being set up. Check back in ~2 minutes.")
    assert out.clean is True
    assert out.claims_checked == 0
    assert out.sources_consulted == []


def test_number_with_no_source_is_unverifiable(tmp_path: Path) -> None:
    # No sources at all, but the relay asserts a factual number.
    out = _run(tmp_path, "The run consumed 512 core-hours.")
    assert out.clean is False
    unv = [m for m in out.mismatches if m.kind == "unverifiable"]
    assert len(unv) == 1
    assert unv[0].claim == "512"
    assert unv[0].nearest_source_value is None
    assert out.sources_consulted == []


def test_scope_run_id_mention_passes(tmp_path: Path) -> None:
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    out = _run(tmp_path, "Run run-1 is in flight.")
    assert [m for m in out.mismatches if m.kind == "run_id"] == []


# ── proving run #3 false-positive class 1: verb names are not run-ids ──────────


def test_block_verb_names_not_flagged_as_run_ids(tmp_path: Path) -> None:
    """Proving run #3 FP: 'Next: submit-s3 ...' flagged submit-s3/s4 as run-ids."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "Next: submit-s3 to harvest results, then submit-s4.")
    assert out.clean is True
    assert [m for m in out.mismatches if m.kind == "run_id"] == []
    # The digits inside the verb names must not leak into number auditing.
    assert [m for m in out.mismatches if m.kind in ("number", "unverifiable")] == []


def test_verb_shaped_token_outside_registry_still_flagged(tmp_path: Path) -> None:
    """Counter: the exclusion is the registry vocabulary, not the shape."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "Next: submit-s9 to harvest results.")
    rid = [m for m in out.mismatches if m.kind == "run_id"]
    assert len(rid) == 1
    assert rid[0].claim == "submit-s9"


# ── proving run #3 false-positive class 2: decimals / verified counts ──────────


def test_decimal_fraction_and_verified_count_not_flagged_as_job_ids(tmp_path: Path) -> None:
    """Proving run #3 FP: '141338909090909' (fractional digits) and '1000000'
    (samples count present in the records) flagged as job-id-shaped tokens."""
    _seed_journal(tmp_path, pi_estimate=3.141338909090909, samples=1000000)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete", job_ids=["13610902"])

    out = _run(
        tmp_path,
        "Run run-1 complete: pi_estimate 3.141338909090909 from 1000000 samples (job 13610902).",
    )
    assert out.clean is True
    assert out.mismatches == []


def test_wrong_job_id_and_misrounded_decimal_still_flagged(tmp_path: Path) -> None:
    """Counter: an unknown job id and a rounding that changes a digit still fire."""
    _seed_journal(tmp_path, pi_estimate=3.141338909090909, samples=1000000)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete", job_ids=["13610902"])

    out = _run(tmp_path, "Job 99999999 produced pi_estimate 3.151338909090909.")
    assert out.clean is False
    rid = [m for m in out.mismatches if m.kind == "run_id"]
    assert len(rid) == 1
    assert rid[0].claim == "99999999"
    num = [m for m in out.mismatches if m.kind == "number"]
    assert len(num) == 1
    assert num[0].claim == "3.151338909090909"


# ── proving run #3 false-positive class 3: quantified state words are counts ───


def test_zero_failed_count_phrasing_not_flagged_as_state(tmp_path: Path) -> None:
    """Proving run #3 FP: '0 failed' / 'no failed waves' tripped the state
    matcher as claiming state 'failed' against recorded 'complete'."""
    _seed_journal(tmp_path, failed=0, complete_waves=4)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete")

    out = _run(tmp_path, "Campaign complete: 0 failed, no failed waves.")
    assert out.clean is True
    assert [m for m in out.mismatches if m.kind == "state"] == []


def test_zero_word_count_contradicting_recorded_failures_flagged(tmp_path: Path) -> None:
    """Counter: 'no failed' over a record that counted failures still fires,
    as the number claim it actually is."""
    _seed_journal(tmp_path, failed=2)
    _seed_record(tmp_path, status="failed")

    out = _run(tmp_path, "All good: no failed waves.")
    assert out.clean is False
    num = [m for m in out.mismatches if m.kind == "number"]
    assert len(num) == 1
    assert num[0].claim == "no failed"


def test_bare_state_word_after_count_phrase_still_flagged(tmp_path: Path) -> None:
    """Counter: the count phrasing does not whitelist a later bare state claim."""
    _seed_journal(tmp_path, failed=0)
    _seed_record(tmp_path, status="complete")

    out = _run(tmp_path, "0 failed earlier, but then the harvest failed.")
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert state[0].claim.lower() == "failed"
    assert state[0].nearest_source_value == "complete"


# ── F-Q: code-written reduce artifacts widen the number corpus ─────────────────


def _seed_metrics_aggregate(tmp_path: Path, aggregated: dict[str, object]) -> None:
    """Write ``_aggregated/<run_id>/metrics_aggregate.json`` (the reducer artifact)."""
    import json

    agg_dir = tmp_path / "_aggregated" / RUN_ID
    agg_dir.mkdir(parents=True, exist_ok=True)
    (agg_dir / "metrics_aggregate.json").write_text(
        json.dumps({"aggregated_metrics": aggregated, "provenance": {"source": "combiner"}}),
        encoding="utf-8",
    )


def _seed_wave_partial(tmp_path: Path, wave: int, grid_points: object) -> None:
    """Write ``_aggregated/<run_id>/_combiner/wave_<N>.json`` (a combiner partial)."""
    import json

    comb = tmp_path / "_aggregated" / RUN_ID / "_combiner"
    comb.mkdir(parents=True, exist_ok=True)
    (comb / f"wave_{wave}.json").write_text(json.dumps(grid_points), encoding="utf-8")


def test_reducer_decimal_from_metrics_aggregate_is_clean(tmp_path: Path) -> None:
    """F-Q regression: a code-drafted completion brief relaying reducer decimals
    (present ONLY in metrics_aggregate.json) verifies CLEAN — including the
    integer part of each decimal, which used to trip the job-id check."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete", job_ids=["13610902"])
    _seed_metrics_aggregate(
        tmp_path,
        {
            "qlike_sum": 29133.060892393198,
            "qlike_count": 218894.5,
            "n_samples": 437839,
            "mse_sum": 13454.6,
            "mae_sum": 40638.6,
        },
    )

    out = _run(
        tmp_path,
        "Run run-1 complete (job 13610902): qlike_sum 29133.060892393198, "
        "qlike_count 218894.5, n_samples 437839, mse_sum 13454.6, mae_sum 40638.6.",
    )
    assert out.clean is True, out.mismatches
    assert out.mismatches == []
    assert "reduce_artifacts" in out.sources_consulted


def test_grid_point_from_wave_partial_is_clean(tmp_path: Path) -> None:
    """F-Q: a combiner wave partial's grid numbers are in-corpus too."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_wave_partial(tmp_path, 1, {"grid": [0.375, 91234.5], "n": 512})

    out = _run(tmp_path, "The best grid point scored 91234.5 over 512 rows.")
    assert out.clean is True, out.mismatches


def test_number_absent_from_reduce_artifacts_still_flagged(tmp_path: Path) -> None:
    """F-Q counter: widening the corpus does not lower the bar — a number in NO
    artifact still fires."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_metrics_aggregate(tmp_path, {"qlike_sum": 29133.060892393198})

    out = _run(tmp_path, "The qlike_sum was 88888.5.")
    num = [m for m in out.mismatches if m.kind == "number"]
    assert len(num) == 1
    assert num[0].claim == "88888.5"


# ── run-14 defect 2: display rounding + em-dash sign tolerance ──────────────────


def test_display_rounding_of_source_value_is_clean(tmp_path: Path) -> None:
    """A standard round-half render of a source value passes — not only a prefix
    truncation. ``-15.4283`` relayed as ``15.43`` (2dp) reconciles."""
    _seed_journal(tmp_path, dm_stat=-15.4283)
    _seed_sidecar(tmp_path)

    # both the truncation (15.428) and the ROUND (15.43) render pass now.
    assert [m for m in _run(tmp_path, "DM stat 15.428.").mismatches if m.kind == "number"] == []
    assert [m for m in _run(tmp_path, "DM stat 15.43.").mismatches if m.kind == "number"] == []
    # a WRONG rounding (a digit the source never rounds to) still flags.
    assert [m for m in _run(tmp_path, "DM stat 15.45.").mismatches if m.kind == "number"]


def test_em_dash_minus_sign_carried_separately_is_clean(tmp_path: Path) -> None:
    """An unsigned claim faces a negative source when the display minus is a
    non-ASCII glyph the grammar drops (em-dash). Sign-insensitive for the UNSIGNED
    claim only — an explicit ``-`` claim stays sign-sensitive (see the sign-flip
    test)."""
    _seed_journal(tmp_path, dm_stat=-15.4283)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "The statistic was ‒15.43 (an em-dash minus).")
    assert [m for m in out.mismatches if m.kind == "number"] == [], out.mismatches


def test_corpus_loader_is_the_single_definition(tmp_path: Path) -> None:
    """Route-through pin (defect 1): the verb AND the Stop hook both build the run
    number corpus from ``collect_run_number_pool`` / ``_load_run_sources`` — a fork
    that rebuilds it elsewhere turns this red."""
    import inspect

    from hpc_agent._kernel.hooks.relay_audit_stop import _contradiction
    from hpc_agent.ops.decision.journal import verify_relay as vr

    # the verb's number pool and the shared collector both route through the one
    # loader + pooler.
    assert "_load_run_sources" in inspect.getsource(vr.collect_run_number_pool)
    assert "_pool_run_numbers" in inspect.getsource(vr.collect_run_number_pool)
    assert "_pool_run_numbers" in inspect.getsource(vr.verify_relay)
    assert "_load_run_sources" in inspect.getsource(vr.verify_relay)
    # the hook consults the verb's collector — never its own corpus.
    assert "collect_run_number_pool" in inspect.getsource(_contradiction._union_number_pool)

    # and it actually works: the pool for a run carries its reduce-artifact numbers.
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_metrics_aggregate(tmp_path, {"qlike_sum": 29133.060892393198})
    strings, floats = vr.collect_run_number_pool(tmp_path, RUN_ID)
    assert "29133.060892393198" in strings
    assert 128.0 in floats


# ── run-12 finding 29: CSV reduce tables + scientific notation ─────────────────


def _seed_csv_table(tmp_path: Path, name: str, text: str) -> None:
    """Write ``_aggregated/<run_id>/<name>`` (a pack reducer's persisted table)."""
    agg_dir = tmp_path / "_aggregated" / RUN_ID
    agg_dir.mkdir(parents=True, exist_ok=True)
    (agg_dir / name).write_text(text, encoding="utf-8")


def test_csv_reduce_table_relay_is_clean(tmp_path: Path) -> None:
    """Finding-29 regression: relaying a registered aggregate_cmd's persisted CSV
    table verifies CLEAN — the scientific-notation cell is ONE number claim (it
    used to read run-id-shaped: hyphen+digit, len>=8) and the 6-digit bar count
    a numeric claim (it used to read job-id-shaped)."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete", job_ids=["13610902"])
    _seed_csv_table(
        tmp_path,
        "metrics_table.csv",
        "estimator,bucket,qlike,mse,dm,n\n"
        "ridge,all_features,0.127879,4.585623e-11,-19.925446,218934\n"
        "ridge,baseline,0.134450,4.301069e-11,5.036257,218934\n",
    )

    out = _run(
        tmp_path,
        "Run run-1 complete (job 13610902): ridge all_features qlike 0.127879 "
        "(mse 4.585623e-11, dm -19.925446) over 218934 bars.",
    )
    assert out.clean is True, out.mismatches
    assert out.mismatches == []


def test_sci_notation_flags_as_number_never_run_id(tmp_path: Path) -> None:
    """Counter + shape isolation: a sci-notation value in NO artifact still
    flags — but as a NUMBER mismatch, never the finding-29 run-id
    misclassification."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_record(tmp_path, status="complete")

    out = _run(tmp_path, "The mse was 4.585623e-11.")
    hits = [m for m in out.mismatches if m.claim.startswith("4.585623")]
    assert hits, "an unsupported sci-notation claim must still be audited"
    assert all(m.kind != "run_id" for m in hits)


def test_csv_ingest_is_bounded_and_top_level_only(tmp_path: Path, monkeypatch) -> None:
    """Bounds: an oversized CSV and one nested under the pulled per-task mirror
    contribute nothing — the corpus carries the reducer's OUTPUT only."""
    import hpc_agent.ops.decision.journal.verify_relay as vr

    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    monkeypatch.setattr(vr, "_CSV_ARTIFACT_MAX_BYTES", 32)
    _seed_csv_table(tmp_path, "metrics_table.csv", "qlike\n" + "0.127879\n" * 16)  # > 32 bytes
    nested = tmp_path / "_aggregated" / RUN_ID / "_per_task_results" / "task_0"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "metrics_table.csv").write_text("qlike\n0.555555\n", encoding="utf-8")

    out = _run(tmp_path, "qlike 0.127879 and per-task qlike 0.555555.")
    flagged = {m.claim for m in out.mismatches}
    assert "0.127879" in flagged  # oversized table skipped
    assert "0.555555" in flagged  # nested mirror never walked


# ── finding 29: the positive numeric-literal grammar (no per-format carve-out) ─
#
# A numeric literal of ANY format the grammar recognizes is audited as a NUMBER,
# never misclassified as a run-id/job-id — even with recorded job_ids making the
# job-id arm live. This is the class the accreting carve-outs (ISO dates, verbs,
# decimal fraction/integer parts, scientific notation) were each patching one at
# a time; the parametrization proves the grammar covers the whole vocabulary.

_STRUCTURED_LITERALS = [
    "4.585623e-11",  # scientific notation, negative exponent
    "4.585623e+11",  # scientific notation, positive exponent
    "1.5e10",  # scientific notation, unsigned exponent
    "2E5",  # scientific notation, capital E
    "-19.925446",  # signed decimal
    "3.14159",  # plain decimal
    "95.5%",  # decimal percentage
    "45%",  # integer percentage
    "1,234,567",  # comma-grouped integer
    "-3.5",  # signed short decimal
]


@pytest.mark.parametrize("literal", _STRUCTURED_LITERALS)
def test_numeric_literal_of_any_format_never_classifies_as_id(tmp_path: Path, literal: str) -> None:
    """Finding 29: a numeric literal NEVER flags as run_id/job_id, whatever its
    format — the job-id arm is live (recorded job_ids) yet the grammar consumes
    the whole literal first. Unsupported here, so it flags as a NUMBER."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete", job_ids=["13610902"])

    out = _run(tmp_path, f"The metric was {literal} this run.")
    assert [m for m in out.mismatches if m.kind == "run_id"] == []
    # It was AUDITED as a number (not silently dropped) — the finding-29 corpus
    # gap is separate; here the value is genuinely absent, so it flags.
    assert any(m.kind in ("number", "unverifiable") for m in out.mismatches), out.mismatches


@pytest.mark.parametrize("literal", ["4.585623e-11", "-19.925446", "95.5%", "1,234,567"])
def test_supported_numeric_literal_of_any_format_is_clean(tmp_path: Path, literal: str) -> None:
    """The passes-side: when the same literal IS in the source corpus, a verbatim
    relay of any format verifies CLEAN and never trips the id classifier."""
    _seed_journal(
        tmp_path,
        core_hours=128,
        m_sci="4.585623e-11",
        m_neg=-19.925446,
        m_pct="95.5%",
        m_comma="1,234,567",
    )
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete", job_ids=["13610902"])

    out = _run(tmp_path, f"The metric was {literal}.")
    assert [m for m in out.mismatches if m.kind == "run_id"] == []
    assert out.clean is True, out.mismatches


def test_grammar_boundaries_still_classify_as_before(tmp_path: Path) -> None:
    """The grammar's edges: tokens that are NOT whole numeric literals still
    classify exactly as before — a run-id, a timestamp-shaped id, an ISO date
    quote (neither), and a trailing-``e`` sci-notation fragment (its numeric
    prefix is a number, the dangling ``e`` is not part of the literal)."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete", job_ids=["13610902"])

    # run-<n>: a run-id claim (run- prefix, not the scope run-1).
    out = _run(tmp_path, "Results for run-2 are ready.")
    assert [m.claim for m in out.mismatches if m.kind == "run_id"] == ["run-2"]

    # timestamp-shaped id (\\d{8}-\\d{6}…): still a run-id claim, NOT a number.
    out = _run(tmp_path, "See run 20260703-141500-ab.")
    assert [m.claim for m in out.mismatches if m.kind == "run_id"] == ["20260703-141500-ab"]

    # ISO date quote: neither id nor number (consumed up front).
    out = _run(tmp_path, "Checked on 2026-07-05.")
    assert out.mismatches == []

    # '4.585623e' trailing-e junk: 'e' with no exponent digits is NOT part of a
    # numeric literal, so the maximal literal is '4.585623' (a number claim) and
    # nothing reads as a run-id.
    out = _run(tmp_path, "The raw cell 4.585623e looked malformed.")
    assert [m for m in out.mismatches if m.kind == "run_id"] == []
    assert any(m.claim == "4.585623" and m.kind == "number" for m in out.mismatches), out.mismatches


# ── F-Q: campaign-scope briefs (numbers only) ──────────────────────────────────


def _seed_sidecar_with_campaign(tmp_path: Path, campaign_id: str) -> None:
    write_run_sidecar(
        tmp_path,
        run_id=RUN_ID,
        cmd_sha="a" * 64,
        hpc_agent_version="0.0.0",
        submitted_at="2026-07-03T00:00:00+00:00",
        executor="python3 .hpc/_hpc_dispatch.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=10,
        tasks_py_sha="b" * 64,
        campaign_id=campaign_id,
    )


def test_campaign_complete_numbers_are_clean(tmp_path: Path) -> None:
    """F-Q regression: the campaign-complete brief's own numbers verify CLEAN
    when the run's sidecar carries the campaign_id."""
    cid = "run10-proving"
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar_with_campaign(tmp_path, cid)
    append_decision(
        tmp_path,
        scope_kind="campaign",
        scope_id=cid,
        block="campaign-complete",
        response="y",
        evidence_digest={"best_score": 12345.75, "iterations": 42},
    )

    out = _run(tmp_path, "Campaign run10-proving done: best_score 12345.75 across 42 iterations.")
    assert out.clean is True, out.mismatches
    assert "campaign_briefs" in out.sources_consulted


def test_campaign_state_words_not_fed_to_run_state_check(tmp_path: Path) -> None:
    """F-Q: a campaign brief's lifecycle words must NOT be checked against the
    run's recorded status — only its numbers widen the corpus. The run is
    'running'; a campaign 'complete' in the corpus does not make the run's own
    'running' relay contradict anything."""
    cid = "camp-x"
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar_with_campaign(tmp_path, cid)
    _seed_record(tmp_path, status="in_flight")
    append_decision(
        tmp_path,
        scope_kind="campaign",
        scope_id=cid,
        block="campaign-complete",
        response="the campaign is complete and finished",
        evidence_digest={"n": 7},
    )

    out = _run(tmp_path, "Run run-1 is still running.")
    # 'running' matches the recorded in_flight family — no state mismatch, and
    # the campaign's 'complete'/'finished' never entered the run-state check.
    assert [m for m in out.mismatches if m.kind == "state"] == []


# ── F-Q: canary-adjacent state words are not misattributed ─────────────────────


def test_canary_failed_not_flagged_against_main_run_state(tmp_path: Path) -> None:
    """F-Q regression: 'canary failed' is a claim about the canary sibling, not
    the main run — it must not flag against the main run's recorded 'abandoned'."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="abandoned")

    out = _run(tmp_path, "Run run-1: the canary failed, so the run was abandoned.")
    assert [m for m in out.mismatches if m.kind == "state"] == []


def test_non_canary_state_word_still_flagged_alongside_canary(tmp_path: Path) -> None:
    """F-Q counter: only the canary-adjacent word is skipped; a later bare
    lifecycle word contradicting the record still fires."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete")

    out = _run(tmp_path, "The canary failed early. Regardless, the main array is still running.")
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert state[0].claim.lower() == "running"
    assert state[0].nearest_source_value == "complete"


# ── F-R: spelled-out number-word laundering ────────────────────────────────────


def test_number_word_laundering_fires(tmp_path: Path) -> None:
    """F-R regression: the demonstrated live evasion — restating a rejected count
    ('10') as the word 'nineteen' (19) — is caught as a number mismatch."""
    _seed_journal(tmp_path, touch_count=10)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "Touch count so far, in words: nineteen.")
    assert out.clean is False
    num = [m for m in out.mismatches if m.kind == "number"]
    assert len(num) == 1
    assert num[0].claim.lower() == "nineteen"
    assert num[0].nearest_source_value == "10"


def test_number_word_matching_source_passes(tmp_path: Path) -> None:
    """F-R passes-side: a spelled count that MATCHES a source number is clean."""
    _seed_journal(tmp_path, touch_count=19)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "Touch count so far, in words: nineteen.")
    assert [m for m in out.mismatches if m.kind in ("number", "unverifiable")] == []


def test_hyphenated_compound_number_word_fires(tmp_path: Path) -> None:
    """F-R: hyphenated compounds ('twenty-one' = 21) are parsed and audited."""
    _seed_journal(tmp_path, count=10)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "The count reached twenty-one.")
    num = [m for m in out.mismatches if m.kind == "number"]
    assert len(num) == 1
    assert num[0].claim.lower() == "twenty-one"


def test_small_number_words_are_ordinary_prose(tmp_path: Path) -> None:
    """F-R conservative guard: cardinals one..twelve are ordinary prose and are
    NEVER audited (the false-positive flood the threshold prevents)."""
    _seed_journal(tmp_path, touch_count=10)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete")

    relay = (
        "One of the two steps is done; there are three checks left and a dozen "
        "tasks. Twelve of them completed. Check back in ~five minutes."
    )
    out = _run(tmp_path, relay)
    assert [m for m in out.mismatches if m.kind in ("number", "unverifiable")] == []
    # And no spelled small-number was audited as a claim.
    assert all(
        m.claim.lower() not in {"one", "two", "three", "twelve", "five"} for m in out.mismatches
    )


def test_number_word_no_source_is_unverifiable(tmp_path: Path) -> None:
    """F-R: a spelled count with no comparable source number is unverifiable
    (flagged, never a silent pass) — same policy as a digit claim."""
    out = _run(tmp_path, "The run touched files ninety-nine times.")
    unv = [m for m in out.mismatches if m.kind == "unverifiable"]
    assert len(unv) == 1
    assert unv[0].claim.lower() == "ninety-nine"


def test_number_word_value_is_public_for_the_hook() -> None:
    """``number_word_value`` is a PUBLIC surface: the relay-audit Stop hook
    reconstructs a spelled-count claim's value to check it against the cross-run
    union number pool (run-14 hook/verb parity, extended to the F-R word path).
    Pins the exact surface shapes the verb emits as a claim, and the None sink."""
    from hpc_agent.ops.decision.journal.verify_relay import number_word_value

    assert number_word_value("nineteen") == 19
    assert number_word_value("NINETEEN") == 19  # case-insensitive (verb surface)
    assert number_word_value("forty") == 40
    assert number_word_value("twenty-one") == 21  # hyphenated compound
    assert number_word_value("thousand") == 1000  # scale word
    assert number_word_value("19") is None  # a digit literal is not a word
    assert number_word_value("nope") is None  # non-cardinal → None


# ── bug-sweep #12: value-semantic verification evidence (not a JSON key) ───────


def _seed_brief(tmp_path: Path, **fields: object) -> None:
    """Append one JSON record to ``<exp>/.hpc/runs/<run_id>.briefs.jsonl``.

    Mirrors the persisted S2 brief (``ops/submit_blocks.append_brief``); the
    canary-outcome brief carries ``"verified": <bool>`` regardless of outcome.
    """
    import json

    runs_dir = tmp_path / ".hpc" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    with (runs_dir / f"{RUN_ID}.briefs.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(fields)) + "\n")


def test_verified_claim_after_failed_canary_is_flagged(tmp_path: Path) -> None:
    """Bug-sweep #12: a persisted S2 brief with ``verified: false`` must NOT
    vouch for a 'verified' relay — the JSON KEY 'verified' in the serialized
    brief used to satisfy the old substring check regardless of the value."""
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, verified=False, failure_kind="canary_failed")

    out = _run(tmp_path, "run-1 is verified and ready to submit.")
    assert out.clean is False
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert state[0].claim.lower() == "verified"


def test_canary_green_claim_after_failed_canary_is_flagged(tmp_path: Path) -> None:
    """Bug-sweep #12: same, for the 'canary green' verification phrase."""
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, verified=False, failure_kind="canary_failed")

    out = _run(tmp_path, "The canary green result confirms run-1.")
    assert out.clean is False
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert "green" in state[0].claim.lower()


def test_verified_true_brief_evidences_the_claim(tmp_path: Path) -> None:
    """Bug-sweep #12 counter: ``verified: true`` (a KEY mapping to boolean True)
    DOES evidence a 'verified' relay — the value-semantic check still passes."""
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, verified=True)

    out = _run(tmp_path, "run-1 is verified.")
    assert [m for m in out.mismatches if m.kind == "state"] == []


def test_canary_green_evidenced_by_string_value_passes(tmp_path: Path) -> None:
    """Bug-sweep #12 counter: a string VALUE 'green' (``evidence_digest={"canary":
    "green"}``) evidences the 'canary green' phrase even alongside a failed
    brief's ``verified: false``."""
    _seed_journal(tmp_path, canary="green")
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, verified=False)

    out = _run(tmp_path, "The canary green check is in for run-1.")
    assert [m for m in out.mismatches if m.kind == "state"] == []


# ── run-13 latent: path-VALUED fields don't evidence a verification claim ──────


def test_path_valued_key_does_not_evidence_verified_claim(tmp_path: Path) -> None:
    """Run-13 latent false-NEGATIVE: a path VALUE under a path-announcing KEY
    (``render_path`` = ``results/verified/green_run.json``) carries 'verified' /
    'green' only as an incidental substring — it must NOT vouch for a fabricated
    verification claim. The value-semantic collector skips path-valued keys."""
    _seed_journal(tmp_path, render_path="results/verified/green_run.json")
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, verified=False, failure_kind="canary_failed")

    out = _run(tmp_path, "run-1 is verified and canary green.")
    assert out.clean is False
    state_claims = {m.claim.lower() for m in out.mismatches if m.kind == "state"}
    assert "verified" in state_claims  # path did not rescue the fabricated claim
    assert any("green" in c for c in state_claims)


def test_path_shaped_token_under_plain_key_does_not_evidence(tmp_path: Path) -> None:
    """The path-SHAPED token guard: a path value under a key that does NOT
    announce a path (``note``) is still dropped token-by-token, so an incidental
    ``.../verified/...`` path fragment never evidences the claim — while a genuine
    verdict word in the same free-text value would survive."""
    _seed_journal(tmp_path, note="artifact at results/verified/green_run.json")
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, verified=False)

    out = _run(tmp_path, "run-1 is verified.")
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert state[0].claim.lower() == "verified"


def test_plain_string_value_verdict_still_evidences(tmp_path: Path) -> None:
    """Counter (no NEW false positive): a value-semantic verdict word under a plain
    key (``canary_status`` = ``verified``) still evidences a 'verified' relay — the
    path guards touch only path-keyed or path-shaped values, not a bare word."""
    _seed_journal(tmp_path, canary_status="verified")
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, verified=False)

    out = _run(tmp_path, "run-1 is verified.")
    assert [m for m in out.mismatches if m.kind == "state"] == []


# ── residuals: substring / trailing-punct / embedded-label verification laundering ─
# (test names deliberately avoid the 'verified'/'green' substrings — the tmp_path,
# derived from the test name, is scanned for verification evidence; see
# test_canary_adjacent_verification_quote_skipped's note.)


def test_negated_status_value_does_not_vouch_positive_claim(tmp_path: Path) -> None:
    """Residual 1 (substring laundering, value side): a source string VALUE that is
    a NEGATED verdict ('unverified') must NOT evidence its positive-stem claim —
    'verified' is a substring of 'unverified' but the negated word is the OPPOSITE
    verdict. RED before the fix (the ``needle in tl`` substring test passed it)."""
    _seed_journal(tmp_path, canary_status="un" + "verified")
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, verified=False, failure_kind="canary_failed")

    out = _run(tmp_path, "run-1 is " + "verified.")
    assert out.clean is False
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert state[0].claim.lower() == "verified"


def test_negated_status_true_key_does_not_vouch_positive_claim(tmp_path: Path) -> None:
    """Residual 1 (substring laundering, key side): a boolean-True schema KEY that
    is a NEGATED verdict ('unverified': true) must NOT evidence 'verified' — the
    key-side substring test ``needle in kl`` passed it before the fix."""
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, **{"un" + "verified": True})

    out = _run(tmp_path, "run-1 is " + "verified.")
    assert out.clean is False
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert state[0].claim.lower() == "verified"


def test_trailing_punct_path_value_stays_excluded(tmp_path: Path) -> None:
    """Residual 2 (trailing-punct filenames): a path/filename VALUE token followed
    by sentence punctuation ('..._run.json.') must still be recognised as a path
    and excluded — the trailing '.' broke ``_PATH_SHAPED_TOKEN_RE``'s extension
    anchor and re-opened the b8148f86 path-evidence hole. RED before the fix."""
    _seed_journal(tmp_path, note="see output " + "green" + "_run.json.")
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, verified=False)

    out = _run(tmp_path, "The canary " + "green" + " check is in for run-1.")
    assert out.clean is False
    state = [m for m in out.mismatches if m.kind == "state"]
    assert any("green" in m.claim.lower() for m in state)


def test_embedded_label_value_does_not_vouch_positive_claim(tmp_path: Path) -> None:
    """Residual 3 (value-scan false negative): a plain NON-path label VALUE
    ('model-verified-v2') carries the verdict word only as an incidental substring
    and must NOT vouch for a fabricated claim (no path separator / extension, so the
    b8148f86 path-shaped guard never touched it). RED before the fix."""
    _seed_journal(tmp_path, run_label="model-" + "verified" + "-v2")
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, verified=False, failure_kind="canary_failed")

    out = _run(tmp_path, "run-1 is " + "verified.")
    assert out.clean is False
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert state[0].claim.lower() == "verified"


# ── honest-case regressions: the fixes must not START flagging truthful relays ──


def test_bare_status_value_with_trailing_punct_still_vouches(tmp_path: Path) -> None:
    """Honest case (residual 2 must not over-strip): a GENUINE bare verdict value
    with trailing sentence punctuation ('verified.') still evidences the claim — the
    punct strip that closes the filename hole also normalises a real verdict word."""
    _seed_journal(tmp_path, canary_status="verified" + ".")
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, verified=False)

    out = _run(tmp_path, "run-1 is " + "verified.")
    assert [m for m in out.mismatches if m.kind == "state"] == []


def test_positive_compound_status_key_still_vouches(tmp_path: Path) -> None:
    """Honest case (residual 1 key fix must not over-reject): a POSITIVE compound
    schema key ('canary_verified': true) still evidences 'verified' — whole-segment
    matching accepts the positive stem as a segment and rejects only the negated
    word ('unverified'), never a legitimate compound field."""
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, **{"canary_" + "verified": True})

    out = _run(tmp_path, "run-1 is " + "verified.")
    assert [m for m in out.mismatches if m.kind == "state"] == []


# ── bug-sweep #39: negative source metrics relayed verbatim ────────────────────


def test_negative_float_metric_relayed_verbatim_is_clean(tmp_path: Path) -> None:
    """Bug-sweep #39: a negative float scalar in the record (log-likelihood,
    delta, loss) relayed byte-for-byte must audit clean — the sign was dropped,
    so '-1234.5' was extracted as '1234.5' and flagged as a contradiction."""
    _seed_journal(tmp_path, mean_log_likelihood=-1234.5)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "mean_log_likelihood -1234.5 over the run.")
    assert out.clean is True, out.mismatches


def test_negative_after_equals_matches_source(tmp_path: Path) -> None:
    """Bug-sweep #39: 'effect = -3.1' matches the stored ``-3.1``."""
    _seed_journal(tmp_path, effect=-3.1)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "The effect = -3.1 across all tasks.")
    assert [m for m in out.mismatches if m.kind in ("number", "unverifiable")] == []


def test_negative_metric_stored_as_string_relayed_verbatim_is_clean(tmp_path: Path) -> None:
    """Bug-sweep #39: a negative stored as a STRING (``"-3.5"``) used to be
    excluded from the number pool entirely (identifier-shaped), so a verbatim
    relay was 'unverifiable'; it now enters the pool as the number it is."""
    _seed_journal(tmp_path, delta="-3.5")
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "The delta was -3.5 this pass.")
    assert [m for m in out.mismatches if m.kind in ("number", "unverifiable")] == []


def test_hyphen_in_id_or_range_not_read_as_negative(tmp_path: Path) -> None:
    """Bug-sweep #39 guard: the sign-capturing regex must not steal a hyphen from
    an identifier ('foo-123') or a range ('waves 1-2') — neither becomes a
    negative number claim."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "Artifact foo-123 spans waves 1-2.")
    claims = {m.claim for m in out.mismatches}
    assert "-123" not in claims
    assert "-2" not in claims


def test_sign_flip_still_flagged(tmp_path: Path) -> None:
    """Bug-sweep #39 counter: a REAL contradiction (source +0.42 relayed as
    -0.42) still fires as a number mismatch — the fix widens truth, not lies."""
    _seed_journal(tmp_path, effect=0.42)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "The effect was -0.42 overall.")
    num = [m for m in out.mismatches if m.kind == "number"]
    assert len(num) == 1
    assert num[0].claim == "-0.42"


# ── notebook-audit relay (T11): status / passed / sha claims ────────────────────

_NB_AUDIT = "demo-audit"

_NB_SOURCE = """# %%
# hpc-audit-section: load-data
import pandas as pd
data = pd.read_csv("in.csv")

# %%
# hpc-audit-section: fit-model
model = fit(data)
"""


def _nb_sha(slug: str) -> str:
    from hpc_agent.state.audit_source import parse_percent_source

    return next(s.section_sha for s in parse_percent_source(_NB_SOURCE).sections if s.slug == slug)


def _nb_write_interview(tmp_path: Path, *, with_template: bool = True) -> None:
    import json

    (tmp_path / "source.py").write_text(_NB_SOURCE, encoding="utf-8")
    block: dict[str, object] = {"source": "source.py", "audit_id": _NB_AUDIT}
    if with_template:
        # Template shares both slugs → required = {load-data, fit-model}.
        (tmp_path / "template.py").write_text(_NB_SOURCE, encoding="utf-8")
        block["template"] = "template.py"
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": block}), encoding="utf-8"
    )


def _nb_sign(tmp_path: Path, slug: str, *, view_sha: str = "view-1") -> None:
    from hpc_agent.state import notebook_audit as nb

    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=_NB_AUDIT,
        block=nb.SIGN_OFF_BLOCK,
        response=f"reviewed the {slug} section",
        resolved={
            "audit_id": _NB_AUDIT,
            "section": slug,
            "section_sha": _nb_sha(slug),
            "view_sha": view_sha,
        },
    )


def _nb_run(tmp_path: Path, relay: str) -> VerifyRelayResult:
    from hpc_agent.ops.decision.journal.verify_relay import verify_notebook_relay

    return verify_notebook_relay(tmp_path, _NB_AUDIT, relay)


def test_notebook_correct_status_claim_passes(tmp_path: Path) -> None:
    _nb_write_interview(tmp_path)
    _nb_sign(tmp_path, "load-data")
    _nb_sign(tmp_path, "fit-model")

    out = _nb_run(tmp_path, "load-data is signed_current; fit-model is signed-current too.")
    assert out.clean is True
    assert out.mismatches == []
    assert out.claims_checked >= 2
    assert "notebook_journal" in out.sources_consulted
    assert "audited_source" in out.sources_consulted


def test_notebook_wrong_status_claim_flagged(tmp_path: Path) -> None:
    """The section is unsigned (no record), relayed as auto_cleared → state mismatch."""
    _nb_write_interview(tmp_path)
    # Sign nothing → load-data reduces to unsigned.

    out = _nb_run(tmp_path, "The load-data section is auto-cleared, ready to go.")
    assert out.clean is False
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert "load-data" in state[0].claim
    assert state[0].nearest_source_value == "unsigned"


def test_notebook_passed_verdict_contradiction_flagged(tmp_path: Path) -> None:
    """Only one required section signed → rollup passed is False; relay claims passed."""
    _nb_write_interview(tmp_path)
    _nb_sign(tmp_path, "load-data")  # fit-model stays unsigned → passed=False

    out = _nb_run(tmp_path, "The demo-audit graduation gate passed; ready to submit.")
    assert out.clean is False
    state = [m for m in out.mismatches if m.kind == "state"]
    assert any("passed" in m.detail and "passed=False" in m.detail for m in state)


def test_notebook_passed_verdict_correct_passes(tmp_path: Path) -> None:
    _nb_write_interview(tmp_path)
    _nb_sign(tmp_path, "load-data")
    _nb_sign(tmp_path, "fit-model")

    out = _nb_run(tmp_path, "The demo-audit gate passed — every section is signed.")
    assert [m for m in out.mismatches if m.kind == "state"] == []


def test_notebook_sha_mismatch_flagged(tmp_path: Path) -> None:
    _nb_write_interview(tmp_path)
    _nb_sign(tmp_path, "load-data")

    out = _nb_run(tmp_path, f"Section load-data was signed at {'f' * 64}.")
    assert out.clean is False
    num = [m for m in out.mismatches if m.kind == "number"]
    assert len(num) == 1
    assert num[0].claim == "f" * 64


def test_notebook_correct_sha_passes(tmp_path: Path) -> None:
    _nb_write_interview(tmp_path)
    _nb_sign(tmp_path, "load-data")

    out = _nb_run(tmp_path, f"Section load-data is at {_nb_sha('load-data')}.")
    assert [m for m in out.mismatches if m.kind == "number"] == []


def test_notebook_unresolvable_source_is_unverifiable_not_contradiction(tmp_path: Path) -> None:
    """No interview.json → the .py cannot resolve → claims are unverifiable, not
    contradictions (the hook drops them; nothing blocks)."""
    _nb_sign(tmp_path, "load-data")  # a journal record exists, but no source resolves

    out = _nb_run(tmp_path, "The load-data section is auto_cleared.")
    assert [m for m in out.mismatches if m.kind in ("state", "number")] == []
    unv = [m for m in out.mismatches if m.kind == "unverifiable"]
    assert len(unv) == 1  # the slug came from the journal record
    assert "audited_source" not in out.sources_consulted


def _nb_sign_with_resolved_source(tmp_path: Path, slug: str) -> None:
    """Sign *slug* the ingest way: ``resolved`` rides source/template (F5 fixture).

    Mirrors ``notebook-ingest-signoffs`` — an interview-less, plugin-driven audit
    whose sign-off records carry the CURRENT source/template the shas were
    recomputed from, and no interview.json anywhere.
    """
    from hpc_agent.state import notebook_audit as nb

    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=_NB_AUDIT,
        block=nb.SIGN_OFF_BLOCK,
        response=f"reviewed the {slug} section end to end",
        resolved={
            "audit_id": _NB_AUDIT,
            "section": slug,
            "section_sha": _nb_sha(slug),
            "view_sha": "view-1",
            "source": "source.py",
            "template": "template.py",
        },
    )


def test_notebook_interview_less_audit_resolves_via_journal_resolved(tmp_path: Path) -> None:
    """F5: no interview.json, but the sign-off records ride ``resolved.source`` /
    ``resolved.template`` — the resolver falls back to the newest such record, so
    claims verify (the audit is no longer permanently unverifiable to the hook)."""
    (tmp_path / "source.py").write_text(_NB_SOURCE, encoding="utf-8")
    (tmp_path / "template.py").write_text(_NB_SOURCE, encoding="utf-8")
    _nb_sign_with_resolved_source(tmp_path, "load-data")
    _nb_sign_with_resolved_source(tmp_path, "fit-model")

    out = _nb_run(tmp_path, "load-data is signed_current; fit-model is signed_current too.")
    assert out.clean is True
    assert out.mismatches == []
    # Resolved via the journal fallback, NOT interview.json.
    assert not (tmp_path / "interview.json").exists()
    assert "audited_source" in out.sources_consulted


def test_notebook_interview_less_wrong_claim_is_a_real_contradiction(tmp_path: Path) -> None:
    """F5: the journal-resolved source makes a wrong status claim a genuine state
    contradiction (not merely unverifiable) — the hook can now block it."""
    (tmp_path / "source.py").write_text(_NB_SOURCE, encoding="utf-8")
    (tmp_path / "template.py").write_text(_NB_SOURCE, encoding="utf-8")
    _nb_sign_with_resolved_source(tmp_path, "load-data")  # fit-model stays unsigned

    out = _nb_run(tmp_path, "The fit-model section is signed_current.")
    assert out.clean is False
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert "fit-model" in state[0].claim
    assert state[0].nearest_source_value == "unsigned"


def test_notebook_no_slug_mentioned_is_clean(tmp_path: Path) -> None:
    """A status word with no section slug in range is module-level noise, skipped."""
    _nb_write_interview(tmp_path)

    out = _nb_run(tmp_path, "Everything is unsigned in the abstract sense of the word.")
    assert out.clean is True
    assert out.claims_checked == 0


def test_notebook_malformed_journal_line_skipped(tmp_path: Path) -> None:
    """A corrupt JSONL line does not strand the audit — a valid claim still verifies."""
    from hpc_agent.state.decision_journal import decisions_path

    _nb_write_interview(tmp_path)
    _nb_sign(tmp_path, "load-data")
    path = decisions_path(tmp_path, "notebook", _NB_AUDIT)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not valid json\n")

    out = _nb_run(tmp_path, "load-data is signed_current.")
    assert out.clean is True


# ── run-14 DEFECT 1: sha→section attribution off-by-one (audit-view digest) ─────
#
# The audit-view DIGEST (``render_summary_markdown``) lists one section per line,
# each carrying that section's trailing ``section_sha`` / ``view_sha`` — one
# newline ABOVE the NEXT section's slug — and the top-of-doc carries the
# whole-view ``view_sha`` / ``module_sha`` one line ABOVE the FIRST section. The
# old nearest-in-both-directions attribution drifted every section's sha onto the
# FOLLOWING section and the module sha onto ``data-selection`` (run-14,
# ``causal_tune_tree`` audit). These tests pin the own-line binding + block-level
# skip.

_NB_MULTI_SOURCE = """# %%
# hpc-audit-section: data-selection
sel = load_rows(0)

# %%
# hpc-audit-section: target-construction
tgt = build_target(1, 2, 3)

# %%
# hpc-audit-section: feature-construction
feat = make_features("a", "b")

# %%
# hpc-audit-section: baseline
base = fit_baseline(seed=42)
"""

_NB_MULTI_SLUGS = ("data-selection", "target-construction", "feature-construction", "baseline")


def _nb_write_multi(tmp_path: Path) -> dict[str, str]:
    """Interview + 4-section source; return ``{slug: full section_sha}`` (unsigned)."""
    import json

    from hpc_agent.state.audit_source import parse_percent_source

    (tmp_path / "multi.py").write_text(_NB_MULTI_SOURCE, encoding="utf-8")
    (tmp_path / "interview.json").write_text(
        json.dumps({"audited_source": {"source": "multi.py", "audit_id": _NB_AUDIT}}),
        encoding="utf-8",
    )
    return {s.slug: s.section_sha for s in parse_percent_source(_NB_MULTI_SOURCE).sections}


def _nb_digest(shas: dict[str, str], *, module_sha: str) -> str:
    """The audit-view digest shape: header module shas, then one line per section."""
    lines = [
        "# Notebook audit view (metadata; bodies live in the render files + popup)",
        "",
        f"- view_sha: {'a' * 64}",
        f"- source module_sha: {module_sha}",
        f"- template module_sha: {module_sha}",
        "",
    ]
    for slug in _NB_MULTI_SLUGS:
        lines.append(
            f"- {slug}  [standard] unsigned — section_sha {shas[slug]}, diff +0/-0, 0 assertion(s)"
        )
    return "\n".join(lines) + "\n"


def test_notebook_digest_shape_no_off_by_one_misattribution(tmp_path: Path) -> None:
    """Each section's OWN section_sha on its OWN digest line verifies clean — the
    old nearest match drifted every sha onto the following section (all four
    distinct, so a one-off shift mismatches and would falsely correct)."""
    shas = _nb_write_multi(tmp_path)
    # A module sha that is NOT any section's sha — under the old logic it would
    # attribute to data-selection (the first section, one line below) and flag.
    relay = _nb_digest(shas, module_sha="b" * 64)

    out = _nb_run(tmp_path, relay)
    assert out.clean is True, [m.detail for m in out.mismatches]
    assert [m for m in out.mismatches if m.kind == "number"] == []
    # Every section's sha WAS checked (bound to its own line), not skipped.
    assert out.claims_checked >= len(_NB_MULTI_SLUGS)


def test_notebook_module_sha_not_misattributed_to_first_section(tmp_path: Path) -> None:
    """A ``template module_sha`` line above the first section is block-level: it
    belongs to no section and is skipped, never flagged against data-selection."""
    shas = _nb_write_multi(tmp_path)
    relay = (
        f"- template module_sha: {'c' * 64}\n"
        f"- data-selection  [standard] unsigned — section_sha {shas['data-selection']}\n"
    )
    out = _nb_run(tmp_path, relay)
    assert out.clean is True, [m.detail for m in out.mismatches]


def test_notebook_genuinely_wrong_sha_in_digest_still_flagged(tmp_path: Path) -> None:
    """A WRONG hex on a section's own line is still corrected — and attributed to
    THAT section, not the following one."""
    shas = _nb_write_multi(tmp_path)
    wrong = "deadbeef" * 8  # 64 hex, not any section's sha
    lines = _nb_digest(shas, module_sha="b" * 64).splitlines()
    # Corrupt feature-construction's sha in place (keep the other three correct).
    lines = [
        ln.replace(shas["feature-construction"], wrong)
        if ln.startswith("- feature-construction")
        else ln
        for ln in lines
    ]
    out = _nb_run(tmp_path, "\n".join(lines) + "\n")
    num = [m for m in out.mismatches if m.kind == "number"]
    assert len(num) == 1, [m.detail for m in out.mismatches]
    assert num[0].claim == wrong
    assert "feature-construction" in num[0].detail  # the RIGHT section, not baseline


# ── run-14 DEFECT 2: cross-scope journal lookup (sibling audit shares slugs) ─────
#
# The relay named two audits whose sections share slug names
# (``causal_tune_linear`` / ``causal_tune_tree``, both ``data-selection``); the
# corrector ran once per audit over the WHOLE text, so the tree audit's shas —
# near the shared slug — were also checked against the LINEAR journal, emitting
# false corrections labelled ``[causal_tune_linear]``. The scope guard binds a
# claim to the audit whose id is mentioned nearest it.

_AUDIT_A = "causal_tune_linear"
_AUDIT_B = "causal_tune_tree"

_NB_SRC_A = """# %%
# hpc-audit-section: data-selection
sel = load_linear(0)
"""
_NB_SRC_B = """# %%
# hpc-audit-section: data-selection
sel = load_tree(999)
"""


def _nb_seed_via_journal(tmp_path: Path, audit_id: str, rel: str, text: str, slug: str) -> str:
    """Seed an audit resolvable via the journal ``resolved.source`` fallback."""
    from hpc_agent.state import notebook_audit as nb
    from hpc_agent.state.audit_source import parse_percent_source

    (tmp_path / rel).write_text(text, encoding="utf-8")
    sha = next(s.section_sha for s in parse_percent_source(text).sections if s.slug == slug)
    append_decision(
        tmp_path,
        scope_kind="notebook",
        scope_id=audit_id,
        block=nb.SIGN_OFF_BLOCK,
        response=f"reviewed the {slug} section",
        resolved={"audit_id": audit_id, "section": slug, "section_sha": sha, "source": rel},
    )
    return sha


def _seed_two_audits(tmp_path: Path) -> tuple[str, str]:
    a = _nb_seed_via_journal(tmp_path, _AUDIT_A, "src_a.py", _NB_SRC_A, "data-selection")
    b = _nb_seed_via_journal(tmp_path, _AUDIT_B, "src_b.py", _NB_SRC_B, "data-selection")
    assert a != b
    return a, b


def _two_audit_relay(a_sha: str, b_sha: str) -> str:
    return (
        f"Audit {_AUDIT_A}: data-selection is signed_current (section_sha {a_sha}).\n"
        f"Audit {_AUDIT_B}: data-selection is signed_current (section_sha {b_sha}).\n"
    )


def test_notebook_sibling_audit_sha_not_checked_against_this_journal(tmp_path: Path) -> None:
    """Verifying audit A with B as a sibling: B's data-selection sha (near B's id)
    is bound to B and NOT flagged against A's journal."""
    from hpc_agent.ops.decision.journal.verify_relay import verify_notebook_relay

    a_sha, b_sha = _seed_two_audits(tmp_path)
    relay = _two_audit_relay(a_sha, b_sha)

    out = verify_notebook_relay(tmp_path, _AUDIT_A, relay, other_audit_ids=[_AUDIT_B])
    assert out.clean is True, [m.detail for m in out.mismatches]


def test_notebook_cross_scope_defect_reproduces_without_the_guard(tmp_path: Path) -> None:
    """Without the sibling set (the pre-fix call), B's sha IS falsely corrected
    under A's scope — the run-14 defect this guard closes."""
    from hpc_agent.ops.decision.journal.verify_relay import verify_notebook_relay

    a_sha, b_sha = _seed_two_audits(tmp_path)
    relay = _two_audit_relay(a_sha, b_sha)

    out = verify_notebook_relay(tmp_path, _AUDIT_A, relay)  # no other_audit_ids → no guard
    false_corrections = [m for m in out.mismatches if m.kind == "number"]
    assert false_corrections, "expected the pre-guard cross-scope false correction"
    assert false_corrections[0].claim == b_sha


def test_notebook_genuinely_wrong_claim_about_this_audit_still_flagged(tmp_path: Path) -> None:
    """The guard never masks A's OWN wrong claim: a bad sha next to A's id fires."""
    from hpc_agent.ops.decision.journal.verify_relay import verify_notebook_relay

    _seed_two_audits(tmp_path)
    wrong = "cafe" * 16  # 64 hex, not A's sha
    relay = (
        f"Audit {_AUDIT_A}: data-selection is signed_current (section_sha {wrong}).\n"
        f"Audit {_AUDIT_B}: nothing to say.\n"
    )
    out = verify_notebook_relay(tmp_path, _AUDIT_A, relay, other_audit_ids=[_AUDIT_B])
    num = [m for m in out.mismatches if m.kind == "number"]
    assert len(num) == 1
    assert num[0].claim == wrong


# ── run-14 finding 5: an AMBIGUOUS (equidistant) claim is corrected by NO scope ─
#
# 5bf7a17a bound a claim to the audit mentioned NEAREST it, but left a tie in
# scope for BOTH audits ("a genuinely-wrong claim is still caught"). The docket
# verdict is the OPPOSITE priority: a claim provably owned by neither (equidistant)
# must yield NO correction — a false correction shown to the human is worse than
# silence — and the skip is counted-and-disclosed, never silent. These pin the
# equidistant tie both ways: no correction under either scope, counted exactly once.


def _tie_relay(b_sha: str) -> str:
    """A relay where the shared slug sits EXACTLY equidistant between the two audit
    ids (an ambiguous ownership tie), carrying B's ``data-selection`` sha.

    Constructed by searching the left padding that equalises the slug anchor's
    distance to each audit-id mention; asserts a genuine tie was built so the test
    can never silently degrade into a non-tie (which the nearest-id guard resolves).
    """
    from hpc_agent.ops.decision.journal.verify_relay import _nb_id_spans, _nb_span_distance

    for pad in range(1, 400):
        relay = (
            f"{_AUDIT_A}" + (" " * pad) + "data-selection section_sha " + b_sha + f" {_AUDIT_B}.\n"
        )
        i = relay.find("data-selection")
        span = (i, i + len("data-selection"))
        if _nb_span_distance(*span, _nb_id_spans(relay, _AUDIT_A)) == _nb_span_distance(
            *span, _nb_id_spans(relay, _AUDIT_B)
        ):
            return relay
    raise AssertionError("could not construct an equidistant tie relay")


def test_notebook_ambiguous_tie_yields_no_correction_under_either_scope(tmp_path: Path) -> None:
    """B's sha, EQUIDISTANT between the two audit ids, is owned by NEITHER — so it
    draws no correction under A's scope (the pre-ruling false correction) NOR B's."""
    from hpc_agent.ops.decision.journal.verify_relay import verify_notebook_relay

    _a_sha, b_sha = _seed_two_audits(tmp_path)
    relay = _tie_relay(b_sha)

    out_a = verify_notebook_relay(tmp_path, _AUDIT_A, relay, other_audit_ids=[_AUDIT_B])
    out_b = verify_notebook_relay(tmp_path, _AUDIT_B, relay, other_audit_ids=[_AUDIT_A])
    assert out_a.clean is True, [m.detail for m in out_a.mismatches]
    assert out_b.clean is True, [m.detail for m in out_b.mismatches]


def test_notebook_ambiguous_tie_counted_once_across_scopes(tmp_path: Path) -> None:
    """The ONE tied claim, skipped once per scope, dedupes to a single recorded span
    in the shared ``ambiguous_out`` — the count the hook discloses (no-silent-caps)."""
    from hpc_agent.ops.decision.journal.verify_relay import verify_notebook_relay

    _a_sha, b_sha = _seed_two_audits(tmp_path)
    relay = _tie_relay(b_sha)

    ambiguous: set[tuple[int, int]] = set()
    verify_notebook_relay(
        tmp_path, _AUDIT_A, relay, other_audit_ids=[_AUDIT_B], ambiguous_out=ambiguous
    )
    verify_notebook_relay(
        tmp_path, _AUDIT_B, relay, other_audit_ids=[_AUDIT_A], ambiguous_out=ambiguous
    )
    assert len(ambiguous) == 1


def test_notebook_owned_wrong_claim_flagged_and_not_recorded_ambiguous(tmp_path: Path) -> None:
    """The ambiguity guard is not over-broad: A's OWN wrong sha (strictly nearer A's
    id, not a tie) still fires AND is never recorded in ``ambiguous_out``."""
    from hpc_agent.ops.decision.journal.verify_relay import verify_notebook_relay

    _seed_two_audits(tmp_path)
    wrong = "cafe" * 16  # 64 hex, not A's sha
    relay = (
        f"Audit {_AUDIT_A}: data-selection is signed_current (section_sha {wrong}).\n"
        f"Audit {_AUDIT_B}: nothing to say.\n"
    )
    ambiguous: set[tuple[int, int]] = set()
    out = verify_notebook_relay(
        tmp_path, _AUDIT_A, relay, other_audit_ids=[_AUDIT_B], ambiguous_out=ambiguous
    )
    num = [m for m in out.mismatches if m.kind == "number"]
    assert len(num) == 1 and num[0].claim == wrong
    assert ambiguous == set()


# ── supersession links are authoritative identifiers ──────────────────────────


def test_superseded_by_token_is_authoritative(tmp_path: Path) -> None:
    """A truthful supersession relay names the successor run: the record's
    stamped ``superseded_by`` link (``ops/supersession`` writes it as the
    durable audit evidence) is an authoritative identifier for this run's
    audit — never an unknown-run-id mismatch."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    upsert_run(
        tmp_path,
        RunRecord(
            run_id=RUN_ID,
            profile="p",
            cluster="hoffman2",
            ssh_target="u@h",
            remote_path="/remote",
            job_name="j",
            job_ids=["13610902"],
            total_tasks=10,
            submitted_at="2026-07-03T00:00:00+00:00",
            experiment_dir=str(tmp_path),
            status="abandoned",
            superseded_by="pi-sweep-v2",
            superseded_at="2026-07-12T00:00:00+00:00",
        ),
    )

    out = _run(tmp_path, f"Run {RUN_ID} was superseded by pi-sweep-v2.")
    assert [m for m in out.mismatches if m.kind == "run_id"] == []


def test_unrelated_run_id_still_flagged_when_superseded_by_present(tmp_path: Path) -> None:
    """The supersession link exempts ONLY the named pair — an unrelated
    run-id-shaped token still flags (the fix must not blanket-exempt)."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    upsert_run(
        tmp_path,
        RunRecord(
            run_id=RUN_ID,
            profile="p",
            cluster="hoffman2",
            ssh_target="u@h",
            remote_path="/remote",
            job_name="j",
            job_ids=["13610902"],
            total_tasks=10,
            submitted_at="2026-07-03T00:00:00+00:00",
            experiment_dir=str(tmp_path),
            status="abandoned",
            superseded_by="pi-sweep-v2",
        ),
    )

    out = _run(tmp_path, f"Run {RUN_ID} was superseded by some-other-run7.")
    rid = [m for m in out.mismatches if m.kind == "run_id"]
    assert len(rid) == 1
    assert rid[0].claim == "some-other-run7"


# ── run-13 finding 8: the correction-flood classes ─────────────────────────────
# (``_seed_brief`` is defined above, in the bug-sweep #12 section.)


# ── (1a) bare month-day date fragments ─────────────────────────────────────────


def test_bare_month_day_fragments_not_flagged(tmp_path: Path) -> None:
    """Finding 8: a session reference '07-09'/'07-11' is a bare month-day date,
    not two numeric claims ('07', '09'). The whole span is consumed."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "Continuing session work from 07-09 and 07-11; the run is set.")
    assert out.clean is True, out.mismatches
    assert [m for m in out.mismatches if m.kind in ("number", "unverifiable", "run_id")] == []


def test_real_number_beside_month_day_still_flagged(tmp_path: Path) -> None:
    """Counter: only the date fragment is consumed — a genuine unsupported number
    on the same line still fires."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "Session 07-09 consumed 999 core-hours.")
    num = [m for m in out.mismatches if m.kind in ("number", "unverifiable")]
    assert [m.claim for m in num] == ["999"]


# ── (1b) hyphenated count phrases + run-level ──────────────────────────────────


def test_hyphenated_count_phrase_not_flagged_as_run_id(tmp_path: Path) -> None:
    """Finding 8: '300-task' is a count phrase, not a run-id-shaped token. The
    '300' is a NUMBER audited against the corpus (here supplied by a brief)."""
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, summary="300 tasks planned for the fleet")

    out = _run(tmp_path, "The 300-task fleet is ready to go.")
    assert [m for m in out.mismatches if m.kind == "run_id"] == []
    assert out.clean is True, out.mismatches


def test_run_level_not_flagged_as_run_id(tmp_path: Path) -> None:
    """Finding 8-addendum: 'run-level' is an English compound, not a run-id — the
    narrowed 'run-' shortcut requires a digit in the suffix."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "This is a run-level summary of the fleet.")
    assert [m for m in out.mismatches if m.kind == "run_id"] == []
    assert out.clean is True, out.mismatches


def test_id_shaped_token_still_flagged_after_narrowing(tmp_path: Path) -> None:
    """Counter: the narrowing keeps a genuinely id-shaped token (a letter+digit
    mixed segment) a run-id claim, and 'run-2' (run- + digit) still fires."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "The sweep-9f3a1c2b run is queued; run-2 is separate.")
    rid = sorted(m.claim for m in out.mismatches if m.kind == "run_id")
    assert rid == ["run-2", "sweep-9f3a1c2b"]


# ── (1c) 'timeout' / verification quoted from a log or the brief ───────────────


def test_timeout_quoted_from_log_not_flagged_as_state(tmp_path: Path) -> None:
    """Finding 8-addendum: 'timeout' quoted from a log line (a '[transport]' tag
    on the line) is a restatement, not a lifecycle claim about the run."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete")

    out = _run(
        tmp_path,
        "The worker log ended with: `[transport] progress` then a command timeout.",
    )
    assert [m for m in out.mismatches if m.kind == "state"] == []


def test_bare_timeout_state_still_flagged(tmp_path: Path) -> None:
    """Counter: a bare 'timeout' claim (no log/quote context) contradicting the
    recorded 'complete' still fires."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete")

    out = _run(tmp_path, "The run hit a timeout.")
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert state[0].claim.lower() == "timeout"


def test_canary_adjacent_verification_quote_skipped(tmp_path: Path) -> None:
    """Finding 8: a 'verified'/'canary green' word ADJACENT to 'canary' quotes the
    canary's own decision line — the guard now covers the verification families
    (it fired only for lifecycle families before), so it is not misattributed to
    the main run's status.

    NOTE the test name deliberately avoids the substring 'verified'/'green': the
    record's ``experiment_dir`` value (== the pytest tmp path, derived from the
    test name) is scanned for verification evidence, so a name carrying 'verified'
    would FALSELY evidence the claim and mask whether the guard actually fires (a
    latent value-scan precision bug surfaced by this suite)."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete")

    out = _run(tmp_path, "Quoting the brief: the canary was not verified.")
    assert [m for m in out.mismatches if m.kind == "state"] == []


def test_non_canary_verification_claim_still_flagged(tmp_path: Path) -> None:
    """Counter: a 'verified' claim NOT adjacent to 'canary' and unevidenced still
    fires — the verification guard is canary-scoped, not a blanket exemption. (Test
    name avoids 'verified'/'green' for the reason given in the sibling test.)"""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)
    _seed_record(tmp_path, status="complete")

    out = _run(tmp_path, "The run is verified and ready to submit.")
    state = [m for m in out.mismatches if m.kind == "state"]
    assert len(state) == 1
    assert state[0].claim.lower() == "verified"


# ── (1d) unit-suffixed sizes (du -sh) ──────────────────────────────────────────


def test_unit_suffixed_size_not_flagged(tmp_path: Path) -> None:
    """Finding 8-addendum: a 'du -sh'-style size ('886M') is a rounded human
    figure, not a citable number — the mantissa + unit are consumed."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "du -sh reports 886M for the results tree.")
    assert out.clean is True, out.mismatches
    assert [m for m in out.mismatches if m.kind in ("number", "unverifiable")] == []


def test_bare_size_number_without_unit_still_flagged(tmp_path: Path) -> None:
    """Counter: a bare number with no size unit is audited as before."""
    _seed_journal(tmp_path, core_hours=128)
    _seed_sidecar(tmp_path)

    out = _run(tmp_path, "The results tree has 886 files.")
    num = [m for m in out.mismatches if m.kind in ("number", "unverifiable")]
    assert [m.claim for m in num] == ["886"]


# ── (2) corpus completeness: a brief's rendered cost-line numbers are pooled ────


def test_brief_cost_line_numbers_are_pooled(tmp_path: Path) -> None:
    """Finding 8: a code-drafted brief's own cost line ('300 tasks × 4 cpus × 3h
    = 3600 core-hours') verifies CLEAN. Its numbers (300, 4, 3, 3600) live only
    in a STRING field that a hyphen ('core-hours') used to exclude WHOLESALE from
    the number pool; per-token extraction pools them."""
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, cost="300 tasks × 4 cpus × 3h = 3600 core-hours")

    out = _run(tmp_path, "Cost estimate: 300 tasks × 4 cpus × 3h = 3600 core-hours.")
    assert out.clean is True, out.mismatches


def test_number_absent_from_brief_string_still_flagged(tmp_path: Path) -> None:
    """Counter: pooling a brief's string numbers does not lower the bar — a number
    in NO field still fires."""
    _seed_sidecar(tmp_path)
    _seed_brief(tmp_path, cost="300 tasks × 4 cpus × 3h = 3600 core-hours")

    out = _run(tmp_path, "The fleet ran 500 tasks total.")
    num = [m for m in out.mismatches if m.kind in ("number", "unverifiable")]
    assert [m.claim for m in num] == ["500"]
