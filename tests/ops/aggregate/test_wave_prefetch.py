"""Wave-incremental harvest prefetch — the aggregate-side seam.

``prefetch_wave_partials`` is the OPPORTUNISTIC mid-flight cache the monitor
watch fills after each combine burst (one pull per burst, never per poll); the
terminal harvest's ``_combiner/`` pull remains the AUTHORITY — it re-runs the
SAME pull over the SAME destination, and the content-hash engine re-verifies
every prefetched file against the cluster's sha256 manifest (a mismatch is
re-pulled; the transport-level fire for that leg is
``tests/infra/test_transport_pull.py::test_delta_pulls_exactly_the_changed_set``,
and the identical-serves-without-transfer leg is
``::test_delta_nothing_to_pull_when_all_identical``).

This module pins:

* the prefetch pull SHAPE — the same ``_combiner`` subdir, the same
  ``_WAVE_PARTIAL_INCLUDE`` two-glob filter, and the DEFAULT harvest
  destination, so the cache is exactly the file set the harvest re-verifies;
* the best-effort contract — every failure is returned as disclosed data,
  never raised into the watch;
* the harvest disclosure — the ``_combiner`` pull says how many files were
  served from the prefetch/delta cache vs pulled fresh, so the cache is never
  silent;
* final-harvest equivalence — the reduce is byte-identical with a warm
  prefetch cache (even a CORRUPTED one, given the transport contract that a
  changed file is re-transferred) and with a cold one.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent.ops import aggregate_flow as af_module

_RUN_ID = "20260729-100000-pref"


def _record(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "run_id": _RUN_ID,
        "cluster": "no-such-cluster-key",  # resolve_ssh_target falls back to ssh_target
        "ssh_target": "user@host",
        "remote_path": "/remote/exp",
        "combined_waves": [],
        "total_tasks": 4,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _pull_recorder(monkeypatch: pytest.MonkeyPatch, result: Any) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake(**kw: Any) -> Any:
        calls.append(kw)
        return result

    monkeypatch.setattr(af_module, "_pull", _fake)
    return calls


def _outcome(**overrides: Any) -> af_module._PullOutcome:
    base: dict[str, Any] = {
        "returncode": 0,
        "stderr": "",
        "files_pulled": 0,
        "bytes_pulled": 0,
        "skipped_unchanged": 0,
    }
    base.update(overrides)
    return af_module._PullOutcome(**base)


# ---------------------------------------------------------------------------
# prefetch_wave_partials — pull shape + best-effort contract
# ---------------------------------------------------------------------------


def test_prefetch_pulls_the_harvest_shape_into_the_default_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prefetch pull is the SAME (subdir, include, destination) triple the
    terminal harvest pulls — anything else would leave the harvest's delta
    blind to part of the cache."""
    calls = _pull_recorder(
        monkeypatch, _outcome(files_pulled=2, bytes_pulled=2048, skipped_unchanged=1)
    )

    res = af_module.prefetch_wave_partials(tmp_path, _RUN_ID, record=_record())

    assert len(calls) == 1
    kw = calls[0]
    assert kw["remote_subdir"] == "_combiner"
    assert kw["include"] == list(af_module._WAVE_PARTIAL_INCLUDE)
    assert kw["local_dir"] == str(tmp_path / "_aggregated" / _RUN_ID / "_combiner")
    assert kw["remote_path"] == "/remote/exp"
    assert res == {
        "ok": True,
        "files_pulled": 2,
        "bytes_pulled": 2048,
        "skipped_unchanged": 1,
        "dir": str(tmp_path / "_aggregated" / _RUN_ID / "_combiner"),
    }


def test_prefetch_include_is_the_one_harvest_definition() -> None:
    """One definition: the prefetch filter IS the F08/F09 two-glob filter the
    incremental harvest pull emits — a drift here silently splits the cache
    from the file set the harvest re-verifies."""
    local = Path("nonexistent-dir-shape-only")
    # The helper only returns the globs when local wave files exist; assert the
    # CONSTANT is the two-glob shape the incremental-pull tests pin.
    assert list(af_module._WAVE_PARTIAL_INCLUDE) == ["wave_*.json", "wave_*.runtime.json"]
    assert af_module._incremental_include_patterns(local, []) is None


def test_prefetch_env_opt_out_returns_none_and_pulls_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _pull_recorder(monkeypatch, _outcome())
    monkeypatch.setenv(af_module.WAVE_PREFETCH_ENV, "0")

    assert af_module.prefetch_wave_partials(tmp_path, _RUN_ID, record=_record()) is None
    assert calls == []


