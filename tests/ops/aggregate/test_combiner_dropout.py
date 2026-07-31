"""The cluster-side combiner went MISSING silently — U5's dropout fixture.

Live evidence, 2026-07-30: the deployed ``.hpc/_hpc_combiner.py`` was not on
the cluster. Nothing said so. Every per-wave combine came back as an ordinary
non-zero and was journaled into ``failed_waves`` and retried; the first thing a
human actually saw was, at 20:52, the CROSS-WAVE reduce failing with

    [combiner] ERROR: no _combiner/<run_id>/wave_*.json ... partials to reduce

— a message about *wave partials*, from a different code path, hours after the
cause — and at 22:00 the human hand-launched the final reduce over ssh.

This module reproduces both evidence shapes against a FAKE CLUSTER that is a
real POSIX shell over a real temp tree, with a ``python3`` on PATH that behaves
the way the real one does: it refuses to open a script that isn't there, and
the deployed combiner writes ``_combiner/<run_id>/wave_<N>.json`` when it is.
Nothing about the dropout is simulated in Python — the commands under test are
the bytes we would put on the wire, executed.

Two halves, and the second is the point:

* ``TestPreU5Behaviour`` pins what the code did BEFORE the guard, by composing
  the pre-U5 command shape from the same pieces minus the guard and running it
  through the same fake cluster. It asserts the SILENCE: no named cause, no
  actionable command, and the failure surfacing only later, second-hand,
  through the final reduce. These are the RED conditions.
* ``TestGuardedBehaviour`` runs the real, current runners and asserts the early
  named refusal with the redeploy command — and that a healthy cluster is
  completely unaffected.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from hpc_agent import errors
from hpc_agent.execution.mapreduce import deployed_artifact as D
from hpc_agent.ops.aggregate.combine import combine_wave, combine_waves
from hpc_agent.state.journal import load_run, upsert_run
from hpc_agent.state.run_record import RunRecord

_SH = shutil.which("sh") or shutil.which("bash")
pytestmark = pytest.mark.skipif(_SH is None, reason="no POSIX shell available")

_RUN_ID = "hoffman2_7f3ac91b"  # begins with 'h' — the incident's `_combiner/h…`


# ── the fake cluster ────────────────────────────────────────────────────────

#: A ``python3`` that behaves like the real one for the ONE thing this fixture
#: turns on: opening the script it was handed. When the deployed combiner is
#: absent, CPython prints exactly this to stderr and exits 2 — the shape that
#: the pre-U5 control plane read as "the combiner ran and failed".
_FAKE_PYTHON3 = r"""#!/bin/sh
# argv: <script> [--wave N | --final] --run-id ID [--force]
script="$1"; shift
if [ ! -f "$script" ]; then
  echo "python3: can't open file '$script': [Errno 2] No such file or directory" >&2
  exit 2
fi
mode=""; wave=""; run_id=""
while [ $# -gt 0 ]; do
  case "$1" in
    --wave) mode=wave; wave="$2"; shift 2 ;;
    --final) mode=final; shift ;;
    --run-id) run_id="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if [ "$mode" = wave ]; then
  mkdir -p "_combiner/$run_id"
  printf '{"run_id": "%s", "wave": %s, "grid_points": {}}\n' "$run_id" "$wave" \
    > "_combiner/$run_id/wave_$wave.json"
  echo "[combiner] wrote _combiner/$run_id/wave_$wave.json"
  exit 0
fi
# --final: merge every wave partial, or emit the 20:52 message.
if [ -z "$(ls -1 "_combiner/$run_id"/wave_*.json 2>/dev/null)" ]; then
  printf '%s\n' "[combiner] ERROR: no _combiner/$run_id/wave_*.json (or legacy" \
    "_combiner/wave_*.json) partials to reduce" >&2
  exit 1
