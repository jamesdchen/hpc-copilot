"""Behaviour-pinning mutation coverage for the announce census + harvest backbone.

The announce census (``ops/monitor/announce.py``) and the harvest receipt
(``ops/monitor/harvest_guard.py``) are the POSITIVE-EVIDENCE substrate every
lifecycle verdict reads: reconcile / settle / the monitor poll loop all key
"did each task reach a terminal state" and "was the guaranteed harvest reached"
off these two readers. A SILENT bug here is the cardinal sin — a run settled on
ABSENCE (a spurious empty census, a dropped receipt) rather than on evidence.

The landed suites cover the happy paths and the run-12 findings
(``test_announce.py`` / ``test_announce_ids.py`` / ``test_harvest_guard.py``)
and the reconcile ORCHESTRATION (``test_reconcile_recovery_coverage.py`` pins
``_adoptable_wave_ids``, the token keying, the Δ6 disjuncts and all three
``_harvest_if_owed`` arms). This module deliberately does NOT re-pin those; it
covers the GAPS at the announce/harvest reader layer itself — covered-but-
UNASSERTED boundaries where a boundary/operator/predicate mutant survives the
existing suite:

* the ack-gate: an ACKED-but-empty dir is ``present:1 announced:0`` (distinct
  from the ``present:0`` absent dir), and a TRUNCATED ack token is severed
  (``present:0``), NEVER read as the visible-but-unvouched counts — the exact-
  line sentinel discipline, mirroring the reconcile severed-vs-empty rung;
* the filename-state regex is ANCHORED — a prefix/suffix/extended marker name
  never mis-parses into a valid COMPLETE id;
* the census counting clamps: over-announced (a resubmit's doubled markers)
  clamps ``missing`` to 0, a malformed count line degrades to 0 not a crash —
  single AND per-host-batch;
* batch-vs-single parity + the batch keys by the REQUESTED run_id (never the
  first row — the decoy discipline at the fold layer);
* ``harvest_receipt_exists`` — the receipt is recorded LAST even on a FAILED
  harvest and a deliberate ``scope_locked`` skip (both ARE receipts), while a
  ``run_not_terminal`` abnormal-exit skip is EXCLUDED (a no-op, not a performed
  harvest); the newest-first scan continues past a run_not_terminal skip to an
  earlier real receipt and survives a torn / non-dict final line.

Each test names the mutant it kills. Cluster-free: the announce readers route
through a patched ``announce.remote.ssh_run``; the receipt tests build the
durable ledger directly through the canonical append seam.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from hpc_agent.infra.io import append_jsonl_line
from hpc_agent.ops.monitor import announce
from hpc_agent.ops.monitor.harvest_guard import harvest_marker_path, harvest_receipt_exists

if TYPE_CHECKING:
    from pathlib import Path


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _patch(monkeypatch: pytest.MonkeyPatch, stdout: str, rc: int = 0) -> None:
    monkeypatch.setattr(announce.remote, "ssh_run", lambda *a, **k: _proc(stdout, returncode=rc))


# ── A. the ack-gate (read_announcements) — present keys off the ACK, not counts ─
#
# ``present`` is the capability signal, sourced ONLY from the positive-evidence
# ack line. An acked-empty dir and an absent dir are DIFFERENT verdicts; a
# truncated read is severed, never the visible-but-unvouched bytes.


def test_acked_but_empty_dir_is_present_with_zero_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dispatcher that STARTED but has no terminal task yet: the ``cd`` acks but
    the two ``ls|wc`` lines are absent. Must read ``present:1, announced:0`` — the
    empty-but-present census, DISTINCT from the ``present:0`` absent-dir case
    (``test_no_ack_reads_as_no_announcements`` pins the other side). Kills a mutant
    that sources ``present`` from ``announced > 0`` instead of the ack: that would
    collapse "dir exists, nothing done" into "dir absent" and a Phase-2 consumer
    would fall through to the reporter walk on a run whose census is authoritative.
    """
    _patch(monkeypatch, "__HPC_ANNOUNCE_ACK__\n")  # acked, no complete=/failed= lines
    res = announce.read_announcements(
        ssh_target="u@h", remote_path="/remote", run_id="r1", task_count=5
    )
    assert res == {"present": 1, "announced": 0, "complete": 0, "failed": 0, "missing": 5}