def test_prefetch_transport_failure_is_disclosed_data_not_a_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero pull returns ``ok=False`` + a bounded stderr tail; the watch
    records it on the tick action row and keeps polling."""
    _pull_recorder(monkeypatch, _outcome(returncode=1, stderr="rsync: connection closed"))

    res = af_module.prefetch_wave_partials(tmp_path, _RUN_ID, record=_record())

    assert res is not None
    assert res["ok"] is False
    assert "connection closed" in res["error"]


def test_prefetch_transport_exception_is_swallowed_and_disclosed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**_kw: Any) -> Any:
        raise errors.SshUnreachable("host down")

    monkeypatch.setattr(af_module, "_pull", _boom)

    res = af_module.prefetch_wave_partials(tmp_path, _RUN_ID, record=_record())

    assert res is not None
    assert res["ok"] is False
    assert "host down" in res["error"]


# ---------------------------------------------------------------------------
# Harvest disclosure — the cache is never silent
# ---------------------------------------------------------------------------


def _write_wave(combiner_dir: Path, wave: int, grid_points: dict[str, Any]) -> None:
    combiner_dir.mkdir(parents=True, exist_ok=True)
    (combiner_dir / f"wave_{wave}.json").write_text(
        json.dumps({"wave": wave, "run_id": _RUN_ID, "grid_points": grid_points}),
        encoding="utf-8",
    )


def test_harvest_discloses_prefetch_served_vs_fresh_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The terminal ``_combiner`` pull names how many files the local
    prefetch/delta cache served (content-verified unchanged) vs pulled fresh —
    a cache hit never masquerades as a fresh pull."""
    out = tmp_path / "_aggregated" / _RUN_ID
    combiner_local = out / "_combiner"

    def _fake_pull(**kw: Any) -> Any:
        _write_wave(Path(kw["local_dir"]), 1, {"g0": {"acc": 1.0, "n_samples": 2}})
        return _outcome(files_pulled=1, bytes_pulled=10, skipped_unchanged=3)

    monkeypatch.setattr(af_module, "_pull", _fake_pull)

    aggregated, incomplete, source = af_module._combiner_only_reduce(
        tmp_path,
        _RUN_ID,
        record=_record(),
        combiner_local=combiner_local,
        summary_name="metrics.json",
        out=out,
        has_wave_map=True,
    )

    assert source == "local_reduce"
    assert aggregated  # the pulled wave reduced
    line = capsys.readouterr().out
    assert "3 file(s) served from the local prefetch/delta cache" in line
    assert "1 pulled fresh (10 bytes)" in line


def test_harvest_disclosure_silent_when_counts_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The legacy rsync ``CompletedProcess`` carries no delta counts — the
    disclosure line is skipped rather than fabricated."""
    import subprocess

    out = tmp_path / "_aggregated" / _RUN_ID
    combiner_local = out / "_combiner"

    def _fake_pull(**kw: Any) -> Any:
        _write_wave(Path(kw["local_dir"]), 0, {"g0": {"acc": 1.0, "n_samples": 2}})
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(af_module, "_pull", _fake_pull)

    af_module._combiner_only_reduce(
        tmp_path,
        _RUN_ID,
        record=_record(),
        combiner_local=combiner_local,
        summary_name="metrics.json",
        out=out,
        has_wave_map=True,
    )

    assert "prefetch/delta cache" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Final-harvest equivalence — prefetch (even corrupted) changes nothing
# ---------------------------------------------------------------------------


def _copying_pull(remote_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``_pull`` honoring the transport contract: after the call, every remote
    file is present locally with the remote's bytes (the content-hash engine
    re-transfers any local file whose sha diverged — pinned at transport level
    by ``test_delta_pulls_exactly_the_changed_set``)."""

    def _fake(**kw: Any) -> Any:
        dst = Path(kw["local_dir"])
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(remote_dir, dst, dirs_exist_ok=True)
        return _outcome(files_pulled=2)

    monkeypatch.setattr(af_module, "_pull", _fake)


def _reduce(exp: Path) -> dict[str, Any]:
    out = exp / "_aggregated" / _RUN_ID
    aggregated, incomplete, source = af_module._combiner_only_reduce(
        exp,
        _RUN_ID,
        record=_record(),
        combiner_local=out / "_combiner",
        summary_name="metrics.json",
        out=out,
        has_wave_map=True,
    )
    assert source == "local_reduce"
    assert incomplete == []
    return aggregated


def test_final_harvest_byte_identical_with_without_and_with_corrupt_prefetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-bearing equivalence: the reduced aggregate is identical when the
    harvest runs cold, when it runs over a warm prefetch cache, and when the
    prefetch cache was CORRUPTED after the fact (the transport re-transfers the
    diverged file, so the reduce reads the cluster's bytes either way)."""
    remote = tmp_path / "remote_combiner"
    _write_wave(remote, 0, {"g0": {"acc": 1.0, "n_samples": 2}})
    _write_wave(remote, 1, {"g0": {"acc": 3.0, "n_samples": 2}})
    _copying_pull(remote, monkeypatch)

    # Cold: no prefetch ever ran.
    cold_exp = tmp_path / "cold"
    cold_exp.mkdir()
    cold = _reduce(cold_exp)

    # Warm: the watch prefetched mid-flight, then the harvest re-pulled.
    warm_exp = tmp_path / "warm"
    warm_exp.mkdir()
    pre = af_module.prefetch_wave_partials(warm_exp, _RUN_ID, record=_record())
    assert pre is not None and pre["ok"] is True
    assert (warm_exp / "_aggregated" / _RUN_ID / "_combiner" / "wave_0.json").is_file()
    warm = _reduce(warm_exp)

    # Corrupt: the prefetched copy was torn/rewritten locally after the fetch.
    corrupt_exp = tmp_path / "corrupt"
    corrupt_exp.mkdir()
    pre2 = af_module.prefetch_wave_partials(corrupt_exp, _RUN_ID, record=_record())
    assert pre2 is not None and pre2["ok"] is True
    torn = corrupt_exp / "_aggregated" / _RUN_ID / "_combiner" / "wave_1.json"
    torn.write_text('{"wave": 1, "run_id": "' + _RUN_ID + '", "grid_po', encoding="utf-8")
    corrupt = _reduce(corrupt_exp)

    assert cold == warm == corrupt
    assert cold  # non-empty — the equivalence is not vacuous
