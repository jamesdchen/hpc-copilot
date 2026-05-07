"""GPU queue selection fallback tests.

Covers ``claude_hpc.infra.gpu.pick_gpu`` and its fallback ordering when
a preferred GPU is unavailable, qstat fails, exclusions wipe the candidate
list, or no live queue qualifies.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from claude_hpc.infra import gpu as gpu_mod


def _cp(stdout: str = "", stderr: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# ---------------------------------------------------------------------------
# Static fallback (live=False)
# ---------------------------------------------------------------------------


class TestStaticFallback:
    def test_returns_first_preferred(self):
        out = gpu_mod.pick_gpu(preferred=["A100", "H200", "V100"])
        assert out["gpus"] == [{"gpu": "A100", "source": "fallback"}]
        assert out["errors"] == []

    def test_exclude_skips_first_preferred(self):
        out = gpu_mod.pick_gpu(preferred=["A100", "H200", "V100"], exclude={"A100"})
        assert out["gpus"] == [{"gpu": "H200", "source": "fallback"}]
        assert out["errors"] == []

    def test_exclude_is_case_insensitive(self):
        out = gpu_mod.pick_gpu(preferred=["A100", "H200"], exclude={"a100"})
        assert out["gpus"] == [{"gpu": "H200", "source": "fallback"}]

    def test_all_excluded_yields_no_candidates(self):
        out = gpu_mod.pick_gpu(preferred=["A100"], exclude={"A100"})
        assert out["gpus"] == []
        assert out["errors"] == [
            {"code": "no_candidates", "detail": "no candidates after exclusions"}
        ]

    def test_empty_preferred_yields_no_candidates(self):
        out = gpu_mod.pick_gpu(preferred=[])
        assert out["gpus"] == []
        assert out["errors"][0]["code"] == "no_candidates"


# ---------------------------------------------------------------------------
# Live mode: qstat unavailable -> static fallback with diagnostic
# ---------------------------------------------------------------------------


class TestLiveQstatUnavailable:
    def test_qstat_timeout_falls_back_to_static(self, monkeypatch):
        def raise_timeout(cmd, *a, **kw):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

        monkeypatch.setattr(subprocess, "run", raise_timeout)
        out = gpu_mod.pick_gpu(preferred=["A100", "H200"], live=True)
        assert out["gpus"] == [{"gpu": "A100", "source": "fallback"}]
        assert out["errors"] == [{"code": "qstat_unavailable", "detail": "qstat could not be run"}]

    def test_qstat_binary_missing_falls_back_to_static(self, monkeypatch):
        def raise_fnf(cmd, *a, **kw):
            raise FileNotFoundError("no qstat on PATH")

        monkeypatch.setattr(subprocess, "run", raise_fnf)
        out = gpu_mod.pick_gpu(preferred=["H200", "A100"], live=True)
        assert out["gpus"] == [{"gpu": "H200", "source": "fallback"}]
        assert out["errors"][0]["code"] == "qstat_unavailable"

    def test_qstat_nonzero_exit_falls_back_to_static(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _cp(stdout="", stderr="boom", returncode=2),
        )
        out = gpu_mod.pick_gpu(preferred=["A6000", "A100"], live=True)
        assert out["gpus"] == [{"gpu": "A6000", "source": "fallback"}]
        assert out["errors"][0]["code"] == "qstat_unavailable"


# ---------------------------------------------------------------------------
# Live mode: qstat parses but no GPU has enough free slots
# ---------------------------------------------------------------------------


_QSTAT_FULL = (
    # name                                 cqload  used/avail/total                  arch  state
    "gpu_a100.q@n1                          0.50    0/256/256                         lx    \n"
    "gpu_h200.q@n2                          0.40    0/64/64                           lx    \n"
)


class TestLiveInsufficientSlots:
    def test_falls_back_to_lowest_utilization(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _cp(stdout=_QSTAT_FULL, returncode=0),
        )
        out = gpu_mod.pick_gpu(preferred=["A100", "H200"], live=True, slots_needed=4)
        assert out["gpus"], "expected at least one fallback entry"
        assert out["errors"][0]["code"] == "insufficient_free_slots"
        # Highest-util queue (A100 at 1.0) and H200 (1.0) are both saturated;
        # both should appear, sorted by utilization ascending.
        utils = [g["utilization"] for g in out["gpus"]]
        assert utils == sorted(utils)


# ---------------------------------------------------------------------------
# Live mode: empty qstat output -> preferred-order fallback
# ---------------------------------------------------------------------------


class TestLiveNoEligibleQueues:
    def test_empty_qstat_falls_back_to_preferred(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _cp(stdout="", returncode=0),
        )
        out = gpu_mod.pick_gpu(preferred=["A100", "H200"], live=True)
        assert out["gpus"] == [{"gpu": "A100", "source": "fallback"}]
        assert out["errors"][0]["code"] == "no_live_gpus"


# ---------------------------------------------------------------------------
# Live mode: a GPU type qualifies -> ordered by score
# ---------------------------------------------------------------------------


_QSTAT_MIXED = (
    "gpu_a100.q@n1                          0.20    0/100/256                         lx    \n"
    "gpu_h200.q@n2                          0.10    0/10/64                           lx    \n"
    "gpu_a6000.q@n3                         0.30    0/200/256                         lx    \n"
)


class TestLiveScoring:
    def test_preferred_filter_orders_by_score(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: _cp(stdout=_QSTAT_MIXED, returncode=0),
        )
        out = gpu_mod.pick_gpu(preferred=["A100", "H200", "A6000"], live=True, slots_needed=4)
        names = [g["gpu"] for g in out["gpus"]]
        # A6000: free=56, perf=1.0 -> 56.0
        # A100:  free=156, perf=1.2 -> 187.2
        # H200:  free=54, perf=1.5 -> 81.0
        # Ranking: A100 > H200 > A6000
        assert names[:3] == ["A100", "H200", "A6000"]
        assert all(g["source"] == "live" for g in out["gpus"])
