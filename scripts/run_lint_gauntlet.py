"""Discovery-based "run every lint" gauntlet — the pre-push routine can never
silently skip a ``scripts/lint_*.py`` again.

WHY THIS EXISTS (the c41c7d24 origin class)
-------------------------------------------
Commit ``c41c7d24`` shipped CI red because two lints —
``lint_subject_imports`` and ``lint_private_cross_package_imports`` — are
NOT part of ``scripts/regen_all.py --check``. That entry point is
regen-PARITY only (schemas, operations.json, doc indices, verb-map): by
design it does not run lints, and lints must NEVER be bolted onto it (a
lint failure is not "generated artifact drifted"). So the local pre-push
routine, which leaned on ``regen_all --check`` for its mechanical gate, had
a blind spot: the CI ``test`` job runs ~19 ``lint_*.py`` that no local
one-shot command ran. A lint that exists in ``scripts/`` (or is added
later) but that the local gauntlet doesn't run is the class this tool
kills.

The defense is DISCOVERY, not a hand-maintained list. This runner globs
``scripts/lint_*.py`` at call time, so a newly added lint is picked up and
run automatically — it is structurally impossible for the gauntlet to
"forget" a lint, because it never enumerated them by hand in the first
place. The only hand-maintained surface is :data:`SPECIAL_CASES`, a small
table for the handful of lints that need a non-default invocation (extra
flags) or that intentionally do not appear in ``ci.yml`` (they run via
pre-commit or the pytest suite instead). Everything else runs plain:
``[sys.executable, scripts/<stem>.py]`` from the repo root — exactly how
CI and pre-commit invoke them.

USAGE
-----
::

    python scripts/run_lint_gauntlet.py                 # run every lint
    python scripts/run_lint_gauntlet.py --only pure_files subject_imports
    python scripts/run_lint_gauntlet.py --check-parity  # audit vs ci.yml only
    python scripts/run_lint_gauntlet.py --with-suggested-tests  # + advisory test slice

The opt-in ``--with-suggested-tests`` flag appends one final gauntlet step: it
runs ``scripts/suggest_tests.py --run``, executing pytest on the advisory slice
the working diff maps to (empty slice -> a loud "run the full battery" line, not
a silent pass). Its result folds into the gauntlet's exit code. This is an
ADVISORY fast lane only — the FULL suite stays the release / CI gate; a green
slice never substitutes for it.

The default run also performs the CI-parity audit (see :func:`check_parity`)
and folds its result into the exit code: an orphan lint — one present in
``scripts/`` but absent from both ``ci.yml`` and this table, or a stale
``ci.yml`` reference to a script that no longer exists — is reported LOUDLY
and reds the run. That report is the whole point of the tool: it is the
tripwire that would have caught the ``c41c7d24`` gap the moment the lint was
authored, rather than at CI time.

FAILURE POLICY
--------------
Every lint runs regardless of earlier failures (the same
summary-beats-truncation principle ``regen_all.py`` uses): one invocation
surfaces the FULL list of what's broken, not just the first thing. Each
lint's output is captured; only failures print their output in full, so a
clean run stays quiet and a red run stays legible. Exit is non-zero iff any
lint failed or the parity audit found a problem.

Note: this is dev tooling — it lives in ``scripts/`` and imports nothing
from ``src/hpc_agent`` (no layering jurisdiction), spawning each lint as a
subprocess exactly as CI does.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# Discovery glob: any file matching this is a lint the gauntlet runs by
# default. A NEW lint dropped into scripts/ is auto-included — no edit here.
LINT_GLOB = "lint_*.py"

# Per-lint wall-clock cap. Every lint is a fast AST/text scan; a hang is a
# bug, not a slow success, so a generous-but-finite cap keeps the gauntlet
# from wedging the whole pre-push routine.
_PER_LINT_TIMEOUT_S = 300


@dataclass(frozen=True)
class LintSpec:
    """The ONLY hand-maintained per-lint knowledge — a deviation from the
    defaults (run plain, expected present in ``ci.yml``). Every field carries
    a ``reason`` so the table is self-documenting and auditable."""

    reason: str
    # Extra argv appended after ``[sys.executable, scripts/<stem>.py]``.
    extra_argv: tuple[str, ...] = ()
    # When False the gauntlet does NOT run this lint (e.g. it cannot run
    # standalone locally). Reserved mechanism; unused on the current tree.
    run: bool = True
    # When True the lint is EXPECTED to be absent from ci.yml because it runs
    # elsewhere (pre-commit and/or the pytest suite). The parity audit then
    # treats its ci.yml absence as acknowledged rather than an orphan.
    ci_absent: bool = False


# ── The special-case table ────────────────────────────────────────────────
#
# ONLY lints that deviate from the defaults appear here. Anything not listed
# runs plain and is expected to be referenced in ci.yml. Keep this tiny; the
# discovery glob — not this dict — is what guarantees coverage.
SPECIAL_CASES: dict[str, LintSpec] = {
    # --- Non-default invocation (documented; local run stays plain) --------
    "lint_plugin_api_surface": LintSpec(
        reason=(
            "CI runs it plain in the test job AND with --fire-path in the "
            "plugins job (notebook-render plugin installed). The gauntlet "
            "runs the plain stay-inside/anti-drift leg only; --fire-path "
            "needs the plugin + its deps installed, out of local scope."
        ),
    ),
    "lint_plugin_manifests": LintSpec(
        reason=(
            "CI runs it in the plugins job with a plugin installed. "
            "Standalone it is a hard no-op (returns 0 when no plugin is "
            "loaded), so the gauntlet runs it plain as a cheap smoke of the "
            "manifest reconciler."
        ),
    ),
    # --- Intentionally absent from ci.yml (run via pre-commit / pytest) ----
    "lint_primitive_doc_templates": LintSpec(
        reason=(
            "runs via pre-commit + tests/contracts/"
            "test_lint_primitive_doc_templates.py, not a ci.yml lint step"
        ),
        ci_absent=True,
    ),
    "lint_skills": LintSpec(
        reason="runs via tests/contracts/test_lint_skills.py, not a ci.yml lint step",
        ci_absent=True,
    ),
    "lint_skill_mcp_reachability": LintSpec(
        reason=(
            "runs via tests/scripts/test_lint_skill_mcp_reachability.py, not a ci.yml lint step"
        ),
        ci_absent=True,
    ),
    "lint_no_raw_ssh": LintSpec(
        reason="runs via the pre-commit hook (lint-no-raw-ssh), not a ci.yml lint step",
        ci_absent=True,
    ),
    "lint_no_blocklisted_commands": LintSpec(
        reason=(
            "runs via the pre-commit hook (lint-no-blocklisted-commands), not a ci.yml lint step"
        ),
        ci_absent=True,
    ),
    "lint_backend_boundary": LintSpec(
        reason=(
            "runs via the pre-commit hook (lint-backend-boundary) + "
            "tests/scripts/test_lint_backend_boundary.py, not a ci.yml lint step"
        ),
        ci_absent=True,
    ),
    "lint_remote_read_ack": LintSpec(
        reason=(
            "runs via the pre-commit hook (lint-remote-read-ack) + "
            "tests/scripts/test_lint_remote_read_ack.py, not a ci.yml lint step"
        ),
        ci_absent=True,
    ),
}

# Matches every ``scripts/lint_<name>.py`` reference in ci.yml text.
_CI_LINT_RE = re.compile(r"scripts/(lint_[a-z0-9_]+)\.py")


@dataclass
class LintResult:
    """Outcome of one lint invocation."""

    stem: str
    returncode: int
    output: str
    argv: tuple[str, ...] = ()
    skipped: bool = False


def _as_text(stream: str | bytes | None) -> str:
    """Coerce a captured stream (str, bytes, or None) to text. ``TimeoutExpired``
    may hand back bytes even under ``text=True``, so decode defensively."""
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


def discover_lints(scripts_dir: Path = SCRIPTS_DIR) -> list[str]:
    """Return the sorted stems of every ``lint_*.py`` in *scripts_dir*.

    This glob IS the coverage guarantee — a lint the gauntlet can forget is a
    lint that isn't on disk.
    """
    return sorted(p.stem for p in scripts_dir.glob(LINT_GLOB) if p.is_file())


def ci_lint_stems(ci_text: str) -> set[str]:
    """Every ``lint_*`` stem referenced anywhere in the ci.yml *text*."""
    return set(_CI_LINT_RE.findall(ci_text))


def check_parity(
    discovered: list[str],
    ci_text: str,
    special: dict[str, LintSpec] = SPECIAL_CASES,
) -> list[str]:
    """Cross-check the discovered lint set against ci.yml. Return a list of
    problem strings (empty == clean).

    Fires — LOUDLY, that is the point — on either divergence:

    * a lint in ``scripts/`` that ci.yml never references AND that the table
      does not acknowledge as ``ci_absent`` (the c41c7d24 orphan class);
    * a ``scripts/lint_*.py`` referenced in ci.yml that has no file on disk
      (a stale ci.yml reference).

    It also flags a table entry gone stale (``ci_absent`` set for a lint
    ci.yml actually references), so the acknowledgement can't rot.
    """
    problems: list[str] = []
    discovered_set = set(discovered)
    ci_stems = ci_lint_stems(ci_text)

    # Forward: every discovered lint must be accounted for.
    for stem in discovered:
        spec = special.get(stem)
        in_ci = stem in ci_stems
        if in_ci:
            if spec is not None and spec.ci_absent:
                problems.append(
                    f"{stem}: SPECIAL_CASES marks it ci_absent, but ci.yml DOES "
                    f"reference it — the table entry is stale, drop ci_absent."
                )
            continue
        # Not referenced in ci.yml.
        if spec is None or not spec.ci_absent:
            problems.append(
                f"{stem}: discovered in scripts/ but NOT referenced in ci.yml and "
                f"NOT acknowledged in SPECIAL_CASES (ci_absent). Wire it into "
                f"ci.yml, or add a SPECIAL_CASES entry naming where it runs."
            )

    # Reverse: every lint ci.yml names must exist on disk.
    for stem in sorted(ci_stems):
        if stem not in discovered_set:
            problems.append(
                f"{stem}: referenced in ci.yml but scripts/{stem}.py does not "
                f"exist — stale ci.yml reference (renamed or removed lint?)."
            )

    return problems


def invocation(stem: str, special: dict[str, LintSpec] = SPECIAL_CASES) -> list[str]:
    """The exact argv the gauntlet runs for *stem* (plain unless the table
    adds extra flags)."""
    script = SCRIPTS_DIR / f"{stem}.py"
    spec = special.get(stem)
    extra = spec.extra_argv if spec is not None else ()
    return [sys.executable, str(script), *extra]


def run_one(stem: str, special: dict[str, LintSpec] = SPECIAL_CASES) -> LintResult:
    """Run one lint as a subprocess, capturing its output. A ``run=False``
    table entry short-circuits to a skipped result."""
    spec = special.get(stem)
    if spec is not None and not spec.run:
        return LintResult(stem=stem, returncode=0, output="", skipped=True)
    argv = invocation(stem, special)
    try:
        proc = subprocess.run(
            argv,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_PER_LINT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        out = _as_text(exc.stdout) + _as_text(exc.stderr)
        return LintResult(
            stem=stem,
            returncode=124,
            output=f"{out}\n[TIMEOUT after {_PER_LINT_TIMEOUT_S}s]",
            argv=tuple(argv[2:]),
        )
    return LintResult(
        stem=stem,
        returncode=proc.returncode,
        output=(proc.stdout or "") + (proc.stderr or ""),
        argv=tuple(argv[2:]),
    )


def _resolve_only(requested: list[str], discovered: list[str]) -> list[str]:
    """Map ``--only`` args (stem, or bare suffix) onto discovered stems."""
    resolved: list[str] = []
    known = set(discovered)
    for arg in requested:
        if arg in known:
            resolved.append(arg)
        elif f"lint_{arg}" in known:
            resolved.append(f"lint_{arg}")
        else:
            print(f"error: --only {arg!r} matches no discovered lint", file=sys.stderr)
            raise SystemExit(2)
    return resolved


def _run_all(stems: list[str], special: dict[str, LintSpec], serial: bool) -> list[LintResult]:
    """Run every lint in *stems*, returning results in the SAME order as
    *stems* regardless of completion order.

    Each lint is its own cold subprocess (see :func:`run_one`), so the work is
    subprocess-bound and thread-parallel: a ``ThreadPoolExecutor`` overlaps the
    I/O waits without the GIL ever mattering. ``run_one`` is fully
    self-contained (no shared mutable state), so it is safe to fan out — the
    only ordering guarantee we owe callers is report order, which
    :meth:`Executor.map` preserves by yielding results positionally.

    ``serial`` (flag or ``HPC_GAUNTLET_SERIAL=1``) forces the plain list
    comprehension — an escape hatch for debugging a lint under a single,
    deterministic worker.
    """
    if serial or len(stems) <= 1:
        return [run_one(stem, special) for stem in stems]
    max_workers = min(len(stems), os.cpu_count() or 4)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # map() yields in submission (== stems) order, so results stay
        # index-aligned with stems no matter which lint finishes first.
        return list(executor.map(lambda stem: run_one(stem, special), stems))


def run_gauntlet(
    stems: list[str],
    special: dict[str, LintSpec] = SPECIAL_CASES,
    *,
    serial: bool = False,
) -> int:
    """Run each lint in *stems*, print a PASS/FAIL table, return 0 iff all
    passed. Runs ALL of them regardless of individual failures.

    Lints run in parallel by default (each is a process-isolated subprocess);
    the report is always emitted in *stems* order. Pass ``serial=True`` (or set
    ``HPC_GAUNTLET_SERIAL=1``) to run them one at a time for debugging."""
    print(f"lint gauntlet: {len(stems)} lint(s)")
    results = _run_all(stems, special, serial)

    # Failures print their output in full (bounded: only the red ones).
    for r in results:
        if r.returncode != 0 and r.output.strip():
            print(f"\n===== {r.stem} FAILED (rc={r.returncode}) =====")
            print(r.output.rstrip())

    width = max((len(r.stem) for r in results), default=0)
    print("\n--- summary ---")
    failed: list[str] = []
    for r in results:
        if r.skipped:
            status = "SKIP"
        elif r.returncode == 0:
            status = "PASS"
        else:
            status = f"FAIL (rc={r.returncode})"
            failed.append(r.stem)
        extra = f"  [{' '.join(r.argv)}]" if r.argv else ""
        print(f"  [{status:<12}] {r.stem:<{width}}{extra}")

    if failed:
        print(
            f"\nlint gauntlet: {len(failed)} of {len(stems)} lint(s) FAILED: " + ", ".join(failed),
            file=sys.stderr,
        )
        return 1
    print(f"\nlint gauntlet: all {len(stems)} lint(s) passed")
    return 0


def run_suggested_tests_step() -> int:
    """Append the advisory suggested-test slice as a final gauntlet step.

    Spawns ``scripts/suggest_tests.py --run`` as a subprocess (inheriting stdout
    so pytest's live output reaches the caller), exactly as the gauntlet spawns
    each lint. Returns its exit code for folding into the gauntlet's. ADVISORY
    only: the full suite remains the release / CI gate."""
    print("\n--- suggested-tests slice (ADVISORY; the full suite still gates CI/release) ---")
    sys.stdout.flush()  # order our header before the child's inherited output when piped
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "suggest_tests.py"), "--run"],
        cwd=REPO_ROOT,
    )
    return proc.returncode


def _print_parity(problems: list[str]) -> None:
    if problems:
        print("\n!!! CI-PARITY PROBLEMS (scripts/ <-> ci.yml drift) !!!", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
    else:
        print("\nCI-parity: scripts/lint_*.py <-> ci.yml consistent")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_lint_gauntlet.py",
        description="Run every scripts/lint_*.py so the pre-push routine can't skip one.",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="STEM",
        help="run only these lints (stem or bare suffix). Skips the parity audit.",
    )
    parser.add_argument(
        "--check-parity",
        action="store_true",
        help="audit the discovered lint set against ci.yml and exit (run no lints).",
    )
    parser.add_argument(
        "--serial",
        action="store_true",
        help=(
            "run lints one at a time instead of in parallel (debugging escape "
            "hatch; also enabled by HPC_GAUNTLET_SERIAL=1)."
        ),
    )
    parser.add_argument(
        "--with-suggested-tests",
        action="store_true",
        help=(
            "opt-in: after the lints, append an ADVISORY suggested-test slice "
            "(scripts/suggest_tests.py --run) as a final step and fold its result "
            "into the exit code. The FULL suite stays the release / CI gate — this "
            "is a fast local signal, never a substitute for it."
        ),
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    # Windows consoles default to cp1252: a failed lint whose captured output
    # carries non-ASCII (e.g. U+FFFD from a decode fallback) must not crash the
    # report printer itself — substitute rather than UnicodeEncodeError, so the
    # failure table always reaches the caller.
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            _reconfigure(errors="replace")

    serial = args.serial or os.environ.get("HPC_GAUNTLET_SERIAL") == "1"

    discovered = discover_lints()
    ci_text = CI_WORKFLOW.read_text(encoding="utf-8") if CI_WORKFLOW.is_file() else ""

    if args.check_parity:
        problems = check_parity(discovered, ci_text)
        _print_parity(problems)
        return 1 if problems else 0

    if args.only:
        stems = _resolve_only(args.only, discovered)
        # Targeted run: lints only, no parity audit (the caller is iterating
        # on specific lints, not gating a push).
        rc = run_gauntlet(stems, serial=serial)
        if args.with_suggested_tests:
            rc = rc or run_suggested_tests_step()
        return rc

    # Default: every lint + the parity audit, folded into one exit code.
    lint_rc = run_gauntlet(discovered, serial=serial)
    problems = check_parity(discovered, ci_text)
    _print_parity(problems)
    suggested_rc = run_suggested_tests_step() if args.with_suggested_tests else 0
    return 1 if (lint_rc != 0 or problems or suggested_rc != 0) else 0


if __name__ == "__main__":
    raise SystemExit(main())
