"""Terminal detached-worker causes — s2-readiness pillar 5 ("failures are product surface").

The night of 2026-07-30 produced four failure classes and every diagnosis was a
human reading a ``_detached/*.log``. These tests pin the seam that ends that: a
terminal worker death journals a DISCRIMINATED cause, the attention queue
surfaces it with the recoveries-registry remediation composed onto the item, and
the morning brief discloses ``failed_at`` vs ``surfaced_at``.

The ``[fatal]`` block STAYS — logs remain the forensic tier. The load-bearing
assertion is not "the log is gone" but "the attention item carries everything the
log carries that a human needs in order to DECIDE", pinned explicitly below.

Cluster-free: the journal home is redirected via ``HPC_JOURNAL_DIR``, no process
is spawned, and nothing dials.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent.ops.attention_queue import WORKER_TERMINAL, collect_items, collect_worker_terminals
from hpc_agent.ops.attention_render import render_queue
from hpc_agent.ops.recover.heal_taxonomy import worker_terminal_sections
from hpc_agent.ops.recover.terminal_cause import (
    CAUSE_RECORD_KIND,
    classify_worker_exit,
    read_terminal_causes,
    record_worker_terminal_cause,
    terminal_cause_path,
)
from hpc_agent.recovery.registry import REGISTRY, menu_for, remediation_for
from hpc_agent.state.block_terminal import record_terminal, terminal_path
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

_FAILED_AT = "2026-07-30T03:00:00+00:00"
_NOW = "2026-07-30T08:00:00+00:00"  # five hours later — the disclosure gap

#: The four classes tonight's operator diagnosed by hand.
_NEW_KINDS = (
    "dead_hop_route",
    "flap_exhausted_staging",
    "canary_reporter_unreachable",
    "zombie_submitting_record",
)


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    return tmp_path


def _mk_run(
    exp: Path,
    run_id: str,
    *,
    status: str = "in_flight",
    **kw: Any,
) -> RunRecord:
    rec = RunRecord(
        run_id=run_id,
        profile="prof",
        cluster="hoffman2",
        ssh_target="user@hoffman2",
        remote_path="/scratch/run",
        job_name="job",
        job_ids=["1"],
        total_tasks=10,
        submitted_at="2026-07-30T02:00:00+00:00",
        experiment_dir=str(exp),
        status=status,
        **kw,
    )
    upsert_run(exp, rec)
    return rec


def _worker_log(tmp_path: Path, body: str) -> str:
    """A worker log ending in a flushed ``[fatal]`` block, as the exit path leaves it."""
    path = tmp_path / "submit-s2-run.log"
    path.write_text(
        "[hb] alive 12s | child=ssh.exe cpu=0.4s\n"
        f"{body}\n"
        "[fatal] detached worker exit-path disclosure\n"
        "[fatal] exit_code=1\n"
        "[fatal] last known stage: [hb] alive 12s | child=ssh.exe cpu=0.4s\n",
        encoding="utf-8",
    )
    return str(path)


# ── the registry entries ─────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", _NEW_KINDS)
def test_new_registry_entry_renders(kind: str) -> None:
    """Each of tonight's four classes has a menu that composes to a remediation."""
    assert kind in REGISTRY, f"{kind}: no registry entry"
    menu = menu_for(kind)
    assert menu.kind == kind
    assert menu.summary.strip()
    assert menu.options
    rendered = remediation_for(kind, placeholders={"run_id": "r1", "experiment_dir": "/exp"})
    assert menu.summary in rendered
    assert "(a)" in rendered
    for opt in menu.options:
        assert opt.when_to_use.strip(), f"{kind}: an option has no when_to_use"
    ranks = [opt.safety_rank for opt in menu.options]
    assert len(set(ranks)) == len(ranks), f"{kind}: duplicate safety_rank"


def test_dead_hop_remediation_refuses_the_2026_07_30_misdiagnosis() -> None:
    """The dead-hop menu must NOT steer at a sibling login node.

    The incident was not a missing probe — it was a confident message pointing at
    the wrong host. The rank-0 option is triage (evidence), and the summary says
    in words that a sibling inherits the dead hop.
    """
    menu = menu_for("dead_hop_route")
    primary = min(menu.options, key=lambda o: o.safety_rank)
    assert "net-triage" in primary.cli_command
    assert "sibling" in menu.summary
    assert "host-retarget" not in primary.cli_command


# ── classification ───────────────────────────────────────────────────────────


def test_classifies_dead_hop_from_the_path_cause_vocabulary(tmp_path: Path) -> None:
    """A ``hop_down_direct_ok`` in the worker's own disclosure keys the dead-hop menu."""
    _mk_run(tmp_path, "run-hop")
    log = _worker_log(
        tmp_path,
        "submit-s2 refused BEFORE detaching run 'run-hop': hop_down_direct_ok — "
        "path dead (hop usc-discovery down); direct alternative OK.",
    )
    cause = classify_worker_exit(
        tmp_path,
        run_id="run-hop",
        block="submit-s2",
        exit_code=1,
        log_path=log,
        now_iso=_FAILED_AT,
    )
    assert cause.path_cause == "hop_down_direct_ok"
    assert cause.recovery_kind == "dead_hop_route"
    assert cause.remediation is not None
    assert "net-triage" in cause.remediation


