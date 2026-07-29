"""§10.S4 wiring in the submit path: pinning, no-op re-deploy, GC seat, fallback.

The hazard: under eager submit a job can sit in the scheduler queue for days and
then execute whatever the shared remote tree holds at START time — a silent
provenance lie. Content-addressing removes the possibility: the submitted job's
``REPO_DIR`` names a tree keyed by the snapshot's own content, so a later push
cannot reach it.

The pure layer (path composition, digest identity, the GC planner) and the
transport dials are covered in ``tests/infra/test_code_tree.py``; this file
covers what ``ops/submit_flow`` does with them.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from hpc_agent.infra import code_tree
from hpc_agent.infra.backends import HPCBackend
from hpc_agent.ops import submit_flow as sf

_D1 = "a" * 12
_D2 = "b" * 12
_D3 = "c" * 12
_D4 = "d" * 12
_D5 = "e" * 12
_TREE = f"/r/.hpc/trees/{_D1}"


class _RecordingBackend(HPCBackend):
    """Minimal stub that records the exact ``job_env`` each dispatch saw."""

    JOB_ID_REGEX = re.compile(r"JOB(\d+)")

    def __init__(self) -> None:
        self.log_dir = "/tmp/tree-logs"
        self._counter = 700
        self.envs: list[dict[str, str]] = []

    def _build_command(self, task_range, job_name, job_env, *, extra_flags=None, array=True):  # type: ignore[override]
        return ["qsub", "-t", str(task_range), "-N", job_name, *(extra_flags or [])]

    def _execute_command(self, cmd, job_env, cwd):  # type: ignore[override]
        self.envs.append(dict(job_env))
        self._counter += 1
        return SimpleNamespace(stdout=f"JOB{self._counter}\n", stderr="", returncode=0)

    def _setup_log_dir(self) -> None:
        pass


def _spec(run_id: str = "r1", *, canary: bool = False, repo_dir: str = "/r"):
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

    return SubmitFlowSpec(
        profile="p",
        cluster="c",
        ssh_target="user@host",
        remote_path="/r",
        job_name=run_id,
        run_id=run_id,
        total_tasks=4,
        backend="sge",
        script="run.sh",
        # build_submit_spec ALWAYS stamps REPO_DIR = deploy_target_for(remote_path);
        # mirror that so the override under test replaces a real value.
        job_env={"EXECUTOR": "python run.py", "REPO_DIR": repo_dir},
        canary=canary,
        result_dir_template="results/{run_id}/task_{task_id}",
    )


@pytest.fixture
def _mirror_sidecar(tmp_path: Path):
    """A canary leg needs the main sidecar on disk to mirror."""
    from hpc_agent.state.runs import write_run_sidecar

    write_run_sidecar(
        tmp_path,
        run_id="r1",
        cmd_sha="c",
        hpc_agent_version="v",
        submitted_at="2026-01-01T00:00:00+00:00",
        executor="python run.py",
        result_dir_template="results/{run_id}/task_{task_id}",
        task_count=4,
        tasks_py_sha="",
    )


# ── 2. the submitted job references its digest tree ────────────────────────


def test_the_submitted_job_is_pinned_to_its_digest_tree(tmp_path: Path, journal_home) -> None:
    """The pin: every dispatched task runs ``cd "$REPO_DIR"``, and REPO_DIR is
    the content-keyed tree. A push that lands after this submit mints a DIFFERENT
    digest and cannot touch this tree, so a job that waits in the queue still
    executes the snapshot it was submitted with."""
    backend = _RecordingBackend()
    with (
        mock.patch.object(sf, "build_remote_backend", return_value=backend),
        mock.patch.object(sf, "submit_and_record"),
    ):
        sf._submit_one_spec(experiment_dir=tmp_path, spec=_spec(), code_tree_path=_TREE)
    assert backend.envs, "the array was dispatched"
    assert backend.envs[0]["REPO_DIR"] == _TREE
    # And the pinned value round-trips back to the digest the GC reasons over —
    # no new record field is needed to answer "which tree does this run need?".
    assert code_tree.digest_from_repo_dir(backend.envs[0]["REPO_DIR"]) == _D1


def test_canary_and_main_pin_to_the_same_tree(
    tmp_path: Path, journal_home, _mirror_sidecar
) -> None:
    """The canary gates the main array; gating on a canary that ran DIFFERENT
    code would prove nothing."""
    backend = _RecordingBackend()
    with (
        mock.patch.object(sf, "build_remote_backend", return_value=backend),
        mock.patch.object(sf, "submit_and_record"),
        mock.patch.object(sf, "_canary_decision", return_value=(True, None)),
    ):
        sf._submit_one_spec(experiment_dir=tmp_path, spec=_spec(canary=True), code_tree_path=_TREE)
    assert len(backend.envs) >= 2, "canary + main both dispatched"
    assert {e["REPO_DIR"] for e in backend.envs} == {_TREE}


# ── 4. migration / compat: the legacy tree path still works ────────────────


def test_a_run_without_a_code_tree_keeps_the_base_repo_dir(tmp_path: Path, journal_home) -> None:
    """Absent-disables (the placement-drift precedent). ``code_tree_path=None``
    — content-addressing off, a pure-API backend, a skip_rsync_deploy re-entry,
    or a disclosed tree-deploy failure — leaves REPO_DIR exactly as
    ``build_submit_spec`` derived it. No flag-day."""
    backend = _RecordingBackend()
    with (
        mock.patch.object(sf, "build_remote_backend", return_value=backend),
        mock.patch.object(sf, "submit_and_record"),
    ):
        sf._submit_one_spec(experiment_dir=tmp_path, spec=_spec(), code_tree_path=None)
    assert backend.envs[0]["REPO_DIR"] == "/r"
    assert code_tree.digest_from_repo_dir(backend.envs[0]["REPO_DIR"]) is None


def test_inspect_deployment_resolves_a_pinned_run_to_its_tree_and_legacy_to_the_base(
    tmp_path: Path, journal_home
) -> None:
    """Reference resolution falls back for a run that predates content-addressing:
    a pinned run is inspected AT ITS TREE (the base has since moved on), a legacy
    run at the base — byte-for-byte the pre-S4 answer."""
    from hpc_agent.ops.inspect_deployment import _resolve_target_path
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    def _rec(run_id: str, job_env: dict[str, str]) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            profile="p",
            cluster="c",
            ssh_target="u@h",
            remote_path="/r",
            job_name=run_id,
            job_ids=["1"],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(tmp_path.resolve()),
            job_env=job_env,
        )

    upsert_run(tmp_path, _rec("pinned", {"REPO_DIR": _TREE}))
    upsert_run(tmp_path, _rec("legacy", {}))

    target, repo_dir, _ = _resolve_target_path(path=None, run_id="pinned", experiment_dir=tmp_path)
    assert target == repo_dir == _TREE
    target, repo_dir, _ = _resolve_target_path(path=None, run_id="legacy", experiment_dir=tmp_path)
    assert target == repo_dir == "/r"


# ── 1. content-addressed deploy: the no-op re-deploy ───────────────────────


def _engine(*, sealed: bool, known: tuple[str, ...] = ()):
    """Patch the transport ENGINE (rsync argv + the ssh legs), not the seams."""
    lines = ["__HPC_TREE_SEALED__"] if sealed else []
    lines += list(known)
    stdout = "\n".join([*lines, "__HPC_TREE_ACK__=0"]) + "\n"
    rsyncs: list[list[str]] = []

    def _fake_run(argv, *_a, **_kw):
        rsyncs.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    ctx = (
        mock.patch("hpc_agent.infra.transport._have_rsync", return_value=True),
        mock.patch("hpc_agent.infra.transport.run_capture_bounded", side_effect=_fake_run),
        mock.patch(
            "hpc_agent.infra.transport._guarded_ssh_bounded",
            return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
        ),
        mock.patch(
            "hpc_agent.infra.transport.ssh_run",
            return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
        ),
    )
    return ctx, rsyncs


def _tree_rsyncs(rsyncs: list[list[str]]) -> list[list[str]]:
    """Every rsync that targeted a digest tree — the snapshot AND the framework
    deploy (``deploy_runtime`` ships into the tree too, which is why the package
    version is folded into the digest)."""
    return [c for c in rsyncs if c and c[0] == "rsync" and "/.hpc/trees/" in c[-1]]


def _snapshot_rsyncs(rsyncs: list[list[str]]) -> list[list[str]]:
    """Just the CODE snapshot transfer (the exclude-filtered one);
    ``deploy_runtime``'s framework leg carries no ``--exclude``."""
    return [c for c in _tree_rsyncs(rsyncs) if "--exclude" in c]


