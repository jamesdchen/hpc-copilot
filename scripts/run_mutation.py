#!/usr/bin/env python3
"""Curated per-module mutation-testing runner (devx B4).

Mutation testing re-runs the test suite once *per mutant*, so a full-tree
sweep of this 8k-test codebase is absurd. This runner encapsulates a CURATED
MODULE MAP -- a handful of high-value, pure-logic modules, each paired with the
focused test file(s) that exercise it -- so a developer never hand-assembles
mutmut CLI args or hand-edits ``[tool.mutmut]`` in ``pyproject.toml``. One
module's scoped sweep stays small enough (its own tests, not the whole suite)
to finish inside a CI job step.

    python scripts/run_mutation.py --list                 # show the module map
    python scripts/run_mutation.py --module block-chain    # sweep one module
    python scripts/run_mutation.py --module block-chain --dry-run
                                                           # validate + print
                                                           #   the scoped config

**Windows is CI-only.** mutmut 3.x hard-``sys.exit(1)``s at import on
``platform.system() == "Windows"`` and imports the POSIX-only ``resource``
module, so it CANNOT run natively on this box (patching the guard still hits
``import resource``). Run the real sweep on Linux -- locally, or via the
``.github/workflows/mutation.yml`` ``workflow_dispatch`` matrix. On a
non-Linux host this script refuses to invoke mutmut and points you there;
``--dry-run`` still works everywhere (it only validates the map + renders the
scoped config, never launching mutmut). See docs/internals/mutation-testing.md.

This runner never edits ``pyproject.toml`` durably: it backs the file up to a
sidecar, writes the scoped ``[tool.mutmut]`` block, runs mutmut, and ALWAYS
restores the original in a ``finally`` (a stale sidecar from an interrupted run
is recovered on the next start). The committed ``[tool.mutmut]`` defaults -- and
the sibling ``scripts/mutmut_shortlist.py`` / scheduled cluster-verb sweep that
depend on them -- are therefore never perturbed.

**paths_to_mutate is RELATIVE, and chdir'ing tests are deselected, not dodged.**
mutmut 3.6.0 derives each mutant's *identity* from the ``paths_to_mutate`` string
at mutant-creation time: a relative ``src/hpc_agent/...`` yields a clean dotted
key (``hpc_agent.execution.mapreduce.combiner.x_...``) that the stats-phase
coverage-join keys on, whereas an ABSOLUTE path bakes the runner's cwd into the
key (``.home.runner.work.....src.hpc_agent....``) and the join then finds no test
covering the module and aborts every mutant -- the triage-2 regression that
zeroed the whole curated matrix (docs/plans/mutation-triage-2-2026-07-17.md,
Finding #1). So the path MUST stay relative.

The reason triage-1 reached for an absolute path was a *different* mutmut
behaviour: ``record_trampoline_hit`` runs ``p.resolve(strict=True)`` over the
(relative) source paths on EVERY mutated-function call, resolving them against
the LIVE cwd -- and mutmut runs the tests under ``change_cwd("mutants")``. An
in-process test that ``monkeypatch.chdir(tmp_path)``\\ s out of the mutants tree
makes ``src/hpc_agent/...`` unresolvable there, so the trampoline raises
``FileNotFoundError`` and crashes stats collection. That is a genuine mutmut-3.6.0
incompatibility with cwd-relocating tests, not something an absolute path should
paper over. The correct fix is to keep the path relative and DESELECT the
handful of chdir'ing in-process tests from the scoped run (``ModuleScope.deselect``
-> pytest ``--deselect``); the module's remaining in-process tests still produce
verdicts. The curated zero-signal tripwire (``--tripwire``) backstops this: if a
newly-added chdir test slips past the deselect list and re-crashes a module, that
module reports zero signal and the tripwire turns the job RED instead of green.
"""

from __future__ import annotations

import argparse
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import tomllib

# Repo root = parent of this scripts/ dir.
REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
# Sidecar backup so an interrupted run (Ctrl-C / kill) can be recovered on the
# next start rather than leaving a scoped pyproject.toml in the working tree.
_BACKUP = REPO_ROOT / "pyproject.toml.run_mutation.bak"


