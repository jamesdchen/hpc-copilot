"""Wiring tests for the bounded auto-prune of manifest-known remote extras and
its folded trailing leg (delta-push round-trip Options 1 + 3 + 4).

Exercises :func:`hpc_agent.infra.transport._prune_manifest_known_extras`
end-to-end with the ssh legs monkeypatched, pinning the ruling-6 properties at
the transport seam:

* a manifest-known extra is journaled (what / why / old sha) AND pruned;
* an anomaly (not manifest-known) is NEVER deleted and is surfaced;
* an over-bound manifest-known set is refused wholesale (nothing deleted),
  with a journaled refusal;

PLUS the round-trip folds this unit adds:

* **Option 1** — the prior-manifest ``paths`` come in as ``known`` (folded into
  the remote hash read), so there is NO separate ``_read_prior_push_manifest``
  dial; the prune uses whatever ``known`` the caller threads through.
* **Option 4** — the prune step OWNS at most one trailing leg: when a prune
  actually fires, the ``rm`` + the retained-union reseal collapse into ONE
  ``_prune_and_reseal`` leg (fired only-when-extras); otherwise a standalone
  ``_write_push_manifest`` seal fires.
* **Option 3** — when the caller passes ``seal_folded=True`` (the last delta batch
  already rode the FINAL provisional seal on its tar leg), the no-retained
  standalone seal is SKIPPED (leg E absorbed); a non-empty retained set still
  writes, and the ``rm``+reseal tail always overwrites the provisional. Default
  ``seal_folded=False`` keeps the standalone seal (the direct-call tests below).

The prune rides the delete=True delta push (already holding the dial), so these
tests stub the trailing ssh legs — the combined prune+reseal tail and the
standalone seal — and read the real journal file the prune writes under
``.hpc/``. The retained-set semantics (a failed/confirmed delete) are computed
REMOTE-SIDE now, so they are pinned by running the real ``_PRUNE_RESEAL_PY``
script (bottom of file), not a client-side return value.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hpc_agent.infra import transport
from hpc_agent.infra.manifest import FileEntry, Manifest


def _remote_manifest(entries: list[FileEntry]) -> Manifest:
    return Manifest(entries=tuple(entries))


def _journal_lines(experiment: Path) -> list[dict]:
    path = experiment / ".hpc" / "deploy_prune.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture
def capture_legs(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Capture the trailing leg: the combined prune+reseal tail (Option 4) and
    the standalone seal. Exactly one of them fires per call (seal_folded default)."""
    reseal: list[dict] = []
    seal: list[list[str]] = []

    def _fake_reseal(*, ssh_target, remote_path, prune_paths, seal_paths, timeout):  # type: ignore[no-untyped-def]
        reseal.append({"prune": list(prune_paths), "seal": list(seal_paths)})

    def _fake_seal(*, ssh_target, remote_path, paths, timeout):  # type: ignore[no-untyped-def]
        seal.append(list(paths))

    monkeypatch.setattr(transport, "_prune_and_reseal", _fake_reseal)
    monkeypatch.setattr(transport, "_write_push_manifest", _fake_seal)
    return {"reseal": reseal, "seal": seal}


def test_manifest_known_extra_is_journaled_and_resealed(
    tmp_path: Path, capture_legs: dict[str, list]
) -> None:
    """A manifest-known extra is journaled (what / why / old sha) and pruned via
    the COMBINED prune+reseal tail (Option 4) — no separate standalone seal."""
    remote = _remote_manifest([FileEntry(path="ours/dropped.py", size=42, sha256="oldsha123")])

    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        known={"ours/dropped.py"},  # Option 1: folded, passed in directly
        extra=("ours/dropped.py",),
        seal_paths=["keep.py"],
        timeout=30,
    )

    # The combined tail fired with the manifest-known path + the local seal base;
    # the standalone seal did NOT (it is folded into the tail).
    assert capture_legs["reseal"] == [{"prune": ["ours/dropped.py"], "seal": ["keep.py"]}]
    assert capture_legs["seal"] == []
    # Journaled: what, why, old sha.
    lines = _journal_lines(tmp_path)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["action"] == "prune"
    assert rec["path"] == "ours/dropped.py"
    assert rec["reason"] == "manifest-known"
    assert rec["old_sha256"] == "oldsha123"
    assert rec["size"] == 42


