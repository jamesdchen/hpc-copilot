"""End-to-end conformance test for the FOREIGN CHAIN over a freestyle run.

The post-exploration fidelity-checker posture, driven hop by hop against a
synthetic freestyle experiment (no ``.hpc/``, no ``tasks.py`` — the run was
made entirely outside hpc-agent), fully local:

1. **fabricate** — a real ``results/task_<i>/metrics.json`` tree with known
   numeric values is the ONLY source of numbers in this test;
2. **adopt-run** (terminal shape) — sidecar with derived ``cmd_sha`` + the
   ``extra.adopted`` marker, journal record, directed settle to ``complete``,
   receipt-gated harvest fired, ``next_block == aggregate-check``;
3. **aggregate** — ``aggregate_flow`` (the SAME entry the terminal harvest
   routes to, ``ensure_all_combined=False``) with the pull seam monkeypatched
   to a local copy: ``_combiner`` 404s, so the no-combiner per-task fallback
   runs the REAL ``reduce_metrics`` weighted-mean over the hop-1 tree and
   persists ``_aggregated/<run_id>/metrics_aggregate.json``;
4. **claim-check** — ``verify-reproduction`` external-baseline against the
   true aggregate (adopted-consistency sentence, ``claim-check`` receipt, NO
   fingerprint sample — the observed-runs-only lock), then an out-of-tolerance
   claim (dated FINDING, exit-0, ``needs_decision``);
5. **attest** — the ``read-decisions`` query verb surfaces the adoption
   decision entry.

Each hop asserts that the PREVIOUS hop's artifact is what the next verb
actually consumed: the pull stub only COPIES the fabricated tree (it authors
no values), the reducer's numbers must equal the independently-computed
weighted mean of hop 1's fixtures, the claim-check receipt must cite the
hop-3 ``metrics_aggregate.json`` as its repro source, and the attest hop
reads back the hop-2 settle decision verbatim.

Seams monkeypatched (all sibling-sanctioned): ``HPC_JOURNAL_DIR`` (journal
isolation), ``adopt_run``'s injected ``_aggregate``/``_sweep`` harvest seams
(same as ``tests/ops/test_adopt_run.py``), and
``aggregate_flow.rsync_pull`` (same as
``tests/ops/aggregate/test_flow_ssh_default_reducer.py``) — no ssh anywhere.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hpc_agent._wire.actions.adopt_run import AdoptRunInput
from hpc_agent._wire.queries.decision_journal import ReadDecisionsInput
from hpc_agent._wire.queries.verify_reproduction import (
    ExternalBaseline,
    ReproTolerance,
    VerifyReproductionSpec,
)
from hpc_agent._wire.workflows.aggregate_flow import AggregateFlowSpec
from hpc_agent.ops import aggregate_flow as af_module
from hpc_agent.ops.adopt_run import adopt_run
from hpc_agent.ops.aggregate_flow import aggregate_flow
from hpc_agent.ops.decision.journal import read_decisions
from hpc_agent.ops.monitor.harvest_guard import harvest_receipt_exists
from hpc_agent.ops.verify_reproduction import (
    CLAIM_CONSISTENT_SENTENCE_ADOPTED,
    verify_reproduction,
)
from hpc_agent.state.journal import load_run
from hpc_agent.state.runs import read_run_sidecar

_RUN_ID = "foreign-freestyle-1"
_COMMAND = "python train.py --seed $SEED --output-file $RESULT_DIR/metrics.json"
_SSH_TARGET = "me@hoffman2.idre.ucla.edu"
_REMOTE_PATH = "/scratch/me/exp"
_EVIDENCE = "reporter RC=0 all-4; result tree on disk"

#: Hop 1's known per-task values — the ONLY numbers this test fabricates.
_TASK_METRICS: list[dict[str, float]] = [
    {"loss": 1.0, "n_samples": 1},
    {"loss": 2.0, "n_samples": 1},
    {"loss": 3.0, "n_samples": 2},
    {"loss": 6.0, "n_samples": 4},
]
#: Independent arithmetic over the SAME fixtures (n_samples-weighted mean /
#: summed n_samples — reduce_metrics' documented semantics): (1+2+6+24)/8.
_EXPECTED_LOSS = 4.125
_EXPECTED_N = 8


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


def _fabricate_freestyle_tree(exp: Path) -> Path:
    """Hop 1: the synthetic freestyle results tree (no .hpc/, no tasks.py)."""
    results = exp / "results"
    for i, metrics in enumerate(_TASK_METRICS):
        task_dir = results / f"task_{i}"
        task_dir.mkdir(parents=True)
        (task_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    return results


def _copy_only_pull_stub(source_results: Path, calls: list[dict[str, Any]]) -> Any:
    """An ``rsync_pull`` stand-in that only ever COPIES the hop-1 tree.

    ``_combiner`` 404s (the combiner never ran for a freestyle run); the
    ``results`` pull mirrors the fabricated tree byte-for-byte into the local
    destination. It authors NO metric values — every number the reducer sees
    originates in hop 1's fixtures.
    """

    def _stub(
        *_a: Any, remote_subdir: str, local_dir: str, **kw: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append({"remote_subdir": remote_subdir, "local_dir": local_dir, **kw})
        if remote_subdir == "_combiner":
            return subprocess.CompletedProcess(
                args=[],
                returncode=23,
                stdout="",
                stderr=(
                    f'rsync: link_stat "{_REMOTE_PATH}/_combiner" failed: '
                    "No such file or directory (2)"
                ),
            )
        if remote_subdir == "results":
            dest = Path(local_dir)
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_results, dest, dirs_exist_ok=True)
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        Path(local_dir).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    return _stub


def test_foreign_chain_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """adopt-run → aggregate → claim-check (match + finding) → read-decisions."""
    exp = tmp_path / "freestyle-exp"

    # ── hop 1: fabricate the freestyle experiment ─────────────────────────────
    results = _fabricate_freestyle_tree(exp)
    assert not (exp / ".hpc").exists()
    assert not (exp / "tasks.py").exists()

    # ── hop 2: ADOPT terminal (no job_ids; directed terminal evidence) ────────
    harvest_calls: list[tuple[Path, str]] = []

    def _seam_aggregate(exp_dir: Path, run_id: str) -> Any:
        harvest_calls.append((Path(exp_dir), run_id))
        return SimpleNamespace()  # inert: hop 3 drives the real reducer itself

    def _seam_sweep(_remote: str, _run_id: str) -> dict[int, list[str]]:
        return {}

    adopted = adopt_run(
        exp,
        spec=AdoptRunInput(
            run_id=_RUN_ID,
            command=_COMMAND,
            cluster="hoffman2",
            ssh_target=_SSH_TARGET,
            remote_path=_REMOTE_PATH,
            terminal_evidence=_EVIDENCE,
            # No result_dir_template / task_count: inference reads hop 1's tree.
            results_sample=str(results),
        ),
        _aggregate=_seam_aggregate,
        _sweep=_seam_sweep,
    )

    assert adopted.stage_reached == "adopted_terminal"
    assert adopted.needs_decision is False
    assert adopted.status == "complete"
    assert adopted.cmd_sha == hashlib.sha256(_COMMAND.encode("utf-8")).hexdigest()
    # Layout was inferred FROM the fabricated tree (hop 1 → hop 2 consumption).
    assert adopted.result_dir_template == "results/task_{task_id}"
    assert adopted.task_count == len(_TASK_METRICS)
    assert adopted.next_block is not None
    assert adopted.next_block["verb"] == "aggregate-check"

    sidecar = read_run_sidecar(exp, _RUN_ID)
    assert sidecar["cmd_sha"] == adopted.cmd_sha
    assert sidecar["extra"]["adopted"]["by"] == "adopt-run"
    assert sidecar["result_dir_template"] == "results/task_{task_id}"

    record = load_run(exp, _RUN_ID)
    assert record is not None
    assert record.status == "complete"
    assert record.job_ids == []
    assert record.total_tasks == len(_TASK_METRICS)

    # The receipt-gated harvest fired, and it consumed THIS adoption's identity.
    assert harvest_receipt_exists(exp, _RUN_ID)
    assert harvest_calls == [(exp, _RUN_ID)]

    # ── hop 3: AGGREGATE via the real flow (per-task fallback reducer) ────────
    # aggregate_flow demands a remote pull even with local files, so the pull
    # seam is monkeypatched to a local copy (sibling-sanctioned rsync_pull
    # stub); the weighted-mean numbers come from the REAL reduce_metrics.
    pull_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(af_module, "rsync_pull", _copy_only_pull_stub(results, pull_calls))

    agg = aggregate_flow(
        exp,
        # ensure_all_combined=False is the harvest-guard posture — the same
        # aggregate entry adopt-run's terminal harvest routes to.
        spec=AggregateFlowSpec(run_id=_RUN_ID, ensure_all_combined=False),
    )

    # The reducer consumed the journal record + sidecar hop 2 wrote: the pull
    # was addressed by the adopted record's remote identity, and scoped by the
    # sidecar's inferred result_dir_template ("results/task_{task_id}" →
    # subtree "results").
    subdirs = [c["remote_subdir"] for c in pull_calls]
    assert "_combiner" in subdirs
    assert "results" in subdirs
    for call in pull_calls:
        # remote_path is record-owned (resolve_ssh_target may consult the local
        # cluster registry for the target, so only the path is asserted exactly).
        assert call.get("remote_path") == _REMOTE_PATH

    # The real reducer's numbers equal the independent weighted mean of hop 1.
    assert set(agg.aggregated_metrics) == {_RUN_ID}
    reduced = agg.aggregated_metrics[_RUN_ID]
    assert reduced["loss"] == pytest.approx(_EXPECTED_LOSS)
    assert reduced["n_samples"] == _EXPECTED_N

    # The mirror the reducer read is byte-identical to hop 1's tree — the stub
    # copied, never authored.
    mirror = exp / "_aggregated" / _RUN_ID / "_per_task_results"
    for i in range(len(_TASK_METRICS)):
        mirrored = (mirror / f"task_{i}" / "metrics.json").read_text(encoding="utf-8")
        original = (results / f"task_{i}" / "metrics.json").read_text(encoding="utf-8")
        assert mirrored == original

    # Durable artifact for the next hop, honestly provenanced.
    agg_path = exp / "_aggregated" / _RUN_ID / "metrics_aggregate.json"
    assert agg_path.is_file()
    agg_doc = json.loads(agg_path.read_text(encoding="utf-8"))
    assert agg_doc["aggregated_metrics"][_RUN_ID]["loss"] == pytest.approx(_EXPECTED_LOSS)
    assert agg_doc["aggregated_metrics"][_RUN_ID]["n_samples"] == _EXPECTED_N
    assert agg_doc["provenance"]["source"] == "per_task_fallback"

    # ── hop 4a: CLAIM-CHECK, claim consistent with the true aggregate ─────────
    claimed_ok = {
        f"{_RUN_ID}.loss": _EXPECTED_LOSS,
        f"{_RUN_ID}.n_samples": float(_EXPECTED_N),
    }
    vr = verify_reproduction(
        exp,
        spec=VerifyReproductionSpec(
            repro_run_id=_RUN_ID,
            external_baseline=ExternalBaseline(
                claimed_values=claimed_ok,
                tolerance=ReproTolerance(default_rel_tol=1e-12),
            ),
        ),
    )

    assert vr.stage_reached == "match"
    assert vr.needs_decision is False
    # Adopted-baseline consistency sentence: the sidecar's extra.adopted marker
    # (hop 2's artifact) is what the claim-check consulted.
    assert vr.reason == CLAIM_CONSISTENT_SENTENCE_ADOPTED
    assert vr.receipt["receipt_kind"] == "claim-check"
    assert vr.receipt["consistency"] == CLAIM_CONSISTENT_SENTENCE_ADOPTED
    assert vr.receipt["overall"] == "match"
    # Hop 3 → hop 4 consumption: the fresh side was loaded from the durable
    # aggregate hop 3 persisted, and the compared values are its values.
    assert vr.receipt["sources"]["repro_artifact"] == str(agg_path)
    per_key = {e["key"]: e for e in vr.receipt["per_key"]}
    assert set(per_key) == set(claimed_ok)
    assert per_key[f"{_RUN_ID}.loss"]["repro"] == pytest.approx(_EXPECTED_LOSS)
    assert per_key[f"{_RUN_ID}.n_samples"]["repro"] == _EXPECTED_N
    assert all(e["verdict"] == "match" for e in vr.receipt["per_key"])
    # The claim rides the receipt VERBATIM.
    assert vr.receipt["claim"]["claimed_values"] == claimed_ok

    receipt_path = exp / "_aggregated" / _RUN_ID / "claim_check_receipts.jsonl"
    assert Path(vr.receipt_path) == receipt_path
    lines = receipt_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["receipt_kind"] == "claim-check"

    # Observed-runs-only lock: NO fingerprint sample is minted for an adopted
    # run's claim-check.
    assert vr.appended_sample is None
    fingerprints = exp / "_aggregated" / "_fingerprints"
    assert not fingerprints.exists() or not any(fingerprints.iterdir())

    # ── hop 4b: CLAIM-CHECK, out-of-tolerance claim → dated FINDING ───────────
    claimed_bad = {
        f"{_RUN_ID}.loss": _EXPECTED_LOSS + 1.0,
        f"{_RUN_ID}.n_samples": float(_EXPECTED_N),
    }
    # Exit-0 semantics: the call RETURNS (a finding, never an error).
    vr2 = verify_reproduction(
        exp,
        spec=VerifyReproductionSpec(
            repro_run_id=_RUN_ID,
            external_baseline=ExternalBaseline(
                claimed_values=claimed_bad,
                tolerance=ReproTolerance(default_abs_tol=1e-6),
            ),
        ),
    )

    assert vr2.stage_reached == "mismatch"
    assert vr2.needs_decision is True
    assert vr2.reason.startswith("claim-check finding: mismatch")
    assert "1 matched, 1 mismatched, 0 incomparable of 2 claimed keys" in vr2.reason
    # No manifest at claim time → the drift disclosure says so.
    assert "cannot distinguish result decay from data drift" in vr2.reason
    # The FINDING is dated: the appended receipt carries its own timestamp.
    lines = receipt_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    finding = json.loads(lines[1])
    assert finding["receipt_kind"] == "claim-check"
    assert finding["overall"] == "mismatch"
    assert finding["ts"]
    # Still no fingerprint sample: the lock holds on the non-match too.
    assert vr2.appended_sample is None
    assert not fingerprints.exists() or not any(fingerprints.iterdir())

    # ── hop 5: ATTEST — the adoption decision is visible to read-decisions ────
    attest = read_decisions(
        experiment_dir=exp,
        spec=ReadDecisionsInput(scope_kind="run", scope_id=_RUN_ID),
    )
    assert attest.count == 1
    entry = attest.records[0]
    assert entry.block == "adopt-run"
    assert entry.response == "y"
    # The directed-settle evidence hop 2 journaled, read back verbatim.
    assert entry.proposal == _EVIDENCE
    assert entry.provenance["kind"] == "adopt-run-directed-settle"
    assert entry.provenance["evidence"] == _EVIDENCE
    assert entry.resolved == {"status": "complete", "terminal_cause": "complete"}