@dataclass(frozen=True)
class ModuleScope:
    """One curated mutation target: a source module + its focused test files."""

    key: str
    source: str  # repo-relative .py file to mutate
    tests: tuple[str, ...]  # repo-relative test file(s) that exercise it
    note: str
    # pytest node-IDs (``file.py::Class`` or ``file.py::Class::test``) to
    # ``--deselect`` from the scoped run. USE ONLY for in-process tests that
    # ``chdir`` out of the mutants tree: mutmut 3.6.0's ``record_trampoline_hit``
    # resolves the relative source path against the live cwd on every mutated
    # call, so such a test crashes stats collection and zeroes the module (see
    # the module docstring). The deselected node-IDs must live in ``tests``; a
    # unit test pins that. Empty for the common case.
    deselect: tuple[str, ...] = ()


# ── the curated module map ────────────────────────────────────────────────────
#
# Selection criteria (docs/internals/mutation-testing.md): pure-logic modules
# where a surviving mutant is a real signal, each with a SMALL, focused test
# file so the scoped sweep stays inside a CI step. ``block-chain`` is the
# reference target -- zero lazy body-imports, so every function is
# mutmut-reachable (see the shortlist tool for why that matters).
#
# Each ``tests`` tuple is the module's TRUE COVERING SET, not just one paired
# file (mutation-triage-2026-07-17 Unit C): the single-file pairing manufactured
# false survivors -- e.g. block_chain's 183 "survivors" were all killed by
# ``tests/contracts/test_spec_hint_completeness.py``, which the old one-file
# scope excluded. The covering set is derived by grepping ``tests/`` for the
# focused files that actually exercise the module's functions (the memo's
# method), accepting the longer per-module runtime -- a false survivor costs
# more than the CI minutes.
MODULE_MAP: dict[str, ModuleScope] = {
    "block-chain": ModuleScope(
        key="block-chain",
        source="src/hpc_agent/infra/block_chain.py",
        tests=(
            "tests/ops/test_block_chain.py",
            # Routes every spec hint through _wrap_run_id_under /
            # _complete_spec_hint + the compose_*_spec round-trips +
            # SuccessorSpecIncomplete.missing (the 183 memo false survivors).
            "tests/contracts/test_spec_hint_completeness.py",
            # chain_successor / next_block_hint cross-coverage.
            "tests/_kernel/lifecycle/test_block_drive.py",
        ),
        note="Deterministic block-successor tables + spec composition. "
        "Pure, zero body-imports -- fully mutmut-reachable. The reference target.",
    ),
    "attestation": ModuleScope(
        key="attestation",
        source="src/hpc_agent/state/attestation.py",
        tests=(
            "tests/state/test_attestation.py",
            # Exercise validate/bind/reduce with real recompute payloads (the
            # load-bearing mismatch-refusal + drift-revocation paths), not just
            # the paired file's message-string asserts.
            "tests/state/test_determinism.py",
            "tests/ops/test_decision_journal_primitives.py",
        ),
        note="Attestation kernel (validate / bind / reduce). Pure logic.",
    ),
    "describe-cache": ModuleScope(
        key="describe-cache",
        source="src/hpc_agent/state/describe_cache.py",
        tests=(
            "tests/cli/test_describe_cache.py",
            # The describe fast path + capabilities path that also drive
            # store()/load()/_cache_path -- the memo's confounding coverage.
            "tests/cli/test_describe.py",
            "tests/cli/test_capabilities_cache.py",
        ),
        note="Build-content-keyed describe cache -- guard-heavy (disable / "
        "safe-name / partial-registry). Some lazy imports blind mutmut (fewer mutants).",
    ),
    "fast-path-cache": ModuleScope(
        key="fast-path-cache",
        # Was tests/cli/test_fast_dispatch.py -- whose _fast_path_cache coverage
        # is all @pytest.mark.slow SUBPROCESS tests (mutmut deselects slow and
        # can't instrument a child interpreter), so mutmut aborted the baseline
        # with "Unable to force test failures" (memo Unit B). The dedicated
        # in-process battery exercises the module directly.
        source="src/hpc_agent/cli/_fast_path_cache.py",
        tests=("tests/cli/test_fast_path_cache.py",),
        note="CLI single-verb fast-path resolution cache. Guard + fingerprint logic. "
        "Paired with the IN-PROCESS battery -- test_fast_dispatch.py's coverage is "
        "subprocess/slow-only, invisible to mutmut (baseline-abort fix).",
    ),
    "capabilities-cache": ModuleScope(
        key="capabilities-cache",
        source="src/hpc_agent/state/capabilities_cache.py",
        tests=(
            "tests/cli/test_capabilities_cache.py",
            "tests/cli/test_describe.py",
        ),
        note="Build+dist-keyed capabilities-envelope cache -- guard-heavy (disable / "
        "dirty / dist-signature / partial-registry / per-variant). Byte-identity to "
        "the walk is the load-bearing invariant. Some lazy imports blind mutmut.",
    ),
    "combiner": ModuleScope(
        key="combiner",
        source="src/hpc_agent/execution/mapreduce/combiner.py",
        # test_combiner_failures.py is DELIBERATELY EXCLUDED (was paired here). Every
        # test in it drives the combiner as a fresh SUBPROCESS -- and it materializes
        # that child by copying ``Path(hpc_agent.__file__).parent/.../combiner.py``.
        # Under mutmut ``hpc_agent.__file__`` resolves INTO the mutants/ tree, so the
        # copied script is the MUTATED combiner, which carries mutmut's trampoline
        # header (``from mutmut.mutation.trampoline import ...``). Running that script
        # as a child re-triggers mutmut's config discovery in the child's cwd (a
        # pytest tmp dir with no src/), which raises "Could not figure out where the
        # code to mutate is" -- so the child prints that instead of the expected
        # HPC_WAVE error, the assertion fails, and mutmut's STATS phase aborts with
        # "failed to collect stats. runner returned 1" (run 29618964851). Being
        # subprocess-only, those tests are also invisible to mutmut's instrumentation
        # (it cannot reach into a child interpreter), so they carry ZERO mutation
        # signal anyway -- the same reason fast-path-cache drops its subprocess battery.
        # The trade-off is honest: combiner.main()'s failure-mode branches (missing
        # env / missing sidecar / malformed metrics) get no mutation verdict here;
        # they are exercised only via subprocess, which mutmut can never instrument.
        tests=("tests/execution/mapreduce/test_combiner.py",),
        note="Deterministic reduce/combine -- the module that computes every "
        "aggregate number. HEAVY (~650 lines): its scoped sweep is the slowest; "
        "budget the most CI time for this key. Two classes of test are held out of "
        "the mutmut run: its end-to-end main() tests chdir() out of the mutants tree "
        "(crashing mutmut 3.6.0's relative source_paths.resolve(strict=True)) and are "
        "DESELECTED (below); its SUBPROCESS failure-mode battery "
        "(test_combiner_failures.py) copies + runs the MUTATED combiner as a child, "
        "tripping mutmut's config bootstrap and aborting stats, so it is dropped from "
        "the covering set entirely (subprocess tests carry no mutation signal). What "
        "remains is the in-process reduce-math battery (grid-key / weighted-mean / "
        "Neumaier-sum), which still produces verdicts. paths_to_mutate stays RELATIVE "
        "(triage-2 Finding #1: an absolute path zeroed the whole matrix via the "
        "coverage-join).",
        # The chdir'ing end-to-end main() tests. Each drives combiner.main()
        # after monkeypatch.chdir(tmp_path); under mutmut that crashes the
        # trampoline's cwd-relative resolve. Class-level except TestGroupSizeWeighting,
        # whose two non-chdir weighting-math tests are KEPT (only its two chdir
        # methods are named).
        deselect=(
            "tests/execution/mapreduce/test_combiner.py::TestMainEndToEnd",
            "tests/execution/mapreduce/test_combiner.py::TestMainMissingMetrics",
            "tests/execution/mapreduce/test_combiner.py::TestMainParallelReads",
            "tests/execution/mapreduce/test_combiner.py::TestMainMultipleGridPoints",
            "tests/execution/mapreduce/test_combiner.py::TestMainWritesOutputAtomically",
            "tests/execution/mapreduce/test_combiner.py::TestFrozenManifestCombine",
            "tests/execution/mapreduce/test_combiner.py::TestGroupSizeWeighting::test_wave_partial_carries_group_count",
            "tests/execution/mapreduce/test_combiner.py::TestGroupSizeWeighting::test_nine_one_wave_split_is_task_weighted",
            "tests/execution/mapreduce/test_combiner.py::TestRuntimeAggregation",
            "tests/execution/mapreduce/test_combiner.py::TestFinalReduceRunScopedLayout",
            "tests/execution/mapreduce/test_combiner.py::TestGroupSizeWeighting",
            "tests/execution/mapreduce/test_combiner.py::TestRunScopedNoClobber",
            "tests/execution/mapreduce/test_combiner.py::TestTasksReadEvidence",
            "tests/execution/mapreduce/test_combiner.py::TestFinalReduceForeignSkip",
        ),
    ),
    # ── the correctness / consent / journal core (memo Unit D) ─────────────────
    # These rank ABOVE renders on the risk ordering yet were in NO target set
    # before. Each is paired with the focused files that exercise it directly
    # (journal/index have diffuse coverage -- the tightest behavior tests are
    # selected, not every tangential mention).
    "state-journal": ModuleScope(
        key="state-journal",
        source="src/hpc_agent/state/journal.py",
        tests=(
            "tests/state/test_wp_i_journal_hygiene.py",
            "tests/state/test_submitting_state.py",
            "tests/state/test_watchdog_and_kill_state.py",
        ),
        note="Per-run journal RMW (load/upsert/update/mark) + paired index refresh. "
        "Correctness core: a silent wrong-path corrupts the run record.",
        # test_stamp_tick_defaults_experiment_dir_to_cwd monkeypatch.chdir(tmp_path)
        # then calls journal RMW to prove the cwd-default -- exactly the in-process
        # chdir that crashes mutmut's cwd-relative resolve. Deselected so the rest
        # of the journal covering set produces verdicts (the other tests pass an
        # explicit experiment_dir and never chdir).
        deselect=(
            "tests/state/test_watchdog_and_kill_state.py::test_stamp_tick_defaults_experiment_dir_to_cwd",
        ),
    ),
    "state-index": ModuleScope(
        key="state-index",
        source="src/hpc_agent/state/index.py",
        tests=(
            "tests/state/test_submitting_state.py",
            "tests/state/test_wp_i_journal_hygiene.py",
            "tests/state/test_pending_verdict.py",
        ),
        note="Index scan/rebuild/prune + cross-run queries (find_in_flight / "
        "find_submitting / find_by_campaign). Pure-logic query core.",
    ),
    "decision-journal": ModuleScope(
        key="decision-journal",
        source="src/hpc_agent/state/decision_journal.py",
        tests=(
            "tests/state/test_decision_journal.py",
            "tests/ops/test_decision_journal_primitives.py",
        ),
        note="Decision-journal read/append -- the consent-gate substrate. A "
        "wrong-path here silently mis-records a human sign-off.",
    ),
    "consent-hint": ModuleScope(
        key="consent-hint",
        source="src/hpc_agent/_kernel/lifecycle/consent_hint.py",
        tests=(
            "tests/ops/test_approve_hint.py",
            "tests/_kernel/lifecycle/test_block_drive.py",
        ),
        note="Pure composer for the OFFERED-CONSENT scoped-utterance hint "
        "(compose_approve_hint / brief_cluster). Deterministic string work; a "
        "mutated scope token would mislabel what a 'y' grants.",
    ),
}


