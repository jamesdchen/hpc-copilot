"""Golden equivalence tests for the profile-driven backend engine.

Agent A (TEST safety net) — these assert that the new profile-driven
``ProfileBackend`` reproduces, byte-for-byte / token-for-token, the
behaviour of the current ``SlurmBackend`` / ``SGEBackend`` as resolved
through ``get_backend(...)`` / ``get_backend_class(...)``.

The expected values below are HARD-CODED from the CURRENT source
(``src/hpc_agent/infra/backends/{slurm,sge}.py`` at the point this
suite was written) and were captured by instantiating the current
classes directly. They are the frozen contract the spine must hit.

NOTE ON TRANSPORT: ``get_backend("slurm")`` resolves to the REMOTE
subclass (``RemoteSlurmBackend``), whose default ``log_dir`` is
``<remote_repo>/logs``. To keep the expected token lists deterministic
we always pass an explicit ``log_dir`` and ``remote_repo``.
"""

from __future__ import annotations

import os
import shlex
from types import SimpleNamespace

import pytest

from hpc_agent import errors
from hpc_agent.infra.backends import get_backend, get_backend_class


def _noop_ssh(_cmd: str) -> SimpleNamespace:
    return SimpleNamespace(stdout="", stderr="", returncode=0)


def _slurm(**overrides):
    kw = dict(
        script="cpu.slurm",
        ssh_run=_noop_ssh,
        remote_repo="/repo",
        log_dir="logs",
        account="",
        cluster="",
    )
    kw.update(overrides)
    return get_backend("slurm", **kw)


def _sge(**overrides):
    kw = dict(
        script="cpu.sh",
        ssh_run=_noop_ssh,
        remote_repo="/repo",
        log_dir="logs",
        pass_env_keys=("K",),
    )
    kw.update(overrides)
    return get_backend("sge", **kw)


# ---------------------------------------------------------------------------
# _build_command — exact token lists
# ---------------------------------------------------------------------------


class TestBuildCommandSlurm:
    def test_basic_with_env_export(self):
        b = _slurm()
        assert b._build_command("1-10", "foo", {"K": "V"}) == [
            "sbatch",
            "--array",
            "1-10",
            "--job-name",
            "foo",
            "--output",
            "logs/%x_%A_%a.out",
            "--error",
            "logs/%x_%A_%a.err",
            "--export",
            "ALL,K=V",
            "cpu.slurm",
        ]

    def test_no_env_omits_export(self):
        b = _slurm()
        assert b._build_command("1-10", "foo", {}) == [
            "sbatch",
            "--array",
            "1-10",
            "--job-name",
            "foo",
            "--output",
            "logs/%x_%A_%a.out",
            "--error",
            "logs/%x_%A_%a.err",
            "cpu.slurm",
        ]

    def test_with_account(self):
        b = _slurm(account="my-acct")
        cmd = b._build_command("1-10", "foo", {})
        assert "--account" in cmd
        assert cmd[cmd.index("--account") + 1] == "my-acct"

    def test_with_cluster_single_token(self):
        b = _slurm(cluster="hoffman2")
        cmd = b._build_command("1-10", "foo", {})
        # --clusters is a single token: --clusters=<name>, placed right
        # after sbatch.
        assert cmd[:2] == ["sbatch", "--clusters=hoffman2"]

    def test_account_and_cluster_together(self):
        b = _slurm(account="acct", cluster="hoffman2")
        assert b._build_command("1-10", "foo", {}) == [
            "sbatch",
            "--clusters=hoffman2",
            "--array",
            "1-10",
            "--job-name",
            "foo",
            "--account",
            "acct",
            "--output",
            "logs/%x_%A_%a.out",
            "--error",
            "logs/%x_%A_%a.err",
            "cpu.slurm",
        ]

    def test_comma_in_env_value_raises_spec_invalid(self):
        b = _slurm()
        with pytest.raises(errors.SpecInvalid, match="','"):
            b._build_command("1-10", "foo", {"MODULES": "python/3.11,gcc/11"})

    def test_extra_flags_before_script(self):
        b = _slurm()
        cmd = b._build_command("1-10", "foo", {}, extra_flags=["--dependency", "afterany:1"])
        assert cmd[-1] == "cpu.slurm"
        assert cmd.index("--dependency") < cmd.index("cpu.slurm")