def _exp(tmp_path: Path) -> Path:
    root = tmp_path / "exp"
    (root / ".hpc").mkdir(parents=True)
    (root / ".hpc" / "tasks.py").write_text("def total():\n    return 1\n", encoding="utf-8")
    return root


def _deploy(root: Path, ctx):
    with ctx[0], ctx[1], ctx[2], ctx[3]:
        return sf._deploy_code_tree(
            experiment_dir=root,
            ssh_target="u@c",
            remote_path="/r",
            rsync_excludes=None,
            scheduler="sge",
            reducer_item=None,
        )


def test_a_matching_digest_makes_deploy_a_no_op_verification(tmp_path: Path) -> None:
    """The latency win: resubmitting unchanged code re-rsyncs NOTHING. Counted at
    the engine mock — zero rsync invocations targeting a tree."""
    root = _exp(tmp_path)
    ctx, rsyncs = _engine(sealed=True, known=(_D1,))
    with mock.patch.object(sf, "_groom_code_trees") as groom:
        tree = _deploy(root, ctx)
    assert tree and "/.hpc/trees/" in tree
    assert _tree_rsyncs(rsyncs) == [], "a sealed tree must not be re-shipped"
    assert groom.call_count == 0, "a reuse pass is charged nothing, not even the GC"