def _fmt_map() -> str:
    """Render the module map as an aligned, human-readable block."""
    width = max(len(k) for k in MODULE_MAP)
    lines = ["Curated mutation module map (--module <key>):", ""]
    for scope in MODULE_MAP.values():
        lines.append(f"  {scope.key.ljust(width)}  {scope.source}")
        for t in scope.tests:
            lines.append(f"  {' '.ljust(width)}    tests: {t}")
        lines.append(f"  {' '.ljust(width)}    {scope.note}")
        lines.append("")
    return "\n".join(lines)


def _validate_scope(scope: ModuleScope) -> list[str]:
    """Return a list of problems (missing source/test paths); empty when clean."""
    problems: list[str] = []
    src = REPO_ROOT / scope.source
    if not src.is_file():
        problems.append(f"source not found: {scope.source}")
    for t in scope.tests:
        if not (REPO_ROOT / t).exists():
            problems.append(f"test path not found: {t}")
    return problems


def _replace_named_array(text: str, key: str, values: list[str]) -> str:
    """Replace the value array of ``<key> = [ ... ]`` inside ``[tool.mutmut]``.

    A minimal line-based rewrite (no tomlkit dep) mirroring
    ``scripts/mutmut_shortlist.py._apply_to_pyproject``: it finds the ``key``
    assignment inside the ``[tool.mutmut]`` table and rewrites through the array's
    closing ``]``, preserving every sibling key. Raises if the key is absent so a
    silent no-scope can never slip through.
    """
    lines = text.splitlines()
    out: list[str] = []
    in_mutmut = False
    replaced = False
    new_block = [f"{key} = ["] + [f'    "{v}",' for v in values] + ["]"]
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_mutmut = stripped == "[tool.mutmut]"
        if in_mutmut and stripped.startswith(key):
            while i < len(lines) and "]" not in lines[i]:
                i += 1
            i += 1  # skip the closing-bracket line
            out.extend(new_block)
            replaced = True
            continue
        out.append(line)
        i += 1
    if not replaced:
        raise SystemExit(f"could not find [tool.mutmut].{key} in {PYPROJECT}")
    return "\n".join(out) + "\n"