def test_classifies_flap_from_the_stamped_identity_not_the_message(tmp_path: Path) -> None:
    """A stamped transport-flap exception keys the staging menu with no marker text.

    Re-deriving the verdict from prose is what made a KNOWN flap read as a hard
    failure; the identity is the trusted channel.
    """
    from hpc_agent.infra.ssh_options import mark_transport_flap

    _mk_run(tmp_path, "run-flap")
    exc = mark_transport_flap(errors.SshUnreachable("the remote hash-manifest probe failed"))
    cause = classify_worker_exit(
        tmp_path,
        run_id="run-flap",
        block="submit-s2",
        exit_code=1,
        exc=exc,
        now_iso=_FAILED_AT,
    )
    assert cause.transport_flap is True
    assert cause.recovery_kind == "flap_exhausted_staging"
    assert cause.error_code == "ssh_unreachable"
    assert cause.category == "network"
    assert cause.retry_safe is True
    assert cause.remediation is not None
    assert "converge" in cause.remediation or "residue" in cause.remediation


def test_stage_exhaustion_markers_match_what_submit_flow_actually_composes() -> None:
    """The marker arm is pinned to its SOURCE, not to a paraphrase of it.

    An earlier draft matched three strings ("staging attempt", "attempts
    exhausted", "could not stage") that appear nowhere in the tree — a dead arm
    that silently classified nothing while its docstring claimed a shared
    vocabulary. This reads the composer's own source so the two cannot drift
    apart silently again.
    """
    import inspect

    from hpc_agent.ops.recover.terminal_cause import _STAGE_EXHAUSTED_MARKERS
    from hpc_agent.ops.submit_flow import _stage_exhausted_error

    src = inspect.getsource(_stage_exhausted_error)
    for marker in _STAGE_EXHAUSTED_MARKERS:
        assert marker in src, (
            f"{marker!r} is not composed by _stage_exhausted_error — the arm is dead"
        )


@pytest.mark.parametrize(
    "body",
    [
        "staging against user@hoffman2 exhausted 3 bounded attempt(s): cause not named.",
        "staging against user@hoffman2 STOPPED after 1 of 3 allowed attempt(s): circuit open.",
    ],
)
def test_stage_exhaustion_message_keys_the_flap_menu(tmp_path: Path, body: str) -> None:
    """Both composed shapes classify — the budget-exhausted arm and the fenced arm."""
    _mk_run(tmp_path, "run-stage")
    log = _worker_log(tmp_path, body)
    cause = classify_worker_exit(
        tmp_path,
        run_id="run-stage",
        block="submit-s2",
        exit_code=1,
        log_path=log,
        now_iso=_FAILED_AT,
    )
    assert cause.recovery_kind == "flap_exhausted_staging"


def test_a_log_without_an_exhaustion_marker_does_not_key_the_flap_menu(tmp_path: Path) -> None:
    """The arm's negative half: near-miss staging prose must NOT classify.

    Without this the arm could be satisfied by any log mentioning staging, and a
    later loosening of the markers would go unnoticed.
    """
    _mk_run(tmp_path, "run-stage")
    log = _worker_log(tmp_path, "staging against user@hoffman2 pushed 4 files, deploy ok")
    cause = classify_worker_exit(
        tmp_path,
        run_id="run-stage",
        block="submit-s2",
        exit_code=1,
        log_path=log,
        now_iso=_FAILED_AT,
    )
    assert cause.recovery_kind is None


def test_flap_menu_summary_is_not_staging_specific() -> None:
    """The menu covers every flap-retry seam, because the identity is stamped.

    The test above classifies a stamped flap raised by the remote manifest probe,
    not by staging — a summary that told a staging-only story would misdescribe
    the case its own battery demonstrates.
    """
    summary = menu_for("flap_exhausted_staging").summary
    assert "TRANSPORT FLAP" in summary
    assert "not staging alone" in summary


def test_classifies_canary_reporter_unreachable(tmp_path: Path) -> None:
    """The canary's own ``failure_kind`` token keys the route-class menu."""
    _mk_run(tmp_path, "run-canary")
    log = _worker_log(tmp_path, '{"ok": false, "failure_kind": "reporter_unreachable"}')
    cause = classify_worker_exit(
        tmp_path,
        run_id="run-canary",
        block="submit-s2",
        exit_code=1,
        log_path=log,
        now_iso=_FAILED_AT,
    )
    assert cause.recovery_kind == "canary_reporter_unreachable"
    assert cause.remediation is not None
    assert "net-triage" in cause.remediation


