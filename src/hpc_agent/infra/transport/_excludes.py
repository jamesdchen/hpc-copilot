"""Exclude-set constants + the shared exclude-match core.

The push/pull exclude vocabulary — the default patterns, the three mandatory
protection groups (credential file, cluster run output, deploy-placed runtime
files), and the ``.gitignore`` carve-out — plus the pure functions that
resolve and apply them. Every transfer path (rsync, tar full-copy, the
content-hash delta) shares ``_path_excluded`` so the disclosure, the local
manifest, and the remote manifest snippet all agree on exactly which files
ship.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

DEFAULT_RSYNC_EXCLUDES: list[str] = [
    ".git/",
    "__pycache__/",
    "*.pyc",
    ".mypy_cache/",
    ".claude/",
    # Virtualenvs / package caches: gigabytes that get re-diffed and
    # re-sent on every submit, and that the cluster job never reads (it
    # builds its own env from MODULES / CONDA_ENV / `uv sync`).
    ".venv/",
    "venv/",
    "node_modules/",
]

# Patterns that must NEVER ship to the cluster, regardless of what
# ``exclude`` a caller passes. ``clusters.yaml`` holds real cluster
# credentials (user/host/scratch paths) and is gitignored locally for
# exactly that reason; when it lives inside the experiment dir (the
# documented demo layout puts it at the repo root with
# ``HPC_CLUSTERS_CONFIG`` pointing there) a default push would rsync it
# onto a shared cluster filesystem. These are unioned into every
# transfer's exclude set so an explicit ``rsync_excludes`` cannot drop
# the protection. Bare names (no ``/``) so rsync/tar match the file at
# any depth in the tree.
MANDATORY_RSYNC_EXCLUDES: list[str] = [
    "clusters.yaml",
]

# Cluster-side RUN OUTPUT directories — written by the job on the compute
# nodes, NOT part of the local deploy tree. A deploy push's ``--delete``
# (rsync) or tar-fallback remote pre-clean must NEVER delete or even traverse
# these (#173): deleting them destroys the user's results, and traversing a
# crash-loop's debris (10^5+ ``_wip_*`` dirs under ``results/``) wedges the push
# past its transfer timeout. Unioned into every push's exclude set (like
# :data:`MANDATORY_RSYNC_EXCLUDES`) so an incomplete caller ``exclude`` can't
# expose them. ``result_dir_template`` defaults to ``results/``; ``_combiner/``
# holds the wave-combiner output. A non-default output dir must be added to the
# caller's ``exclude``. Bare names (trailing slash documents "directory") so
# rsync/tar/find match the dir at any depth.
PROTECTED_OUTPUT_DIRS: list[str] = [
    "results/",
    "_combiner/",
    # The scheduler's per-task stdout/stderr dir (qsub/sbatch ``-o <remote>/logs``,
    # default name ``logs``). Written by the job on the compute nodes, NOT part of
    # the local deploy tree — so a re-submit's ``--delete`` pre-clean would wipe it,
    # and the scheduler then recreates ``logs`` as a *file* (its ``-o`` target with
    # no directory present), losing per-task log separation. Protect it like the
    # other run-output dirs. Empirical 2026-06-09 demo: a re-deploy left ``logs`` a
    # 24KB file instead of a dir of ``*.o<job>.<task>`` entries.
    "logs/",
    # aggregate-flow lands its outputs under ``_aggregated/<run_id>/`` and the
    # cluster-side final reduce (``_hpc_combiner.py --final``) writes the
    # aggregate there too. Without protection the next submit's ``--delete``
    # pre-clean clobbers the cluster's own aggregate artifacts, and the local
    # pulled tree (per-task result mirrors + evidence ledgers, potentially GBs)
    # is pushed back up on every submit (F10). Protect it like the other
    # run-output dirs — written by the job, not part of the local deploy tree.
    "_aggregated/",
    # The no-combiner reduce fallbacks pull each task's metrics / trace sidecars
    # into a LOCAL mirror under the aggregate ``out`` dir — ``_per_task_results/``
    # (:data:`hpc_agent.ops.aggregate_flow.PER_TASK_RESULTS_DIRNAME`) and
    # ``_per_task_traces/`` (:data:`...PER_TASK_TRACES_DIRNAME`). ``out`` defaults
    # under ``_aggregated/`` (already protected above) but a caller-supplied
    # ``output_dir`` can place these mirrors at ANY depth, so protect the bare
    # names too. Run-13 finding 4: run 12's 2,700-file ``_per_task_results``
    # mirror rode a code deploy back to the cluster as a 1.18 GB "changed/new"
    # payload because these stack-minted pull destinations were not excluded.
    # Core mints these names; they belong in the default set, not per-repo config
    # memory. The lockstep pin
    # (``tests/infra/test_pull_dest_excludes.py``) fails if the mint-site
    # constants are renamed without updating this set.
    "_per_task_results/",
    "_per_task_traces/",
    # The dossier export store core mints at the experiment root — the archive
    # zip + attestation jsonl (``ops/export_dossier.DOSSIER_DIRNAME``). A local
    # OUTPUT store, not code; without this a code deploy re-ships prior dossier
    # archives to the cluster (run-13 finding 4's sibling render-store class).
    # Pinned lockstep by ``tests/infra/test_pull_dest_excludes.py``.
    "_dossier/",
]

# Framework runtime files placed on the cluster by ``deploy_runtime`` (scp'd
# into ``.hpc/`` separately from the user-repo push): the per-scheduler job
# scripts + shared preamble under ``.hpc/templates/``, the dispatcher, the
# combiner, and the ``hpc_agent/`` runtime stub. The local deploy tree does NOT
# contain them, so a push's ``--delete`` / pre-clean would wipe them — and
# protecting them only via :data:`DEFAULT_RSYNC_EXCLUDES` is not enough,
# because an explicit caller ``exclude`` (the ``rsync_excludes`` spec field)
# *replaces* that default. Empirically (2026-06-08 Windows demo) a push whose
# exclude set lacked these deleted ``.hpc/templates/`` on the cluster; every
# array task then died at preamble-source time with ``hpc_preamble.sh: No such
# file or directory`` — a ~26ms exit-1 on SGE — while the canary that ran
# before the wipe passed. Unioned into every push's exclude set, exactly like
# MANDATORY / PROTECTED_OUTPUT, so no caller can drop the protection.
PROTECTED_RUNTIME_FILES: list[str] = [
    "hpc_agent/",
    ".hpc/_hpc_dispatch.py",
    ".hpc/_hpc_combiner.py",
    ".hpc/templates/",
    # deploy_runtime-placed bookkeeping (never in the local push tree): the
    # deploy-cache manifest (:data:`_DEPLOY_MANIFEST_REL`) and the push manifest
    # (:data:`_PUSH_MANIFEST_REL`). A push's ``--delete`` / tar pre-clean would
    # wipe them on every standard push-then-deploy cycle, so the #242 content-
    # hash deploy cache would ALWAYS miss (re-ship every file) and the ruling-6
    # manifest prune would lose its record of what we shipped (#66).
    ".hpc/.deploy_state.json",
    ".hpc/.push_manifest.json",
]

# Paths a scaffolded ``.gitignore`` marks as generated but the cluster
# node *needs*: the executor package built at Step 0 (``src/``) and the
# dispatch contract (``.hpc/tasks.py`` / ``.hpc/cli.py``). A caller derives
# rsync excludes from ``.gitignore``, so these would otherwise be stripped
# from the deploy bundle. The carve-out lives here — next to the exclude
# constants it modifies — so every submit path (``submit_flow`` restoring the
# push, ``executor_guard`` mirroring it in the static deploy-manifest check)
# shares one definition. ``.hpc/.build-cache.json`` is NOT listed: it stays
# excluded (a local-build artifact the node never reads).
_GENERATED_SHIPPABLE: frozenset[str] = frozenset({"src", ".hpc/tasks.py", ".hpc/cli.py"})


def _effective_excludes(exclude: list[str] | None) -> list[str]:
    """Resolve the exclude list, always enforcing the mandatory patterns.

    ``None`` selects :data:`DEFAULT_RSYNC_EXCLUDES`. Three mandatory groups are
    then appended (de-duplicated) so a caller-supplied list can never drop
    them: :data:`MANDATORY_RSYNC_EXCLUDES` (the credential file
    ``clusters.yaml`` — never ship), :data:`PROTECTED_OUTPUT_DIRS` (cluster
    run output — never ``--delete``/pre-clean; see #173), and
    :data:`PROTECTED_RUNTIME_FILES` (the ``deploy_runtime``-placed framework
    files — never ``--delete``, or every array task loses its preamble).
    """
    base = DEFAULT_RSYNC_EXCLUDES if exclude is None else list(exclude)
    out = list(base)
    for pat in (*MANDATORY_RSYNC_EXCLUDES, *PROTECTED_OUTPUT_DIRS, *PROTECTED_RUNTIME_FILES):
        if pat not in out:
            out.append(pat)
    return out


def _path_excluded(parts: tuple[str, ...], pats: list[str]) -> bool:
    """Would the transfer's exclude set drop the file at *parts*?

    The shared exclude-match core used by both :func:`_disclose_payload` (the
    ship-size WARN) and :func:`_pushable_relpaths` (the delta manifest's local
    file set), so the disclosure, the local manifest, and the remote manifest
    snippet all agree on exactly which files ship. *pats* are patterns already
    stripped of any trailing ``/``.

    Semantics mirror tar/rsync as the codebase applies them: a bare pattern
    matches ANY path component (match-any-depth); an anchored ``./name`` /
    ``^name`` pattern (the F-I dialects) matches only the TOP-LEVEL component;
    an internal-slash pattern (e.g. ``data/interim/``) is anchored to the
    transfer ROOT — it matches that path and everything under it — mirroring
    rsync's transfer-root anchoring and the same shape :func:`_remote_clean_cmd`
    already applies via ``find -path`` (#F57). Before this it was inert here (an
    anchored exclude silently SHIPPED in delta mode while the rsync and full-copy
    tar modes honored it); the remote snippet :data:`_REMOTE_MANIFEST_SNIPPET`
    carries the identical logic so the local and remote manifests stay in
    lockstep.
    """
    relposix = "/".join(parts)
    for pat in pats:
        if pat.startswith("./") or pat.startswith("^"):
            anchored = pat[2:] if pat.startswith("./") else pat[1:]
            if parts and fnmatch.fnmatch(parts[0], anchored):
                return True
            continue
        if "/" in pat:
            # Root-anchored internal-slash pattern: the path itself or any file
            # under it (``pat/*`` — fnmatch's ``*`` spans ``/``).
            if fnmatch.fnmatch(relposix, pat) or fnmatch.fnmatch(relposix, f"{pat}/*"):
                return True
            continue
        for part in parts:
            if fnmatch.fnmatch(part, pat):
                return True
    return False


def _pushable_relpaths(root: Path, exclude: list[str]) -> list[str]:
    """POSIX relpaths of every file under *root* the push would ship.

    The exclude-filtered file set that the local delta manifest is built over,
    using the same :func:`_path_excluded` test the disclosure and the remote
    snippet use — so the two manifests describe the same tree.
    """
    pats = [p.rstrip("/") for p in exclude]
    rels: list[str] = []
    for p in root.rglob("*"):
        try:
            rel = p.relative_to(root)
        except ValueError:
            continue
        if _path_excluded(rel.parts, pats):
            continue
        if p.is_file():
            rels.append(rel.as_posix())
    return rels


def _is_runtime_placed(relpath: str) -> bool:
    """True when *relpath* is a ``deploy_runtime``-placed framework file.

    Those files ride their own deploy leg OUTSIDE the repo push, so the push
    manifest never knows them — they are the framework's own, never prune
    candidates and never anomalies to nag a human about (run-#12: six eternal
    "needs a human decision" lines for the dispatcher + templates the
    framework itself deployed). Matches the same :data:`PROTECTED_RUNTIME_FILES`
    set every push's exclude union protects.
    """
    for prot in PROTECTED_RUNTIME_FILES:
        if prot.endswith("/"):
            if relpath == prot.rstrip("/") or relpath.startswith(prot):
                return True
        elif relpath == prot:
            return True
    return False
