#!/usr/bin/env python
"""Measure the per-surface stateless dispatch floor (R4 measure-then-decide).

ARCHITECT-MEMO sec 2a (``docs/plans/daemon-engineering-2026-07-16/``) ruled
MEASURE-THEN-DECIDE: after the latency waves land the stateless program, measure
the real per-call floor on the primary Windows box and let the residual gap
decide whether the WS-DAEMON lifecycle machinery (DW1+) earns its 13 units.
This script is that measurement harness. It is the *evidence producer*; it does
NOT rule. It computes the numbers sec 2a names as the gate and prints them with
the memo's reference band so a human can apply step 3 of the ruling.

Surfaces measured (each ``--runs`` samples, median + min + all samples):

* ``bare``      — bare interpreter (``python -c pass``): the irreducible spawn +
  Defender/AV tax with no package import at all.
* ``bare2``     — the SAME bare spawn, a second independent series. The spread
  between ``bare`` and ``bare2`` medians *is* the process-spawn / AV variance
  signal (sec 2, "measure (a) twice").
* ``import``    — ``python -c "import hpc_agent"``: spawn + top-level package
  import, nothing dispatched.
* ``fast_path`` — a fast-path verb cold in a fresh subprocess (default
  ``describe find``; override with ``--fast-verb``).
* ``full_walk`` — a full-registry-walk verb cold (default ``capabilities``;
  override with ``--full-verb``).
* ``hook``      — a Stop-hook-shaped invocation: ``python -m
  hpc_agent._kernel.hooks.stop_multiplex`` with a minimal Stop payload on stdin.
  Measured in DRY mode (payload ``cwd`` has no ``.hpc`` and ``HPC_JOURNAL_DIR``
  points at a nonexistent dir) so the syntactic prefilter short-circuits and NO
  guard runs — zero side effects. See CAVEATS: this measures the no-op per-turn
  hook floor (interpreter + ``stop_multiplex`` import + stdin read); a
  guard-active Stop pays an additional ``import hpc_agent``-class cost on top.
* ``warm``      — the fast-path verb run WARM in-process: import once, call the
  runner once to warm, then time subsequent calls. The reference floor a warm
  daemon would approach.

Honest-reporting rules baked in:

* OS filesystem-cache warmth is NOT controlled — this box has already imported
  the tree (pytest, prior runs). The ``boot_state`` field says so; pass
  ``--cold-claim`` only if you truly just rebooted and this is the first run.
* ``git rev-parse HEAD`` + a ``git status --porcelain`` dirty-line count go into
  the report, so a measurement taken mid-swarm against a dirty importable tree
  is visibly labelled (a loud banner prints; the run is NOT silently trusted).
* Timestamp + env fingerprint (python version, wheel version via
  ``importlib.metadata``, box name, platform) are recorded.

Usage::

    .venv/Scripts/python.exe scripts/measure_dispatch_floor.py
    .venv/Scripts/python.exe scripts/measure_dispatch_floor.py --runs 7 \
        --fast-verb describe find --full-verb capabilities --out report.json

Never uses ssh; never mutates the repo; safe to run anytime (the dirty-tree
label is the only consequence of a mid-swarm run).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── Constants from ARCHITECT-MEMO sec 2a (the gate) ──────────────────────────
# The daemon's warm-call reference band the residual gap is judged against.
WARM_REFERENCE_LOW_MS = 15.0
WARM_REFERENCE_HIGH_MS = 20.0
# Hooks fire ~3× per turn (UserPromptSubmit capture + Stop multiplex + a
# PostToolUse fence), so the per-turn hook tax is 3× a single hook median.
HOOKS_PER_TURN = 3

HOOK_MODULE = "hpc_agent._kernel.hooks.stop_multiplex"

# The subprocess surfaces, in a canonical order; the collector rotates this per
# round to defeat cache-warming order bias.
SUBPROCESS_KEYS: tuple[str, ...] = (
    "bare",
    "bare2",
    "import",
    "fast_path",
    "full_walk",
    "hook",
)


@dataclass
class Config:
    """Resolved run parameters."""

    runs: int
    fast_verb: list[str]
    full_verb: list[str]
    out_path: Path
    repo_root: Path
    python: str
    hpc_cmd: list[str]
    cold_claim: bool
    warm_verb_argv: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # The warm in-process reference uses the same verb as ``fast_path``.
        self.warm_verb_argv = list(self.fast_verb)


# ── Timing primitives ────────────────────────────────────────────────────────
def time_subprocess(
    cmd: list[str],
    *,
    stdin_data: str | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> float:
    """Wall-clock seconds for one full subprocess: spawn → run → teardown."""
    start = time.perf_counter()
    subprocess.run(
        cmd,
        input=stdin_data,
        env=env,
        cwd=cwd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    return time.perf_counter() - start


def time_warm_call(argv: list[str]) -> float:
    """Wall-clock seconds for one WARM in-process ``dispatch.main(argv)`` call.

    Imports are already resolved by the time this is called (the collector runs
    one discarded warm-up first). stdout/stderr are captured so the verb's own
    output does not pollute the harness stream. ``dispatch.main`` returns an int
    (it does not ``sys.exit``), so no ``SystemExit`` handling is needed.
    """
    from hpc_agent.cli import dispatch

    sink = io.StringIO()
    start = time.perf_counter()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        dispatch.main(list(argv))
    return time.perf_counter() - start


# ── Per-surface single-sample runners ────────────────────────────────────────
def _dry_hook_env(base: dict[str, str]) -> dict[str, str]:
    """Env that forces the Stop prefilter to short-circuit (no guard runs)."""
    env = dict(base)
    env["HPC_JOURNAL_DIR"] = os.path.join(
        tempfile.gettempdir(), "hpc_measure_nonexistent_journal_home"
    )
    return env


def run_surface_once(key: str, cfg: Config) -> float:
    """Run one sample of subprocess surface *key*; return elapsed seconds."""
    base_env = dict(os.environ)
    if key in ("bare", "bare2"):
        return time_subprocess([cfg.python, "-c", "pass"])
    if key == "import":
        return time_subprocess([cfg.python, "-c", "import hpc_agent"])
    if key == "fast_path":
        return time_subprocess([*cfg.hpc_cmd, *cfg.fast_verb])
    if key == "full_walk":
        return time_subprocess([*cfg.hpc_cmd, *cfg.full_verb])
    if key == "hook":
        # A minimal Stop payload whose cwd has no ``.hpc``; combined with a
        # nonexistent HPC_JOURNAL_DIR the prefilter proves every guard is a
        # no-op and returns 0 without importing the heavy guard chain.
        no_hpc_cwd = tempfile.gettempdir()
        payload = json.dumps(
            {
                "cwd": no_hpc_cwd,
                "hook_event_name": "Stop",
                "session_id": "measure-dispatch-floor",
                "transcript_path": "",
            }
        )
        return time_subprocess(
            [cfg.python, "-m", HOOK_MODULE],
            stdin_data=payload,
            env=_dry_hook_env(base_env),
            cwd=no_hpc_cwd,
        )
    raise ValueError(f"unknown surface key: {key!r}")


# ── Collection (interleaved to defeat order bias) ─────────────────────────────
def collect_samples(
    cfg: Config,
    *,
    surface_runner: Callable[[str, Config], float] = run_surface_once,
    warm_runner: Callable[[list[str]], float] = time_warm_call,
) -> dict[str, list[float]]:
    """Collect ``cfg.runs`` samples per surface.

    Subprocess surfaces are interleaved: each round rotates the surface order so
    no single surface consistently pays (or dodges) the coldest filesystem
    state. The warm surface runs one discarded warm-up, then ``cfg.runs`` timed
    in-process calls.
    """
    results: dict[str, list[float]] = {k: [] for k in SUBPROCESS_KEYS}
    order = list(SUBPROCESS_KEYS)
    n = len(order)
    for i in range(cfg.runs):
        shift = i % n
        rotated = order[shift:] + order[:shift]
        for key in rotated:
            results[key].append(surface_runner(key, cfg))

    # Warm reference: one discarded warm-up call, then timed samples.
    with contextlib.suppress(Exception):
        warm_runner(cfg.warm_verb_argv)
    results["warm"] = [warm_runner(cfg.warm_verb_argv) for _ in range(cfg.runs)]
    return results


# ── Pure report assembly (unit-tested with injected timings) ─────────────────
def summarize(samples_s: list[float]) -> dict[str, Any]:
    """Reduce a list of per-sample seconds to a stats block (all in ms)."""
    if not samples_s:
        return {
            "n": 0,
            "median_ms": None,
            "min_ms": None,
            "max_ms": None,
            "mean_ms": None,
            "samples_ms": [],
        }
    ms = [round(s * 1000.0, 3) for s in samples_s]
    return {
        "n": len(ms),
        "median_ms": round(statistics.median(ms), 3),
        "min_ms": round(min(ms), 3),
        "max_ms": round(max(ms), 3),
        "mean_ms": round(statistics.fmean(ms), 3),
        "samples_ms": ms,
    }


def compute_decision(summaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compute the sec-2a decision line from surface summaries.

    Returns the measured cold fast-path floor, the measured warm reference, the
    per-turn hook cost (``HOOKS_PER_TURN`` × hook median), the residual gap vs
    the memo's 15–20ms warm band, and an advisory *reading* string. This is
    guidance for the maintainer's step-3 ruling, NOT the ruling itself.
    """
    cold_fast = summaries["fast_path"]["median_ms"]
    full_walk = summaries["full_walk"]["median_ms"]
    warm = summaries["warm"]["median_ms"]
    hook = summaries["hook"]["median_ms"]
    bare = summaries["bare"]["median_ms"]
    bare2 = summaries["bare2"]["median_ms"]

    per_turn_hook = round(hook * HOOKS_PER_TURN, 3) if hook is not None else None
    spawn_variance = round(abs(bare - bare2), 3) if bare is not None and bare2 is not None else None
    # Residual gap: measured cold floor above the memo's warm reference band.
    gap_vs_band = round(cold_fast - WARM_REFERENCE_HIGH_MS, 3) if cold_fast is not None else None
    gap_vs_warm = round(cold_fast - warm, 3) if cold_fast is not None and warm is not None else None

    reading = _reading(cold_fast)
    return {
        "cold_fast_path_median_ms": cold_fast,
        "cold_full_walk_median_ms": full_walk,
        "warm_reference_measured_ms": warm,
        "warm_reference_band_ms": [WARM_REFERENCE_LOW_MS, WARM_REFERENCE_HIGH_MS],
        "hook_single_median_ms": hook,
        "per_turn_hook_cost_ms": per_turn_hook,
        "hooks_per_turn": HOOKS_PER_TURN,
        "spawn_variance_bare_vs_bare2_ms": spawn_variance,
        "residual_gap_vs_warm_band_ms": gap_vs_band,
        "residual_gap_vs_measured_warm_ms": gap_vs_warm,
        "reading": reading,
        "ruling_ref": "ARCHITECT-MEMO sec 2a step 3 (this script does not rule)",
    }