fi
mkdir -p "_aggregated/$run_id"
printf '{"aggregated_metrics": {}}\n' > "_aggregated/$run_id/metrics_aggregate.json"
echo "[combiner] wrote _aggregated/$run_id/metrics_aggregate.json"
exit 0
"""


class FakeCluster:
    """A deploy root plus a ``sh`` that runs the exact command under test."""

    def __init__(self, root: Path, *, combiner_deployed: bool) -> None:
        self.root = root
        (root / ".hpc").mkdir(parents=True, exist_ok=True)
        if combiner_deployed:
            shutil.copyfile(D.combiner_source_path(), root / D.COMBINER_REL)
        self._bin = root.parent / "bin"
        self._bin.mkdir(exist_ok=True)
        py3 = self._bin / "python3"
        py3.write_text(_FAKE_PYTHON3, encoding="utf-8", newline="\n")
        py3.chmod(0o755)
        self.calls: list[str] = []

    @property
    def remote_path(self) -> str:
        return self.root.as_posix()

    def drop_combiner(self) -> None:
        """The dropout: the artifact goes away, nothing else changes."""
        (self.root / D.COMBINER_REL).unlink()

    def ssh_run(self, cmd: str, **_kw: Any) -> subprocess.CompletedProcess[str]:
        """Stand-in for ``infra.remote.ssh_run`` — runs *cmd* for real."""
        self.calls.append(cmd)
        env = dict(os.environ)
        env["PATH"] = str(self._bin) + os.pathsep + env.get("PATH", "")
        assert _SH is not None
        return subprocess.run(  # noqa: S603 — fixed argv; cmd is the artifact under test
            [_SH, "-c", cmd],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            env=env,
            cwd=str(self.root.parent),
        )

    def wave_partials(self) -> list[Path]:
        return sorted((self.root / "_combiner" / _RUN_ID).glob("wave_*.json"))


@pytest.fixture(autouse=True)
def _journal_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


@pytest.fixture(autouse=True)
def _no_env_activation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare login python for the fake cluster.

    The combine legs thread ``remote_activation_for_sidecar`` in front of the
    command, which resolves against the DEVELOPER'S real ``clusters.yaml`` —
    so a run recorded against a cluster the machine happens to know would
    prepend a ``module load … && conda activate … &&`` this fixture's shell
    cannot honour. The activation is orthogonal to the artifact-presence
    question under test; pin it empty so the command that runs is the command
    the assertions are about.
    """
    monkeypatch.setattr(
        "hpc_agent.infra.clusters.remote_activation_for_sidecar",
        lambda *_a, **_kw: "",
    )


@pytest.fixture
def cluster(tmp_path: Path) -> FakeCluster:
    return FakeCluster(tmp_path / "cluster" / "exp", combiner_deployed=True)


def _seed_run(experiment_dir: Path, cluster: FakeCluster) -> RunRecord:
    rec = RunRecord(
        run_id=_RUN_ID,
        profile="ml",
        cluster="hoffman2",
        ssh_target="user@hoffman2",
        remote_path=cluster.remote_path,
        job_name="ml",
        job_ids=["4815162"],
        total_tasks=4,
        submitted_at="2026-07-30T18:00:00+00:00",
        experiment_dir=str(experiment_dir),
    )
    upsert_run(experiment_dir, rec)
    return rec


def _patch_ssh(cluster: FakeCluster):  # noqa: ANN202 — context manager factory
    """Patch the ONE seam both the combine runners and the reduce go through."""
    return patch("hpc_agent.infra.transport._combiner.ssh_run", side_effect=cluster.ssh_run)


# ── half one: what it did before the guard (the RED conditions) ─────────────


