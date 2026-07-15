"""Keep-ours merge driver for 100%-regenerable artifacts + its deployer.

The dominant cross-worktree / swarm merge-conflict class in this repo is the
set of files that are a PURE FUNCTION of the primitive registry
(``src/hpc_agent/operations.json`` — 48 touches since 2026-07-01,
``docs/generated/operations.md`` — 36, ``src/hpc_agent/cli/_verb_module_map.py``
— 33, the emitted JSON schemas). Two branches that each add a verb both
regenerate these files and collide on the diff, even though the collision is
mechanical: re-running the regen scripts over the merged registry reproduces
the file exactly.

This module supplies a custom git merge driver named ``generated`` that, for a
both-sides-changed file carrying ``merge=generated`` in ``.gitattributes``,
KEEPS OURS and exits 0 (git's built-ins are text/binary/union only — there is
no attribute-level ``ours`` driver, so a script is required). The mandatory
follow-up is a single regen pass, which rebuilds every kept-ours file from the
merged registry. The driver prints ONE loud stderr line naming the path and the
required follow-up so the human never silently ships a stale generated file;
the hard backstop is CI's ``--check`` gates on push.

Subcommands
-----------
``driver %O %A %B %P``
    The git merge-driver entry. ``%A`` (ours) is left byte-untouched and the
    process exits 0, so git takes ours. Prints the regen notice to stderr.

``ensure``
    Idempotently install the driver into the repo-local git config
    (``merge.generated.driver`` / ``.name``). Worktrees share the common git
    dir's config, so ONE ``ensure`` per clone covers every agent worktree.
    WS1's ``scripts/regen_all.py`` is expected to call this at the top of every
    run so the driver self-installs wherever regen runs; until that wire lands
    it is runnable by hand. A clone that has never run ``ensure`` degrades
    LOUDLY — the attribute is declared but the driver is undefined, so git
    falls back to a normal text merge and CONFLICTS (never a silent wrong pick).

``check``
    Exit 0 iff the driver is installed (config references this script), else 1.
    A cheap hook for CI/doctor; not wired anywhere by this unit.

merge OR REBASE
---------------
Git invokes merge drivers from BOTH ``git merge`` and the merge machinery
underneath ``git rebase`` / ``git cherry-pick`` — but rebase INVERTS the sides,
so during a rebase "ours" is the branch being replayed ONTO (e.g. ``main``) and
the replayed commit's own generated changes are the "theirs" that keep-ours
discards. An agent branch that rebases onto main therefore silently drops its
OWN generated edits at each step until a regen pass restores them from the
merged source. This is self-healing (regen rebuilds from the merged registry
either way) and never silent at HEAD-of-main (CI ``--check`` is the backstop),
but it is why the notice and the enforcement row say "after any merge OR
rebase touching generated files, run regen".

One definition
--------------
``FULLY_GENERATED_PATTERNS`` / ``SCHEMA_MERGE_UNSET`` / ``PARTIALLY_GENERATED_EXCLUDED``
below are THE manifest of what carries ``merge=generated``; the root
``.gitattributes`` mirrors them and ``tests/contracts/test_generated_merge_driver.py``
pins the two in lockstep AND against the live regen-script outputs. WS1's
``regen_all`` and any future doctor check may import these constants.
"""

from __future__ import annotations

import shlex
import subprocess
import sys

# --------------------------------------------------------------------------- #
# The one-definition manifest (mirrored verbatim by root .gitattributes,
# pinned by tests/contracts/test_generated_merge_driver.py).
# --------------------------------------------------------------------------- #

# gitattributes path patterns that carry ``merge=generated``. Each is a file
# recreated IN FULL by a regen script from the registry, so keep-ours-then-regen
# never loses information. The ``src/hpc_agent/schemas/*.json`` glob is
# single-star (does NOT cross ``/``) so nested ``schemas/skill_returns/*.json``
# — which are mirror-authored from ``cli/skill_returns.py``, not emitted by
# ``build_schemas.py`` — are deliberately NOT covered.
FULLY_GENERATED_PATTERNS: tuple[str, ...] = (
    "docs/generated/**",
    "src/hpc_agent/cli/_verb_module_map.py",
    "src/hpc_agent/operations.json",
    "src/hpc_agent/schemas/*.json",
)

# Top-level ``schemas/*.json`` files that the glob above would sweep in but that
# ``build_schemas.py`` does NOT emit — hand-authored composite / preflight
# operation schemas. Keep-ours on these would silently drop theirs-side hand
# edits that no regen script restores (the exact silent-wrong-pick class run-13
# finding 1 exists to kill), so they are explicitly reset to git's default
# 3-way text merge with ``!merge``. Pinned equal to (live top-level schemas −
# build_schemas emitted set) by the contract test: a NEW hand-authored schema
# turns the pin RED (loud), forcing an entry here rather than a silent inclusion.
SCHEMA_MERGE_UNSET: tuple[str, ...] = (
    "src/hpc_agent/schemas/aggregate_preflight.input.json",
    "src/hpc_agent/schemas/aggregate_preflight.output.json",
    "src/hpc_agent/schemas/check_task_generator_mismatch.input.json",
    "src/hpc_agent/schemas/check_task_generator_mismatch.output.json",
    "src/hpc_agent/schemas/classify_axis_preflight.input.json",
    "src/hpc_agent/schemas/classify_axis_preflight.output.json",
    "src/hpc_agent/schemas/decide_resubmit.input.json",
    "src/hpc_agent/schemas/decide_resubmit.output.json",
    "src/hpc_agent/schemas/detect_entry_point.input.json",
    "src/hpc_agent/schemas/detect_entry_point.output.json",
    "src/hpc_agent/schemas/inspect_deployment.input.json",
    "src/hpc_agent/schemas/inspect_deployment.output.json",
    "src/hpc_agent/schemas/inspect_parallel_axes.input.json",
    "src/hpc_agent/schemas/inspect_parallel_axes.output.json",
    "src/hpc_agent/schemas/prepare_followup_specs.input.json",
    "src/hpc_agent/schemas/prepare_followup_specs.output.json",
    "src/hpc_agent/schemas/prepare_phase2_spec.output.json",
    "src/hpc_agent/schemas/resolve_resources.input.json",
    "src/hpc_agent/schemas/resolve_resources.output.json",
    "src/hpc_agent/schemas/smoke_test_executor.input.json",
    "src/hpc_agent/schemas/smoke_test_executor.output.json",
    "src/hpc_agent/schemas/status_preflight.input.json",
    "src/hpc_agent/schemas/status_preflight.output.json",
    "src/hpc_agent/schemas/submit_preflight.input.json",
    "src/hpc_agent/schemas/submit_preflight.output.json",
)

