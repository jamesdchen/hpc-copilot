"""Windows retry behaviour for :func:`hpc_agent.infra.io.atomic_write_json`.

Windows has no POSIX atomic rename-over-an-open-file: ``os.replace`` raises
``PermissionError`` ([WinError 5]) if the destination is momentarily held
open by another thread/process (a sharing violation). ``atomic_write_json``
retries the replace with a short backoff on ``win32`` only; everywhere else
the first failure propagates unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hpc_agent.infra import io


def test_retries_replace_on_windows_permission_error(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "doc.json"
    real_replace = io.os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("[WinError 5] Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(io.sys, "platform", "win32")
    monkeypatch.setattr(io.time, "sleep", lambda _s: None)
    monkeypatch.setattr(io.os, "replace", flaky_replace)

    io.atomic_write_json(path, {"ok": True})

    assert calls["n"] == 3
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
    assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_gives_up_after_bounded_retries_on_windows(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "doc.json"
    calls = {"n": 0}

    def always_fail(src, dst):
        calls["n"] += 1
        raise PermissionError("[WinError 5] Access is denied")

    monkeypatch.setattr(io.sys, "platform", "win32")
    monkeypatch.setattr(io.time, "sleep", lambda _s: None)
    monkeypatch.setattr(io.os, "replace", always_fail)

    with pytest.raises(PermissionError):
        io.atomic_write_json(path, {"ok": True})

    assert calls["n"] == 5
    # The failed write leaves no temp file behind.
    assert [p for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_does_not_retry_on_posix(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "doc.json"
    calls = {"n": 0}

    def fail_once(src, dst):
        calls["n"] += 1
        raise PermissionError("not a windows sharing violation")

    monkeypatch.setattr(io.sys, "platform", "linux")
    monkeypatch.setattr(io.os, "replace", fail_once)

    with pytest.raises(PermissionError):
        io.atomic_write_json(path, {"ok": True})

    # No retry loop off win32 — the first failure propagates.
    assert calls["n"] == 1