def test_truncated_ack_token_is_severed_never_the_visible_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read cut mid-flight emits a TRUNCATED ack (``__HPC_ANNOUNCE_AC``) followed
    by a visible ``complete=9``. The exact-LINE sentinel check must reject the
    partial token → ``present:0`` with the counts DISCARDED — never mis-settle a
    run on unvouched truncated bytes (the severed-vs-empty distinction, the
    announce twin of the reconcile severed-read rung). Kills a mutant that relaxes
    the exact-line match to a substring/prefix test (which would ack on the
    truncated token) and any mutant that reads ``complete=9`` before the ack gate.
    """
    _patch(monkeypatch, "__HPC_ANNOUNCE_AC\ncomplete=9\nfailed=0\n")
    res = announce.read_announcements(
        ssh_target="u@h", remote_path="/remote", run_id="r1", task_count=10
    )
    # The visible complete=9 is DISCARDED — no ack, so no census.
    assert res == {"present": 0, "announced": 0, "complete": 0, "failed": 0, "missing": 10}


def test_malformed_count_line_parses_zero_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """A garbled count line (``complete=notanumber``) must degrade to 0, never raise
    (a raise in the census read would sever the whole tick). The sibling ``failed``
    line still parses. Kills removal of the ``_parse_count`` ``except ValueError:
    return 0`` guard — without it the census read raises on a torn digit."""
    _patch(monkeypatch, "__HPC_ANNOUNCE_ACK__\ncomplete=notanumber\nfailed=2\n")
    res = announce.read_announcements(
        ssh_target="u@h", remote_path="/remote", run_id="r1", task_count=10
    )
    assert res == {"present": 1, "announced": 2, "complete": 0, "failed": 2, "missing": 8}


def test_over_announced_clamps_missing_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over-announced markers (a resubmit that re-touched markers ⇒ announced >
    task_count) must clamp ``missing`` to 0, never a NEGATIVE missing that a
    downstream ``pending = missing`` arithmetic would read as work-still-owed on a
    finished run. Kills dropping the ``max(0, task_count - announced)`` clamp."""
    _patch(monkeypatch, "__HPC_ANNOUNCE_ACK__\ncomplete=8\nfailed=4\n")
    res = announce.read_announcements(
        ssh_target="u@h", remote_path="/remote", run_id="r1", task_count=10
    )
    assert res["announced"] == 12  # complete + failed, uncapped
    assert res["missing"] == 0  # clamped, never -2


# ── B. filename-state parsing (read_announced_task_ids) — anchored regex ────────
#
# ``_COMPLETE_MARKER_RE`` is ``^task_(\d+)\.complete$`` — fully anchored so a
# stray/truncated/extended name never mis-parses into a valid done id. The landed
# ids test covers ``task_.complete`` (no digit) and ``README``; it does NOT
# exercise the ANCHORS, so a mutant dropping ``^`` or ``$`` survives it.


def test_marker_regex_is_anchored_rejecting_prefix_suffix_and_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adversarial marker names that differ ONLY at the anchors must be rejected:

    * ``xtask_4.complete`` — a leading char (kills dropping ``^``),
    * ``task_3.complete.bak`` — an editor-swap suffix (kills dropping ``$``),
    * ``task_5.completed`` — the state string EXTENDED (kills dropping ``$``, which
      would let ``.complete`` match a ``.completed``/``.complete.tmp`` marker),
    * ``task_.complete`` — no digit id.

    Only the two well-formed names (``task_9`` / ``task_10``) parse. A mis-parse
    here mints a bogus done id — the migration would skip a task that never ran.
    """
    out = (
        "__HPC_ANNOUNCE_IDS_ACK__\n"
        "xtask_4.complete\n"
        "task_3.complete.bak\n"
        "task_5.completed\n"
        "task_.complete\n"
        "task_9.complete\n"
        "task_10.complete\n"
    )
    _patch(monkeypatch, out)
    res = announce.read_announced_task_ids(ssh_target="u@h", remote_path="/r", run_id="r1")
    assert res.present is True
    assert res.done_ids == frozenset({9, 10})  # every anchor-violating name excluded


# ── C. per-host batch census — boundaries + requested-run keying ────────────────


def test_batch_keys_by_requested_run_id_not_first_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """The batch output carries a FOREIGN row first (``run=r2``) and the requested
    run (``run=r1``) second. The reader must key its result off the REQUESTED
    run_id, never the first/any row — the decoy discipline (mirroring the token
    keying at the reconcile layer) applied to the census fold. Kills a mutant that
    distributes the first present row to the requested run."""
    out = (
        "__HPC_ANNOUNCE_BATCH_ACK__\n"
        "run=r2 present=1 complete=9 failed=9\n"  # foreign decoy
        "run=r1 present=1 complete=1 failed=0\n"
    )
    _patch(monkeypatch, out)
    res = announce.read_announcements_batch(
        ssh_target="u@h", remote_path="/remote", run_task_counts={"r1": 4}
    )
    assert res["r1"] == {"present": 1, "announced": 1, "complete": 1, "failed": 0, "missing": 3}


def test_batch_malformed_count_degrades_run_to_not_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A batch row with a torn count (``complete=abc``) must degrade THAT run to
    not-present (fall through per-run), never raise across the whole fleet fold.
    Kills removal of the ``_parse_batch_rows`` ``except ValueError: continue`` — a
    single torn row would otherwise crash the entire per-host census."""
    _patch(monkeypatch, "__HPC_ANNOUNCE_BATCH_ACK__\nrun=r1 present=1 complete=abc failed=0\n")
    res = announce.read_announcements_batch(
        ssh_target="u@h", remote_path="/remote", run_task_counts={"r1": 4}
    )
    assert res["r1"] == {"present": 0, "announced": 0, "complete": 0, "failed": 0, "missing": 4}