def test_classifies_zombie_submitting_record_from_dispatch_evidence(tmp_path: Path) -> None:
    """Rung 0's evidence class outranks anything sensed over a wire.

    ``dispatch_evidence.state == 'pending'`` proves offline that no qsub was ever
    sent, so it wins even when the log ALSO names a path cause.
    """
    _mk_run(
        tmp_path,
        "run-zombie",
        status="submitting",
        dispatch_evidence={"state": "pending", "at": _FAILED_AT},
    )
    log = _worker_log(tmp_path, "target_unreachable — the effective path did not verify")
    cause = classify_worker_exit(
        tmp_path,
        run_id="run-zombie",
        block="submit-s2",
        exit_code=1,
        log_path=log,
        now_iso=_FAILED_AT,
    )
    assert cause.dispatch_never_actuated is True
    assert cause.recovery_kind == "zombie_submitting_record"
    assert cause.remediation is not None
    assert "abandoned" in cause.remediation


def test_zombie_over_a_dead_hop_leads_with_the_route_fact(tmp_path: Path) -> None:
    """Precedence collision: the record wins, but the message must not read "resubmit".

    ``zombie_submitting_record``'s rank-1 option is ``/submit-hpc``. Read after a
    headline that names only the zombie class, with the dead hop never mentioned,
    that is an instruction to re-fire straight back through the hop that killed
    the worker. The route fact therefore leads.
    """
    _mk_run(
        tmp_path,
        "run-both",
        status="submitting",
        dispatch_evidence={"state": "pending", "at": _FAILED_AT},
    )
    log = _worker_log(tmp_path, "hop_down_direct_ok — ProxyJump hop is DOWN")
    cause = classify_worker_exit(
        tmp_path,
        run_id="run-both",
        block="submit-s2",
        exit_code=1,
        log_path=log,
        now_iso=_FAILED_AT,
    )
    assert cause.recovery_kind == "zombie_submitting_record"  # precedence unchanged
    head, _, tail = cause.message.partition("zombie_submitting_record")
    assert "hop_down_direct_ok" in head, f"the route fact must LEAD: {cause.message}"
    assert "fix the path FIRST" in head
    assert tail, "the zombie class must still be named"


def test_pre_evidence_record_reads_unknown_not_false(tmp_path: Path) -> None:
    """An EMPTY ``dispatch_evidence`` is UNKNOWN — the tri-state rung 0 refuses to flatten."""
    _mk_run(tmp_path, "run-old", status="submitting")
    cause = classify_worker_exit(
        tmp_path, run_id="run-old", block="submit-s2", exit_code=1, now_iso=_FAILED_AT
    )
    assert cause.dispatch_never_actuated is None
    assert cause.recovery_kind != "zombie_submitting_record"


def test_undiscriminated_death_names_no_remediation(tmp_path: Path) -> None:
    """No matching class → no guessed remediation, and the record SAYS so."""
    _mk_run(tmp_path, "run-mystery")
    log = _worker_log(tmp_path, "something entirely unfamiliar happened")
    cause = classify_worker_exit(
        tmp_path,
        run_id="run-mystery",
        block="submit-s4",
        exit_code=7,
        log_path=log,
        now_iso=_FAILED_AT,
    )
    assert cause.recovery_kind is None
    assert cause.remediation is None
    assert "NOT discriminated" in cause.message


def test_passing_path_causes_are_never_reported_as_the_cause(tmp_path: Path) -> None:
    """``path_unproven`` / ``route_unresolved`` are the PASSING set — not failures."""
    _mk_run(tmp_path, "run-pass")
    log = _worker_log(tmp_path, "readiness read: path_unproven; route_unresolved for host")
    cause = classify_worker_exit(
        tmp_path,
        run_id="run-pass",
        block="submit-s2",
        exit_code=1,
        log_path=log,
        now_iso=_FAILED_AT,
    )
    assert cause.path_cause is None


# ── the journal record ───────────────────────────────────────────────────────


def test_record_is_appended_to_the_run_sidecar_journal(tmp_path: Path) -> None:
    _mk_run(tmp_path, "run-rec")
    written = record_worker_terminal_cause(
        tmp_path, run_id="run-rec", block="submit-s2", exit_code=1, now_iso=_FAILED_AT
    )
    assert written is not None
    assert written["kind"] == CAUSE_RECORD_KIND
    path = terminal_cause_path(tmp_path, "run-rec")
    assert path.exists()
    raws = [raw for raw in path.read_text(encoding="utf-8").splitlines() if raw.strip()]
    lines = [json.loads(raw) for raw in raws]
    assert len(lines) == 1
    assert lines[0]["run_id"] == "run-rec"
    assert read_terminal_causes(tmp_path, "run-rec") == lines


def test_record_write_is_fail_open(tmp_path: Path) -> None:
    """A disclosure record must never turn a failed worker into a wedged one."""
    assert (
        record_worker_terminal_cause(
            tmp_path, run_id="bad/id", block="submit-s2", exit_code=1, now_iso=_FAILED_AT
        )
        is None
    )


