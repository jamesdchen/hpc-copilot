"""Single entry point for the whole generated-artifact regen pipeline.

One recipe, one invocation — replaces the six-plus-one scattered
``python scripts/build_*.py`` incantations that three different docs each
enumerated as a *different* subset (the drift that shipped a stale
``cli/_verb_module_map.py`` more than once). Invoke it exactly two ways::

    python scripts/regen_all.py --check    # CI / pre-commit gate: report drift
    python scripts/regen_all.py --write    # apply: regenerate every artifact

Bare invocation is REFUSED (rc 2): the underlying scripts have inconsistent
bare-argv semantics (``build_schemas`` previews a diff, the index scripts
WRITE, ``build_verb_module_map`` checks-without-writing), so a single entry
point must not inherit that ambiguity — the caller states intent explicitly.

The eight steps run as **subprocesses of the current interpreter** (never
in-process imports) in the dependency order below. Subprocess isolation
matches exactly how pre-commit and CI invoke them today, so behaviour is
provably unchanged, and it sidesteps the env-timing / registry-cache
ordering hazards of chaining registry-mutating ``main()``s in one process
(each script does its own ``os.environ.setdefault`` + registry warm-up
before the first ``hpc_agent`` import).

Order (WS1 DC1, verified against the scripts):

1. ``build_schemas``            — Pydantic models -> ``schemas/*.json``;
   the operations catalog resolves schema names by FILE EXISTENCE, so
   schemas must exist first.
2. ``bake_operations_json``     — registry projection -> ``operations.json``.
3. ``build_primitive_frontmatter`` — scaffolds each ``docs/primitives/<name>.md``
   stub; the index parses that frontmatter, so frontmatter strictly precedes it.
4. ``build_primitive_index``    — ``docs/primitives/README.md`` catalog table.
5. ``build_operations_index``   — subprocess ``capabilities`` -> ``docs/generated/operations.md``.
6. ``build_verb_module_map``    — registry-only CLI fast-path map (order-free;
   placed sixth for determinism).
7. ``build_principles_index``   — regenerates the section listing in
   ``docs/internals/engineering-principles.md`` from the ``principles/<slug>.md``
   frontmatter; registry-free (reads only the section files), so its only
   ordering constraint is that it precede the pending-docs check.
8. ``check_no_pending_primitive_docs`` — LAST, so a freshly scaffolded stub fails
   loudly (correct: the human must fill the body).

Failure policy (both modes): run EVERY step regardless of failures, print one
PASS/FAIL line per step, exit non-zero if any step failed. Full visibility
beats a truncated report for a ~20s, seven-step run — in ``--check`` one run
surfaces ALL drift; in ``--write`` a failed earlier step only makes later
diffs loud, never silently wrong.

``REGEN_SCRIPTS`` is the module-level canonical list of the eight steps (the
tuple other units probe — e.g. a count gate — since the pre-commit hooks may
collapse to one). It is the single source of truth for the pipeline order.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ── The canonical pipeline ────────────────────────────────────────────────
#
# Each entry: (script stem, --check argv, --write argv). The per-script argv
# absorbs the scripts' INCONSISTENT flag semantics so this entry point never
# inherits them:
#   - build_schemas / bake_operations_json / build_primitive_frontmatter /
#     build_verb_module_map: take an explicit ``--check`` or ``--write``.
#   - build_primitive_index / build_operations_index: test ONLY ``--check``;
#     any other argv (bare) falls through to the WRITE branch — so ``--write``
#     is expressed as bare argv ``[]`` there. (Passing ``--write`` would also
#     write, but bare is the documented shape those scripts recognise.)
#   - check_no_pending_primitive_docs: takes no flags; it is ALWAYS a check
#     and runs last in both modes.
_STEPS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("build_schemas", ("--check",), ("--write",)),
    ("bake_operations_json", ("--check",), ("--write",)),
    ("build_primitive_frontmatter", ("--check",), ("--write",)),
    ("build_primitive_index", ("--check",), ()),
    ("build_operations_index", ("--check",), ()),
    ("build_verb_module_map", ("--check",), ("--write",)),
    ("build_principles_index", ("--check",), ("--write",)),
    ("check_no_pending_primitive_docs", (), ()),
)

# Canonical ordered list of the pipeline's script stems. Exported so other
# units read the pipeline membership/order from ONE place (the pre-commit
# hooks may collapse to a single ``regen-all`` hook, so the tuple — not the
# hook list — is the source of truth). A LITERAL tuple: the frozen seam is
# consumed by static AST (tests/contracts/test_doc_frozen_counts.py), so it
# must be parseable without executing this module; the assert below pins it
# lockstep with _STEPS.
REGEN_SCRIPTS: tuple[str, ...] = (
    "build_schemas",
    "bake_operations_json",
    "build_primitive_frontmatter",
    "build_primitive_index",
    "build_operations_index",
    "build_verb_module_map",
    "build_principles_index",
    "check_no_pending_primitive_docs",
)
assert tuple(stem for stem, _check, _write in _STEPS) == REGEN_SCRIPTS


def _run_step(stem: str, argv: tuple[str, ...]) -> int:
    """Run one regen script as a subprocess; return its exit code."""
    script = REPO_ROOT / "scripts" / f"{stem}.py"
    proc = subprocess.run(
        [sys.executable, str(script), *argv],
        cwd=REPO_ROOT,
    )
    return proc.returncode


def regen_all(*, write: bool) -> int:
    """Run every regen step in order; report all; return 0 iff all passed.

    ``write=True`` regenerates in place; ``write=False`` gates (``--check``).
    Runs ALL steps regardless of individual failures (run-all-report-all).
    """
    mode = "--write" if write else "--check"
    if write:
        # Keep the generated-artifact merge driver installed (repo-local git
        # config; idempotent). --check stays a pure gate. Loudly degrading:
        # a missing/failing installer never blocks regen.
        ensure = REPO_ROOT / "scripts" / "merge_generated.py"
        if ensure.is_file():
            rc = subprocess.run(
                [sys.executable, str(ensure), "ensure"], cwd=REPO_ROOT, check=False
            ).returncode
            if rc != 0:
                print(f"WARN merge_generated.py ensure exited {rc}", file=sys.stderr)
        else:
            print("WARN scripts/merge_generated.py missing — driver not ensured", file=sys.stderr)
    print(f"regen_all ({mode}): {len(_STEPS)} steps")
    failed: list[str] = []
    for stem, check_argv, write_argv in _STEPS:
        argv = write_argv if write else check_argv
        rc = _run_step(stem, argv)
        status = "PASS" if rc == 0 else f"FAIL (rc={rc})"
        shown = " ".join(argv) if argv else "(no args)"
        print(f"  [{status}] {stem} {shown}")
        if rc != 0:
            failed.append(stem)
    if failed:
        print(
            f"regen_all ({mode}): {len(failed)} of {len(_STEPS)} step(s) failed: "
            + ", ".join(failed),
            file=sys.stderr,
        )
        return 1
    print(f"regen_all ({mode}): all {len(_STEPS)} steps passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    check = "--check" in args
    write = "--write" in args
    if check == write:  # neither, or both — refuse
        print(
            "usage: python scripts/regen_all.py --check | --write\n"
            "  --check  gate: regenerate-and-compare every artifact, report drift\n"
            "  --write  apply: regenerate every artifact in place\n"
            "Exactly one mode is required (bare invocation is refused: the\n"
            "underlying scripts' bare semantics are inconsistent).",
            file=sys.stderr,
        )
        return 2
    return regen_all(write=write)


if __name__ == "__main__":
    sys.exit(main())