def test_batch_over_announced_clamps_missing_to_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """The batch path shares the single reader's clamp: over-announced ⇒ ``missing``
    clamps to 0, never negative. Kills dropping ``max(0, tc - announced)`` in the
    batch fold (an independent copy of the single-reader arithmetic)."""
    _patch(monkeypatch, "__HPC_ANNOUNCE_BATCH_ACK__\nrun=r1 present=1 complete=8 failed=4\n")
    res = announce.read_announcements_batch(
        ssh_target="u@h", remote_path="/remote", run_task_counts={"r1": 10}
    )
    assert res["r1"]["announced"] == 12
    assert res["r1"]["missing"] == 0


# ── D. batch-vs-single parity — the two readers return the SAME shape ───────────


def test_batch_matches_single_for_present_and_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller may swap ``read_announcements`` for the per-host batch freely, so
    for the same logical state each must produce a BYTE-identical per-run dict —
    both the present census AND the not-present fall-through. Kills any drift where
    the batch fold computes ``announced``/``missing``/``present`` differently from
    the single reader (they are separate arithmetic sites)."""
    monkeypatch.setattr(
        announce.remote,
        "ssh_run",
        lambda *a, **k: _proc("__HPC_ANNOUNCE_ACK__\ncomplete=3\nfailed=1\n"),
    )
    single_present = announce.read_announcements(
        ssh_target="u@h", remote_path="/remote", run_id="r1", task_count=10
    )
    monkeypatch.setattr(
        announce.remote,
        "ssh_run",
        lambda *a, **k: _proc("__HPC_ANNOUNCE_BATCH_ACK__\nrun=r1 present=1 complete=3 failed=1\n"),
    )
    batch_present = announce.read_announcements_batch(
        ssh_target="u@h", remote_path="/remote", run_task_counts={"r1": 10}
    )
    assert batch_present["r1"] == single_present

    # Absent parity: no ack (single) vs no present row (batch) → identical shape.
    monkeypatch.setattr(announce.remote, "ssh_run", lambda *a, **k: _proc(""))
    single_absent = announce.read_announcements(
        ssh_target="u@h", remote_path="/remote", run_id="r1", task_count=10
    )
    monkeypatch.setattr(
        announce.remote, "ssh_run", lambda *a, **k: _proc("__HPC_ANNOUNCE_BATCH_ACK__\n")
    )
    batch_absent = announce.read_announcements_batch(
        ssh_target="u@h", remote_path="/remote", run_task_counts={"r1": 10}
    )
    assert batch_absent["r1"] == single_absent
    assert single_absent["present"] == 0 and single_absent["missing"] == 10


# ── E. harvest_receipt_exists — the exact receipt predicate ─────────────────────
#
# The receipt is the durable, journal-side proof the guaranteed harvest was
# REACHED for a terminal state. The reconcile backstop (pinned in
# ``test_reconcile_recovery_coverage.py``) derives "harvest owed" from its
# NEGATION, so the predicate itself is safety-critical: a false-True strands an
# unharvested terminal run; a false-False re-fires an rsync+reduce forever. These
# pin the predicate directly (the reconcile suite exercises only real receipts).


@pytest.fixture
def experiment(tmp_path: Path) -> Path:
    d = tmp_path / "exp"
    d.mkdir()
    return d


def _append(experiment_dir: Path, run_id: str, marker: dict[str, object]) -> None:
    append_jsonl_line(harvest_marker_path(experiment_dir, run_id), marker)


def test_receipt_absent_when_ledger_missing(journal_home: Path, experiment: Path) -> None:
    """No ledger file at all ⇒ no receipt (the terminal-with-no-harvest gap the
    backstop is FOR). Kills a mutant that flips the ``if not path.is_file(): return
    False`` guard to True (which would suppress every backstop re-fire)."""
    assert harvest_receipt_exists(experiment, "never-harvested") is False


def test_run_not_terminal_skip_is_not_a_receipt(journal_home: Path, experiment: Path) -> None:
    """A ledger holding ONLY the abnormal-exit ``run_not_terminal`` skip records a
    NO-OP (the watch died, the run was not terminal, nothing was pulled) — it is
    NOT a performed harvest, so the receipt predicate must return False and the
    backstop stays armed. Kills the exact predicate ``harvest_skipped_reason ==
    'run_not_terminal'``: a mutant to ``!=`` or to a different literal would count
    this skip as a receipt and permanently suppress the owed harvest."""
    _append(
        experiment, "r-skip", {"harvest_skipped_reason": "run_not_terminal", "harvest_ok": False}
    )
    assert harvest_receipt_exists(experiment, "r-skip") is False


def test_scope_locked_skip_is_a_receipt(journal_home: Path, experiment: Path) -> None:
    """A ``scope_locked`` skip is a DELIBERATE human decision reached AT a terminal
    state — the guaranteed harvest ran and chose not to reduce. It IS a receipt
    (the backstop must not re-fire against a human lock). Paired with the
    ``run_not_terminal`` test above, this pins the predicate to the EXACT
    ``run_not_terminal`` literal — a mutant keying the exclusion on ``scope_locked``
    instead would flip both tests."""
    _append(experiment, "r-locked", {"harvest_skipped_reason": "scope_locked", "harvest_ok": True})
    assert harvest_receipt_exists(experiment, "r-locked") is True


def test_failed_harvest_still_records_a_receipt(journal_home: Path, experiment: Path) -> None:
    """A harvest that FAILED (``harvest_ok: false``, no skip reason) was still
    REACHED for the terminal state — the marker is written LAST regardless of
    outcome, so it counts as a receipt (the backstop is about REACHED, not
    SUCCEEDED). Kills a mutant that gates the receipt on ``harvest_ok`` being
    truthy — that would re-drive a genuinely-failed harvest on every reconcile."""
    _append(experiment, "r-failed", {"harvest_skipped_reason": None, "harvest_ok": False})
    assert harvest_receipt_exists(experiment, "r-failed") is True


def test_run_not_terminal_newest_does_not_mask_earlier_receipt(
    journal_home: Path, experiment: Path
) -> None:
    """A real receipt landed, THEN a later abnormal exit appended a
    ``run_not_terminal`` skip as the NEWEST line. The newest-first scan must
    CONTINUE past the skip to the earlier real receipt ⇒ True. Kills a mutant that
    ``return False`` on the first ``run_not_terminal`` (instead of ``continue``),
    which would resurrect an already-harvested run's backstop."""
    _append(experiment, "r-mix", {"harvest_skipped_reason": None, "harvest_ok": True})
    _append(
        experiment, "r-mix", {"harvest_skipped_reason": "run_not_terminal", "harvest_ok": False}
    )
    assert harvest_receipt_exists(experiment, "r-mix") is True


