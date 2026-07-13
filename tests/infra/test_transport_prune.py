"""Wiring tests for the bounded auto-prune of manifest-known remote extras.

Exercises :func:`hpc_agent.infra.transport._prune_manifest_known_extras`
end-to-end with the ssh legs monkeypatched, pinning the three ruling-6
properties at the transport seam:

* a manifest-known extra is journaled (what / why / old sha) AND deleted;
* an anomaly (not manifest-known) is NEVER deleted and is surfaced;
* an over-bound manifest-known set is refused wholesale (nothing deleted),
  with a journaled refusal.

The prune rides the delete=True delta push (already holding the dial), so these
tests stub the two ssh helpers it calls — the prior-manifest read and the delete
exec — and read the real journal file the prune writes under ``.hpc/``.
"""

from __future__ import annotations

import json
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
def capture_deletes(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record every ``_execute_prune`` call's path list; report success."""
    calls: list[list[str]] = []

    def _fake_execute(*, ssh_target: str, remote_path: str, paths: list[str], timeout):  # type: ignore[no-untyped-def]
        calls.append(list(paths))
        return True

    monkeypatch.setattr(transport, "_execute_prune", _fake_execute)
    return calls


def test_manifest_known_extra_is_journaled_and_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture_deletes: list[list[str]]
) -> None:
    # The prior push shipped ours/dropped.py; it is now a remote extra.
    monkeypatch.setattr(
        transport,
        "_read_prior_push_manifest",
        lambda **_: {"ours/dropped.py"},
    )
    remote = _remote_manifest([FileEntry(path="ours/dropped.py", size=42, sha256="oldsha123")])

    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        extra=("ours/dropped.py",),
        timeout=30,
    )

    # Deleted exactly the manifest-known path.
    assert capture_deletes == [["ours/dropped.py"]]
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
    monkeypatch: pytest.MonkeyPatch,
    capture_deletes: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Nothing manifest-known → the extra is a foreign anomaly.
    monkeypatch.setattr(transport, "_read_prior_push_manifest", lambda **_: set())
    remote = _remote_manifest([FileEntry(path="foreign/mystery.dat", size=7, sha256="x")])

    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        extra=("foreign/mystery.dat",),
        timeout=30,
    )

    # Never deleted.
    assert capture_deletes == []
    # No prune journal record (nothing was pruned).
    assert _journal_lines(tmp_path) == []
    # Surfaced to ask.
    err = capsys.readouterr().err
    assert "ANOMALY" in err
    assert "foreign/mystery.dat" in err


def test_over_bound_refuses_with_disclosure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_deletes: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = [f"ours/f{i}.py" for i in range(5)]
    monkeypatch.setattr(transport, "_read_prior_push_manifest", lambda **_: set(paths))
    monkeypatch.setenv("HPC_DEPLOY_PRUNE_MAX_FILES", "3")
    remote = _remote_manifest([FileEntry(path=p, size=1, sha256="s") for p in paths])

    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        extra=tuple(paths),
        timeout=30,
    )

    # Refused → nothing deleted.
    assert capture_deletes == []
    # Refusal journaled.
    lines = _journal_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0]["action"] == "prune-refused"
    assert lines[0]["manifest_known_count"] == 5
    # Disclosed.
    assert "REFUSED" in capsys.readouterr().err


def test_kill_switch_skips_prune_entirely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture_deletes: list[list[str]]
) -> None:
    monkeypatch.setenv("HPC_NO_DEPLOY_PRUNE", "1")
    monkeypatch.setattr(transport, "_read_prior_push_manifest", lambda **_: {"ours/dropped.py"})
    remote = _remote_manifest([FileEntry(path="ours/dropped.py", size=1, sha256="s")])

    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        extra=("ours/dropped.py",),
        timeout=30,
    )
    assert capture_deletes == []
    assert _journal_lines(tmp_path) == []