def test_anomaly_is_never_pruned_and_surfaced(
    tmp_path: Path,
    capture_legs: dict[str, list],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Nothing manifest-known (``known`` empty) -> the extra is a foreign anomaly:
    NEVER pruned (no combined tail), surfaced, and the manifest is sealed by the
    standalone seal (only-when-NO-extras path)."""
    remote = _remote_manifest([FileEntry(path="foreign/mystery.dat", size=7, sha256="x")])

    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        known=set(),
        extra=("foreign/mystery.dat",),
        seal_paths=["keep.py"],
        timeout=30,
    )

    # Never pruned (no combined tail); the standalone seal wrote just the base.
    assert capture_legs["reseal"] == []
    assert capture_legs["seal"] == [["keep.py"]]
    # No prune journal record (nothing was pruned).
    assert _journal_lines(tmp_path) == []
    # Surfaced to ask.
    err = capsys.readouterr().err
    assert "ANOMALY" in err
    assert "foreign/mystery.dat" in err


def test_over_bound_refuses_with_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_legs: dict[str, list],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An over-bound manifest-known set is refused wholesale — nothing pruned (no
    combined tail) — and the standalone seal RETAINS every refused extra's
    provenance in the union (#F58)."""
    paths = [f"ours/f{i}.py" for i in range(5)]
    monkeypatch.setenv("HPC_DEPLOY_PRUNE_MAX_FILES", "3")
    remote = _remote_manifest([FileEntry(path=p, size=1, sha256="s") for p in paths])

    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        known=set(paths),
        extra=tuple(paths),
        seal_paths=["keep.py"],
        timeout=30,
    )

    # Refused -> nothing pruned; the standalone seal carries base ∪ all 5 refused.
    assert capture_legs["reseal"] == []
    assert capture_legs["seal"] == [sorted({"keep.py", *paths})]
    # Refusal journaled.
    lines = _journal_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0]["action"] == "prune-refused"
    assert lines[0]["manifest_known_count"] == 5
    # Disclosed.
    assert "REFUSED" in capsys.readouterr().err


def test_kill_switch_skips_prune_but_still_seals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture_legs: dict[str, list]
) -> None:
    """The kill-switch skips the prune entirely (no combined tail, no journal) but
    STILL seals the manifest with the current base (the seal is unconditional)."""
    monkeypatch.setenv("HPC_NO_DEPLOY_PRUNE", "1")
    remote = _remote_manifest([FileEntry(path="ours/dropped.py", size=1, sha256="s")])

    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        known={"ours/dropped.py"},
        extra=("ours/dropped.py",),
        seal_paths=["keep.py"],
        timeout=30,
    )
    assert capture_legs["reseal"] == []
    assert capture_legs["seal"] == [["keep.py"]]
    assert _journal_lines(tmp_path) == []


