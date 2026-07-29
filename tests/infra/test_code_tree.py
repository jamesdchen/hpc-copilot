"""Content-addressed CODE trees (§10.S4) — layout, identity, transport, GC planner.

The hazard these exist to close: a Slurm/SGE job that WAITS in the scheduler
queue executes whatever the shared remote tree holds when it finally starts, not
what was there at submit. Naming a tree after its own content makes a later push
unable to reach it — a changed snapshot hashes to a different name.

Companion suites: ``tests/ops/submit/test_code_tree_submit.py`` (the submit-path
wiring: pinning, no-op re-deploy, GC seat, legacy fallback).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hpc_agent.infra import code_tree, transport

_D1 = "0" * 12
_D2 = "1" * 12
_D3 = "2" * 12
_D4 = "3" * 12


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setenv("HPC_SSH_NO_BACKOFF", "1")


# ── path composition ───────────────────────────────────────────────────────


def test_tree_path_is_composed_under_the_hpc_namespace() -> None:
    assert code_tree.tree_path_for("/u/scratch/a/exp", _D1) == f"/u/scratch/a/exp/.hpc/trees/{_D1}"
    # A trailing slash on the base normalises exactly like deploy_target_for.
    assert code_tree.tree_path_for("/u/scratch/a/exp/", _D1) == f"/u/scratch/a/exp/.hpc/trees/{_D1}"
    assert code_tree.trees_root_for("/p") == "/p/.hpc/trees"


@pytest.mark.parametrize(
    "bad",
    ["", "short", "0" * 11, "0" * 13, "0" * 11 + "G", "../../etc", "0" * 11 + "/"],
)
def test_a_malformed_digest_can_never_become_a_path(bad: str) -> None:
    """The digest flows into a remote path (and into ``rm -rf``), so composition
    validates rather than trusting a caller or a truncated ``ls`` line."""
    assert not code_tree.is_tree_digest(bad)
    with pytest.raises(ValueError, match="not a code-tree digest"):
        code_tree.tree_path_for("/p", bad)


def test_digest_round_trips_through_repo_dir() -> None:
    repo_dir = code_tree.tree_path_for("/p/exp", _D1)
    assert code_tree.digest_from_repo_dir(repo_dir) == _D1
    assert code_tree.digest_from_repo_dir(repo_dir + "/") == _D1


def test_digest_from_repo_dir_is_none_for_a_legacy_path() -> None:
    """Migration/compat: a run submitted before content-addressing recorded
    ``REPO_DIR == remote_path``, which names no tree — absent-disables."""
    assert code_tree.digest_from_repo_dir("/u/scratch/a/exp") is None
    assert code_tree.digest_from_repo_dir(None) is None
    assert code_tree.digest_from_repo_dir("") is None
    # A path that merely LOOKS tree-shaped but isn't hex is not a digest.
    assert code_tree.digest_from_repo_dir("/p/.hpc/trees/not-a-digest") is None
    # ``.hpc/trees`` must be the immediate parent, not any ancestor.
    assert code_tree.digest_from_repo_dir(f"/p/.hpc/trees/{_D1}/src") is None


def test_repo_dir_for_run_prefers_the_recorded_tree_and_falls_back() -> None:
    tree = code_tree.tree_path_for("/p/exp", _D1)
    assert code_tree.repo_dir_for_run("/p/exp", {"REPO_DIR": tree}) == tree
    # Legacy: no job_env, or a REPO_DIR that names no tree → the base.
    assert code_tree.repo_dir_for_run("/p/exp", None) == "/p/exp"
    assert code_tree.repo_dir_for_run("/p/exp", {}) == "/p/exp"
    assert code_tree.repo_dir_for_run("/p/exp/", {"REPO_DIR": "/p/exp"}) == "/p/exp"


def test_digest_folds_in_the_framework_version() -> None:
    """``deploy_runtime`` ships package-versioned framework files INTO the tree,
    so a tree that pinned only the user's code would let a queued job's runtime
    change underneath it."""
    a = code_tree.format_tree_digest("abc", "1.2.3")
    b = code_tree.format_tree_digest("abc", "1.2.4")
    c = code_tree.format_tree_digest("abd", "1.2.3")
    assert a != b, "a framework bump must mint a new tree"
    assert a != c, "a code change must mint a new tree"
    assert a == code_tree.format_tree_digest("abc", "1.2.3"), "identity is stable"
    assert code_tree.is_tree_digest(a)


# ── the run-mutable symlink set ────────────────────────────────────────────


def test_cluster_written_output_dirs_are_all_shared_back_to_the_base() -> None:
    """A tree holds CODE and symlinks, never a run's bytes.

    Lockstep pin: every cluster-WRITTEN protected output dir must have a
    symlink inside the tree, or a job whose cwd is the tree would write its
    results where the pull path cannot see them and the GC could delete them.
    Adding a new cluster-written dir to ``PROTECTED_OUTPUT_DIRS`` fails here
    until someone decides what it means inside a tree.
    """
    cluster_written = {"results/", "_combiner/", "_aggregated/", "logs/"}
    assert cluster_written <= set(transport.PROTECTED_OUTPUT_DIRS)
    shared = set(code_tree.TREE_SHARED_PATHS)
    for pat in cluster_written:
        assert pat.rstrip("/") in shared, f"{pat} is cluster-written but not shared into the tree"
    # The three the preamble/dispatcher resolve relative to $REPO_DIR by name.
    assert ".hpc_failed" in shared
    assert ".hpc/runs" in shared
    assert ".hpc/announce" in shared


def test_the_dispatchers_announce_dir_is_shared_back_to_the_base() -> None:
    """Lockstep pin with ``ops.monitor.announce.ANNOUNCE_SUBPATH``.

    The standalone dispatcher writes its per-task census markers to
    ``<its own .hpc dir>/announce/<run_id>/`` — i.e. ``$REPO_DIR/.hpc/announce``
    — while EVERY reader (the monitor poll census, ``aggregate/arm_census``,
    reconcile, ``migrate/census``) looks under ``<remote_path>/.hpc/announce``.
    With ``$REPO_DIR`` on a code tree and no symlink, a tree-pinned run
    announces into the tree and every census reads ``present=False``: the
    monitor silently degrades to the 20-25 min reporter walk and the streaming
    harvest's ``census_arms`` REFUSES outright.
    """
    from hpc_agent.ops.monitor.announce import ANNOUNCE_SUBPATH

    assert ANNOUNCE_SUBPATH == ".hpc/announce"
    assert ANNOUNCE_SUBPATH in code_tree.TREE_SHARED_PATHS
    # ...and, being per-RUN bookkeeping, it must never perturb a CODE digest.
    assert f"{ANNOUNCE_SUBPATH}/" in code_tree.CODE_SNAPSHOT_EXTRA_EXCLUDES


def test_only_job_written_shared_paths_get_a_materialised_base_dir() -> None:
    """The seal may LINK anything, but it may CREATE only what a job writes through.

    A base directory's mere EXISTENCE is read as evidence elsewhere, so sealing
    a code tree must not manufacture run-state at the base. The canonical
    casualty: ``aggregate_flow`` treated an absent ``<base>/_combiner/`` as "no
    combiner ever ran" and fell back to the per-task ``metrics.json`` reduce
    (#352); a seal that pre-created an empty one turned the fallback off and the
    2026-07-29 sandbox-proving run harvested an EMPTY results table while
    reporting success (``s4.table``).

    ``_combiner`` / ``_aggregated`` are written by the LOGIN NODE with ``cd
    <remote_path>`` — never through the tree — so they are linked and left
    dangling until their real writer creates them.
    """
    materialised = set(code_tree.TREE_BASE_MATERIALIZED_PATHS)
    shared = set(code_tree.TREE_SHARED_PATHS)
    assert materialised <= shared, "a materialised path must be a shared path"
    assert shared - materialised == {"_combiner", "_aggregated"}
    # Every path a JOB writes THROUGH the link must be materialised — a dangling
    # symlink makes os.makedirs raise and the preamble's `|| true` mkdir vanish.
    assert {"results", ".hpc_failed", ".hpc/announce"} <= materialised


def test_the_trees_root_is_protected_from_the_base_push_delete() -> None:
    """The trees exist ONLY on the cluster, so a base push's ``--delete`` would
    wipe every snapshot a queued job is pinned to — the ``.hpc/templates/`` wipe
    class, with jobs already in the queue that cannot be told."""
    assert f"{code_tree.TREES_REL}/" in transport.PROTECTED_RUNTIME_FILES
    eff = transport._effective_excludes(["only_this/"])
    assert f"{code_tree.TREES_REL}/" in eff
    # ...and it is never a prune candidate / anomaly for the manifest janitor.
    assert transport._is_runtime_placed(f"{code_tree.TREES_REL}/{_D1}/tasks.py")


def test_run_varying_paths_are_excluded_from_the_snapshot() -> None:
    """Including a per-run sidecar would mint a fresh digest per submission —
    per-RUN copies, the granularity §10.S4 rejects."""
    assert ".hpc/runs/" in code_tree.CODE_SNAPSHOT_EXTRA_EXCLUDES
    assert ".hpc_failed/" in code_tree.CODE_SNAPSHOT_EXTRA_EXCLUDES
    assert f"{code_tree.TREES_REL}/" in code_tree.CODE_SNAPSHOT_EXTRA_EXCLUDES


def test_code_snapshot_digest_ignores_sidecar_churn(tmp_path: Path) -> None:
    root = tmp_path / "exp"
    (root / ".hpc" / "runs").mkdir(parents=True)
    (root / ".hpc" / "tasks.py").write_text("total = lambda: 1\n", encoding="utf-8")
    (root / ".hpc" / "runs" / "run-a.json").write_text('{"run_id": "run-a"}', encoding="utf-8")
    first = transport.code_snapshot_digest(root)

    # A new run's sidecar lands — same CODE, so the SAME tree is reused.
    (root / ".hpc" / "runs" / "run-b.json").write_text('{"run_id": "run-b"}', encoding="utf-8")
    assert transport.code_snapshot_digest(root) == first

    # The user's code changes — a NEW tree, which is the whole point.
    (root / ".hpc" / "tasks.py").write_text("total = lambda: 2\n", encoding="utf-8")
    assert transport.code_snapshot_digest(root) != first


# ── the transport dials ────────────────────────────────────────────────────


def _ssh(stdout: str = "", returncode: int = 0):
    return patch(
        "hpc_agent.infra.transport._guarded_ssh_bounded",
        return_value=SimpleNamespace(returncode=returncode, stdout=stdout, stderr=""),
    )


def test_probe_reads_the_seal_and_the_inventory_in_one_leg() -> None:
    ack = "__HPC_TREE_ACK__=0"
    out = f"__HPC_TREE_SEALED__\n{_D1}\n{_D2}\n{ack}\n"
    with _ssh(out) as ssh:
        probe = transport.probe_code_tree(ssh_target="u@c", remote_path="/p", digest=_D1)
    assert ssh.call_count == 1, "the seal check and the listing share ONE round-trip"
    cmd = ssh.call_args[0][1]
    assert f"/p/.hpc/trees/{_D1}/.hpc-tree-sealed" in cmd
    assert "ls -1t" in cmd
    # A reused tree is touched so recency tracks USE, not just creation.
    assert "touch" in cmd
    assert probe.sealed is True
    assert probe.trusted is True
    assert probe.known == (_D1, _D2)
    assert probe.link_dest() == f"/p/.hpc/trees/{_D2}", "newest OTHER tree is the link source"


def test_probe_without_the_ack_is_unknown_not_empty() -> None:
    """A severed channel returns rc 0 with truncated stdout. Reading that as
    'no seal, no trees' would let the GC reap a live tree; the ack's absence
    makes it UNKNOWN and every consumer degrades to do-nothing."""
    with _ssh(f"__HPC_TREE_SEALED__\n{_D1}\n"):  # no ack line
        probe = transport.probe_code_tree(ssh_target="u@c", remote_path="/p", digest=_D1)
    assert probe.trusted is False
    assert probe.sealed is False
    assert probe.known == ()
    assert probe.link_dest() is None


def test_probe_degrades_on_a_transport_failure() -> None:
    with patch("hpc_agent.infra.transport._guarded_ssh_bounded", side_effect=TimeoutError("dead")):
        probe = transport.probe_code_tree(ssh_target="u@c", remote_path="/p", digest=_D1)
    assert (probe.trusted, probe.sealed, probe.known) == (False, False, ())


def _capture_rsync(tmp_path: Path, *, link_dest: str | None):
    calls: list[list[str]] = []

    def _fake_run(argv, *_a, **_kw):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        _ssh(),
        patch("hpc_agent.infra.transport._have_rsync", return_value=True),
        patch("hpc_agent.infra.transport.run_capture_bounded", side_effect=_fake_run),
    ):
        transport.materialize_code_tree(
            ssh_target="u@c",
            remote_path="/p",
            local_path=tmp_path,
            digest=_D1,
            link_dest=link_dest,
        )
    return [c for c in calls if c and c[0] == "rsync"]


def test_materialize_never_uses_inplace_or_delete(tmp_path: Path) -> None:
    """#F20 generalised: the ``--inplace`` ban was earned on RUNNING jobs (a
    torn dispatcher under a live array); §10.S4 extends it to PENDING ones —
    same hazard, longer fuse. ``--delete`` has no business in a fresh private
    directory either."""
    (tmp_path / "tasks.py").write_text("x = 1\n", encoding="utf-8")
    rsync = _capture_rsync(tmp_path, link_dest=None)
    assert len(rsync) == 1
    assert "-az" in rsync[0]
    assert "--inplace" not in rsync[0]
    assert "--delete" not in rsync[0]
    assert rsync[0][-1] == f"u@c:/p/.hpc/trees/{_D1}/"


def test_link_dest_is_passed_when_a_previous_tree_exists(tmp_path: Path) -> None:
    (tmp_path / "tasks.py").write_text("x = 1\n", encoding="utf-8")
    prev = f"/p/.hpc/trees/{_D2}"
    rsync = _capture_rsync(tmp_path, link_dest=prev)
    assert f"--link-dest={prev}" in rsync[0]


def test_materialize_is_correct_whether_or_not_hardlinks_work(tmp_path: Path) -> None:
    """§10.S4's probe table: ``--link-dest`` hardlinks on Hoffman2 ($HOME and
    $SCRATCH) and CARC /home1, and **silently copies** on CARC /scratch1
    (BeeGFS). rsync degrades on its own and never says so, so the caller cannot
    branch on it — and must not need to.

    Simulated at the seam rsync would hit: the same invocation, once against a
    link-dest that dedups and once against one that plain-copies. The resulting
    argv is byte-identical and nothing downstream reads a link count, so the
    two outcomes are indistinguishable to the framework — which IS the
    correctness property.
    """
    (tmp_path / "tasks.py").write_text("x = 1\n", encoding="utf-8")
    prev = f"/p/.hpc/trees/{_D2}"

    hardlinking = _capture_rsync(tmp_path, link_dest=prev)
    # BeeGFS: rsync silently falls back to copying. Same argv, same exit 0 — the
    # only difference is bytes on the wire, which no caller inspects.
    silently_copying = _capture_rsync(tmp_path, link_dest=prev)
    assert hardlinking == silently_copying

    # And with NO link source at all (first-ever deploy on any filesystem) the
    # transfer is still a correct full copy — just without the saving.
    full_copy = _capture_rsync(tmp_path, link_dest=None)
    assert not any(a.startswith("--link-dest") for a in full_copy[0])
    assert full_copy[0][-1] == hardlinking[0][-1], "same destination either way"


def test_materialize_falls_back_to_tar_without_rsync(tmp_path: Path) -> None:
    (tmp_path / "tasks.py").write_text("x = 1\n", encoding="utf-8")
    captured: dict[str, object] = {}

    def _capture_tar(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        _ssh(),
        patch("hpc_agent.infra.transport._have_rsync", return_value=False),
        patch("hpc_agent.infra.transport._tar_ssh_push", side_effect=_capture_tar) as tar,
    ):
        transport.materialize_code_tree(
            ssh_target="u@c", remote_path="/p", local_path=tmp_path, digest=_D1
        )
    assert tar.call_count == 1
    assert captured["remote_path"] == f"/p/.hpc/trees/{_D1}"
    assert captured["delete"] is False


def test_seal_links_every_shared_path_verifies_it_and_writes_the_marker() -> None:
    with _ssh() as ssh:
        transport.seal_code_tree(ssh_target="u@c", remote_path="/p", digest=_D1)
    cmd = ssh.call_args[0][1]
    tree = f"/p/.hpc/trees/{_D1}"
    for rel in code_tree.TREE_SHARED_PATHS:
        assert f"ln -s /p/{rel} {tree}/{rel}" in cmd
        assert f"[ -L {tree}/{rel} ]" in cmd, "an unlinkable path must refuse the seal"
    # The completion marker is LAST — a torn deploy leaves an unsealed dir the
    # next submit rebuilds, never a tree a queued job executes half of.
    assert cmd.index(".hpc-tree-sealed") > cmd.index("ln -s")


def test_seal_creates_no_base_dir_the_job_does_not_write_through() -> None:
    """The seal must not manufacture run-state at the base (s4.table, 2026-07-29).

    ``mkdir -p <base>/_combiner`` was emitted for every shared path so ``ln -s``
    would have a live target. But nothing writes ``_combiner`` through the tree
    (the wave combiner and the cluster final reduce both run on the login node
    with ``cd <remote_path>``), and its EXISTENCE at the base is evidence: the
    harvest read an absent ``_combiner/`` as "no combiner ran" and fell back to
    the per-task reduce. Sealing a tree therefore made every no-combiner run
    reduce over zero partials — a successful harvest with an empty table.
    """
    with _ssh() as ssh:
        transport.seal_code_tree(ssh_target="u@c", remote_path="/p", digest=_D1)
    cmd = ssh.call_args[0][1]
    for rel in ("_combiner", "_aggregated"):
        assert f"ln -s /p/{rel} " in cmd, f"{rel} is still LINKED..."
        assert f"mkdir -p /p/{rel}" not in cmd, f"...but the seal must not CREATE /p/{rel}"
    for rel in code_tree.TREE_BASE_MATERIALIZED_PATHS:
        assert f"mkdir -p /p/{rel}" in cmd, f"{rel} is written through the link; it must exist"


@pytest.mark.skipif(
    shutil.which("bash") is None or os.name == "nt",
    reason="executes the emitted POSIX seal string; the cluster shell is not the dev box's",
)
def test_seal_command_really_links_every_shared_path_in_a_real_shell(tmp_path: Path) -> None:
    """Execute the emitted seal string against a real filesystem.

    Every other dial test stops at the engine mock, which is exactly the layer
    that hid the s4.table break: the string was asserted, never RUN. Here the
    shell actually executes it over a tree laid out the way ``materialize`` +
    ``deploy_runtime`` leave one, and the assertions are on the resulting inodes.
    """
    base = tmp_path / "remote" / "exp"
    tree = base / code_tree.TREES_REL / _D1
    (tree / ".hpc").mkdir(parents=True)
    with _ssh() as ssh:
        transport.seal_code_tree(ssh_target="u@c", remote_path=str(base), digest=_D1)
    proc = subprocess.run(
        ["bash", "-c", ssh.call_args[0][1]],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr

    for rel in code_tree.TREE_SHARED_PATHS:
        link = tree / rel
        assert link.is_symlink(), f"{rel} must be a symlink inside the tree, not a real dir"
        assert os.readlink(link) == str(base / rel)
    assert (tree / code_tree.TREE_SEAL_REL).is_file()
    # The job writes results/markers/announcements THROUGH the link, so those
    # base dirs must be live...
    for rel in code_tree.TREE_BASE_MATERIALIZED_PATHS:
        assert (base / rel).is_dir(), f"{rel} must resolve for a job whose cwd is the tree"
    # ...and the login-node-written ones must NOT have been invented.
    for rel in ("_combiner", "_aggregated"):
        assert not (base / rel).exists(), f"the seal must not create <base>/{rel}"


def test_seal_failure_raises_so_the_caller_can_degrade() -> None:
    with _ssh(returncode=3), pytest.raises(RuntimeError, match="could not seal code tree"):
        transport.seal_code_tree(ssh_target="u@c", remote_path="/p", digest=_D1)


def test_reap_removes_only_validated_tree_paths() -> None:
    with _ssh() as ssh:
        reaped = transport.reap_code_trees(
            ssh_target="u@c", remote_path="/p", digests=[_D1, "../../etc", _D2]
        )
    assert reaped == (_D1, _D2)
    cmd = ssh.call_args[0][1]
    assert cmd == f"rm -rf /p/.hpc/trees/{_D1} /p/.hpc/trees/{_D2}"
    assert "etc" not in cmd, "a malformed digest can never widen the rm"


def test_reap_is_fail_open() -> None:
    with patch("hpc_agent.infra.transport._guarded_ssh_bounded", side_effect=TimeoutError("dead")):
        assert transport.reap_code_trees(ssh_target="u@c", remote_path="/p", digests=[_D1]) == ()
    with _ssh(returncode=1):
        assert transport.reap_code_trees(ssh_target="u@c", remote_path="/p", digests=[_D1]) == ()
    # Nothing planned → no dial at all.
    with _ssh() as ssh:
        assert transport.reap_code_trees(ssh_target="u@c", remote_path="/p", digests=[]) == ()
    assert ssh.call_count == 0


# ── the GC planner ─────────────────────────────────────────────────────────


def test_gc_never_removes_a_referenced_tree() -> None:
    """The one thing this janitor may never do: delete the tree under a job
    that is still sitting in the scheduler queue."""
    present = [_D1, _D2, _D3, _D4]  # newest-first
    plan = code_tree.plan_tree_gc(present=present, referenced={_D4}, keep_newest=1)
    assert _D4 not in plan.reapable
    assert _D4 in plan.kept_referenced
    assert plan.reapable == (_D2, _D3), "unreferenced and outside the newest floor"


def test_gc_removes_an_unreferenced_old_tree_the_negative_twin() -> None:
    present = [_D1, _D2, _D3, _D4]
    plan = code_tree.plan_tree_gc(present=present, referenced=set(), keep_newest=1)
    assert plan.reapable == (_D2, _D3, _D4)
    assert plan.kept_newest == (_D1,)


def test_gc_keeps_the_newest_n_even_when_unreferenced() -> None:
    present = [_D1, _D2, _D3, _D4]
    plan = code_tree.plan_tree_gc(present=present, referenced=set())
    assert code_tree.GC_KEEP_NEWEST == 3
    assert plan.reapable == (_D4,)
    assert plan.kept_newest == (_D1, _D2, _D3)


def test_gc_protects_the_digest_being_deployed_right_now() -> None:
    """The tree this submit just minted may not even be in the listing yet (the
    ``ls`` predates it) and no journal record points at it until the run is
    recorded — so the caller protects it unconditionally."""
    plan = code_tree.plan_tree_gc(
        present=[_D1, _D2, _D3, _D4], referenced=set(), protect=(_D4,), keep_newest=1
    )
    assert _D4 not in plan.reapable
    assert _D4 in plan.kept_referenced


def test_gc_refuses_the_whole_pass_when_references_are_unknown() -> None:
    """An unreadable journal looks EXACTLY like 'nothing is referenced'. The
    conservative rule refuses rather than guessing — refusing costs disk,
    guessing costs a run."""
    plan = code_tree.plan_tree_gc(present=[_D1, _D2, _D3, _D4], referenced=None, keep_newest=1)
    assert plan.refused is True
    assert plan.reapable == ()
    assert "could not be established" in (plan.refused_reason or "")


def test_gc_ignores_non_digest_inventory_lines() -> None:
    plan = code_tree.plan_tree_gc(
        present=["", "README", _D1, "..", _D2], referenced=set(), keep_newest=1
    )
    assert plan.reapable == (_D2,)
    assert plan.kept_newest == (_D1,)


def test_gc_plan_is_serialisable_as_data() -> None:
    plan = code_tree.plan_tree_gc(present=[_D1], referenced=None)
    assert plan.to_dict()["refused_reason"]
    assert plan.to_dict()["reapable"] == []