def test_own_push_manifest_is_never_an_anomaly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_deletes: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The .hpc/.push_manifest.json bookkeeping file is filtered out — neither
    pruned nor surfaced as an anomaly."""
    monkeypatch.setattr(transport, "_read_prior_push_manifest", lambda **_: set())
    remote = _remote_manifest([FileEntry(path=transport._PUSH_MANIFEST_REL, size=10, sha256="s")])

    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        extra=(transport._PUSH_MANIFEST_REL,),
        timeout=30,
    )

    assert capture_deletes == []
    assert _journal_lines(tmp_path) == []
    assert "ANOMALY" not in capsys.readouterr().err


# --- #F58: a refused/failed prune must retain the extra's manifest provenance ---


def test_refused_prune_retains_manifest_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#F58: a cap-REFUSED prune returns its manifest-known extras so the caller
    keeps them in the push manifest. Without this they downgrade to never-touched
    ANOMALYs on the very next push and the disclosed 'raise the cap and re-push'
    remediation can never work."""
    paths = [f"ours/f{i}.py" for i in range(5)]
    monkeypatch.setattr(transport, "_read_prior_push_manifest", lambda **_: set(paths))
    monkeypatch.setenv("HPC_DEPLOY_PRUNE_MAX_FILES", "3")  # 5 > 3 → refuse

    def _no_delete(**_):  # type: ignore[no-untyped-def]
        raise AssertionError("a refused plan must never delete")

    monkeypatch.setattr(transport, "_execute_prune", _no_delete)
    remote = _remote_manifest([FileEntry(path=p, size=1, sha256="s") for p in paths])

    retained = transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        extra=tuple(paths),
        timeout=30,
    )
    assert retained == set(paths)


def test_failed_delete_retains_manifest_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#F58: a manifest-known extra whose delete FAILS (fail-open ssh error →
    _execute_prune returns False) is still on the remote, so its provenance must
    be retained for the next push's prune retry."""
    monkeypatch.setattr(transport, "_read_prior_push_manifest", lambda **_: {"ours/dropped.py"})
    monkeypatch.setattr(transport, "_execute_prune", lambda **_: False)  # delete failed
    remote = _remote_manifest([FileEntry(path="ours/dropped.py", size=1, sha256="s")])

    retained = transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        extra=("ours/dropped.py",),
        timeout=30,
    )
    assert retained == {"ours/dropped.py"}


def test_confirmed_delete_drops_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A CONFIRMED delete removes the extra from the remote, so it must NOT be
    retained — keeping it would resurrect a phantom prunable on the next push."""
    monkeypatch.setattr(transport, "_read_prior_push_manifest", lambda **_: {"ours/dropped.py"})
    monkeypatch.setattr(transport, "_execute_prune", lambda **_: True)  # delete confirmed
    remote = _remote_manifest([FileEntry(path="ours/dropped.py", size=1, sha256="s")])

    retained = transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        extra=("ours/dropped.py",),
        timeout=30,
    )
    assert retained == set()


def test_rsync_push_writes_union_manifest_on_refused_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#F58 fire-path at the transport seam: when the delta push's prune is
    REFUSED, the push manifest rsync_push writes must include BOTH the current
    local paths AND the un-pruned manifest-known extra — so a re-push with a
    raised cap still classifies it prunable."""
    from unittest.mock import patch

    from hpc_agent.infra.manifest import build_manifest

    (tmp_path / "keep.py").write_text("code")
    # Remote holds keep.py (identical → nothing to ship) plus a manifest-known
    # extra we dropped locally.
    local_m = build_manifest(tmp_path)
    remote_m = Manifest(
        entries=(*local_m.entries, FileEntry(path="ours/dropped.py", size=10, sha256="oldsha"))
    )
    monkeypatch.setattr(transport, "_read_prior_push_manifest", lambda **_: {"ours/dropped.py"})
    monkeypatch.setenv("HPC_DEPLOY_PRUNE_MAX_FILES", "0")  # force REFUSE

    written: dict[str, list[str]] = {}

    def _capture_write(*, ssh_target, remote_path, paths, timeout):  # type: ignore[no-untyped-def]
        written["paths"] = list(paths)

    with (
        patch("hpc_agent.infra.transport.shutil.which", return_value=None),
        patch("hpc_agent.infra.transport._remote_push_manifest", return_value=remote_m),
        patch("hpc_agent.infra.transport._write_push_manifest", side_effect=_capture_write),
    ):
        result = transport.rsync_push(
            ssh_target="u@h", remote_path="/r", local_path=tmp_path, exclude=[], delete=True
        )

    assert result.returncode == 0
    assert "keep.py" in written["paths"]
    # Provenance retained despite the refusal — the un-pruned extra survives.
    assert "ours/dropped.py" in written["paths"]