class TestBuildCommandSge:
    def test_basic_with_v_export(self):
        b = _sge()
        assert b._build_command("1-10", "foo", {"K": "V"}) == [
            "qsub",
            "-t",
            "1-10",
            "-N",
            "foo",
            "-o",
            "logs",
            "-j",
            "y",
            "-v",
            "K=V",
            "cpu.sh",
        ]

    def test_unlisted_env_keys_filtered(self):
        b = _sge(pass_env_keys=("FOO",))
        cmd = b._build_command("1-10", "foo", {"FOO": "1", "BAR": "2"})
        v = cmd[cmd.index("-v") + 1]
        assert "FOO=1" in v.split(",")
        assert all(not p.startswith("BAR=") for p in v.split(","))

    def test_empty_pass_env_keys_omits_v(self):
        b = _sge(pass_env_keys=())
        assert b._build_command("1-10", "foo", {"K": "V"}) == [
            "qsub",
            "-t",
            "1-10",
            "-N",
            "foo",
            "-o",
            "logs",
            "-j",
            "y",
            "cpu.sh",
        ]

    def test_comma_in_passed_env_value_raises_spec_invalid(self):
        b = _sge(pass_env_keys=("MODULES",))
        with pytest.raises(errors.SpecInvalid, match="','"):
            b._build_command("1-10", "foo", {"MODULES": "python/3.11,gcc/11"})

    def test_comma_in_unpassed_env_value_is_ignored(self):
        # The comma guard only fires for keys actually forwarded via -v.
        b = _sge(pass_env_keys=("K",))
        cmd = b._build_command("1-10", "foo", {"K": "V", "OTHER": "a,b"})
        assert cmd[cmd.index("-v") + 1] == "K=V"

    def test_extra_flags_before_script(self):
        b = _sge()
        cmd = b._build_command("1-10", "foo", {}, extra_flags=["-hold_jid", "1,2"])
        assert cmd[-1] == "cpu.sh"
        assert cmd.index("-hold_jid") < cmd.index("cpu.sh")


# ---------------------------------------------------------------------------
# dependency flags
# ---------------------------------------------------------------------------


class TestDependencyFlags:
    def test_slurm(self):
        b = _slurm()
        assert b._build_dependency_flag(["a", "b"]) == ["--dependency", "afterany:a:b"]
        assert b._build_dependency_flag([]) == []

    def test_sge(self):
        b = _sge()
        assert b._build_dependency_flag(["a", "b"]) == ["-hold_jid", "a,b"]
        assert b._build_dependency_flag([]) == []


# ---------------------------------------------------------------------------
# resource_flags
# ---------------------------------------------------------------------------


def _res(**kw):
    from hpc_agent._wire.workflows.submit_flow import SubmitResources

    return SubmitResources(**kw)


class TestResourceFlags:
    def test_slurm_none_and_empty(self):
        b = _slurm()
        assert b.resource_flags(None) == []
        assert b.resource_flags(_res()) == []

    def test_slurm_walltime_mem_cpus(self):
        b = _slurm()
        assert b.resource_flags(_res(walltime_sec=7200)) == ["--time", "120"]
        assert b.resource_flags(_res(walltime_sec=90)) == ["--time", "2"]  # ceil
        flags = b.resource_flags(_res(mem_mb=4096, cpus=8))
        assert flags[flags.index("--mem") + 1] == "4096M"
        assert flags[flags.index("--cpus-per-task") + 1] == "8"

    def test_sge_none_and_empty(self):
        b = _sge()
        assert b.resource_flags(None) == []
        assert b.resource_flags(_res()) == []

    def test_sge_walltime_mem_cpus(self, monkeypatch):
        b = _sge()
        assert b.resource_flags(_res(walltime_sec=7200)) == ["-l", "h_rt=02:00:00"]
        assert b.resource_flags(_res(walltime_sec=90061)) == ["-l", "h_rt=25:01:01"]
        # run-14: h_data is PER-SLOT + vmem-enforced — mem_mb (per-task total) is
        # divided across -pe slots and grown by the disclosed vmem headroom
        # (factor pinned for determinism): ceil(8192 * 2.0 / 4) = 4096M per slot.
        monkeypatch.setenv("HPC_SGE_VMEM_FACTOR", "2")
        flags = b.resource_flags(_res(mem_mb=8192, cpus=4))
        assert "h_data=4096M" in flags
        assert flags[flags.index("-pe") : flags.index("-pe") + 3] == ["-pe", "shared", "4"]