def test_an_absent_digest_ships_the_snapshot_once(tmp_path: Path) -> None:
    """The negative twin: no seal → exactly one code transfer into the tree."""
    root = _exp(tmp_path)
    ctx, rsyncs = _engine(sealed=False, known=())
    with mock.patch.object(sf, "_groom_code_trees"):
        tree = _deploy(root, ctx)
    assert tree
    snapshot = _snapshot_rsyncs(rsyncs)
    assert len(snapshot) == 1, f"expected ONE snapshot rsync, got {snapshot}"
    # Plus exactly one framework-deploy leg into the same tree.
    assert len(_tree_rsyncs(rsyncs)) == 2
    # #F20 generalised from running jobs to pending ones.
    assert "--inplace" not in snapshot[0]
    assert "--delete" not in snapshot[0]


def test_link_dest_targets_the_previous_tree_opportunistically(tmp_path: Path) -> None:
    """§10.S4's probe table: this saves bytes where the filesystem hardlinks and
    silently copies on CARC /scratch1 (BeeGFS). Correctness does not depend on
    which happened — only the flag's presence is asserted."""
    root = _exp(tmp_path)
    ctx, rsyncs = _engine(sealed=False, known=(_D2, _D3))
    with mock.patch.object(sf, "_groom_code_trees"):
        _deploy(root, ctx)
    flags = [a for a in _snapshot_rsyncs(rsyncs)[0] if a.startswith("--link-dest=")]
    assert flags == [f"--link-dest=/r/.hpc/trees/{_D2}"], "newest OTHER tree"


def test_a_tree_deploy_failure_degrades_to_the_legacy_base(tmp_path: Path, caplog) -> None:
    """Losing the pin is worth strictly less than refusing to submit. The
    fallback is DISCLOSED, and the run-start code-identity check still stands."""
    root = _exp(tmp_path)
    ctx, _ = _engine(sealed=False)
    with (
        ctx[0],
        ctx[1],
        ctx[2],
        ctx[3],
        mock.patch(
            "hpc_agent.infra.transport.seal_code_tree", side_effect=RuntimeError("no symlink")
        ),
        caplog.at_level("WARNING"),
    ):
        tree = sf._deploy_code_tree(
            experiment_dir=root,
            ssh_target="u@c",
            remote_path="/r",
            rsync_excludes=None,
            scheduler="sge",
            reducer_item=None,
        )
    assert tree is None
    assert "NOT pinned" in caplog.text


def test_the_kill_switch_returns_the_pre_s4_shape(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HPC_NO_CODE_TREES", "1")
    assert sf._code_trees_enabled() is False
    root = _exp(tmp_path)
    with (
        mock.patch.object(sf, "rsync_push", return_value=SimpleNamespace(returncode=0, stderr="")),
        mock.patch.object(sf, "deploy_runtime"),
        mock.patch.object(sf, "_deploy_code_tree") as tree_deploy,
    ):
        assert (
            sf._push_and_deploy(
                experiment_dir=root, ssh_target="u@c", remote_path="/r", rsync_excludes=None
            )
            is None
        )
    assert tree_deploy.call_count == 0


# ── 3. the GC seat ─────────────────────────────────────────────────────────


def _seed(tmp_path: Path, run_id: str, *, status: str, repo_dir: str | None) -> None:
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        tmp_path,
        RunRecord(
            run_id=run_id,
            profile="p",
            cluster="c",
            ssh_target="u@h",
            remote_path="/r",
            job_name=run_id,
            job_ids=["1"] if status != "submitting" else [],
            total_tasks=1,
            submitted_at="2026-01-01T00:00:00+00:00",
            experiment_dir=str(tmp_path.resolve()),
            status=status,
            job_env={"REPO_DIR": repo_dir} if repo_dir else {},
        ),
    )


