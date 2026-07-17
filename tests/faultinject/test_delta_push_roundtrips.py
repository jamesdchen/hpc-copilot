"""Delta-push round-trip fault drills — the five owed by DELTA-PUSH-ROUNDTRIPS.md.

The rsync-less content-hash DELTA path (``transport/__init__.py`` delta caller +
``_delta.py`` + ``_prune.py``) is AUDIT Rank 4 (highest round-trip count anywhere
AND the live native-Windows push path). Its `§5 FALLBACK` consolidation
(Options 1 + 4) landed at 831e4a40; **Option 2 (the per-batch checkpoint fold into
``_tar_ssh_push``'s ``delete=False`` branch, ack-gated by ``__HPC_PUSH_CP_OK__``)
landed** on top (U4's stage-swap edit committed first); and **Option 3 (the
final-seal fold — the LAST batch's cumulative payload IS the final provisional
seal, so leg E folds into its tar leg) is now LANDED** — the consolidation is
complete (all of Options 1 + 2 + 3 + 4). These drills exercise the COMMITTED shape
and pin the four load-bearing invariants of `DELTA-PUSH-ROUNDTRIPS.md §2`:

1. a mid-op drop RESUMES from the live remote hash, never re-transfers landed data;
2. the prune stays FAIL-OPEN — a path is deleted only if PROVEN ours;
3. remote writes stay ATOMIC (temp + ``os.replace``);
4. house disciplines (positive-evidence, None-on-trouble).

Doctrine assertions only (durable / re-derive / ANOMALY / no-re-transfer /
rc≠0), per ``FAULT-HARNESS.md §5``. The drills map to the memo's owed list
(`§4`), and cover AUDIT §7 row 8 ("kill ssh mid-``tar|ssh`` push") — moving it
from FAULT-HARNESS §4 (needed) to §2 (covered).

Every consolidation option is now landed and correct, so none of these are
``xfail``; a folded write's absent ack (a checkpoint OR the final seal) is a
fail-open bookkeeping lag the next push re-derives, never a correctness hole.
Contrast the stage-swap drill's strict-xfail, which pins a genuinely OPEN
torn-window gap.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from hpc_agent.infra import transport
from hpc_agent.infra.manifest import FileEntry, Manifest
from hpc_agent.infra.prune import plan_prune
from hpc_agent.infra.transport import _delta, _prune

from .conftest import proc

# Module-global seam targets (resolved from the transport package namespace, so a
# call-time ``from hpc_agent.infra.transport import _ssh_bounded`` inside the
# private submodules picks up the patch — the same surface the infra unit tests
# and the pull-pump drill patch).
_SSH_BOUNDED = "hpc_agent.infra.transport._ssh_bounded"
_REMOTE_MANIFEST = "hpc_agent.infra.transport._remote_push_manifest"
_PUSH_PUMP = "hpc_agent.infra.transport._pump_with_progress"


def _remote_from(state: dict[str, bytes]) -> Manifest:
    """A REMOTE :class:`Manifest` from a fake remote tree (path -> content bytes)."""
    import hashlib

    entries = tuple(
        FileEntry(path=p, size=len(c), sha256=hashlib.sha256(c).hexdigest())
        for p, c in state.items()
    )
    return Manifest(entries=tuple(sorted(entries, key=lambda e: e.path)))


# ===========================================================================
# Drill 1 — sever mid folded-manifest READ (owed #3; Option 1, LANDED)
# ===========================================================================
# AUDIT §7 row A: the remote hash read now ALSO folds the prior push-manifest
# ``paths`` (Option 1). A severed read must still collapse to (None, set()) so
# the push takes the full-copy fallback — the added field cannot change the
# None-on-trouble contract, and a severed read can NEVER hand the prune a
# partial ``known`` set (Invariant 1 + 4).


def test_sever_mid_manifest_read_yields_none_and_empty_known(sever_at) -> None:
    """A severed folded read -> ``(None, set())``. NEVER a manifest with a
    partial ``known`` a prune could act on: the ``(TimeoutError, OSError)`` guard
    in ``_remote_push_manifest`` swallows the sever and returns the None-fallback
    sentinel. (``ConnectionError`` is an ``OSError`` subclass, so a dropped socket
    lands here too.)"""
    sever_at(_SSH_BOUNDED, exc=TimeoutError, message="read severed mid-manifest")
    result = _delta._remote_push_manifest(
        ssh_target="u@h", remote_path="/r", exclude=[], timeout=5.0
    )
    assert result == (None, set())  # DOCTRINE: None-on-trouble, empty known


def test_none_manifest_routes_to_full_copy_never_a_prune(tmp_path) -> None:
    """The caller consequence of the severed read: a ``None`` remote manifest
    routes ``rsync_push`` to the WHOLE-TREE full-copy tar (``only_paths`` absent)
    and NEVER derives a prune plan — there is no partial-known deletion window."""
    (tmp_path / "a.txt").write_text("payload")

    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        # The severed-read outcome, injected at the caller boundary.
        patch(_REMOTE_MANIFEST, return_value=(None, set())),
        patch("hpc_agent.infra.transport.guarded_call", side_effect=lambda _t, fn: fn()),
        patch("hpc_agent.infra.transport._tar_ssh_push", return_value=proc(0)) as tar,
        patch("hpc_agent.infra.transport._prune_manifest_known_extras") as prune,
    ):
        transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )

    assert prune.call_count == 0  # DOCTRINE: no manifest -> no prune plan at all
    assert tar.call_count == 1
    # Full-copy tar: the whole tree, NOT a delta batch (no ``only_paths``).
    assert "only_paths" not in tar.call_args.kwargs


# ===========================================================================
# Drill 2 — sever mid-BATCH (owed #5; resume pin at drill level)
# ===========================================================================
# AUDIT §7 row 8 (resume half): a batch that dies leaves EARLIER batches durable
# (checkpointed), leaves the remote otherwise as-is (no prune, no final seal),
# and the retry re-derives the delta from the LIVE remote hash — shipping ONLY
# the remainder (Invariant 1, run-13 finding 3).
#
# Options 2+3 (LANDED): EVERY batch's manifest write now RIDES its tar leg — a
# ``checkpoint_payload_b64`` folded into ``_tar_ssh_push`` (no separate
# ``_write_push_manifest`` dial), ack-gated by ``__HPC_PUSH_CP_OK__``. A non-last
# batch folds a mid-ship CHECKPOINT (Option 2); the LAST folds the FINAL provisional
# SEAL (Option 3). This drill asserts the ack governs "landed vs written": the fold
# rides the leg, and ``tar x``'s rc — not the folded write — decides batch failure.
# The RESUME behavior is fully present and correct regardless.


def test_died_mid_batch_lands_prior_durably_and_leaves_remote_untouched(
    tmp_path, monkeypatch
) -> None:
    """Attempt 1 dies shipping batch ``c`` (after ``a``, ``b`` land): the failed
    push returns rc≠0, the two landed batches are durable, and NEITHER the prune
    NOR the final seal fires (the remote is left as-is for a clean retry). EVERY
    batch carries a FOLDED write on its tar leg — the non-last a checkpoint
    (Option 2), the last the final seal (Option 3)."""
    names = ["a", "b", "c", "d", "e"]
    for n in names:
        (tmp_path / n).write_text(f"body-of-{n}")
    monkeypatch.setenv("HPC_DELTA_BATCH_MAX_FILES", "1")  # 1 file/batch -> 5 batches

    fake_remote: dict[str, bytes] = {}
    state = {"fail_path": "c"}
    shipped: list[str] = []
    folds: list[tuple[str, bool]] = []  # (batch head, carried a folded checkpoint?)

    def _fake_tar(*, only_paths, checkpoint_payload_b64=None, **_kw):  # noqa: ANN001, ANN002
        # Options 2+3: record whether this batch rode a folded manifest write on its
        # leg (a checkpoint for a non-last batch, the final seal for the last).
        folds.append((only_paths[0], checkpoint_payload_b64 is not None))
        # A severed tar|ssh folds to rc≠0 (Drill 5 proves that fold); consume it
        # here as the batch-death shape and prove the RESUME doctrine. The
        # checkpoint is &&-gated on tar x, so a batch death never records one.
        for p in only_paths:
            if p == state["fail_path"]:
                state["fail_path"] = None
                return proc(1, stderr="batch severed mid-stream")
            fake_remote[p] = (tmp_path / p).read_bytes()  # batch landed durably
            shipped.append(p)
        return proc(0)

    common = [
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.guarded_call", side_effect=lambda _t, fn: fn()),
        patch("hpc_agent.infra.transport._tar_ssh_push", side_effect=_fake_tar),
        patch("hpc_agent.infra.transport._prune_manifest_known_extras"),
        # Option 2 removed the mid-ship _write_push_manifest dial; keep it patched
        # so a REGRESSION that re-added one would surface as an unexpected call.
        patch("hpc_agent.infra.transport._write_push_manifest"),
        # The retry's delta reads the LIVE remote hash (Option 1: (manifest, known)).
        patch(_REMOTE_MANIFEST, side_effect=lambda **_kw: (_remote_from(fake_remote), set())),
    ]

    with common[0], common[1], common[2], common[3] as prune, common[4] as seal, common[5]:
        r1 = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
        assert r1.returncode != 0
        assert set(fake_remote) == {"a", "b"}  # only confirmed-landed batches
        assert shipped == ["a", "b"]
        # DOCTRINE: a died batch leaves the remote as-is — no prune, no final seal.
        assert prune.call_count == 0
        # Option 2: the mid-ship checkpoints rode the tar legs — NO standalone
        # _write_push_manifest dial fired (a re-split regression would bump this).
        assert seal.call_count == 0
        # a, b, c were attempted; each is a non-last batch (of 5), so each carried
        # a folded checkpoint on its tar leg (the ack rides the batch dial).
        assert folds == [("a", True), ("b", True), ("c", True)]
        # (The push died at c, so d/e were never reached; whether e would fold is
        # asserted on the clean retry below.)

    shipped.clear()
    folds.clear()
    with common[0], common[1], common[2], common[3] as prune2, common[4], common[5]:
        r2 = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
        assert r2.returncode == 0
        assert shipped == ["c", "d", "e"]  # ONLY the remainder — never a, b again
        assert set(fake_remote) == set(names)  # tree now complete
        assert prune2.call_count == 1  # a clean push seals + prunes exactly once
        # The clean 3-batch retry: c, d fold a mid-ship checkpoint; e (the LAST) now
        # folds the FINAL provisional seal (Option 3) — so ALL three carry a fold.
        assert folds == [("c", True), ("d", True), ("e", True)]


# ===========================================================================
# Drill 2b — sentinel ABSENT after a landed batch (owed #1; Options 2+3, LANDED)
# ===========================================================================
# The load-bearing fold case (memo §3 Options 2+3, drop after ``tar x`` before
# ``__HPC_PUSH_CP_OK__``): a batch DID land (``tar x`` rc 0 authoritative) but its
# folded write did NOT ack — a mid-ship checkpoint (Option 2) OR the LAST batch's
# final seal (Option 3). The ack's absence must NEVER read as a batch failure —
# the batch stays durable, the push proceeds through every batch, and only the
# manifest bookkeeping lags (fail-open, Invariant 2), which the next push
# re-derives. Here EVERY batch (including the last, whose fold is the final seal)
# returns an absent ack, so this also exercises the Option-3 final-seal ack-lag.


def test_sentinel_absence_after_landed_batch_is_not_a_failure(tmp_path, monkeypatch) -> None:
    """A batch that lands (rc 0) with the folded-write ack ABSENT (drop after
    ``tar x`` before ``__HPC_PUSH_CP_OK__``) is treated as LANDED + durable, not a
    failure: the push proceeds through every batch — including the LAST, whose fold
    is the final seal (Option 3) — and completes. The sentinel governs
    write-committed; ``tar x``'s rc governs batch-landed — orthogonal signals.
    Contrast an rc≠0 batch, which DOES stop the push (Drill 2)."""
    names = ["a", "b", "c"]
    for n in names:
        (tmp_path / n).write_text(f"body-of-{n}")
    monkeypatch.setenv("HPC_DELTA_BATCH_MAX_FILES", "1")  # 1 file/batch -> 3 batches

    fake_remote: dict[str, bytes] = {}
    shipped: list[str] = []

    def _fake_tar(*, only_paths, checkpoint_payload_b64=None, **_kw):  # noqa: ANN001, ANN002
        # Every batch LANDS (rc 0), but the leg returns EMPTY stdout — the
        # sentinel is ABSENT (the drop-after-tar-x-before-ack shape). The batch is
        # durable regardless; the client must not read the missing ack as failure.
        for p in only_paths:
            fake_remote[p] = (tmp_path / p).read_bytes()
            shipped.append(p)
        return proc(0, stdout="")  # rc 0, NO __HPC_PUSH_CP_OK__

    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.guarded_call", side_effect=lambda _t, fn: fn()),
        patch("hpc_agent.infra.transport._tar_ssh_push", side_effect=_fake_tar),
        patch("hpc_agent.infra.transport._prune_manifest_known_extras") as prune,
        patch(_REMOTE_MANIFEST, side_effect=lambda **_kw: (_remote_from(fake_remote), set())),
    ):
        result = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
    # DOCTRINE: an absent ack after a landed batch is NOT a failure.
    assert result.returncode == 0
    assert shipped == names  # every batch shipped — the push never stopped
    assert set(fake_remote) == set(names)  # tree complete + durable
    assert prune.call_count == 1  # the push completed to its single trailing seal


# ===========================================================================
# Drill 3 — sever mid trailing PRUNE-RESEAL (owed #4; Option 4, LANDED)
# ===========================================================================
# AUDIT §7 row D: the ``rm`` + retained-union manifest seal fold into ONE trailing
# ``_prune_and_reseal`` leg. A severed leg is FAIL-OPEN — the push is never
# broken (Invariant 2), and the remote-side script is atomic (temp + os.replace)
# so a kill mid-write can never corrupt the live manifest (Invariant 3).


def test_sever_mid_prune_reseal_is_fail_open(sever_at) -> None:
    """A severed prune-reseal leg is a SKIPPED prune, not a broken push:
    ``_prune_and_reseal`` swallows the ``(TimeoutError, OSError)`` and returns
    without raising. The extras simply stay (degrade to next-push anomalies);
    nothing is left half-done on the control-plane side."""
    sever_at(_SSH_BOUNDED, exc=TimeoutError, message="prune-reseal leg severed")
    # No exception escapes — fail-open (a prune we cannot do cleanly is skipped).
    # The call itself returning is the assertion: a raise would fail the test.
    _prune._prune_and_reseal(
        ssh_target="u@h",
        remote_path="/r",
        prune_paths=["data/old.bin"],
        seal_paths=["a.txt"],
        timeout=5.0,
    )


def test_prune_reseal_script_is_atomic_and_retains_remote_side() -> None:
    """The trailing script (:data:`_PRUNE_RESEAL_PY`) writes a temp sibling then
    ``os.replace``-s it into place (atomic under a kill — Invariant 3), and
    computes the RETAINED survivors REMOTE-SIDE via ``os.path.lexists`` — so a
    delete the ``rm`` could not perform stays ours (fail-open per-path,
    Invariant 2), never a manifest that forgets a still-present extra."""
    reseal = _prune._PRUNE_RESEAL_PY
    assert "t=d+'.tmp'" in reseal  # writes to a temp sibling first
    assert "os.replace(t,d)" in reseal  # atomic swap into the live path
    assert "os.path.lexists(p)" in reseal  # retained computed remote-side, per-path
    # A live-manifest direct redirect (the non-atomic shape) never appears.
    assert ">d" not in reseal and "> d" not in reseal


# ===========================================================================
# Drill 4 — GARBLED / truncated folded-paths JSON (owed #2; Option 1, LANDED)
# ===========================================================================
# AUDIT §7 row B: the read now carries the prune ``paths``. A garbled ``paths``
# (present, wrong shape) must degrade to ``known = ∅`` so EVERY remote extra
# routes to ANOMALY (never deleted) — a wrong deletion is the load-bearing
# failure the fail-open prune forbids (Invariant 2). A truncated STREAM
# (unparseable JSON) collapses to (None, set()) — full-copy, no prune at all.


def test_garbled_folded_paths_degrades_to_empty_known_no_wrong_deletion(garble_at) -> None:
    """A rc-0 read with valid ``files`` but a GARBLED ``paths`` (wrong shape)
    yields ``known = ∅``; feeding the extras to ``plan_prune`` then routes every
    one to ANOMALY (``to_prune`` empty) — never a wrong deletion."""
    garbled = json.dumps(
        {
            "files": [{"path": "a.txt", "size": 1, "sha256": "aa"}],
            "paths": {"not": "a list"},  # present, wrong shape
        }
    )
    garble_at(_SSH_BOUNDED, return_value=proc(0, stdout=garbled))
    manifest, known = _delta._remote_push_manifest(
        ssh_target="u@h", remote_path="/r", exclude=[], timeout=5.0
    )
    assert manifest is not None  # the manifest still parses from ``files``
    assert known == set()  # DOCTRINE: garbled paths -> empty known (fail-open)

    # With an empty ``known`` set, a remote extra is an ANOMALY, never prunable.
    extras = [FileEntry(path="data/old.bin", size=10, sha256="dd")]
    plan = plan_prune(extras, known, max_files=100, max_bytes=10**9)
    assert plan.to_prune == ()  # DOCTRINE: never a wrong deletion
    assert plan.anomalies == ("data/old.bin",)  # surfaced to ask, not deleted


def test_truncated_manifest_stream_collapses_to_none(garble_at) -> None:
    """A truncated stream (unparseable JSON) -> ``(None, set())``: full-copy
    fallback, no delta and no prune plan at all (Invariant 1 + 4)."""
    garble_at(_SSH_BOUNDED, return_value=proc(0, stdout='{"files": [{"path": "a.txt"'))
    assert _delta._remote_push_manifest(
        ssh_target="u@h", remote_path="/r", exclude=[], timeout=5.0
    ) == (None, set())


# ===========================================================================
# Drill 5 — PUSH-PUMP sever (owed #1 core; AUDIT §7 row 8, NEWLY COVERED)
# ===========================================================================
# AUDIT §7 row 8 ("kill ssh mid-``tar|ssh`` push") — the push pump is symmetric to
# the pull pump (``test_pull_pump_sever_forces_nonzero_rc``) but its ``_attempt``
# was not import-isolated. Using the same Popen / ``run_capture_bounded`` patch
# surface the memo names, we mirror the pull drill: a pump break must fold to
# rc≠0 even when BOTH pipe halves report 0, so a truncated push is REFUSED, never
# trusted as success (the transfer-plane's positive-evidence-of-completion
# contract, AUDIT §2/§8). This moves the row from FAULT-HARNESS §4 to §2.
#
# Option 2 (LANDED): the ack-gated "sever after ``tar x`` before
# ``__HPC_PUSH_CP_OK__``" variant (owed #1) is now covered by Drill 2b
# (sentinel-absent-after-landed-batch). The pump-fold rc≠0 pinned HERE is the
# distinct "the transfer stream itself tore" case (a truncated push is refused as
# rc≠0) and is independent of the checkpoint fold.


def _fake_tar_popen(returncode: int = 0) -> MagicMock:
    """Stand-in for the ``tar c`` ``Popen`` — no real tar spawned."""
    m = MagicMock(name="tar_proc")
    m.returncode = returncode
    m.stdout = MagicMock(name="tar_stdout")  # truthy; the code closes it
    m.wait.return_value = returncode
    return m


def test_push_pump_sever_forces_nonzero_rc(tmp_path) -> None:
    """A severed push pump folds to rc≠0 with a disclosed 'pump error' even though
    the faked ``tar`` and ``ssh`` halves both report 0 — a truncated push is not
    success. (Pure-local: no ssh/tar spawned.)"""
    with (
        # tar half (Popen) + ssh half (run_capture_bounded) both "succeed"...
        patch("hpc_agent.infra.transport.subprocess.Popen", return_value=_fake_tar_popen(0)),
        patch("hpc_agent.infra.transport.run_capture_bounded", return_value=proc(0)),
        patch("hpc_agent.infra.transport.run_with_named_pipe_retry", side_effect=lambda fn: fn()),
        # ...but the byte pump between them breaks mid-stream.
        patch(_PUSH_PUMP, side_effect=ConnectionError("peer reset mid-push")),
    ):
        result = transport._tar_ssh_push(
            ssh_target="u@h",
            remote_path="/r",
            local_path=tmp_path,
            exclude=[],
            delete=False,  # additive extract — no stage-swap tail to fake
            timeout=5.0,
            total_bytes=1_000,
        )
    assert result.returncode != 0  # DOCTRINE: truncated push stream is not success
    assert "pump error" in result.stderr


# ===========================================================================
# Drill 6 — last-batch SEAL fold ack absent, then re-seal (owed: Option 3)
# ===========================================================================
# The Option-3 load-bearing case (memo §3 Option 3): the LAST batch lands (``tar x``
# rc 0) but the FINAL provisional seal it folded onto its leg does NOT ack (drop
# after ``tar x`` before ``__HPC_PUSH_CP_OK__``). "Landed" and "sealed" are
# orthogonal — the tree is durable; only the manifest reseal lagged. The NEXT push
# re-derives the (now-empty) delta from the LIVE remote hash, ships NOTHING (no
# re-transfer), and re-seals via the standalone leg. This is the seal-fold analogue
# of Drill 2b's checkpoint case, carried through to the recovering second push.


def test_last_batch_seal_ack_absent_durable_then_reseals_without_retransfer(
    tmp_path, monkeypatch
) -> None:
    """Push 1: the sole (LAST) batch lands with its final-seal ack ABSENT — rc 0,
    tree durable, NO standalone seal fires (the fold owns it, Option 3). Push 2: the
    live remote hash shows the file present -> delta EMPTY -> zero batches shipped
    (no re-transfer), and the standalone seal (``seal_folded=False`` on an empty
    ship) re-seals the manifest. Landed vs sealed stay orthogonal; the lagged seal
    self-heals on the next push."""
    (tmp_path / "only.txt").write_text("the-body")
    monkeypatch.setenv("HPC_DELTA_BATCH_MAX_FILES", "1")  # 1 file -> 1 (last) batch

    fake_remote: dict[str, bytes] = {}
    shipped: list[str] = []
    seals: list[list[str]] = []

    def _fake_tar(*, only_paths, checkpoint_payload_b64=None, **_kw):  # noqa: ANN001, ANN002
        # The last batch lands durably but returns EMPTY stdout — its folded FINAL
        # seal never acks (the drop-after-tar-x-before-seal-ack shape). The payload
        # IS present (Option 3 folds the seal), proving the fold rode the leg.
        for p in only_paths:
            fake_remote[p] = (tmp_path / p).read_bytes()
            shipped.append(p)
        return proc(0, stdout="")  # rc 0, NO __HPC_PUSH_CP_OK__

    def _record_seal(*, paths, **_kw):  # noqa: ANN001, ANN002
        seals.append(list(paths))

    common = [
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport.guarded_call", side_effect=lambda _t, fn: fn()),
        patch("hpc_agent.infra.transport._tar_ssh_push", side_effect=_fake_tar),
        # The empty remote (push 1) / present remote (push 2) has no extras, so the
        # prune runs to its seal step; capture the standalone seal directly.
        patch("hpc_agent.infra.transport._write_push_manifest", side_effect=_record_seal),
        patch(_REMOTE_MANIFEST, side_effect=lambda **_kw: (_remote_from(fake_remote), set())),
    ]

    # Push 1: ships the one file; batch lands; seal ack absent.
    with common[0], common[1], common[2], common[3], common[4]:
        r1 = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
    assert r1.returncode == 0  # DOCTRINE: the batch landed — an absent seal ack is not a failure
    assert shipped == ["only.txt"]
    assert set(fake_remote) == {"only.txt"}  # tree durable
    # Option 3: the final seal rode the batch leg — NO standalone seal fired on push 1
    # (its ack merely lagged; no corrective dial).
    assert seals == []

    shipped.clear()
    # Push 2: the live remote hash shows the file -> delta empty -> nothing ships.
    with common[0], common[1], common[2], common[3], common[4]:
        r2 = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )
    assert r2.returncode == 0
    assert shipped == []  # DOCTRINE: no re-transfer — the file was already durable
    # The lagged seal self-heals: an empty ship folds nothing, so the standalone
    # seal (seal_folded=False) re-seals the manifest exactly once.
    assert seals == [["only.txt"]]