# ---------------------------------------------------------------------------
# classmethods read off cls.profile — alive / state cmds + parsers
# ---------------------------------------------------------------------------


class TestAliveAndStateCommands:
    # Sentinel-ack (positive-evidence rule, docs/design/connection-broker.md):
    # each query ends by echoing ``__HPC_SCHED_ACK__=$?`` instead of ``|| true``,
    # so a silent/truncated read (no ack) is UNKNOWN rather than "no jobs".
    _ACK = '; echo "__HPC_SCHED_ACK__=$?"'

    @staticmethod
    def _login_inner(out: str) -> str:
        """Assert *out* is a ``bash -lc <inner>`` login-shell command; return <inner>.

        Every remote scheduler-query builder wraps its command in a
        NON-interactive LOGIN shell (``bash -lc``), so the scheduler binary
        (qstat/squeue/sacct) resolves on ``PATH``: Hoffman2/UGE et al. install it
        onto PATH only via the login profile chain, and ssh_run's transport uses a
        non-login ``bash -c`` (the reconcile ``unable_to_verify`` incident,
        2026-07-17 — a bare ``qstat -u "$USER"`` was rc 127 on hoffman2's non-login
        shell). This mirrors the SUBMIT leg's idiom EXACTLY
        (``infra/backends/_remote_base.py::_execute_command`` — ``bash -lc
        {shlex.quote(inner)}`` with the marker echo INSIDE the login shell); it is
        ``-lc`` NOT ``-lic`` (interactive init hangs a PTY-less exec channel,
        proving-run #2). ``shlex.split`` reverses ``shlex.quote``, so the third
        token is the exact ack-suffixed inner the login shell runs.
        """
        parts = shlex.split(out)
        assert parts[:2] == ["bash", "-lc"], f"not a login-shell command: {out!r}"
        assert len(parts) == 3, f"expected `bash -lc <inner>`, got {out!r}"
        return parts[2]

    def test_slurm_build_alive_check_cmd(self):
        cls = get_backend_class("slurm")
        out = cls.build_alive_check_cmd(["1", "2"])
        assert out.startswith("bash -lc ")  # login-shell contract (see _login_inner)
        inner = self._login_inner(out)
        assert inner == "squeue -j 1,2 -h -o '%i' 2>/dev/null" + self._ACK
        assert self._ACK in inner  # ack rides INSIDE the quoted login-shell inner
        assert cls.build_alive_check_cmd([]) == "true"  # empty short-circuit no-op

    def test_sge_build_alive_check_cmd(self):
        cls = get_backend_class("sge")
        out = cls.build_alive_check_cmd(["1", "2"])
        assert out.startswith("bash -lc ")
        inner = self._login_inner(out)
        assert inner == 'qstat -u "$USER" 2>/dev/null' + self._ACK
        assert self._ACK in inner
        assert cls.build_alive_check_cmd([]) == "true"

    def test_slurm_build_scheduler_state_cmd(self):
        cls = get_backend_class("slurm")
        out = cls.build_scheduler_state_cmd(["1"])
        assert out.startswith("bash -lc ")
        inner = self._login_inner(out)
        assert inner == "squeue -j 1 -h -o '%i %T' 2>/dev/null" + self._ACK
        assert self._ACK in inner
        assert cls.build_scheduler_state_cmd([]) == "true"

    def test_sge_build_scheduler_state_cmd(self):
        cls = get_backend_class("sge")
        out = cls.build_scheduler_state_cmd(["1"])
        assert out.startswith("bash -lc ")
        inner = self._login_inner(out)
        assert inner == 'qstat -u "$USER" 2>/dev/null' + self._ACK
        assert self._ACK in inner
        assert cls.build_scheduler_state_cmd([]) == "true"

    def test_every_scheduler_query_builder_is_login_shell_wrapped(self):
        """The login-shell contract, uniform across families and query builders.

        Every non-empty ``build_alive_check_cmd`` / ``build_scheduler_state_cmd`` /
        ``build_token_query_cmd`` output is a ``bash -lc <inner>`` command whose
        <inner> carries the sentinel-ack token — so the scheduler binary resolves
        on a non-login ssh channel AND the ack rides inside the login shell (a
        PATH-less login shell that can't run the query still emits no ack, never a
        spurious rc-0 empty read). Mirrors the submit-leg precedent
        (``_remote_base.py::_execute_command``)."""
        for name in ("slurm", "sge", "pbspro", "torque"):
            cls = get_backend_class(name)
            outs = [
                cls.build_alive_check_cmd(["1", "2"]),
                cls.build_scheduler_state_cmd(["1", "2"]),
                cls.build_token_query_cmd(),
            ]
            for out in outs:
                assert out.startswith("bash -lc "), f"{name}: not login-wrapped: {out!r}"
                inner = self._login_inner(out)
                assert "__HPC_SCHED_ACK__=" in inner, (
                    f"{name}: ack not inside the login-shell inner: {out!r}"
                )

    def test_slurm_build_cancel_cmd(self):
        cls = get_backend_class("slurm")
        out = cls.build_cancel_cmd(["1", "2"])
        # Login-shell wrapped so scancel resolves on PATH (ssh_run's non-login
        # transport is rc 127 for a bare scancel) — but NO ack: cancel's success
        # is confirmed by the follow-up alive-check, not its own exit code, so it
        # is the PLAIN login wrap, not the ack-bearing query wrap.
        assert out.startswith("bash -lc ")
        assert self._login_inner(out) == "scancel 1 2"
        assert self._ACK not in out  # cancel carries no sentinel-ack
        # Empty ids short-circuit to a bare ``true`` no-op — matching the
        # alive/state builders — with no login shell.
        assert cls.build_cancel_cmd([]) == "true"

    def test_sge_build_cancel_cmd(self):
        cls = get_backend_class("sge")
        out = cls.build_cancel_cmd(["1", "2"])
        assert out.startswith("bash -lc ")
        assert self._login_inner(out) == "qdel 1 2"
        assert self._ACK not in out
        assert cls.build_cancel_cmd([]) == "true"