# Files that LOOK generated but are only PARTIALLY generated — regen rewrites a
# fenced region or the YAML frontmatter, leaving hand-authored prose. Keep-ours
# would silently discard theirs-side prose that regen does NOT restore, so these
# must NEVER carry ``merge=generated``. Kept here (with rationale) as the
# negative half of the manifest; the contract test asserts none resolves to the
# driver.
PARTIALLY_GENERATED_EXCLUDED: dict[str, str] = {
    "docs/primitives/README.md": (
        "regen rewrites only the table between the BEGIN/END PRIMITIVE CATALOG "
        "markers; the surrounding prose is hand-authored"
    ),
    "docs/primitives/*.md": (
        "the frontmatter script rewrites only YAML frontmatter; each doc's body "
        "is hand-authored prose"
    ),
}

# The regen entry point (WS1) and its constituent scripts, named in the driver's
# stderr notice. regen_all.py may not exist yet when this unit merges first, so
# the notice names BOTH it and the six underlying regen scripts.
_REGEN_ALL = "scripts/regen_all.py"
_SIX_REGEN_SCRIPTS: tuple[str, ...] = (
    "scripts/build_schemas.py",
    "scripts/bake_operations_json.py",
    "scripts/build_primitive_frontmatter.py",
    "scripts/build_primitive_index.py",
    "scripts/build_operations_index.py",
    "scripts/build_verb_module_map.py",
)

# Git config keys for the driver.
_CFG_DRIVER = "merge.generated.driver"
_CFG_NAME = "merge.generated.name"
_DRIVER_NAME = "keep-ours for regenerable artifacts (scripts/merge_generated.py)"

# Marker that identifies OUR driver command inside git config (interpreter-path
# independent, so `check` is robust across clones/interpreters).
_DRIVER_MARKER = "scripts/merge_generated.py driver"


def _regen_notice(path: str) -> str:
    scripts = " ".join(_SIX_REGEN_SCRIPTS)
    return (
        f"[merge=generated] kept OURS for {path}. This file is regenerable; "
        f"after this merge OR rebase, run `{_REGEN_ALL} --write` (or the six "
        f"regen scripts: {scripts}) and commit the result before pushing - "
        f"CI --check gates on stale generated files."
    )


def _driver_command() -> str:
    """The command written into ``merge.generated.driver``.

    Uses ``sys.executable`` (sh-quoted — this repo's clone path contains a
    space, ``CC Allowed``, and git executes drivers via ``sh -c``) plus the
    RELATIVE script path. Git runs merge drivers with cwd = the merging
    worktree's top level, so the relative path resolves in every worktree while
    never embedding a second space-bearing absolute path.
    """
    return f"{shlex.quote(sys.executable)} scripts/merge_generated.py driver %O %A %B %P"


def _run_driver(argv: list[str]) -> int:
    # git calls: driver %O %A %B %P  ->  argv == [O, A, B, P]
    path = argv[3] if len(argv) >= 4 else "<unknown>"
    # Keep OURS: %A already holds our version; leave it byte-untouched and
    # succeed so git takes it. Only emit the loud follow-up notice.
    sys.stderr.write(_regen_notice(path) + "\n")
    return 0


def _in_git_repo() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.returncode == 0


def _git_config_get(key: str) -> str | None:
    result = subprocess.run(
        ["git", "config", "--get", key],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_config_set(key: str, value: str) -> None:
    subprocess.run(
        ["git", "config", key, value],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _ensure() -> int:
    if not _in_git_repo():
        sys.stderr.write("merge_generated ensure: not inside a git repository\n")
        return 1
    _git_config_set(_CFG_NAME, _DRIVER_NAME)
    _git_config_set(_CFG_DRIVER, _driver_command())
    return 0


def _check() -> int:
    if not _in_git_repo():
        return 1
    driver = _git_config_get(_CFG_DRIVER)
    if driver and _DRIVER_MARKER in driver:
        return 0
    return 1


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        sys.stderr.write("usage: merge_generated.py {driver %O %A %B %P | ensure | check}\n")
        return 2
    sub, rest = args[0], args[1:]
    if sub == "driver":
        return _run_driver(rest)
    if sub == "ensure":
        return _ensure()
    if sub == "check":
        return _check()
    sys.stderr.write(f"merge_generated.py: unknown subcommand {sub!r}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