@pytest.mark.parametrize(
    "run_id",
    [
        "bad/id",
        "bad\\id",
        ".",
        "..",
        "",
        # Windows drive-relative: NO separator, yet it resolves against the
        # process's per-drive cwd and lands outside the sidecar tree entirely.
        "C:evil",
        # NTFS alternate data stream: writes a hidden stream on a real file.
        "run-1:hidden",
    ],
)
def test_path_guard_refuses_every_escaping_run_id(tmp_path: Path, run_id: str) -> None:
    """The path guard is what stands between an env-supplied run_id and the tree.

    This record is written by a DYING process against a ``run_id`` that came off
    the environment, so the guard takes the stricter side: separators, the dot
    entries, AND ``:`` (which escapes on Windows without a separator at all).
    """
    with pytest.raises(errors.SpecInvalid):
        terminal_cause_path(tmp_path, run_id)


def test_reading_a_torn_journal_yields_fewer_records_not_an_exception(tmp_path: Path) -> None:
    _mk_run(tmp_path, "run-torn")
    record_worker_terminal_cause(
        tmp_path, run_id="run-torn", block="submit-s2", exit_code=1, now_iso=_FAILED_AT
    )
    path = terminal_cause_path(tmp_path, "run-torn")
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "detached-worker-term\n')
    assert len(read_terminal_causes(tmp_path, "run-torn")) == 1


# ── the attention item ───────────────────────────────────────────────────────


def _one_item(tmp_path: Path, run_id: str = "run-hop") -> Any:
    items = collect_worker_terminals(tmp_path, now=_NOW)
    assert len(items) == 1, f"expected exactly one item, got {items}"
    item = items[0]
    assert item.scope_id == run_id
    return item


def test_killed_worker_produces_record_item_and_named_remediation(tmp_path: Path) -> None:
    """THE headline: a killed worker → journal record + attention item + named remediation.

    The fixture is a worker that died terminal with a dead ProxyJump hop — the
    exact class that cost an hour of log archaeology.
    """
    _mk_run(tmp_path, "run-hop")
    log = _worker_log(
        tmp_path,
        "submit-s2 refused BEFORE detaching run 'run-hop': hop_down_direct_ok — "
        "path dead (hop usc-discovery down); direct alternative OK.",
    )
    record = record_worker_terminal_cause(
        tmp_path,
        run_id="run-hop",
        block="submit-s2",
        exit_code=1,
        log_path=log,
        now_iso=_FAILED_AT,
    )
    assert record is not None and record["recovery_kind"] == "dead_hop_route"

    item = _one_item(tmp_path)
    assert item.kind == WORKER_TERMINAL
    assert item.item_class == "blocked"
    assert item.block == "submit-s2"
    assert item.since == _FAILED_AT
    # The remediation IS the registry's, byte-identical — not re-authored here.
    assert item.action == remediation_for(
        "dead_hop_route",
        placeholders={
            "run_id": "run-hop",
            "experiment_dir": str(tmp_path),
            "ssh_target": "user@hoffman2",
            "cluster_host": "hoffman2",
            "log_path": log,
        },
    )
    assert item.evidence["recovery_kind"] == "dead_hop_route"
    assert item.evidence["path_cause"] == "hop_down_direct_ok"


def _real_fatal_block(
    monkeypatch: pytest.MonkeyPatch, *, message: str, exit_code: int
) -> tuple[str, BaseException]:
    """The REAL ``[fatal]`` block ``crash_disclosure.emit_fatal_block`` writes.

    Not a hand-built fixture: the emitter is invoked with a genuinely raised
    exception (so the traceback is real) and its output captured verbatim. The
    test below then enumerates that output, so a fact ADDED to the emitter shows
    up as an unrecognised line and fails — a hand-built fixture could not notice.
    """
    import io

    from hpc_agent._kernel.lifecycle.crash_disclosure import emit_fatal_block

    monkeypatch.setenv("HPC_DETACHED_RUN_ID", "run-hop")  # the emitter's own gate
    try:
        raise errors.SshUnreachable(message)
    except errors.SshUnreachable as exc:
        stream = io.StringIO()
        assert emit_fatal_block(
            exc=exc,
            exit_code=exit_code,
            last_stage="[hb] alive 12s | child=ssh.exe cpu=0.4s",
            stream=stream,
        )
        # The exception is handed back so the RECORDER sees exactly what the
        # EMITTER saw — that pairing is the real unhandled-exception arm, and it
        # is what makes "every emitted fact is carried" a meaningful claim.
        return stream.getvalue(), exc