class TestParseAliveOutput:
    def test_slurm_strips_array_and_dot_suffixes(self):
        cls = get_backend_class("slurm")
        out = "12345_1\n12345_2\n99\n"
        assert cls.parse_alive_output(out, ["12345", "99"]) == {"12345", "99"}

    def test_sge_skips_header_rows(self):
        cls = get_backend_class("sge")
        out = (
            "job-ID prior name user state\n"
            "-----------------------------\n"
            "12345 0.5 foo user r 05/30\n"
            "99 0.5 bar user qw 05/30\n"
        )
        assert cls.parse_alive_output(out, ["12345", "99"]) == {"12345", "99"}


class TestParseSchedulerStates:
    def test_slurm(self):
        cls = get_backend_class("slurm")
        out = "12345 RUNNING\n99 PENDING\nheaderless\n"
        assert cls.parse_scheduler_states(out, ["12345", "99"]) == {
            "12345": "RUNNING",
            "99": "PENDING",
        }

    def test_sge(self):
        cls = get_backend_class("sge")
        out = (
            "job-ID prior name user state submit/start at\n"
            "------------------------------------------\n"
            "12345 0.5 foo user Eqw 05/30 1\n"
            "99 0.5 bar user r 05/30 1\n"
        )
        assert cls.parse_scheduler_states(out, ["12345", "99"]) == {
            "12345": "Eqw",
            "99": "r",
        }


# ---------------------------------------------------------------------------
# classify_scheduler_state — full token table
# ---------------------------------------------------------------------------