def test_referenced_digests_cover_every_non_terminal_status(tmp_path: Path, journal_home) -> None:
    """``JournalStatus`` has five values and three are terminal, so
    ``in_flight ∪ submitting`` IS the non-terminal set — exactly the runs whose
    array may still be sitting in the queue."""
    _seed(tmp_path, "live", status="in_flight", repo_dir=f"/r/.hpc/trees/{_D1}")
    _seed(tmp_path, "minting", status="submitting", repo_dir=f"/r/.hpc/trees/{_D2}")
    _seed(tmp_path, "done", status="complete", repo_dir=f"/r/.hpc/trees/{_D3}")
    _seed(tmp_path, "old", status="in_flight", repo_dir=None)  # pre-S4 run
    refs = sf._referenced_tree_digests(tmp_path)
    assert refs == {_D1, _D2}


def test_referenced_digests_are_unknown_not_empty_when_the_scan_fails(
    tmp_path: Path,
) -> None:
    with mock.patch(
        "hpc_agent.state.index.find_in_flight_runs", side_effect=OSError("journal gone")
    ):
        assert sf._referenced_tree_digests(tmp_path) is None


#: Five trees, newest-first: the newest three sit inside GC_KEEP_NEWEST, so
#: _D4 and _D5 are the only candidates and the reference test has real teeth.
_INVENTORY = (_D1, _D2, _D3, _D4, _D5)


def _probe(known: tuple[str, ...] = _INVENTORY) -> code_tree.CodeTreeProbe:
    return code_tree.CodeTreeProbe(
        digest=_D1, tree_path=f"/r/.hpc/trees/{_D1}", sealed=False, known=known, trusted=True
    )


def test_gc_never_removes_the_tree_of_a_live_run(tmp_path: Path, journal_home) -> None:
    """The invariant: a submitting / in_flight / queued run's tree is never
    reaped, even when it is old enough to be outside the keep-newest floor."""
    _seed(tmp_path, "live", status="in_flight", repo_dir=f"/r/.hpc/trees/{_D5}")
    with mock.patch("hpc_agent.infra.transport.reap_code_trees", return_value=()) as reap:
        sf._groom_code_trees(
            experiment_dir=tmp_path,
            ssh_target="u@c",
            remote_path="/r",
            probe=_probe(),
            minted=_D1,
        )
    assert reap.call_count == 1
    planned = reap.call_args.kwargs["digests"]
    assert _D5 not in planned, "a live run's tree is never a candidate"
    assert planned == (_D4,)


def test_gc_reaps_an_unreferenced_old_tree(tmp_path: Path, journal_home) -> None:
    """Negative twin: the SAME inventory with the old tree's run TERMINAL — now
    both out-of-floor trees go."""
    _seed(tmp_path, "done", status="complete", repo_dir=f"/r/.hpc/trees/{_D5}")
    with mock.patch("hpc_agent.infra.transport.reap_code_trees", return_value=(_D4, _D5)) as reap:
        sf._groom_code_trees(
            experiment_dir=tmp_path,
            ssh_target="u@c",
            remote_path="/r",
            probe=_probe(),
            minted=_D1,
        )
    assert reap.call_args.kwargs["digests"] == (_D4, _D5)


def test_gc_plans_nothing_from_an_untrusted_inventory(tmp_path: Path, journal_home) -> None:
    """A severed probe read yields an empty listing that MUST NOT be mistaken for
    'these are all the trees'."""
    probe = code_tree.CodeTreeProbe(
        digest=_D1, tree_path=f"/r/.hpc/trees/{_D1}", sealed=False, known=(), trusted=False
    )
    with mock.patch("hpc_agent.infra.transport.reap_code_trees") as reap:
        sf._groom_code_trees(
            experiment_dir=tmp_path,
            ssh_target="u@c",
            remote_path="/r",
            probe=probe,
            minted=_D1,
        )
    assert reap.call_count == 0


def test_gc_refuses_when_the_journal_cannot_be_read(tmp_path: Path, caplog) -> None:
    with (
        mock.patch.object(sf, "_referenced_tree_digests", return_value=None),
        mock.patch("hpc_agent.infra.transport.reap_code_trees") as reap,
        caplog.at_level("WARNING"),
    ):
        sf._groom_code_trees(
            experiment_dir=tmp_path,
            ssh_target="u@c",
            remote_path="/r",
            probe=_probe(),
            minted=_D1,
        )
    assert reap.call_count == 0
    assert "refused" in caplog.text


