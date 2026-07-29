"""Content-addressed remote CODE trees — path composition, identity, GC planning.

The plan's S4 resolution (``docs/plans/run-queue-placement-2026-07-28.md``
§10.S4). Today every run of an experiment shares ONE remote tree
(``rsync_push`` stage-swaps with ``--delete`` into a single ``remote_path``),
so a job that is WAITING in the scheduler queue executes whatever a LATER push
left behind — "the job waiting in line runs the wrong code". The run-start
code-identity check (``state/code_drift.py`` + the layer-1/2 dedup gates) turns
that into a loud park instead of a silent provenance lie, but catching-and-
parking is not the same as the job running the code it was submitted with.

Content-addressing fixes it by construction: a submission deploys its code
snapshot to a tree KEYED BY THAT SNAPSHOT'S CONTENT, and the job's
``$REPO_DIR`` names that tree. Nothing a later push does can reach a tree whose
name is its own content hash, because a changed snapshot hashes to a DIFFERENT
name. The existing run-start check stays — defense in depth, and with the trees
in place it becomes an assertion that essentially never fires, which is the
correct end state for a guard.

The layout, and why
-------------------

::

    <remote_path>/                      the BASE — unchanged, still the anchor
      .hpc/trees/<digest12>/            an immutable CODE snapshot
        results   -> <base>/results     symlink (run-mutable, shared)
        logs      -> <base>/logs        symlink
        _combiner -> <base>/_combiner   symlink
        _aggregated -> <base>/_aggregated
        .hpc_failed -> <base>/.hpc_failed
        .hpc/runs -> <base>/.hpc/runs   symlink
        .hpc/announce -> <base>/.hpc/announce
        .hpc-tree-sealed                written LAST — the "complete" proof
      results/ logs/ .hpc/runs/ ...     the shared run-mutable state

``.hpc/trees/`` (not a sibling of ``remote_path``, not a new top-level dir)
because ``.hpc/`` is ALREADY the repo's cluster-side framework namespace: the
dispatcher, the combiner, the templates, the deploy/push manifests and the run
sidecars all live there, and the base push's ``--delete`` already has a
protection list for exactly that content (:data:`PROTECTED_RUNTIME_FILES`).
Putting the trees anywhere else would have invented a second convention *and* a
second ``--delete`` hazard. It is also the layout §10.S4 names.

**Only ONE variable moves.** ``remote_path`` keeps its whole meaning: the rsync
destination, the ``cd`` the submit dials into (so ``sbatch``/``qsub`` reads the
job script from the base — and the scheduler spools that script at submit time,
which freezes it independently), the ``log_dir``, the jobmap markers, the
results the pull/aggregate path reads. Only ``job_env["REPO_DIR"]`` — the
directory the *job* ``cd``s into at RUN time (``hpc_preamble.sh``) — becomes the
digest tree. That is §10.S4's "pointing a run at it is one variable".

**Everything run-mutable inside the tree is a symlink to the base.** The tree is
code; a run's outputs are not. ``result_dir_template`` ("results/{run_id}/...")
is resolved RELATIVE TO CWD by the cluster dispatcher, and ``hpc_preamble.sh``
writes ``${RESULT_DIR:-.}/.hpc_failed/<run>.<task>.failed``; with ``cd
$REPO_DIR`` pointing at the tree, both would land INSIDE the tree — where the
pull path would not find them and where the tree GC could delete them. So the
tree carries a symlink for each run-mutable path (:data:`TREE_SHARED_PATHS`) and
the code snapshot deliberately EXCLUDES those paths, so nothing ever
materialises a real directory over one. The invariant is small enough to state
in one line: *a tree contains code and symlinks, never a run's bytes.*

There is a MIRROR invariant, learned the expensive way (the 2026-07-29
sandbox-proving ``s4.table`` failure): *sealing a tree must not manufacture
run-state at the BASE either.* The seal creates a base target only for the paths
a job writes THROUGH the link (:data:`TREE_BASE_MATERIALIZED_PATHS`), because a
directory's mere existence at the base is read as evidence elsewhere — an empty
``<base>/_combiner/`` told the harvest "a combiner ran and produced nothing" and
turned a real results table into an empty one.

Snapshot identity
-----------------

The digest is :meth:`hpc_agent.infra.manifest.Manifest.digest` over the pushable
file set (whose docstring already invites exactly this: "two trees with
identical file content ... produce the same digest, so a stage-in can dedup on
it") folded with the framework's own version — because ``deploy_runtime`` ships
package-versioned framework files INTO the tree, and a tree that pinned the
user's code but floated the dispatcher would still let a queued job's runtime
change underneath it. One digest, everything the job executes.

Garbage collection
------------------

Trees accumulate — one per distinct code version, bounded by edit frequency, not
by run count. :func:`plan_tree_gc` is the PURE planner (identity / comparison /
counting over opaque digests — no ssh, no I/O), mirroring
:mod:`hpc_agent.infra.prune`'s split between planner and executor. Its policy
and the argument for it live on that function.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

__all__ = [
    "CODE_SNAPSHOT_EXTRA_EXCLUDES",
    "DIGEST_CHARS",
    "GC_KEEP_NEWEST",
    "TREES_REL",
    "TREE_BASE_MATERIALIZED_PATHS",
    "TREE_SEAL_REL",
    "TREE_SHARED_PATHS",
    "CodeTreeProbe",
    "TreeGcPlan",
    "digest_from_repo_dir",
    "format_tree_digest",
    "is_tree_digest",
    "parse_tree_listing",
    "plan_tree_gc",
    "repo_dir_for_run",
    "tree_path_for",
    "trees_root_for",
]

#: Where the digest-keyed trees live, relative to the run's ``remote_path``.
#: Under ``.hpc/`` — the established cluster-side framework namespace — so the
#: base push's ``--delete`` protection list (``PROTECTED_RUNTIME_FILES``) covers
#: it with the same mechanism that already protects the dispatcher/templates.
TREES_REL = ".hpc/trees"

#: The completion proof, written INSIDE a tree as the LAST step of
#: materialising it (after the code transfer, the symlinks, and the framework
#: deploy). Presence of this file — never mere directory existence — is what
#: makes a re-deploy a no-op. A torn deploy leaves an unsealed directory that
#: the next submit re-materialises rather than trusting, which is the #F53
#: "never let bookkeeping land ahead of the code it attests" discipline applied
#: to a whole tree.
TREE_SEAL_REL = ".hpc-tree-sealed"

#: Digest prefix length. 12 hex chars = 48 bits; a collision needs ~2^24
#: distinct code snapshots of ONE experiment before it is even a coin flip, and
#: the failure mode of a collision is bounded (two snapshots share a tree — the
#: same class of event as today's single shared tree, not worse). Short enough
#: that a human reads it off a path and matches it against a sidecar.
DIGEST_CHARS = 12

#: How many of the newest trees are kept regardless of references. Three, and
#: the number is doing three separate jobs: (1) the tree the CURRENT submit just
#: sealed must never be a candidate; (2) the PREVIOUS tree is the
#: ``--link-dest`` source that makes the next materialisation cheap, so reaping
#: it converts a hardlink pass into a full copy; (3) one more covers the
#: edit → submit → "oops" → revert → resubmit loop, where the tree you want back
#: is two versions old. Two would satisfy (1) and (2) but not (3); larger only
#: delays reclamation of trees nothing can want. This floor is also the ONLY
#: protection left when the journal cannot be read (see :func:`plan_tree_gc`),
#: which is why it is a floor and not a tuning knob.
GC_KEEP_NEWEST = 3

#: Run-mutable paths that exist as SYMLINKS inside a tree, pointing at the
#: shared base. Each entry is a path the cluster job resolves RELATIVE TO ITS
#: CWD (``cd "$REPO_DIR"``), so with ``$REPO_DIR`` = the tree it would otherwise
#: land inside the tree:
#:
#: * ``results`` — ``result_dir_template`` ("results/{run_id}/task_{task_id}")
#:   is rendered and ``os.makedirs``-ed relative to cwd by the cluster
#:   dispatcher.
#: * ``_combiner`` / ``_aggregated`` — the wave combiner and the cluster-side
#:   final reduce write there.
#: * ``logs`` — the templates' ``#SBATCH --output=logs/...`` default, for the
#:   path where the backend does not override ``log_dir``.
#: * ``.hpc_failed`` — ``hpc_preamble.sh`` writes
#:   ``${RESULT_DIR:-.}/.hpc_failed/<run>.<task>.failed``, and every reader
#:   (``infra/cluster_status.ssh_marker_scan``, ``ops/recover_flow``,
#:   ``ops/verify_canary``) looks for it at ``<remote_path>/.hpc_failed``.
#: * ``.hpc/runs`` — the dispatcher reads its run sidecar from
#:   ``<dispatcher dir>/runs/<run_id>.json``, i.e. ``$REPO_DIR/.hpc/runs``;
#:   sidecars are per-RUN data (they must not perturb a CODE digest) and both
#:   ``rsync_push`` and ``push_run_sidecar`` write them at the base.
#: * ``.hpc/announce`` — the dispatcher writes its per-task census markers to
#:   ``<dispatcher dir>/announce/<run_id>/`` (``dispatch._ANNOUNCE_DIRNAME``),
#:   i.e. ``$REPO_DIR/.hpc/announce``, while every reader
#:   (``ops.monitor.announce.ANNOUNCE_SUBPATH``, the arm census, reconcile)
#:   looks at ``<remote_path>/.hpc/announce``. Without the link a tree-pinned
#:   run announces into the tree and every census reads ``present=False``.
#:
#: The first four are the cluster-written members of
#: ``transport.PROTECTED_OUTPUT_DIRS``, so a new run-output dir there cannot be
#: added without deciding what it means inside a tree.
#:
#: MIRROR: hpc_agent.infra.transport.PROTECTED_OUTPUT_DIRS pinned-by
#: tests/infra/test_code_tree.py::test_cluster_written_output_dirs_are_all_shared_back_to_the_base
#: MIRROR: hpc_agent.ops.monitor.announce.ANNOUNCE_SUBPATH pinned-by
#: tests/infra/test_code_tree.py::test_the_dispatchers_announce_dir_is_shared_back_to_the_base
TREE_SHARED_PATHS: tuple[str, ...] = (
    "results",
    "_combiner",
    "_aggregated",
    "logs",
    ".hpc_failed",
    ".hpc/runs",
    ".hpc/announce",
)

#: The subset of :data:`TREE_SHARED_PATHS` whose BASE target the seal creates.
#:
#: A symlink resolves to nothing when its target is absent, and the cluster-side
#: writers that go THROUGH the link cannot recover from that: ``os.makedirs``
#: raises on a dangling ``results``; ``hpc_preamble.sh``'s ``mkdir -p "$fail_dir"
#: 2>/dev/null || true`` swallows the error and the terminal marker vanishes;
#: the dispatcher's eager announce ``mkdir`` is best-effort and the whole census
#: degrades. So for every path the JOB writes through the tree, the seal must
#: ``mkdir -p`` the base target first.
#:
#: The complement — ``_combiner`` and ``_aggregated`` — is deliberately LINKED
#: BUT NOT MATERIALISED. Nothing writes them through the tree (the wave combiner
#: and the cluster final reduce both run on the login node with ``cd
#: <remote_path>``, so they create their own dirs at the base), and creating them
#: here would fabricate EVIDENCE: ``ops/aggregate_flow._combiner_only_reduce``
#: reads "``<base>/_combiner`` is absent" as "no combiner ever ran" and falls back
#: to the per-task ``metrics.json`` reduce (#352 — the ``@register_run`` sweep
#: shape). A seal that pre-created an empty ``_combiner/`` turned that fallback
#: off and the harvest reduced over zero wave partials to an EMPTY results table
#: — reported as a successful harvest (the 2026-07-29 sandbox-proving ``s4.table``
#: failure). Sealing a code tree must never manufacture run-state at the base.
#:
#: (``aggregate_flow`` no longer leans on that absence either — a wave_map-less
#: run whose ``_combiner/`` pull lands empty takes the same fallback — but the
#: two fixes are independent, and this one is the general rule: a seal does not
#: get to invent base state, whoever happens to read it.)
TREE_BASE_MATERIALIZED_PATHS: tuple[str, ...] = (
    "results",
    "logs",
    ".hpc_failed",
    ".hpc/runs",
    ".hpc/announce",
)

#: Excludes the CODE snapshot adds on top of the push's own effective excludes.
#: These are run-varying, not code: including them would mint a fresh digest per
#: run (per-RUN copies — the granularity §10.S4 explicitly rejects) and would
#: materialise real directories where :data:`TREE_SHARED_PATHS` needs symlinks.
#: ``.hpc/trees/`` excludes itself so a tree can never nest a tree.
CODE_SNAPSHOT_EXTRA_EXCLUDES: tuple[str, ...] = (
    ".hpc/runs/",
    ".hpc/announce/",
    ".hpc_failed/",
    f"{TREES_REL}/",
)

_DIGEST_RE = re.compile(rf"^[0-9a-f]{{{DIGEST_CHARS}}}$")


def is_tree_digest(value: str) -> bool:
    """True when *value* is a well-formed tree digest (lowercase hex, fixed width).

    A digest flows verbatim into a remote path, so every composition and every
    parse gates on this rather than trusting a caller. Anything else — a
    truncated ``ls`` line, a stray dotfile, a hand-edited ``REPO_DIR`` — is not a
    digest and is treated as "no tree here".
    """
    return bool(_DIGEST_RE.match(value or ""))


def format_tree_digest(content_digest: str, runtime_version: str) -> str:
    """Fold the snapshot's content digest with the framework version → a tree name.

    *content_digest* is :meth:`hpc_agent.infra.manifest.Manifest.digest` over the
    snapshot's file set — the DATA identity the manifest module already defines.
    *runtime_version* is the installed ``hpc-agent`` version, folded in because
    ``deploy_runtime`` ships package-versioned framework files (the dispatcher,
    the combiner, the templates, the preamble) INTO the tree: a tree that pinned
    the user's code but let the framework float would still let a queued job's
    runtime change underneath it. Hashing the pair keeps ONE name for
    "everything this job will execute".

    The fold is a re-hash rather than a concatenation so the result is uniformly
    :data:`DIGEST_CHARS` lowercase hex whatever the version string looks like.
    """
    import hashlib

    h = hashlib.sha256()
    h.update(f"{content_digest}\0{runtime_version}".encode())
    return h.hexdigest()[:DIGEST_CHARS]


def trees_root_for(remote_path: str) -> str:
    """``<remote_path>/.hpc/trees`` — the parent every digest tree lives under."""
    return posixpath.join(remote_path.rstrip("/"), TREES_REL)


def tree_path_for(remote_path: str, digest: str) -> str:
    """``<remote_path>/.hpc/trees/<digest>`` — the run's frozen code tree.

    Raises :class:`ValueError` on a malformed digest: the value becomes a remote
    filesystem path, and "validate at composition" is cheaper than trusting
    every call site (``validate_remote_path`` then re-checks the whole string at
    the transport boundary).
    """
    if not is_tree_digest(digest):
        raise ValueError(f"not a code-tree digest: {digest!r}")
    return posixpath.join(trees_root_for(remote_path), digest)


def digest_from_repo_dir(repo_dir: str | None) -> str | None:
    """The tree digest a ``REPO_DIR`` names, or ``None`` for a legacy path.

    This is the **reference resolution** the GC and every "which tree did this
    run use?" question route through, and it needs NO new record field: a run's
    ``REPO_DIR`` is already journaled on its ``RunRecord.job_env``, so the digest
    is derivable from state the journal has stored since long before
    content-addressing existed.

    ``None`` is the migration answer (absent-disables, the placement-drift
    precedent): a run submitted before content-addressing has
    ``REPO_DIR == <remote_path>``, which names no digest, so it references no
    tree and its code still lives at the base — exactly where the legacy
    resolution looks. No flag-day, no backfill.
    """
    if not repo_dir:
        return None
    parts = repo_dir.rstrip("/").split("/")
    tail = TREES_REL.split("/")
    if len(parts) < len(tail) + 1:
        return None
    if parts[-len(tail) - 1 : -1] != tail:
        return None
    digest = parts[-1]
    return digest if is_tree_digest(digest) else None


def repo_dir_for_run(remote_path: str, job_env: Mapping[str, str] | None) -> str:
    """Where a recorded run's code actually is — tree if content-addressed, else base.

    The single fallback rule for every reader (``inspect-deployment``, an
    executor-existence probe, a human asking where a run ran): trust the run's
    OWN recorded ``REPO_DIR`` when it names a digest tree, otherwise fall back to
    the legacy identity ``deploy_target_for(remote_path)``. A record whose
    ``job_env`` is empty, or whose ``REPO_DIR`` points somewhere that is not a
    tree, resolves to the base — the pre-S4 behaviour, byte-identical.
    """
    recorded = (job_env or {}).get("REPO_DIR") or ""
    if digest_from_repo_dir(recorded):
        return recorded.rstrip("/")
    return remote_path.rstrip("/")


def parse_tree_listing(stdout: str) -> tuple[str, ...]:
    """Digests parsed out of an ``ls -1t <trees root>`` read, newest-first.

    Non-digest lines are dropped rather than reported: the listing is a remote
    read whose only jobs are picking a ``--link-dest`` source and feeding the GC
    planner, and BOTH are safe under under-reporting (fewer link sources = a
    slower copy; fewer known trees = fewer reaped). Order is preserved because
    ``ls -1t`` sorts by mtime, newest first — the recency the keep-newest floor
    is defined over.
    """
    seen: list[str] = []
    for line in (stdout or "").splitlines():
        name = line.strip()
        if is_tree_digest(name) and name not in seen:
            seen.append(name)
    return tuple(seen)


@dataclass(frozen=True)
class CodeTreeProbe:
    """What one remote round-trip learned about the tree store.

    ``trusted`` is the positive-evidence flag: False means the read never
    completed (severed channel, expired deadline, breaker open), so ``sealed``
    and ``known`` carry NO information and must be read as "unknown", not as
    "nothing there". Every consumer degrades the same way — re-materialise, do
    not reap — so an untrusted probe costs bandwidth and never correctness.
    """

    digest: str
    tree_path: str
    sealed: bool
    known: tuple[str, ...] = ()
    trusted: bool = True

    def link_dest(self) -> str | None:
        """The newest OTHER tree, as a ``--link-dest`` candidate — or ``None``.

        Purely a bandwidth hint (see ``materialize_code_tree``): unchanged files
        become hardlinks where the filesystem allows it and are copied where it
        does not, and the caller cannot tell which happened. ``None`` (first ever
        deploy, or an untrusted probe) simply means a full copy.
        """
        for other in self.known:
            if other != self.digest:
                return posixpath.join(posixpath.dirname(self.tree_path), other)
        return None


@dataclass(frozen=True)
class TreeGcPlan:
    """What a GC pass would delete, and what it deliberately kept.

    ``refused`` is not an error: a refused plan is a plan with an empty
    ``reapable`` and a stated reason, which is how "failures are data" is spelled
    for a janitor that must never guess.
    """

    reapable: tuple[str, ...]
    kept_referenced: tuple[str, ...]
    kept_newest: tuple[str, ...]
    refused_reason: str | None = None

    @property
    def refused(self) -> bool:
        return self.refused_reason is not None

    def to_dict(self) -> dict[str, object]:
        return {
            "reapable": list(self.reapable),
            "kept_referenced": list(self.kept_referenced),
            "kept_newest": list(self.kept_newest),
            "refused_reason": self.refused_reason,
        }


def plan_tree_gc(
    *,
    present: Iterable[str],
    referenced: Collection[str] | None,
    protect: Collection[str] = (),
    keep_newest: int = GC_KEEP_NEWEST,
) -> TreeGcPlan:
    """Decide which trees are reapable. Pure — identity, comparison, counting.

    *present* is the remote inventory NEWEST-FIRST (see :func:`parse_tree_listing`).
    *referenced* is the set of digests any journal-known NON-TERMINAL run points
    at — ``submitting`` / ``in_flight`` and anything not in
    ``TERMINAL_STATUSES``; those runs either have a job sitting in the scheduler
    queue right now or are about to. *protect* is the caller's unconditional
    keep-set (in practice: the digest being deployed on this very pass).

    A tree is reapable when ALL hold:

    1. no non-terminal run references it,
    2. the caller did not protect it,
    3. it is not among the newest *keep_newest* (see :data:`GC_KEEP_NEWEST`).

    ``referenced=None`` means *the references could not be established* — an
    unreadable journal, a partial scan. That REFUSES the whole plan rather than
    treating "I saw no references" as "there are none": the one thing this
    janitor must never do is delete the tree under a job that is still queued,
    and an empty set is exactly what a failed read looks like. Refusing loses
    nothing but disk; guessing loses a run. (The keep-newest floor would have
    saved the most recent trees anyway — but "would have" is not a guarantee,
    and the conservative rule is the one that does not depend on which failure
    happened.)

    Nothing here does I/O, so nothing here can fail in a way that surprises a
    caller: the executor's job is to run ``rm -rf`` over ``reapable`` and to
    treat its own failures as data.
    """
    ordered = [d for d in present if is_tree_digest(d)]
    if referenced is None:
        return TreeGcPlan(
            reapable=(),
            kept_referenced=(),
            kept_newest=tuple(ordered[: max(keep_newest, 0)]),
            refused_reason="run references could not be established (journal unreadable)",
        )
    keep_n = max(int(keep_newest), 0)
    newest = set(ordered[:keep_n])
    ref = {d for d in referenced if is_tree_digest(d)}
    prot = {d for d in protect if is_tree_digest(d)}
    reapable: list[str] = []
    kept_ref: list[str] = []
    for digest in ordered:
        if digest in ref or digest in prot:
            kept_ref.append(digest)
            continue
        if digest in newest:
            continue
        reapable.append(digest)
    return TreeGcPlan(
        reapable=tuple(reapable),
        kept_referenced=tuple(kept_ref),
        kept_newest=tuple(ordered[:keep_n]),
    )