_SLURM_CLASSIFY = [
    ("RUNNING", "alive"),
    ("PENDING", "alive"),
    ("COMPLETING", "alive"),
    ("FAILED", "error"),
    ("CANCELLED", "error"),
    ("TIMEOUT", "error"),
    ("OUT_OF_MEMORY", "error"),
    ("NODE_FAIL", "error"),
    ("BOOT_FAIL", "error"),
    ("DEADLINE", "error"),
    ("PREEMPTED", "error"),
    ("REVOKED", "error"),
    ("SPECIAL_EXIT", "held"),
    ("SOMETHING_HOLD", "held"),
    ("SUSPENDED", "held"),  # not progressing -> held (matches slurm-drmaa)
    ("STOPPED", "held"),
    ("CANCELLED by 100123", "error"),  # sacct trailing-text form -> match leading token
    ("UNKNOWN_STATE", "alive"),  # conservative default
]

_SGE_CLASSIFY = [
    ("r", "alive"),
    ("qw", "alive"),
    ("t", "alive"),
    ("Rr", "alive"),
    ("dr", "alive"),
    ("Eqw", "error"),
    ("Er", "error"),
    ("hqw", "held"),
    ("hRwq", "held"),
]


@pytest.mark.parametrize(("state", "bucket"), _SLURM_CLASSIFY)
def test_slurm_classify(state, bucket):
    assert get_backend_class("slurm").classify_scheduler_state(state) == bucket


@pytest.mark.parametrize(("state", "bucket"), _SGE_CLASSIFY)
def test_sge_classify(state, bucket):
    assert get_backend_class("sge").classify_scheduler_state(state) == bucket


# ---------------------------------------------------------------------------
# batch_status — raw scheduler token -> TaskStatus (#2 connection-storm fix)
# ---------------------------------------------------------------------------

# Finer than classify_scheduler_state's alive/error/held: a live queue token
# splits into pending (queued/held — waiting) vs running (executing); an error
# token maps to failed. A finished job leaves the live queue, so 'complete'
# is never emitted (the caller infers it from absence).
_SLURM_BATCH = [
    ("RUNNING", "running"),
    ("COMPLETING", "running"),
    ("CONFIGURING", "running"),
    ("PENDING", "pending"),
    ("SUSPENDED", "pending"),  # held -> waiting, not executing
    ("FAILED", "failed"),
    ("TIMEOUT", "failed"),
    ("PREEMPTED", "failed"),
    ("CANCELLED by 100123", "failed"),
]
_SGE_BATCH = [
    ("r", "running"),
    ("t", "running"),
    ("dr", "running"),
    ("Rr", "running"),
    ("qw", "pending"),
    ("hqw", "pending"),  # held -> pending
    ("Eqw", "failed"),
    ("Er", "failed"),
]
_PBS_BATCH = [
    ("R", "running"),
    ("E", "running"),  # exiting -> still progressing
    ("B", "running"),
    ("Q", "pending"),
    ("W", "pending"),
    ("H", "pending"),  # held -> pending
    ("S", "pending"),  # suspended -> pending
]


@pytest.mark.parametrize(("state", "status"), _SLURM_BATCH)
def test_slurm_batch_status(state, status):
    assert get_backend_class("slurm").batch_status({"1": state}) == {"1": status}


@pytest.mark.parametrize(("state", "status"), _SGE_BATCH)
def test_sge_batch_status(state, status):
    assert get_backend_class("sge").batch_status({"1": state}) == {"1": status}


@pytest.mark.parametrize(("state", "status"), _PBS_BATCH)
def test_pbs_batch_status(state, status):
    assert get_backend_class("pbspro").batch_status({"1": state}) == {"1": status}


def test_batch_status_bulk_and_empty():
    cls = get_backend_class("slurm")
    assert cls.batch_status({}) == {}
    assert cls.batch_status({"1": "RUNNING", "2": "PENDING", "3": "FAILED"}) == {
        "1": "running",
        "2": "pending",
        "3": "failed",
    }


# ---------------------------------------------------------------------------
# log paths
# ---------------------------------------------------------------------------