def test_item_carries_everything_the_log_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log STAYS (forensic tier) and the item needs none of it to decide.

    The fact set is DERIVED from ``emit_fatal_block``'s real output rather than
    asserted against a hand-written list: every ``[fatal]``-marked line the
    emitter produced must be accounted for by a fact the attention item carries.
    A new emitter fact therefore fails this test until it is either carried on
    the item or consciously declared forensic-only.

    (The traceback body is deliberately NOT marked — the ``[fatal]`` prefix is
    the emitter's own "this is a disclosed fact" convention, the same one
    ``log_has_fatal_marker`` keys on, and the traceback is the forensic payload
    the item points AT rather than carries.)
    """
    _mk_run(tmp_path, "run-hop")
    fatal, exc = _real_fatal_block(
        monkeypatch,
        message="submit-s2 refused BEFORE detaching: hop_down_direct_ok — dead hop.",
        exit_code=1,
    )
    log = tmp_path / "worker.log"
    log.write_text("[hb] alive 12s | child=ssh.exe cpu=0.4s\n" + fatal, encoding="utf-8")

    record_worker_terminal_cause(
        tmp_path,
        run_id="run-hop",
        block="submit-s2",
        exit_code=1,
        exc=exc,
        log_path=str(log),
        now_iso=_FAILED_AT,
    )
    ev = _one_item(tmp_path).evidence

    # (a) the forensic tier is untouched — the emitter's block is still on disk.
    assert fatal in log.read_text(encoding="utf-8")

    # (b) every marked fact the emitter wrote is accounted for on the item.
    #     Each entry is (predicate on the line, the item fact that carries it).
    accounted: list[tuple[Any, Any]] = [
        # the header — carried as "the log disclosed on its way out"
        (lambda ln: ln == "detached worker exit-path disclosure", lambda: ev["log_disclosed"]),
        # the exception type + message — carried as error_code + the discriminated
        # cause the classifier read OUT of that very message
        (
            lambda ln: ln.startswith("SshUnreachable:"),
            lambda: (
                ev["error_code"] == "ssh_unreachable"
                and ev["path_cause"] == "hop_down_direct_ok"
                and ev["recovery_kind"] == "dead_hop_route"
            ),
        ),
        # the exit code
        (lambda ln: ln.startswith("exit_code="), lambda: ev["exit_code"] == 1),
        # the last known stage — carried as the last log line
        (lambda ln: ln.startswith("last known stage:"), lambda: bool(ev["last_log_line"])),
    ]
    marked = [
        line[len("[fatal] ") :].strip()
        for line in fatal.splitlines()
        if line.startswith("[fatal] ")
    ]
    assert marked, "the emitter produced no marked facts — the fixture is not real"
    for line in marked:
        matches = [carried for predicate, carried in accounted if predicate(line)]
        assert matches, (
            f"emit_fatal_block wrote an UNACCOUNTED fact: {line!r}. Either carry it "
            "on the attention item, or add it here as a declared forensic-only line."
        )
        for carried in matches:
            assert carried(), f"the item does not carry the fact behind {line!r}"

    # (c) and the item POINTS at the forensic tier for the traceback it does not carry.
    assert ev["log_path"] == str(log)
    assert "Traceback" in log.read_text(encoding="utf-8")


def test_item_discloses_failed_at_vs_surfaced_at(tmp_path: Path) -> None:
    """Disclosure-latency honesty: a 3am death read at 8am shows the five-hour gap."""
    _mk_run(tmp_path, "run-hop")
    record_worker_terminal_cause(
        tmp_path, run_id="run-hop", block="submit-s2", exit_code=1, now_iso=_FAILED_AT
    )
    ev = _one_item(tmp_path).evidence
    assert ev["failed_at"] == _FAILED_AT
    assert ev["recorded_at"] == _FAILED_AT
    assert ev["surfaced_at"] == _NOW
    assert ev["disclosure_latency_seconds"] == 5 * 3600
    assert ev["record_latency_seconds"] == 0.0


def test_render_shows_the_disclosure_gap_and_the_remediation(tmp_path: Path) -> None:
    _mk_run(tmp_path, "run-hop")
    log = _worker_log(tmp_path, "hop_down_direct_ok — dead hop.")
    record_worker_terminal_cause(
        tmp_path,
        run_id="run-hop",
        block="submit-s2",
        exit_code=1,
        log_path=log,
        now_iso=_FAILED_AT,
    )
    rendered = render_queue([_one_item(tmp_path)], computed_at=_NOW)
    assert "worker-terminal" in rendered
    assert "disclosed +5h" in rendered
    assert "net-triage" in rendered, "the composed remediation must reach the digest"


def test_render_of_an_undiscriminated_death_is_honest(tmp_path: Path) -> None:
    """No remediation → the line says the cause was not discriminated, and points at the log."""
    _mk_run(tmp_path, "run-mystery")
    log = _worker_log(tmp_path, "nothing familiar")
    record_worker_terminal_cause(
        tmp_path,
        run_id="run-mystery",
        block="submit-s4",
        exit_code=7,
        log_path=log,
        now_iso=_FAILED_AT,
    )
    items = collect_worker_terminals(tmp_path, now=_NOW)
    rendered = render_queue(items, computed_at=_NOW)
    assert "cause not discriminated" in rendered
    assert log in rendered


def test_collector_is_wired_into_collect_items(tmp_path: Path) -> None:
    _mk_run(tmp_path, "run-hop")
    record_worker_terminal_cause(
        tmp_path, run_id="run-hop", block="submit-s2", exit_code=1, now_iso=_FAILED_AT
    )
    kinds = {item.kind for item in collect_items(tmp_path, now=_NOW).items}
    assert WORKER_TERMINAL in kinds


def test_empty_journal_yields_no_items(tmp_path: Path) -> None:
    assert collect_worker_terminals(tmp_path, now=_NOW) == []


# ── clearing: the item's SUBJECT is (run_id, block), and it resolves ──────────
#
# The attention queue's own rule (docs/design/attention-queue.md, "Delivery
# de-scoped"): an item persists — recomputed, with its age — until the human
# clears its SUBJECT. An append-only journal projected record-per-item can never
# clear, which is a standing BLOCKED item for a run that succeeded weeks ago.


def _die(tmp_path: Path, run_id: str, *, block: str = "submit-s2", at: str) -> None:
    """One journalled worker death for ``(run_id, block)`` stamped at *at*."""
    record_worker_terminal_cause(tmp_path, run_id=run_id, block=block, exit_code=1, now_iso=at)


def _succeed_block(tmp_path: Path, run_id: str, *, block: str = "submit-s2", at: str) -> None:
    """A SUCCESSFUL block terminal for ``(run_id, block)`` stamped at *at*.

    Written through the real ``record_terminal`` so the stored shape is the real
    one, then re-stamped to a deterministic ``ts`` (the writer stamps wall-clock,
    which would make the later-than comparison depend on when the suite runs).
    """
    record_terminal(tmp_path, run_id=run_id, block=block, cmd_sha="sha", result_dump={"ok": True})
    path = terminal_path(tmp_path, run_id, block)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["ts"] = at
    path.write_text(json.dumps(stored, sort_keys=True), encoding="utf-8")


def test_three_deaths_then_a_completed_run_render_zero_items(tmp_path: Path) -> None:
    """Died 3x then SUCCEEDED → the subject is resolved; nothing stands."""
    _mk_run(tmp_path, "run-retry", status="in_flight")
    for hour in ("01", "02", "03"):
        _die(tmp_path, "run-retry", at=f"2026-07-30T{hour}:00:00+00:00")
    assert len(collect_worker_terminals(tmp_path, now=_NOW)) == 1  # still dead: one item

    _mk_run(tmp_path, "run-retry", status="complete")
    assert collect_worker_terminals(tmp_path, now=_NOW) == []


def test_three_deaths_then_a_successful_block_terminal_render_zero_items(tmp_path: Path) -> None:
    """The second clearing leg: the block itself got there on a later attempt."""
    _mk_run(tmp_path, "run-retry", status="in_flight")
    for hour in ("01", "02", "03"):
        _die(tmp_path, "run-retry", at=f"2026-07-30T{hour}:00:00+00:00")
    _succeed_block(tmp_path, "run-retry", at="2026-07-30T04:00:00+00:00")
    assert collect_worker_terminals(tmp_path, now=_NOW) == []


def test_three_deaths_still_dead_render_exactly_one_latest_item(tmp_path: Path) -> None:
    """Died 3x and still dead → ONE item, and it is the LATEST death, not the first."""
    _mk_run(tmp_path, "run-retry", status="in_flight")
    for hour in ("01", "02", "03"):
        _die(tmp_path, "run-retry", at=f"2026-07-30T{hour}:00:00+00:00")
    items = collect_worker_terminals(tmp_path, now=_NOW)
    assert len(items) == 1
    assert items[0].since == "2026-07-30T03:00:00+00:00"


def test_a_block_terminal_that_predates_the_death_clears_nothing(tmp_path: Path) -> None:
    """An EARLIER success is the record of a previous attempt — the death is newer."""
    _mk_run(tmp_path, "run-retry", status="in_flight")
    _succeed_block(tmp_path, "run-retry", at="2026-07-30T01:00:00+00:00")
    _die(tmp_path, "run-retry", at="2026-07-30T03:00:00+00:00")
    assert len(collect_worker_terminals(tmp_path, now=_NOW)) == 1


@pytest.mark.parametrize("status", ["failed", "abandoned"])
def test_an_anomalous_terminal_status_does_not_clear(tmp_path: Path, status: str) -> None:
    """``failed`` / ``abandoned`` are terminals the human still owes a verdict on."""
    _mk_run(tmp_path, "run-dead", status=status)
    _die(tmp_path, "run-dead", at=_FAILED_AT)
    assert len(collect_worker_terminals(tmp_path, now=_NOW)) == 1


def test_distinct_blocks_of_one_run_are_distinct_subjects(tmp_path: Path) -> None:
    """The subject is ``(run_id, block)`` — S2 dying does not hide S4 dying."""
    _mk_run(tmp_path, "run-two", status="in_flight")
    _die(tmp_path, "run-two", block="submit-s2", at="2026-07-30T01:00:00+00:00")
    _die(tmp_path, "run-two", block="submit-s4", at="2026-07-30T02:00:00+00:00")
    items = collect_worker_terminals(tmp_path, now=_NOW)
    assert sorted(item.block or "" for item in items) == ["submit-s2", "submit-s4"]


def test_clearing_fails_SAFE_when_no_run_record_exists(tmp_path: Path) -> None:
    """No record to consult → the item STANDS. Wrongly hiding a failure is the defect."""
    _die(tmp_path, "run-orphan", at=_FAILED_AT)  # no run record was ever written
    assert len(collect_worker_terminals(tmp_path, now=_NOW)) == 1


def test_clearing_fails_SAFE_when_the_run_record_read_RAISES(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TORN journal must leave the item standing, not clear it.

    Distinct from the absent-record case above, which never enters the ``except``
    at all: this drives the exception arm, on a run whose status would OTHERWISE
    clear (``complete``). The fail-safe direction is the whole point — a
    disclosure surface that hides failures when its own reads break is worse than
    one that shows a stale item.
    """
    import hpc_agent.state.journal as journal_mod

    _mk_run(tmp_path, "run-torn-rec", status="complete")
    _die(tmp_path, "run-torn-rec", at=_FAILED_AT)
    assert collect_worker_terminals(tmp_path, now=_NOW) == []  # clears while readable

    def _boom(*_args: object, **_kw: object) -> None:
        raise OSError("journal record is torn")

    monkeypatch.setattr(journal_mod, "load_run", _boom)
    assert len(collect_worker_terminals(tmp_path, now=_NOW)) == 1