def test_torn_final_line_leaves_earlier_receipt_readable(
    journal_home: Path, experiment: Path
) -> None:
    """A crash-torn (invalid-JSON) final line must be skipped, leaving an earlier
    whole-line-atomic receipt readable ⇒ True. Kills removal of the
    ``json.JSONDecodeError`` guard — a torn tail would otherwise raise and the
    read's ``except OSError`` would NOT catch it, stranding the finished run's
    evidence behind a crash."""
    path = harvest_marker_path(experiment, "r-torn")
    _append(experiment, "r-torn", {"harvest_skipped_reason": None, "harvest_ok": True})
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"harvest_ok": true, "torn\n')  # a half-written final line
    assert harvest_receipt_exists(experiment, "r-torn") is True


def test_non_dict_ledger_line_is_skipped(journal_home: Path, experiment: Path) -> None:
    """A ledger line that is valid JSON but NOT an object (a bare list — a
    corruption) must be skipped, not fed to ``.get(...)``. With no real receipt
    present the answer is False (no crash). Kills removal of the ``isinstance(parsed,
    dict)`` guard — ``[1, 2].get('harvest_skipped_reason')`` raises AttributeError,
    which the ``except OSError`` read guard would NOT catch."""
    path = harvest_marker_path(experiment, "r-nondict")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]\n", encoding="utf-8")
    assert harvest_receipt_exists(experiment, "r-nondict") is False