class TestLogPaths:
    def test_slurm_stderr_log_path_index_plus_one(self):
        cls = get_backend_class("slurm")
        assert cls.stderr_log_path("/r/path/", "foo", "99", 0) == "/r/path/logs/foo_99_1.err"

    def test_sge_stderr_log_path_index_plus_one(self):
        cls = get_backend_class("sge")
        assert cls.stderr_log_path("/r/path/", "foo", "99", 0) == "/r/path/logs/foo.o99.1"

    def test_slurm_err_log_disk_path(self):
        cls = get_backend_class("slurm")
        # err_log_disk_path is a LOCAL-disk path (os.path.join), so compare
        # against os.path.join rather than a hardcoded '/' — on Windows the
        # separator is '\\' and a literal '/' would spuriously fail (the
        # cluster-side stderr_log_path stays POSIX, tested separately).
        assert cls.err_log_disk_path("logs", "scratch", "foo", "99", 3) == os.path.join(
            "logs", "foo_99_3.err"
        )

    def test_sge_err_log_disk_path_uses_scratch(self):
        cls = get_backend_class("sge")
        assert cls.err_log_disk_path("logs", "scratch", "foo", "99", 3) == os.path.join(
            "scratch", "foo.o99.3"
        )


# ---------------------------------------------------------------------------
# class metadata / profile fields
# ---------------------------------------------------------------------------


class TestMetadata:
    def test_slurm(self):
        cls = get_backend_class("slurm")
        assert cls.scheduler_name == "slurm"
        assert cls.template_ext == ".slurm"
        assert cls.supports_test_only_eta is True
        assert cls.JOB_ID_REGEX.search("Submitted batch job 12345").group(1) == "12345"
        # warning prefix must not poison the parse
        assert (
            cls.JOB_ID_REGEX.search(
                "sbatch: warning: 30% pre-empt; Submitted batch job 12345"
            ).group(1)
            == "12345"
        )

    def test_sge(self):
        cls = get_backend_class("sge")
        assert cls.scheduler_name == "sge"
        assert cls.template_ext == ".sh"
        assert cls.supports_test_only_eta is False
        assert cls.JOB_ID_REGEX.search('Your job 777 ("n") has been submitted').group(1) == "777"
        assert (
            cls.JOB_ID_REGEX.search(
                'Your job-array 12345.1-10:1 ("name") has been submitted'
            ).group(1)
            == "12345"
        )


# ---------------------------------------------------------------------------
# FROZEN-CONTRACT tests against the new profile module (may xfail until the
# spine lands the engine — they encode the exact target).
# ---------------------------------------------------------------------------


class TestProfileModuleContract:
    """These import the NEW modules the spine is building. Until integration
    they will ERROR on import — that is expected; they pin the contract.
    """

    def test_profile_fields(self):
        from hpc_agent.infra.backends.profile import SGE_PROFILE, SLURM_PROFILE

        assert SLURM_PROFILE.name and SLURM_PROFILE.family == "slurm"
        assert SLURM_PROFILE.submit_bin == "sbatch"
        assert SLURM_PROFILE.template_ext == ".slurm"
        assert SLURM_PROFILE.supports_test_only_eta is True
        assert {
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
            "OUT_OF_MEMORY",
            "NODE_FAIL",
            "PREEMPTED",
        }.issubset(SLURM_PROFILE.error_states)

        assert SGE_PROFILE.family == "sge"
        assert SGE_PROFILE.submit_bin == "qsub"
        assert SGE_PROFILE.template_ext == ".sh"
        assert SGE_PROFILE.supports_test_only_eta is False

    def test_profile_is_frozen(self):
        import dataclasses

        from hpc_agent.infra.backends.profile import SLURM_PROFILE

        with pytest.raises(dataclasses.FrozenInstanceError):
            SLURM_PROFILE.name = "x"  # type: ignore[misc]

    def test_profile_backend_carries_profile(self):
        # The base engine declares the ``profile`` slot (annotation only);
        # concrete subclasses bind an actual SchedulerProfile, from which
        # __init_subclass__ derives the capability metadata.
        from hpc_agent.infra.backends import ProfileBackend
        from hpc_agent.infra.backends.profile import SLURM_PROFILE
        from hpc_agent.infra.backends.slurm import SlurmBackend

        assert "profile" in ProfileBackend.__annotations__
        assert SlurmBackend.profile is SLURM_PROFILE
        assert SlurmBackend.scheduler_name == "slurm"
        assert SlurmBackend.template_ext == ".slurm"