def test_clearing_fails_SAFE_when_the_block_terminal_read_RAISES(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same fail-safe direction on the second clearing leg."""
    import hpc_agent.state.block_terminal as bt_mod

    _mk_run(tmp_path, "run-torn-bt", status="in_flight")
    _die(tmp_path, "run-torn-bt", at=_FAILED_AT)
    _succeed_block(tmp_path, "run-torn-bt", at="2026-07-30T04:00:00+00:00")
    assert collect_worker_terminals(tmp_path, now=_NOW) == []  # clears while readable

    def _boom(*_args: object, **_kw: object) -> None:
        raise OSError("block terminal is torn")

    monkeypatch.setattr(bt_mod, "read_terminal_with_fallback", _boom)
    assert len(collect_worker_terminals(tmp_path, now=_NOW)) == 1


# ── the morning brief ────────────────────────────────────────────────────────


def test_morning_brief_section_discloses_failed_at_vs_surfaced_at(tmp_path: Path) -> None:
    _mk_run(tmp_path, "run-hop")
    record_worker_terminal_cause(
        tmp_path, run_id="run-hop", block="submit-s2", exit_code=1, now_iso=_FAILED_AT
    )
    sections = worker_terminal_sections(tmp_path, "run", "run-hop", now_iso=_NOW)
    assert len(sections) == 1
    entry = sections[0]
    assert entry["failed_at"] == _FAILED_AT
    assert entry["surfaced_at"] == _NOW
    assert entry["latency_seconds"] == 5 * 3600
    assert entry["remediation"] is None or isinstance(entry["remediation"], str)


def test_morning_brief_folds_the_section_in_and_earns_a_brief(tmp_path: Path) -> None:
    """A terminal worker death earns a morning brief on its own — no consent needed."""
    from hpc_agent.ops.overnight import morning_brief_if_any

    _mk_run(tmp_path, "run-hop")
    assert morning_brief_if_any(tmp_path, scope_kind="run", scope_id="run-hop") is None

    log = _worker_log(tmp_path, "hop_down_direct_ok — dead hop.")
    record_worker_terminal_cause(
        tmp_path,
        run_id="run-hop",
        block="submit-s2",
        exit_code=1,
        log_path=log,
        now_iso=_FAILED_AT,
    )
    brief = morning_brief_if_any(tmp_path, scope_kind="run", scope_id="run-hop", now_iso=_NOW)
    assert brief is not None
    failures = brief["class_sections"]["worker_terminal_failures"]
    assert len(failures) == 1
    assert failures[0]["recovery_kind"] == "dead_hop_route"
    assert failures[0]["latency_seconds"] == 5 * 3600
    assert "net-triage" in failures[0]["remediation"]


def test_campaign_scope_has_no_worker_terminal_section(tmp_path: Path) -> None:
    assert worker_terminal_sections(tmp_path, "campaign", "camp-1", now_iso=_NOW) == []


# ── the REAL exit path (wiring, not just the module) ─────────────────────────


def _as_detached_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, log_body: str
) -> tuple[Path, Path]:
    """Put this process in the shoes of a marked detached worker.

    Mirrors ``_spawn_detached``'s contract exactly: the three ``HPC_DETACHED_*``
    env vars plus cwd == the experiment dir. The heartbeat is disabled so no
    thread outlives the test.
    """
    exp = tmp_path / "exp"
    exp.mkdir()
    log = tmp_path / "worker.log"
    log.write_text(log_body + "\n", encoding="utf-8")
    monkeypatch.setenv("HPC_DETACHED_RUN_ID", "run-e2e")
    monkeypatch.setenv("HPC_DETACHED_BLOCK", "submit-s2")
    monkeypatch.setenv("HPC_DETACHED_LOG", str(log))
    monkeypatch.setenv("HPC_DETACH_HEARTBEAT_SEC", "0")
    monkeypatch.chdir(exp)
    return exp, log


def _assert_fatal_block_written(capsys: pytest.CaptureFixture[str], *needles: str) -> None:
    """The ``[fatal]`` block reached the worker's stderr (== its captured log).

    The pillar ADDS a structured tier; it never removes the forensic one. Asserted
    on every exit arm so a future refactor that routes disclosure through the
    journal alone cannot quietly drop the log block.
    """
    err = capsys.readouterr().err
    assert "[fatal] detached worker exit-path disclosure" in err, (
        f"the [fatal] block must still be written; stderr was: {err!r}"
    )
    for needle in needles:
        assert needle in err, f"{needle!r} missing from the [fatal] block: {err!r}"


def test_exit_path_journals_the_cause_on_an_unhandled_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real ``cli.dispatch.main`` seam: a crashing worker journals its cause.

    Pins that the ``[fatal]`` block is STILL written on this arm — the forensic
    tier and the structured tier are additive, never a swap. (In a real worker
    stderr IS the captured log; here it is pytest's capture, which is the same
    stream the emitter targets.)
    """
    import hpc_agent.cli.dispatch as dispatch

    exp, log = _as_detached_worker(tmp_path, monkeypatch, log_body="[hb] alive 12s | no children")
    _mk_run(exp, "run-e2e")

    def _boom(_argv: object) -> int:
        raise errors.SshUnreachable("hop_down_direct_ok — ProxyJump hop is DOWN")

    monkeypatch.setattr(dispatch, "_dispatch_main", _boom)
    with pytest.raises(errors.SshUnreachable):
        dispatch.main([])

    _assert_fatal_block_written(capsys, "SshUnreachable:")

    records = read_terminal_causes(exp, "run-e2e")
    assert len(records) == 1
    assert records[0]["recovery_kind"] == "dead_hop_route"
    assert records[0]["error_code"] == "ssh_unreachable"
    assert records[0]["exit_code"] == 3
    assert records[0]["remediation"] and "net-triage" in records[0]["remediation"]

    items = collect_worker_terminals(exp, now=_NOW)
    assert [item.kind for item in items] == [WORKER_TERMINAL]
    assert items[0].action == records[0]["remediation"]


def test_exit_path_journals_the_cause_on_a_nonzero_return(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No exception survives the ``rc != 0`` arm, so the log tail is the evidence."""
    import hpc_agent.cli.dispatch as dispatch

    exp, _log = _as_detached_worker(
        tmp_path,
        monkeypatch,
        log_body='{"ok": false, "failure_kind": "reporter_unreachable"}',
    )
    _mk_run(exp, "run-e2e")
    monkeypatch.setattr(dispatch, "_dispatch_main", lambda _argv: 1)

    assert dispatch.main([]) == 1
    _assert_fatal_block_written(capsys, "exit_code=1")

    records = read_terminal_causes(exp, "run-e2e")
    assert len(records) == 1
    assert records[0]["recovery_kind"] == "canary_reporter_unreachable"
    assert records[0]["exit_code"] == 1


def test_exit_path_journals_the_cause_on_a_nonzero_system_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ``SystemExit`` arm disclosed to the log but recorded NOTHING before."""
    import hpc_agent.cli.dispatch as dispatch

    exp, _log = _as_detached_worker(tmp_path, monkeypatch, log_body="[hb] alive 3s | no children")
    _mk_run(exp, "run-e2e")

    def _exit(_argv: object) -> int:
        raise SystemExit(2)

    monkeypatch.setattr(dispatch, "_dispatch_main", _exit)
    with pytest.raises(SystemExit):
        dispatch.main([])

    _assert_fatal_block_written(capsys, "SystemExit:")

    records = read_terminal_causes(exp, "run-e2e")
    assert len(records) == 1
    assert records[0]["exit_code"] == 2


def test_a_clean_exit_journals_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """rc == 0 is not a terminal death — no record, no attention item."""
    import hpc_agent.cli.dispatch as dispatch

    exp, _log = _as_detached_worker(tmp_path, monkeypatch, log_body="[hb] alive 3s | no children")
    _mk_run(exp, "run-e2e")
    monkeypatch.setattr(dispatch, "_dispatch_main", lambda _argv: 0)

    assert dispatch.main([]) == 0
    assert read_terminal_causes(exp, "run-e2e") == []
    assert collect_worker_terminals(exp, now=_NOW) == []


def test_foreground_cli_journals_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unmarked (no ``HPC_DETACHED_RUN_ID``) means the foreground CLI — stays silent."""
    import hpc_agent.cli.dispatch as dispatch

    exp = tmp_path / "exp"
    exp.mkdir()
    monkeypatch.delenv("HPC_DETACHED_RUN_ID", raising=False)
    monkeypatch.delenv("HPC_DETACHED_BLOCK", raising=False)
    monkeypatch.setenv("HPC_DETACH_HEARTBEAT_SEC", "0")
    monkeypatch.chdir(exp)
    _mk_run(exp, "run-e2e")
    monkeypatch.setattr(dispatch, "_dispatch_main", lambda _argv: 1)

    assert dispatch.main([]) == 1
    assert read_terminal_causes(exp, "run-e2e") == []