def test_own_push_manifest_is_never_an_anomaly(
    tmp_path: Path,
    capture_legs: dict[str, list],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The .hpc/.push_manifest.json bookkeeping file is filtered out — neither
    pruned nor surfaced as an anomaly — and the manifest is still sealed."""
    remote = _remote_manifest([FileEntry(path=transport._PUSH_MANIFEST_REL, size=10, sha256="s")])

    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        known=set(),
        extra=(transport._PUSH_MANIFEST_REL,),
        seal_paths=["keep.py"],
        timeout=30,
    )

    assert capture_legs["reseal"] == []
    assert capture_legs["seal"] == [["keep.py"]]
    assert _journal_lines(tmp_path) == []
    assert "ANOMALY" not in capsys.readouterr().err


def test_trailing_leg_fires_only_when_extras(tmp_path: Path, capture_legs: dict[str, list]) -> None:
    """Doctrine pin (Option 4, 'fires only-when-extras'): the combined prune+reseal
    tail fires IFF there is a manifest-known extra to delete; with none, only the
    standalone seal fires. Both directions in one place."""
    # (a) a manifest-known extra -> combined tail fires, no standalone seal.
    remote_a = _remote_manifest([FileEntry(path="ours/dropped.py", size=1, sha256="s")])
    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/r",
        local_path=tmp_path,
        remote_manifest=remote_a,
        known={"ours/dropped.py"},
        extra=("ours/dropped.py",),
        seal_paths=["keep.py"],
        timeout=30,
    )
    assert len(capture_legs["reseal"]) == 1 and capture_legs["seal"] == []

    # (b) nothing to prune -> standalone seal only, no combined tail.
    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/r",
        local_path=tmp_path,
        remote_manifest=_remote_manifest([]),
        known=set(),
        extra=(),
        seal_paths=["keep.py"],
        timeout=30,
    )
    assert len(capture_legs["reseal"]) == 1  # unchanged from (a)
    assert capture_legs["seal"] == [["keep.py"]]


def test_seal_folded_absorbs_the_standalone_seal_only_when_no_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture_legs: dict[str, list]
) -> None:
    """Option 3 seal-fold skip: with ``seal_folded=True`` (the last batch already
    rode the FINAL provisional seal on its tar leg), the no-retained standalone seal
    is ABSORBED (leg E fully gone); but a cap-refused RETAINED set — which the
    provisional did NOT carry — STILL writes a standalone seal so provenance is not
    lost. Both directions in one place."""
    # (a) no extras, seal_folded -> NEITHER a reseal tail NOR a standalone seal.
    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/r",
        local_path=tmp_path,
        remote_manifest=_remote_manifest([]),
        known=set(),
        extra=(),
        seal_paths=["keep.py"],
        timeout=30,
        seal_folded=True,
    )
    assert capture_legs["reseal"] == []
    assert capture_legs["seal"] == []  # leg E absorbed — the fold already sealed

    # (b) a cap-REFUSED manifest-known extra, seal_folded -> no reseal tail, but the
    # standalone seal STILL fires to carry the retained provenance beyond the
    # provisional (which only had the local base).
    monkeypatch.setenv("HPC_DEPLOY_PRUNE_MAX_FILES", "0")  # force REFUSE
    remote = _remote_manifest([FileEntry(path="ours/dropped.py", size=1, sha256="s")])
    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/r",
        local_path=tmp_path,
        remote_manifest=remote,
        known={"ours/dropped.py"},
        extra=("ours/dropped.py",),
        seal_paths=["keep.py"],
        timeout=30,
        seal_folded=True,
    )
    assert capture_legs["reseal"] == []  # refused -> nothing pruned
    assert capture_legs["seal"] == [sorted({"keep.py", "ours/dropped.py"})]  # retained union sealed


def test_seal_folded_still_fires_the_prune_reseal_tail(
    tmp_path: Path, capture_legs: dict[str, list]
) -> None:
    """Option 3 does NOT suppress the Option 4 tail: when a prune actually fires,
    the ``rm``+retained-union reseal leg runs regardless of ``seal_folded`` — it
    OVERWRITES the last batch's provisional seal with the authoritative union. No
    standalone seal (that is what the reseal replaces)."""
    remote = _remote_manifest([FileEntry(path="ours/dropped.py", size=1, sha256="s")])
    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/r",
        local_path=tmp_path,
        remote_manifest=remote,
        known={"ours/dropped.py"},
        extra=("ours/dropped.py",),
        seal_paths=["keep.py"],
        timeout=30,
        seal_folded=True,
    )
    assert capture_legs["reseal"] == [{"prune": ["ours/dropped.py"], "seal": ["keep.py"]}]
    assert capture_legs["seal"] == []  # the reseal tail is the authoritative seal


def test_delta_path_does_not_read_prior_manifest_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture_legs: dict[str, list]
) -> None:
    """Option 1 fire-path: the prune uses the folded ``known`` and NEVER calls the
    standalone ``_read_prior_push_manifest`` dial (its read is now part of leg A)."""

    def _boom(**_):  # type: ignore[no-untyped-def]
        raise AssertionError("_read_prior_push_manifest must not be dialed (Option 1)")

    monkeypatch.setattr(transport, "_read_prior_push_manifest", _boom)
    remote = _remote_manifest([FileEntry(path="ours/dropped.py", size=1, sha256="s")])
    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/r",
        local_path=tmp_path,
        remote_manifest=remote,
        known={"ours/dropped.py"},
        extra=("ours/dropped.py",),
        seal_paths=["keep.py"],
        timeout=30,
    )
    assert capture_legs["reseal"] == [{"prune": ["ours/dropped.py"], "seal": ["keep.py"]}]


# --- #F58: end-to-end refused prune retains provenance in the manifest ---------


def test_rsync_push_writes_union_manifest_on_refused_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#F58 fire-path at the transport seam: when the delta push's prune is
    REFUSED, the push manifest rsync_push writes must include BOTH the current
    local paths AND the un-pruned manifest-known extra — so a re-push with a
    raised cap still classifies it prunable. Option 1: the folded ``known`` arrives
    via ``_remote_push_manifest`` returning ``(manifest, known)`` — no separate
    prior read."""
    from unittest.mock import patch

    from hpc_agent.infra.manifest import build_manifest

    (tmp_path / "keep.py").write_text("code")
    # Remote holds keep.py (identical → nothing to ship) plus a manifest-known
    # extra we dropped locally.
    local_m = build_manifest(tmp_path)
    remote_m = Manifest(
        entries=(*local_m.entries, FileEntry(path="ours/dropped.py", size=10, sha256="oldsha"))
    )
    monkeypatch.setenv("HPC_DEPLOY_PRUNE_MAX_FILES", "0")  # force REFUSE

    written: dict[str, list[str]] = {}

    def _capture_write(*, ssh_target, remote_path, paths, timeout):  # type: ignore[no-untyped-def]
        written["paths"] = list(paths)

    def _no_reseal(**_):  # type: ignore[no-untyped-def]
        raise AssertionError("a refused plan must never fire the prune+reseal tail")

    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch(
            "hpc_agent.infra.transport._remote_push_manifest",
            return_value=(remote_m, {"ours/dropped.py"}),
        ),
        patch("hpc_agent.infra.transport._write_push_manifest", side_effect=_capture_write),
        patch("hpc_agent.infra.transport._prune_and_reseal", side_effect=_no_reseal),
    ):
        result = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )

    assert result.returncode == 0
    assert "keep.py" in written["paths"]
    # Provenance retained despite the refusal — the un-pruned extra survives.
    assert "ours/dropped.py" in written["paths"]