# ── the prelude threads the tree (or None) to every spec ───────────────────


def test_the_prelude_returns_the_tree_and_probes_the_executor_there(
    tmp_path: Path,
) -> None:
    """The post-deploy ``test -f "$REPO_DIR/<executor>"`` must probe the tree the
    JOB will cd into, not the base."""
    seen: dict[str, object] = {}

    with (
        mock.patch.object(sf, "_validate_ssh_target"),
        mock.patch.object(sf, "_preflight_probe"),
        mock.patch.object(sf, "_run_uv_preflight_for_batch"),
        mock.patch.object(sf, "_push_and_deploy", return_value=_TREE),
        mock.patch.object(
            sf, "_run_executor_existence_preflight", side_effect=lambda **kw: seen.update(kw)
        ),
    ):
        out = sf._run_shared_prelude(
            experiment_dir=tmp_path,
            ssh_target="u@c",
            remote_path="/r",
            rsync_excludes=None,
            scheduler="sge",
            job_envs=[{}],
            skip_preflight=True,
            skip_prelude_io=False,
            per_task_executors=["python3 run.py"],
        )
    assert out == _TREE
    assert seen["remote_path"] == _TREE


def test_a_skip_staging_reentry_re_derives_the_pin_read_only(tmp_path: Path) -> None:
    """Phase 2 of the two-phase canary gate ships nothing but still qsubs the
    main array. It must land on the SAME tree Phase 1's canary validated —
    gating an array on a canary that ran different code proves nothing — and it
    must get there WITHOUT staging anything."""
    root = _exp(tmp_path)
    ctx, rsyncs = _engine(sealed=True, known=(_D1,))
    with (
        mock.patch.object(sf, "_validate_ssh_target"),
        mock.patch.object(sf, "_preflight_probe"),
        mock.patch.object(sf, "_run_uv_preflight_for_batch"),
        mock.patch.object(sf, "_push_and_deploy") as pd,
        ctx[0],
        ctx[1],
        ctx[2],
        ctx[3],
    ):
        out = sf._run_shared_prelude(
            experiment_dir=root,
            ssh_target="u@c",
            remote_path="/r",
            rsync_excludes=None,
            scheduler="sge",
            job_envs=[{}],
            skip_preflight=True,
            skip_prelude_io=True,
        )
    assert out and "/.hpc/trees/" in out
    assert pd.call_count == 0, "skip-staging must not stage"
    assert rsyncs == [], "a read-only re-derivation moves no bytes"


def test_a_skip_staging_reentry_falls_back_when_no_tree_is_sealed(tmp_path: Path) -> None:
    """Conservative: a skip-the-staging request must never BECOME a staging
    request — an unsealed snapshot degrades to the legacy base."""
    root = _exp(tmp_path)
    ctx, rsyncs = _engine(sealed=False, known=())
    with (
        mock.patch.object(sf, "_validate_ssh_target"),
        mock.patch.object(sf, "_preflight_probe"),
        mock.patch.object(sf, "_run_uv_preflight_for_batch"),
        ctx[0],
        ctx[1],
        ctx[2],
        ctx[3],
    ):
        out = sf._run_shared_prelude(
            experiment_dir=root,
            ssh_target="u@c",
            remote_path="/r",
            rsync_excludes=None,
            scheduler="sge",
            job_envs=[{}],
            skip_preflight=True,
            skip_prelude_io=True,
        )
    assert out is None
    assert rsyncs == []


def test_the_prelude_returns_none_when_the_kill_switch_is_set(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HPC_NO_CODE_TREES", "1")
    with (
        mock.patch.object(sf, "_validate_ssh_target"),
        mock.patch.object(sf, "_preflight_probe"),
        mock.patch.object(sf, "_run_uv_preflight_for_batch"),
        mock.patch.object(sf, "_push_and_deploy") as pd,
    ):
        out = sf._run_shared_prelude(
            experiment_dir=tmp_path,
            ssh_target="u@c",
            remote_path="/r",
            rsync_excludes=None,
            scheduler="sge",
            job_envs=[{}],
            skip_preflight=True,
            skip_prelude_io=True,
        )
    assert out is None
    assert pd.call_count == 0


def test_a_pure_api_backend_has_no_tree(tmp_path: Path) -> None:
    assert (
        sf._run_shared_prelude(
            experiment_dir=tmp_path,
            ssh_target="u@c",
            remote_path="/r",
            rsync_excludes=None,
            scheduler=None,
            job_envs=[{}],
            requires_ssh=False,
            skip_preflight=True,
            skip_prelude_io=False,
        )
        is None
    )