class TestPreU5Behaviour:
    """Pins the silence. Every assertion here describes the incident."""

    @staticmethod
    def _pre_u5_wave_cmd(cluster: FakeCluster, wave: int) -> str:
        """The command shape shipped BEFORE U5: no presence guard."""
        import shlex

        return (
            f"cd {shlex.quote(cluster.remote_path)} && "
            f"HPC_WAVE={wave} HPC_RUN_ID={shlex.quote(_RUN_ID)} "
            f"python3 {D.COMBINER_REL} --wave {wave} --run-id {shlex.quote(_RUN_ID)}"
        )

    @staticmethod
    def _pre_u5_final_cmd(cluster: FakeCluster) -> str:
        import shlex

        return (
            f"cd {shlex.quote(cluster.remote_path)} && "
            f"HPC_RUN_ID={shlex.quote(_RUN_ID)} "
            f"python3 {D.COMBINER_REL} --final --run-id {shlex.quote(_RUN_ID)}"
        )

    def test_missing_combiner_looked_like_an_ordinary_combine_failure(
        self, cluster: FakeCluster
    ) -> None:
        """RED: nothing in the wave-combine output names the deploy dropout."""
        cluster.drop_combiner()
        proc = cluster.ssh_run(self._pre_u5_wave_cmd(cluster, 0))

        assert proc.returncode != 0
        # This is all the operator got. It names a file, not a cause; it offers
        # no command; and it is indistinguishable from a hundred other reasons
        # a remote python exits non-zero.
        assert "No such file or directory" in proc.stderr
        assert D.COMBINER_ABSENT_SENTINEL not in proc.stderr
        assert "redeploy" not in (proc.stdout + proc.stderr).lower()

    def test_the_failure_surfaced_only_later_as_a_message_about_partials(
        self, tmp_path: Path
    ) -> None:
        """RED: the 20:52 evidence line — and it is about the WRONG thing.

        Reproduces the incident's two-root shape, which is what makes the
        symptom so misleading. §10.S4 gives a run TWO deploy roots — the
        content-addressed code tree its job runs in, and the shared base — and
        ``deploy_runtime`` ships the framework files to each independently. A
        tree that never got its copy still has ``_combiner/`` (a symlink to the
        base), so:

        * every wave combine dies in the TREE, where the artifact is absent,
          writing no partial anywhere;
        * the cross-wave reduce runs at the BASE, where the artifact IS
          present, and therefore runs fine — and reports the only thing it can
          see, which is that there are no PARTIALS to reduce.

        A reader chasing that message investigates the waves, the tasks, the
        reporter env — everything except the artifact that was never deployed
        to the root the waves actually used.
        """
        base = FakeCluster(tmp_path / "cluster" / "exp", combiner_deployed=True)
        tree = FakeCluster(
            tmp_path / "cluster" / "exp" / ".hpc" / "trees" / "ab12cd34ef56",
            combiner_deployed=False,
        )

        for wave in (0, 1):
            wave_proc = tree.ssh_run(self._pre_u5_wave_cmd(tree, wave))
            assert wave_proc.returncode != 0
        assert base.wave_partials() == []
        assert tree.wave_partials() == []

        final = base.ssh_run(self._pre_u5_final_cmd(base))

        assert final.returncode == 1
        assert "[combiner] ERROR: no _combiner/" in final.stderr
        assert f"_combiner/{_RUN_ID}" in final.stderr
        assert _RUN_ID.startswith("h")  # the incident's truncated `_combiner/h…`
        # The dropout is nowhere in the only message anyone saw: the message
        # names the partials directory, never the artifact, and never the root
        # the artifact is missing from.
        assert D.COMBINER_REL not in final.stderr
        assert "trees/" not in final.stderr

    def test_the_two_roots_are_why_the_symptom_pointed_the_wrong_way(
        self, tmp_path: Path
    ) -> None:
        """RED: presence at the base 'proved' the deployment was fine.

        Anyone who checked — including the ``inspect-deployment`` path, which
        resolves ``REPO_DIR`` correctly but was not what got run at 20:52 —
        would find ``.hpc/_hpc_combiner.py`` sitting at ``remote_path`` exactly
        where it belongs. Nothing in the pre-U5 pipeline ever asked the tree.
        """
        base = FakeCluster(tmp_path / "cluster" / "exp", combiner_deployed=True)
        tree = FakeCluster(
            tmp_path / "cluster" / "exp" / ".hpc" / "trees" / "ab12cd34ef56",
            combiner_deployed=False,
        )

        base_probe, _ = D.split_combiner_probe(
            base.ssh_run(D.combiner_probe_snippet(root=base.remote_path)).stdout
        )
        tree_probe, _ = D.split_combiner_probe(
            tree.ssh_run(D.combiner_probe_snippet(root=tree.remote_path)).stdout
        )

        assert base_probe is not None and base_probe.state == "present"
        assert tree_probe is not None and tree_probe.state == "absent"

    def test_the_control_plane_journaled_it_as_a_failed_wave_and_retried(
        self, tmp_path: Path, cluster: FakeCluster
    ) -> None:
        """RED: the recovery machinery pointed the wrong way.

        ``combine-wave``'s contract turns a non-zero combiner into a
        ``failed_waves`` entry, which the retry ladder then re-attempts with
        ``--force`` and the recovery registry treats as ``combiner_failed`` —
        whose menu says *resubmit the offending tasks*. For a missing artifact
        every one of those is wasted work against an unchanged cause.
        """
        _seed_run(tmp_path, cluster)
        cluster.drop_combiner()

        # Feed the PRE-U5 command shape through the same seam the primitive
        # uses, so the journal outcome is the historical one.
        def _pre_u5_checked(**kw: Any) -> tuple[bool, str, str]:
            proc = cluster.ssh_run(self._pre_u5_wave_cmd(cluster, int(kw["wave"])))
            return (proc.returncode == 0, proc.stdout, proc.stderr)

        with patch("hpc_agent.infra.transport.run_combiner_checked", _pre_u5_checked):
            ok, _stdout, stderr = combine_wave(
                tmp_path,
                _RUN_ID,
                wave=0,
                ssh_target="user@hoffman2",
                remote_path=cluster.remote_path,
            )

        assert ok is False
        assert "No such file or directory" in stderr
        record = load_run(tmp_path, _RUN_ID)
        assert record is not None
        assert record.failed_waves == [0], "the dropout was recorded as a data failure"
        assert record.combined_waves == []