# --- Option 4: the combined prune+reseal leg (client + real remote script) -----


def test_prune_and_reseal_is_one_bounded_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_prune_and_reseal`` is ONE bounded ssh leg: base64-piped python (no raw
    shell ``rm``), the payload carries the prune + seal sets, and the live manifest
    is never a direct redirect target (temp + os.replace inside the script)."""
    seen: dict[str, str] = {}

    def _capture(_target, remote_cmd, **_kw):  # type: ignore[no-untyped-def]
        seen["cmd"] = remote_cmd
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(transport, "_ssh_bounded", _capture)
    transport._prune_and_reseal(
        ssh_target="u@h",
        remote_path="/r",
        prune_paths=["a/gone.py"],
        seal_paths=["keep.py"],
        timeout=5.0,
    )
    cmd = seen["cmd"]
    assert "base64 -d" in cmd
    assert "HPC_PM_PAYLOAD=" in cmd
    assert cmd.rstrip().endswith("python3")
    assert "rm -f -- " not in cmd  # no raw shell rm — the delete is os.remove in-script
    assert f"> {transport._PUSH_MANIFEST_REL}" not in cmd  # never a direct redirect
    # The reseal script itself removes, retains survivors, and is crash-safe.
    src = transport._prune._PRUNE_RESEAL_PY
    assert "os.remove(p)" in src
    assert "os.path.lexists(p)" in src  # survivor detection (retained set)
    assert "os.replace(t,d)" in src
    assert "new['entries']=cur['entries']" in src  # cache preserved


def test_prune_reseal_script_deletes_seals_and_retains_survivors(tmp_path: Path) -> None:
    """Run the REAL ``_PRUNE_RESEAL_PY`` under this interpreter (as the cluster
    runs it), cwd=remote tree. It must: delete a cleanly-removable prune path
    (dropping its provenance); RETAIN a path it could not remove (a directory ->
    ``os.remove`` fails -> fail-open, stays in the manifest); write the manifest as
    ``sorted(seal ∪ survivors)`` while PRESERVING the ``entries`` cache; and swap
    atomically (no ``.tmp`` left)."""
    tree = tmp_path / "tree"
    (tree / ".hpc").mkdir(parents=True)
    (tree / "keep.py").write_text("kept")
    (tree / "del_me.txt").write_text("deletable")  # a plain file -> os.remove succeeds
    (tree / "stubborn_dir").mkdir()  # os.remove(dir) raises OSError -> retained
    (tree / "stubborn_dir" / "x").write_text("blocks removal")
    prior_entries = [{"path": "keep.py", "size": 4, "mtime_ns": 1, "sha256": "deadbeef"}]
    (tree / ".hpc" / ".push_manifest.json").write_text(
        json.dumps(
            {"paths": ["old"], "pkg_version": "0.1", "manifest_schema": 2, "entries": prior_entries}
        )
    )

    script = tmp_path / "reseal.py"
    script.write_text(transport._prune._PRUNE_RESEAL_PY)
    payload = base64.b64encode(
        json.dumps(
            {
                "prune": ["del_me.txt", "stubborn_dir"],
                "seal": ["keep.py"],
                "pkg_version": "0.2",
                "manifest_schema": 2,
            }
        ).encode()
    ).decode()
    subprocess.run(
        [sys.executable, str(script)],
        cwd=str(tree),
        env={**os.environ, "HPC_PM_PAYLOAD": payload},
        check=True,
        timeout=30,
    )

    # Cleanly-removable file is gone; the un-removable dir survives.
    assert not (tree / "del_me.txt").exists()
    assert (tree / "stubborn_dir").exists()

    doc = json.loads((tree / ".hpc" / ".push_manifest.json").read_text())
    # Manifest paths = seal ∪ survivors: keep.py + the retained (undeletable) extra;
    # the cleanly-deleted del_me.txt dropped its provenance.
    assert doc["paths"] == sorted(["keep.py", "stubborn_dir"])
    assert doc["pkg_version"] == "0.2"
    assert doc["manifest_schema"] == 2
    assert doc["entries"] == prior_entries  # cache preserved verbatim
    assert not (tree / ".hpc" / ".push_manifest.json.tmp").exists()  # atomic swap