def _reading(cold_fast: float | None) -> str:
    """Advisory reading of the cold floor against the sec-2a gate band."""
    if cold_fast is None:
        return "no fast-path samples — cannot read the gate"
    if cold_fast <= WARM_REFERENCE_HIGH_MS * 1.5:
        return (
            f"cold fast-path median {cold_fast:.1f}ms is within ~1.5× the "
            f"{WARM_REFERENCE_LOW_MS:.0f}-{WARM_REFERENCE_HIGH_MS:.0f}ms warm band: "
            "residual gap is SMALL — sec 2a step 3 leans SHELVE DW1+ "
            "(stateless floor already near warm)."
        )
    return (
        f"cold fast-path median {cold_fast:.1f}ms sits well above the "
        f"{WARM_REFERENCE_LOW_MS:.0f}-{WARM_REFERENCE_HIGH_MS:.0f}ms warm band: "
        "a residual gap remains. Whether it JUSTIFIES 13 units of daemon "
        "lifecycle machinery is the maintainer's sec-2a step-3 call — compare "
        "this against the projected post-wave stateless target, not the "
        "pre-wave baseline."
    )


def git_state(repo_root: Path) -> dict[str, Any]:
    """``HEAD`` sha + dirty-line count, so a mid-swarm tree is visibly labelled."""

    def _run(args: list[str]) -> str | None:
        try:
            out = subprocess.run(
                args,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if out.returncode != 0:
            return None
        return out.stdout

    head_out = _run(["git", "rev-parse", "HEAD"])
    head = head_out.strip() if head_out else None
    status_out = _run(["git", "status", "--porcelain"])
    if status_out is None:
        dirty = None
        dirty_count = None
    else:
        lines = [ln for ln in status_out.splitlines() if ln.strip()]
        dirty_count = len(lines)
        dirty = dirty_count > 0
    return {"head": head, "dirty": dirty, "dirty_line_count": dirty_count}


def wheel_version() -> str | None:
    """Installed hpc-agent version via importlib.metadata, or None."""
    try:
        from importlib import metadata

        return metadata.version("hpc-agent")
    except Exception:
        return None


def env_fingerprint(cfg: Config) -> dict[str, Any]:
    """Timestamp + interpreter + wheel + box identity."""
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "python_executable": cfg.python,
        "wheel_version": wheel_version(),
        "box_name": socket.gethostname(),
        "platform": platform.platform(),
        "system": platform.system(),
        "hpc_cmd": cfg.hpc_cmd,
    }