# ── half two: the guarded behaviour (the GREEN conditions) ──────────────────


class TestGuardedBehaviour:
    """The same fake cluster, the real runners."""

    def test_wave_combine_refuses_early_by_name(
        self, tmp_path: Path, cluster: FakeCluster
    ) -> None:
        _seed_run(tmp_path, cluster)
        cluster.drop_combiner()

        with _patch_ssh(cluster), pytest.raises(errors.CombinerMissing) as exc:
            combine_wave(
                tmp_path,
                _RUN_ID,
                wave=0,
                ssh_target="user@hoffman2",
                remote_path=cluster.remote_path,
            )

        msg = str(exc.value)
        assert D.COMBINER_REL in msg
        assert "absent" in msg
        # Discriminated from the data-failure sibling it used to masquerade as.
        assert isinstance(exc.value, errors.CombinerFailed)
        assert exc.value.retry_safe is False

    def test_the_refusal_carries_the_registry_redeploy_command(
        self, tmp_path: Path, cluster: FakeCluster
    ) -> None:
        """The remediation must be runnable, and must come from the registry."""
        from hpc_agent.recovery.registry import menu_for, remediation_for

        _seed_run(tmp_path, cluster)
        cluster.drop_combiner()

        with _patch_ssh(cluster), pytest.raises(errors.CombinerMissing) as exc:
            combine_wave(
                tmp_path,
                _RUN_ID,
                wave=0,
                ssh_target="user@hoffman2",
                remote_path=cluster.remote_path,
            )

        remediation = exc.value.remediation or ""
        assert remediation == remediation_for("combiner_missing")
        assert "hpc-agent redeploy-runtime" in remediation

        # And it must NOT be the ``combiner_failed`` menu it used to be routed
        # to. That menu's fix is "resubmit the offending tasks", which for a
        # missing artifact is wasted cluster time against an unchanged cause —
        # so no OPTION here may be a resubmit, and the redeploy must be rank 0.
        menu = menu_for("combiner_missing")
        assert min(menu.options, key=lambda o: o.safety_rank).cli_command.startswith(
            "hpc-agent redeploy-runtime"
        )
        assert not any("resubmit" in o.cli_command.lower() for o in menu.options)
        assert remediation != remediation_for("combiner_failed")

    def test_a_refused_combine_is_not_journaled_as_a_failed_wave(
        self, tmp_path: Path, cluster: FakeCluster
    ) -> None:
        """A wave that never ran is not a wave that failed.

        Recording it would re-enter the retry ladder and the ``combiner_failed``
        recovery menu — the two things that wasted the incident's evening.
        """
        _seed_run(tmp_path, cluster)
        cluster.drop_combiner()

        with _patch_ssh(cluster), pytest.raises(errors.CombinerMissing):
            combine_wave(
                tmp_path,
                _RUN_ID,
                wave=0,
                ssh_target="user@hoffman2",
                remote_path=cluster.remote_path,
            )

        record = load_run(tmp_path, _RUN_ID)
        assert record is not None
        assert record.failed_waves == []
        assert record.combined_waves == []

    def test_fused_batch_refuses_on_the_first_exec_not_after_n_fallbacks(
        self, tmp_path: Path, cluster: FakeCluster
    ) -> None:
        """The absence verdict must beat the truncation degrade.

        A guarded absent artifact emits no ``__HPC_BATCH_END__``, which is
        indistinguishable from a NAT truncation — so a naive ordering would
        degrade to one per-wave call PER WAVE before raising, paying N-1 cold
        round-trips for evidence it already had.
        """
        _seed_run(tmp_path, cluster)
        cluster.drop_combiner()

        with _patch_ssh(cluster), pytest.raises(errors.CombinerMissing):
            combine_waves(
                tmp_path,
                _RUN_ID,
                waves=[0, 1, 2, 3],
                ssh_target="user@hoffman2",
                remote_path=cluster.remote_path,
            )

        assert len(cluster.calls) == 1, f"expected one exec, got {len(cluster.calls)}"

    def test_final_reduce_refuses_with_the_cause_not_the_partials_message(
        self, cluster: FakeCluster
    ) -> None:
        """The aggregate path, item 3: same discriminated cause, no raw stderr."""
        from hpc_agent.infra.transport import run_final_reduce

        cluster.drop_combiner()

        with _patch_ssh(cluster), pytest.raises(errors.CombinerMissing) as exc:
            run_final_reduce(
                ssh_target="user@hoffman2",
                remote_path=cluster.remote_path,
                run_id=_RUN_ID,
                force=True,
            )

        msg = str(exc.value)
        assert D.COMBINER_REL in msg
        # The 20:52 line is precisely what must NOT be what the caller re-presents.
        assert "no _combiner/" not in msg
        assert "hpc-agent redeploy-runtime" in (exc.value.remediation or "")

    def test_a_healthy_cluster_is_completely_unaffected(
        self, tmp_path: Path, cluster: FakeCluster
    ) -> None:
        """The guard is transparent: combine, then final-reduce, both succeed."""
        from hpc_agent.infra.transport import run_final_reduce

        _seed_run(tmp_path, cluster)

        with _patch_ssh(cluster):
            ok, stdout, _stderr = combine_wave(
                tmp_path,
                _RUN_ID,
                wave=0,
                ssh_target="user@hoffman2",
                remote_path=cluster.remote_path,
            )
            assert ok is True, stdout
            final = run_final_reduce(
                ssh_target="user@hoffman2",
                remote_path=cluster.remote_path,
                run_id=_RUN_ID,
                force=True,
            )

        assert final.returncode == 0
        assert cluster.wave_partials(), "the real combine must still have run"
        agg = cluster.root / "_aggregated" / _RUN_ID / "metrics_aggregate.json"
        assert json.loads(agg.read_text(encoding="utf-8")) == {"aggregated_metrics": {}}

        record = load_run(tmp_path, _RUN_ID)
        assert record is not None
        assert record.combined_waves == [0]
        assert record.failed_waves == []

    def test_the_wave_combine_preflight_refuses_before_asking_about_data(
        self, cluster: FakeCluster
    ) -> None:
        """The early gate, folded into the preflight's own sidecar read.

        ``verify_per_task_outputs`` is the ``--require-outputs`` precondition
        that runs immediately before ``combine-wave``. Its first act is an ssh
        ``cat`` of the run sidecar; the artifact probe rides THAT exec, so the
        gate costs nothing and fires before a single per-task path is checked.
        """
        from hpc_agent.ops.aggregate import runner

        (cluster.root / ".hpc" / "runs").mkdir(parents=True, exist_ok=True)
        (cluster.root / ".hpc" / "runs" / f"{_RUN_ID}.json").write_text(
            json.dumps({"sidecar_schema_version": 1, "task_count": 2, "wave_map": {"0": [0, 1]}}),
            encoding="utf-8",
        )
        cluster.drop_combiner()

        with (
            patch("hpc_agent.infra.remote.ssh_run", side_effect=cluster.ssh_run),
            pytest.raises(errors.CombinerMissing) as exc,
        ):
            runner.verify_per_task_outputs(
                ssh_target="user@hoffman2",
                remote_path=cluster.remote_path,
                run_id=_RUN_ID,
                wave=0,
                template="results/metrics.{task_id}.json",
            )

        assert "hpc-agent redeploy-runtime" in (exc.value.remediation or "")
        # The per-task existence loop never ran: one exec, and it was the
        # sidecar read the preflight was making anyway.
        assert len(cluster.calls) == 1
        assert "MISSING:" not in cluster.calls[0]

    def test_the_folded_probe_does_not_swallow_a_failed_sidecar_read(
        self, cluster: FakeCluster
    ) -> None:
        """The probe always exits 0 — so it must not become the command's rc.

        ``cat <sidecar>; <probe>`` would hand back the PROBE's success and turn
        a missing sidecar into a JSON-parse error at best, a silent empty read
        at worst. The composed command saves the ``cat``'s status and re-raises
        it, so an unreadable sidecar stays loud.
        """
        from hpc_agent.ops.aggregate import runner

        # No sidecar written — the cat fails.
        with (
            patch("hpc_agent.infra.remote.ssh_run", side_effect=cluster.ssh_run),
            pytest.raises(errors.RemoteCommandFailed) as exc,
        ):
            runner.verify_per_task_outputs(
                ssh_target="user@hoffman2",
                remote_path=cluster.remote_path,
                run_id=_RUN_ID,
                wave=0,
                template="results/metrics.{task_id}.json",
            )

        assert "failed to read remote sidecar" in str(exc.value)
        assert cluster.calls[0].startswith("cat "), cluster.calls[0][:60]

    def test_fused_batch_still_works_on_a_healthy_cluster(
        self, tmp_path: Path, cluster: FakeCluster
    ) -> None:
        """One exec, every wave combined — the batching win is intact."""
        _seed_run(tmp_path, cluster)

        with _patch_ssh(cluster):
            results = combine_waves(
                tmp_path,
                _RUN_ID,
                waves=[0, 1, 2],
                ssh_target="user@hoffman2",
                remote_path=cluster.remote_path,
            )

        assert {w: ok for w, (ok, _o, _e) in results.items()} == {0: True, 1: True, 2: True}
        assert len(cluster.calls) == 1
        assert [p.name for p in cluster.wave_partials()] == [
            "wave_0.json",
            "wave_1.json",
            "wave_2.json",
        ]