def render_scoped_pyproject(scope: ModuleScope) -> str:
    """Return the pyproject.toml text scoped to *scope* (source + tests only).

    Rewrites ``[tool.mutmut].paths_to_mutate`` to the single source module and
    ``[tool.mutmut].tests_dir`` to the module's focused test file(s). mutmut 3.x
    treats both keys as deprecated aliases (``source_paths`` /
    ``pytest_add_cli_args_test_selection``) but honours them, and ``tests_dir``
    accepts individual test-file paths -- that is the per-module test-selection
    lever that keeps one sweep inside a CI step. Every other key (``also_copy``,
    ``do_not_mutate``, the xdist-override ``pytest_add_cli_args``) is preserved.

    ``paths_to_mutate`` is written RELATIVE (``scope.source``, a repo-relative
    POSIX path), exactly like ``scripts/mutmut_shortlist.py``'s working sweep.
    mutmut 3.6.0 derives each mutant's IDENTITY from this string at creation
    time; a relative ``src/hpc_agent/...`` produces the clean dotted key the
    stats-phase coverage-join keys on, while an absolute path bakes the runner
    cwd into the key and the join then covers nothing -- the triage-2 regression
    that zeroed the whole curated matrix (docs/plans/mutation-triage-2-2026-07-17.md).

    The cwd-relative ``resolve(strict=True)`` crash that triage-1's absolute path
    was dodging (an in-process test ``chdir``\\ ing out of the mutants tree) is
    handled the correct way instead: ``scope.deselect`` names the chdir'ing tests
    and they are ``--deselect``\\ ed from the scoped ``pytest_add_cli_args`` below,
    so the relative path never has to resolve while a test sits in a foreign cwd.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    text = _replace_named_array(text, "paths_to_mutate", [scope.source])
    text = _replace_named_array(text, "tests_dir", list(scope.tests))
    if scope.deselect:
        # Append --deselect args to the existing pytest_add_cli_args (never
        # replace the xdist/addopts overrides the committed block carries).
        existing = tomllib.loads(text)["tool"]["mutmut"]["pytest_add_cli_args"]
        combined = list(existing) + [f"--deselect={node_id}" for node_id in scope.deselect]
        text = _replace_named_array(text, "pytest_add_cli_args", combined)
    # Fail loudly if the rewrite produced non-parseable TOML.
    tomllib.loads(text)
    return text


# mutant exit codes in a ``*.meta`` ``exit_code_by_key`` map: 1 = killed,
# 0 = survived, 33/34 = no-tests/skipped, ``null`` = NEVER EXECUTED. This mirrors
# ``mutmut_shortlist.count_checked_mutants`` but returns the STRONGER *signal*
# count the curated tripwire gates on.
def count_mutant_signal(mutants_dir: Path) -> tuple[int, int, int]:
    """Return ``(signal, checked, total)`` across every ``*.meta`` under *dir*.

    * ``total`` -- every mutant key mutmut generated.
    * ``checked`` -- keys with a NON-NULL exit code (killed / survived / no-tests /
      skipped): mutmut evaluated them at all.
    * ``signal`` -- keys that were killed (1) OR survived (0): a mutant that
      actually exercised the module. This is what the tripwire gates on -- a run
      whose mutants are ALL exit-33 "no tests" is ``checked > 0`` but carries no
      mutation signal (the triage-2 caveat), so ``checked`` alone can read green
      on a dark run. Pure I/O over the same ``*.meta`` artifacts CI uploads, so it
      is unit-testable without ever launching mutmut.
    """
    import json

    signal = 0
    checked = 0
    total = 0
    for meta in sorted(mutants_dir.rglob("*.meta")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        codes = data.get("exit_code_by_key")
        if not isinstance(codes, dict):
            continue
        for code in codes.values():
            total += 1
            if code is not None:
                checked += 1
            if code in (0, 1):
                signal += 1
    return signal, checked, total


def _tripwire(mutants_dir: Path) -> int:
    """Curated zero-signal tripwire: exit 1 unless a mutant was killed or survived.

    The curated matrix inherits mutmut's "survivors are not a failure" semantics,
    so a module that aborted with ZERO verdicts (e.g. a chdir test crashed stats
    collection, or the paths_to_mutate coverage-join found nothing) still concluded
    GREEN before this existed (triage-2 Finding #2). Mirrors the sweep tripwire in
    ``mutmut_shortlist.py`` so the same green==signal guarantee holds per curated
    module.
    """
    signal, checked, total = count_mutant_signal(mutants_dir)
    print(
        f"curated mutation tripwire: {signal} with-signal (killed/survived) / "
        f"{checked} checked / {total} generated mutant(s)."
    )
    if signal == 0:
        print(
            "CURATED TRIPWIRE FAILED: not one mutant was killed or survived -- the "
            "module produced NO mutation signal (aborted stats / all 'no tests'). A "
            "green curated job must mean signal. This is the triage-2 zero-signal-but-"
            "green failure; check the scoped paths_to_mutate + tests_dir + deselect "
            "(a chdir'ing in-process test crashes mutmut's cwd-relative resolve).",
            file=sys.stderr,
        )
        return 1
    print("tripwire OK: the curated module produced at least one killed/survived mutant.")
    return 0


def _recover_stale_backup() -> None:
    """Restore pyproject from a leftover sidecar (a prior interrupted run)."""
    if _BACKUP.exists():
        print(f"recovering pyproject.toml from stale backup {_BACKUP.name} (prior run interrupted)")
        PYPROJECT.write_text(_BACKUP.read_text(encoding="utf-8"), encoding="utf-8")
        _BACKUP.unlink()


def run_sweep(scope: ModuleScope) -> int:
    """Scope pyproject, run mutmut, restore pyproject, print survivors.

    Assumes the caller already gated on platform (mutmut is unusable on Windows).
    """
    original = PYPROJECT.read_text(encoding="utf-8")
    scoped = render_scoped_pyproject(scope)
    _BACKUP.write_text(original, encoding="utf-8")
    try:
        PYPROJECT.write_text(scoped, encoding="utf-8")
        print(f"scoped [tool.mutmut] to {scope.source}")
        print(f"  tests: {', '.join(scope.tests)}\n")

        mutants = REPO_ROOT / "mutants"
        if mutants.exists():
            import shutil

            shutil.rmtree(mutants, ignore_errors=True)

        # mutmut exits non-zero when any mutant survives -- that is the SIGNAL,
        # not a runner failure, so a non-zero ``run`` is tolerated and the
        # results step below carries the outcome.
        #
        # Launch through scripts/_mutmut_guarded_run.py, NOT ``-m mutmut``: mutmut
        # 3.6.0's run loop reaps children with a bare os.wait() and looks the pid up
        # in its worker table unguarded, so a stray child reparented to the run
        # process -- a subprocess a covering test spawns during the IN-PROCESS stats
        # phase, orphaned when its parent exits -- raises KeyError and aborts the whole
        # module on the FIRST reap, leaving every mutant "not checked" (consent-hint,
        # run 29618964851). The launcher makes os.wait() drop pids mutmut never forked.
        # ``stderr=STDOUT`` merges mutmut's stderr into the tee'd stdout so a mutmut
        # traceback lands in the artifact instead of being lost (that KeyError read as
        # a silent exit 1 because the workflow's ``| tee`` captured stdout only).
        run = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "_mutmut_guarded_run.py")],
            cwd=REPO_ROOT,
            text=True,
            encoding="utf-8",
            stderr=subprocess.STDOUT,
        )
        print(
            f"\nmutmut run exit code: {run.returncode} (non-zero = survivors/skips, not a failure)"
        )

        results = subprocess.run(
            [sys.executable, "-m", "mutmut", "results"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        print("\n===== mutmut results =====")
        print(results.stdout or "(no results output)")
        if results.stderr:
            print(results.stderr, file=sys.stderr)
    finally:
        PYPROJECT.write_text(original, encoding="utf-8")
        _BACKUP.unlink(missing_ok=True)
        print("restored original pyproject.toml")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Curated per-module mutation-testing runner (devx B4).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--module",
        metavar="KEY",
        help="run a scoped mutation sweep on this module key (see --list).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="print the curated module map and exit.",
    )
    parser.add_argument(
        "--keys",
        action="store_true",
        help="print the module keys as a JSON array (drives the CI matrix) and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the module + render the scoped [tool.mutmut] block "
        "WITHOUT running mutmut (works on every platform).",
    )
    parser.add_argument(
        "--tripwire",
        action="store_true",
        help="curated zero-signal tripwire: FAIL (exit 1) unless a mutant in "
        "--mutants-dir was killed or survived (a green curated job MUST mean signal).",
    )
    parser.add_argument(
        "--mutants-dir",
        metavar="DIR",
        default="mutants",
        help="--tripwire: the mutmut output dir to scan for *.meta (default: mutants).",
    )
    args = parser.parse_args(argv)

    if args.tripwire:
        mutants_dir = Path(args.mutants_dir)
        if not mutants_dir.is_absolute():
            mutants_dir = REPO_ROOT / mutants_dir
        return _tripwire(mutants_dir)

    if args.keys:
        import json

        print(json.dumps(list(MODULE_MAP)))
        return 0

    if args.list or (not args.module and not args.dry_run):
        print(_fmt_map())
        if not args.list:
            print("Pass --module <key> to run a sweep, or --dry-run to validate.")
        return 0

    if not args.module:
        print("error: --dry-run requires --module <key>.", file=sys.stderr)
        print(_fmt_map(), file=sys.stderr)
        return 2

    scope = MODULE_MAP.get(args.module)
    if scope is None:
        print(f"error: unknown module key {args.module!r}.\n", file=sys.stderr)
        print(_fmt_map(), file=sys.stderr)
        return 2

    problems = _validate_scope(scope)
    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"module {scope.key!r} validated: source + {len(scope.tests)} test path(s) exist.\n")
        print("scoped [tool.mutmut] block that would be written:\n")
        scoped = render_scoped_pyproject(scope)
        # Echo just the [tool.mutmut] section for a readable proof.
        section: list[str] = []
        capturing = False
        for line in scoped.splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                if s == "[tool.mutmut]":
                    capturing = True
                elif capturing:
                    break
            if capturing:
                section.append(line)
        print("\n".join(section))
        print("\n(dry run -- mutmut NOT invoked; TOML validated as parseable.)")
        return 0

    # Real sweep: gate on platform. mutmut is unusable on Windows.
    if platform.system() != "Linux":
        print(
            f"refusing to run mutmut on {platform.system()}: mutmut 3.x is Linux-only "
            "(it sys.exit(1)s on Windows and imports the POSIX-only `resource` module).",
            file=sys.stderr,
        )
        print(
            "Run the real sweep on Linux: locally, or via the "
            "`.github/workflows/mutation.yml` workflow_dispatch matrix.\n"
            "On this box, use --dry-run to validate the scoped config.",
            file=sys.stderr,
        )
        return 3

    _recover_stale_backup()
    return run_sweep(scope)


if __name__ == "__main__":
    raise SystemExit(main())