def build_report(cfg: Config, raw: dict[str, list[float]]) -> dict[str, Any]:
    """Assemble the full JSON report from raw per-surface seconds."""
    summaries = {key: summarize(raw.get(key, [])) for key in (*SUBPROCESS_KEYS, "warm")}
    git = git_state(cfg.repo_root)
    boot_state = "user-asserted-cold" if cfg.cold_claim else "warm-uncontrolled"
    return {
        "schema": "hpc.measure_dispatch_floor.v1",
        "runs_per_surface": cfg.runs,
        "env": env_fingerprint(cfg),
        "git": git,
        "boot_state": boot_state,
        "boot_state_note": (
            "OS filesystem-cache warmth is NOT controlled by this script; the "
            "tree is already imported on this box. 'first_run_of_boot' is left "
            "null on purpose — pass --cold-claim only after a real reboot."
        ),
        "first_run_of_boot": None,
        "surfaces": {
            "bare": {"desc": "python -c pass", **summaries["bare"]},
            "bare2": {"desc": "python -c pass (2nd series)", **summaries["bare2"]},
            "import": {"desc": "python -c 'import hpc_agent'", **summaries["import"]},
            "fast_path": {
                "desc": f"cold: hpc-agent {' '.join(cfg.fast_verb)}",
                **summaries["fast_path"],
            },
            "full_walk": {
                "desc": f"cold: hpc-agent {' '.join(cfg.full_verb)}",
                **summaries["full_walk"],
            },
            "hook": {
                "desc": f"dry Stop hook: python -m {HOOK_MODULE}",
                "caveat": (
                    "DRY: prefilter short-circuits, no guard chain imported. A "
                    "guard-active Stop adds an 'import hpc_agent'-class cost."
                ),
                **summaries["hook"],
            },
            "warm": {
                "desc": f"WARM in-process: dispatch.main({cfg.fast_verb})",
                **summaries["warm"],
            },
        },
        "decision": compute_decision(summaries),
    }


# ── Human table rendering ────────────────────────────────────────────────────
def _fmt(v: Any) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "n/a"


def render_table(report: dict[str, Any]) -> str:
    """A human-readable table + the decision line, for stdout."""
    lines: list[str] = []
    env = report["env"]
    git = report["git"]
    lines.append("=" * 72)
    lines.append("STATELESS DISPATCH FLOOR  (R4 measure-then-decide, memo sec 2a)")
    lines.append("=" * 72)
    lines.append(f"  when      : {env['timestamp_utc']}")
    lines.append(f"  box       : {env['box_name']}  |  {env['platform']}")
    lines.append(f"  python    : {env['python_version']}")
    lines.append(f"  wheel     : {env['wheel_version']}")
    lines.append(
        f"  git HEAD  : {git['head']}  dirty={git['dirty']} ({git['dirty_line_count']} lines)"
    )
    lines.append(f"  boot_state: {report['boot_state']}")
    lines.append(f"  runs/surf : {report['runs_per_surface']}")
    if git.get("dirty"):
        lines.append("")
        lines.append(
            "  !! WARNING: importable tree is DIRTY — this measurement was taken mid-change."
        )
        lines.append(
            "     Numbers are labelled dirty in the JSON; do not cite as a clean baseline."
        )
    lines.append("")
    header = f"  {'surface':<12}{'median':>10}{'min':>10}{'max':>10}{'n':>5}"
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for key in (*SUBPROCESS_KEYS, "warm"):
        s = report["surfaces"][key]
        lines.append(
            f"  {key:<12}{_fmt(s['median_ms']):>10}{_fmt(s['min_ms']):>10}"
            f"{_fmt(s['max_ms']):>10}{s['n']:>5}    ({s['desc']})"
        )
    lines.append("")
    d = report["decision"]
    lines.append("-" * 72)
    lines.append("DECISION LINE (sec 2a gate numbers):")
    lines.append(f"  cold fast-path floor (median) : {_fmt(d['cold_fast_path_median_ms'])} ms")
    lines.append(f"  cold full-walk  floor (median): {_fmt(d['cold_full_walk_median_ms'])} ms")
    lines.append(
        f"  warm reference  (measured)    : {_fmt(d['warm_reference_measured_ms'])} ms"
        f"   [memo band {WARM_REFERENCE_LOW_MS:.0f}-{WARM_REFERENCE_HIGH_MS:.0f} ms]"
    )
    lines.append(
        f"  per-turn hook cost (3× hook)  : {_fmt(d['per_turn_hook_cost_ms'])} ms"
        f"   (single hook {_fmt(d['hook_single_median_ms'])} ms)"
    )
    lines.append(
        f"  spawn variance (bare vs bare2): {_fmt(d['spawn_variance_bare_vs_bare2_ms'])} ms"
    )
    lines.append(f"  residual gap vs warm band     : {_fmt(d['residual_gap_vs_warm_band_ms'])} ms")
    lines.append("")
    lines.append(f"  reading: {d['reading']}")
    lines.append(f"  ({d['ruling_ref']})")
    lines.append("-" * 72)
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────
def resolve_hpc_cmd(python: str) -> list[str]:
    """The command that invokes the ``hpc-agent`` CLI.

    Prefer the installed console script next to the interpreter (the real path
    the agent/harness uses); fall back to ``python -m hpc_agent``.
    """
    scripts_dir = Path(python).parent
    exe = scripts_dir / "hpc-agent.exe"
    if exe.exists():
        return [str(exe)]
    plain = scripts_dir / "hpc-agent"
    if plain.exists():
        return [str(plain)]
    return [python, "-m", "hpc_agent"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Measure the per-surface stateless dispatch floor (memo sec 2a).",
    )
    p.add_argument("--runs", type=int, default=7, help="samples per surface (default 7)")
    p.add_argument(
        "--fast-verb",
        nargs="+",
        default=["describe", "find"],
        help="fast-path verb argv (default: describe find)",
    )
    p.add_argument(
        "--full-verb",
        nargs="+",
        default=["capabilities"],
        help="full-registry-walk verb argv (default: capabilities)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="JSON report path (default: a timestamped file in the temp dir)",
    )
    p.add_argument(
        "--cold-claim",
        action="store_true",
        help="assert this is the first run after a real reboot (labels boot_state)",
    )
    return p.parse_args(argv)


def build_config(ns: argparse.Namespace) -> Config:
    python = sys.executable
    repo_root = Path(__file__).resolve().parents[1]
    if ns.out is not None:
        out_path = ns.out
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = Path(tempfile.gettempdir()) / f"dispatch_floor_{stamp}.json"
    return Config(
        runs=max(1, int(ns.runs)),
        fast_verb=list(ns.fast_verb),
        full_verb=list(ns.full_verb),
        out_path=out_path,
        repo_root=repo_root,
        python=python,
        hpc_cmd=resolve_hpc_cmd(python),
        cold_claim=bool(ns.cold_claim),
    )


def main(argv: list[str] | None = None) -> int:
    ns = parse_args(argv)
    cfg = build_config(ns)
    raw = collect_samples(cfg)
    report = build_report(cfg, raw)
    cfg.out_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(render_table(report))
    print(f"\nJSON report written: {cfg.out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
