"""Profile-driven scheduler engine.

:class:`ProfileBackend` is the single submission engine that the old
``SlurmBackend`` / ``SGEBackend`` collapse into. It carries no
scheduler literals of its own: every value it needs (submit binary,
job-id regex, template extension, error vocabulary, script bodies)
comes from its :class:`~hpc_agent.infra.backends.profile.SchedulerProfile`.
``profile.family`` selects the structural flag grammar for the handful
of operations whose *shape* (not just literals) differs between
scheduler families — command assembly, the alive/state query, and the
log-path layout.

Concrete backends bind a profile as a class attribute:

    class SlurmBackend(ProfileBackend):
        profile = SLURM_PROFILE

``__init_subclass__`` then derives the class-level capability metadata
(``scheduler_name`` / ``template_ext`` / ``supports_test_only_eta`` /
``JOB_ID_REGEX``) from that profile, so the historical
``get_backend_class(name).<attr>`` access pattern keeps working
unchanged. The B5-PR2 capability hooks are ``classmethod``\\s (they
read ``cls.profile``) so callers can still invoke them off the class.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import TYPE_CHECKING, Any

from hpc_agent import errors
from hpc_agent._kernel.contract.task_id import HpcTaskId, to_array_index
from hpc_agent.infra.backends import _TASK_OFFSET_ENV, HPCBackend
from hpc_agent.infra.backends.profile import SchedulerProfile
from hpc_agent.infra.backends.profile import render_script as _render_script

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable


def _fmt_hms(total_seconds: int) -> str:
    """Format *total_seconds* as ``HH:MM:SS`` for SGE ``-l h_rt``.

    Hours are not zero-padded beyond two digits (SGE accepts >99h), so a
    multi-day walltime still renders correctly.
    """
    seconds = max(0, int(total_seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# Per-family walltime / memory flag emitters. Shared by the single-node
# ``resource_flags`` path and the MPI path (#293) so the two never drift on
# how a walltime or a memory ask is spelled for a given scheduler.
def _slurm_time_flags(walltime_sec: int | None) -> list[str]:
    if not walltime_sec:
        return []
    minutes = -(-int(walltime_sec) // 60)  # ceil division
    return ["--time", str(minutes)]


def _slurm_mem_flags(mem_mb: int | None) -> list[str]:
    return ["--mem", f"{int(mem_mb)}M"] if mem_mb else []


def _sge_time_flags(walltime_sec: int | None) -> list[str]:
    return ["-l", f"h_rt={_fmt_hms(int(walltime_sec))}"] if walltime_sec else []


def _sge_mem_flags(mem_mb: int | None) -> list[str]:
    return ["-l", f"h_data={int(mem_mb)}M"] if mem_mb else []


class ProfileBackend(HPCBackend):
    """Scheduler-agnostic submission engine parameterised by a profile."""

    # Concrete subclasses set this; ``__init_subclass__`` derives the rest.
    profile: SchedulerProfile

    # Instance attributes populated by concrete subclasses' ``__init__`` (or
    # by ``build_backend_class``); declared here so the family-shaped command
    # builders type-check. ``log_dir`` is already declared on the base.
    script: str
    cluster: str
    account: str

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        prof = cls.__dict__.get("profile")
        if prof is not None:
            cls.scheduler_name = prof.name
            cls.template_ext = prof.template_ext
            cls.supports_test_only_eta = prof.supports_test_only_eta
            cls.JOB_ID_REGEX = re.compile(prof.job_id_regex)

    # ------------------------------------------------------------------
    # Script rendering (Phase 2 / Option C)
    # ------------------------------------------------------------------

    @classmethod
    def render_script(cls, *, kind: str, **_opts: Any) -> str:
        """Return the runtime array-job script body for *kind* (cpu/gpu)."""
        return _render_script(cls.profile, kind=kind)

    # ------------------------------------------------------------------
    # Dependency flag + resource flags (instance — family-shaped)
    # ------------------------------------------------------------------

    def _build_dependency_flag(self, job_ids: list[str]) -> list[str]:
        if not job_ids:
            return []
        if self.profile.family == "slurm":
            return ["--dependency", f"afterany:{':'.join(job_ids)}"]
        if self.profile.family in ("pbspro", "torque"):
            # PBS dependency: -W depend=afterany:<id>:<id>
            return ["-W", f"depend=afterany:{':'.join(job_ids)}"]
        # sge
        return ["-hold_jid", ",".join(job_ids)]

    @property
    def supports_afterok(self) -> bool:
        """Whether this scheduler expresses an afterok (success-only) dependency (#250).

        SLURM and the PBS family do; SGE's ``-hold_jid`` only waits for the job to
        *end* (any exit), so it cannot gate on success and is treated as
        unsupported (the caller falls back to the un-gated co-submission).
        """
        return self.profile.family in ("slurm", "pbspro", "torque")

    def _build_afterok_dependency_flag(self, job_ids: list[str]) -> list[str]:
        """Scheduler flags making this job depend on *job_ids* SUCCEEDING (#250).

        Distinct from :meth:`_build_dependency_flag` (afterany, which only waits
        for the dependency to *terminate*): afterok additionally DROPS the
        dependent job when the dependency fails, so a canary failure means the
        main array never runs — enforced by the scheduler, no orchestrator
        round-trip.

        * SLURM: ``--dependency afterok:<id> --kill-on-invalid-dep=yes`` — the
          second flag removes the held main job when the canary fails (else it
          would sit queued forever waiting on a dependency that can't satisfy).
        * PBS Pro / TORQUE: ``-W depend=afterok:<id>`` — the scheduler drops the
          dependent job on a non-zero dependency exit.
        * SGE / unknown: ``[]`` — no native afterok (see :attr:`supports_afterok`);
          the caller must not rely on a gate it didn't get.
        """
        if not job_ids:
            return []
        if self.profile.family == "slurm":
            return [
                "--dependency",
                f"afterok:{':'.join(job_ids)}",
                "--kill-on-invalid-dep=yes",
            ]
        if self.profile.family in ("pbspro", "torque"):
            return ["-W", f"depend=afterok:{':'.join(job_ids)}"]
        return []

    def _build_wave_dependency_flag(
        self, *, afterok_ids: list[str], afterany_ids: list[str]
    ) -> list[str]:
        """One combined dependency flag gating on success AND/OR completion (#339).

        The wave submitter needs a *single* dependency expression per wave: an
        over-cap wave may have to both success-gate on the canary (``afterok``,
        so a canary failure drops every wave) and completion-gate on the prior
        wave (``afterany``, the concurrency chain that must NOT drop later waves
        when one task fails). SLURM/PBS accept only one ``--dependency`` /
        ``-W depend=`` flag, so the two conditions are ANDed into one
        comma-separated expression here rather than emitted as two flags (the
        second of which would clobber the first).

        * SLURM: ``--dependency afterok:<c>,afterany:<p> --kill-on-invalid-dep=yes``
          (the kill flag only when an afterok condition is present).
        * PBS Pro / TORQUE: ``-W depend=afterok:<c>,afterany:<p>``.
        * SGE: ``-hold_jid`` is completion-only and cannot express afterok, so
          both id sets collapse to a single hold list (a canary gate is not
          enforceable here — matching :attr:`supports_afterok` = False).
        """
        if not afterok_ids and not afterany_ids:
            return []
        fam = self.profile.family
        if fam == "slurm":
            conds: list[str] = []
            if afterok_ids:
                conds.append(f"afterok:{':'.join(afterok_ids)}")
            if afterany_ids:
                conds.append(f"afterany:{':'.join(afterany_ids)}")
            flags = ["--dependency", ",".join(conds)]
            if afterok_ids:
                flags.append("--kill-on-invalid-dep=yes")
            return flags
        if fam in ("pbspro", "torque"):
            conds = []
            if afterok_ids:
                conds.append(f"afterok:{':'.join(afterok_ids)}")
            if afterany_ids:
                conds.append(f"afterany:{':'.join(afterany_ids)}")
            return ["-W", f"depend={','.join(conds)}"]
        # sge: completion-only hold on the union (no native afterok).
        return ["-hold_jid", ",".join(afterok_ids + afterany_ids)]

    def resource_flags(self, resources: object) -> list[str]:
        """Translate a resources object into scheduler command-line flags.

        Opt-in per field — an empty / ``None`` ``resources`` emits nothing,
        so the template directives and cluster defaults stay in force.
        """
        flags: list[str] = []
        if resources is None:
            return flags
        walltime_sec = getattr(resources, "walltime_sec", None)
        mem_mb = getattr(resources, "mem_mb", None)
        cpus = getattr(resources, "cpus", None)
        # #293: a multi-rank job sizes from the MPI block (ranks / topology),
        # not the per-task cpus axis. The MPI emitter reuses the same
        # walltime/mem helpers so only the slot grammar differs.
        mpi = getattr(resources, "mpi", None)
        if mpi is not None:
            return self._mpi_resource_flags(mpi, walltime_sec=walltime_sec, mem_mb=mem_mb)
        if self.profile.family == "slurm":
            flags += _slurm_time_flags(walltime_sec)
            flags += _slurm_mem_flags(mem_mb)
            if cpus:
                flags += ["--cpus-per-task", str(int(cpus))]
        elif self.profile.family == "pbspro":
            # PBS Pro: chunk syntax ``-l select=1:ncpus=N:mem=Mmb`` + a
            # separate ``-l walltime=`` (walltime is job-wide, not in select).
            if cpus or mem_mb:
                sel = "select=1"
                if cpus:
                    sel += f":ncpus={int(cpus)}"
                if mem_mb:
                    sel += f":mem={int(mem_mb)}mb"
                flags += ["-l", sel]
            if walltime_sec:
                flags += ["-l", f"walltime={_fmt_hms(int(walltime_sec))}"]
        elif self.profile.family == "torque":
            # TORQUE: ``-l nodes=1:ppn=N,mem=Mmb,walltime=HH:MM:SS`` (one
            # comma-joined resource list).
            parts: list[str] = []
            parts.append(f"nodes=1:ppn={int(cpus)}" if cpus else "nodes=1")
            if mem_mb:
                parts.append(f"mem={int(mem_mb)}mb")
            if walltime_sec:
                parts.append(f"walltime={_fmt_hms(int(walltime_sec))}")
            if cpus or mem_mb or walltime_sec:
                flags += ["-l", ",".join(parts)]
        else:  # sge
            flags += _sge_time_flags(walltime_sec)
            flags += _sge_mem_flags(mem_mb)
            if cpus:
                flags += ["-pe", "shared", str(int(cpus))]
        return flags

    def _mpi_resource_flags(
        self, mpi: Any, *, walltime_sec: int | None, mem_mb: int | None
    ) -> list[str]:
        """Scheduler flags for a multi-rank job (#293).

        *mpi* is a ``SubmitResources.MpiSpec`` (or any object exposing
        ``ranks`` / ``ranks_per_node`` / ``threads_per_rank`` / ``pe_name``).
        ``ranks_per_node`` is guaranteed by the wire validator to divide
        ``ranks`` evenly, so ``nodes`` is integral when it is set; left null
        the scheduler packs ranks and the node-pinning flags are omitted.

        Walltime / memory reuse the same family helpers as the single-node
        path — only the *slot* grammar (how N ranks across M nodes are
        requested) is MPI-specific.
        """
        ranks = int(mpi.ranks)
        rpn_raw = getattr(mpi, "ranks_per_node", None)
        rpn = int(rpn_raw) if rpn_raw else None
        threads = int(getattr(mpi, "threads_per_rank", 1) or 1)
        nodes = ranks // rpn if rpn else None
        flags: list[str] = []
        fam = self.profile.family
        if fam == "slurm":
            if nodes:
                flags += ["--nodes", str(nodes)]
            flags += ["--ntasks", str(ranks)]
            if rpn:
                flags += ["--ntasks-per-node", str(rpn)]
            if threads > 1:
                flags += ["--cpus-per-task", str(threads)]
            flags += _slurm_time_flags(walltime_sec)
            flags += _slurm_mem_flags(mem_mb)
        elif fam in ("pbspro", "torque"):
            # PBS chunk: N nodes × (ranks_per_node procs × threads cpus each).
            # When ranks_per_node is null, fall back to a single chunk holding
            # every rank (mpiprocs=ranks) — the scheduler then places them.
            chunk_nodes = nodes or 1
            procs = rpn if rpn else ranks
            ncpus = procs * threads
            sel = f"select={chunk_nodes}:ncpus={ncpus}:mpiprocs={procs}"
            if threads > 1:
                sel += f":ompthreads={threads}"
            if mem_mb:
                sel += f":mem={int(mem_mb)}mb"
            flags += ["-l", sel]
            if walltime_sec:
                flags += ["-l", f"walltime={_fmt_hms(int(walltime_sec))}"]
        else:  # sge
            # SGE routes multi-rank work through a parallel environment. The
            # wire guard (build_submit_spec) guarantees pe_name is present for
            # sge+mpi, so a missing one here is a non-sge-path caller; emit no
            # slot request rather than a malformed ``-pe`` with no name.
            pe_name = getattr(mpi, "pe_name", None)
            if pe_name:
                flags += ["-pe", str(pe_name), str(ranks)]
            flags += _sge_time_flags(walltime_sec)
            flags += _sge_mem_flags(mem_mb)
        return flags

    # ------------------------------------------------------------------
    # Submit command (instance — family-shaped)
    # ------------------------------------------------------------------

    def _build_command(
        self,
        task_range: str | None,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
        array: bool = True,
    ) -> list[str]:
        """Assemble the submit command.

        *array* defaults True (the fan-out shape: one array of ``task_range``
        elements). A single multi-rank MPI job (#293) is submitted with
        ``array=False`` and ``task_range=None`` — one job whose internal
        parallelism is the rank count, not a scheduler array.
        """
        if self.profile.family == "slurm":
            return self._build_slurm_command(
                task_range, job_name, job_env, extra_flags=extra_flags, array=array
            )
        if self.profile.family in ("pbspro", "torque"):
            return self._build_pbs_command(
                task_range, job_name, job_env, extra_flags=extra_flags, array=array
            )
        return self._build_sge_command(
            task_range, job_name, job_env, extra_flags=extra_flags, array=array
        )

    def _build_pbs_command(
        self,
        task_range: str | None,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
        array: bool = True,
    ) -> list[str]:
        # PBS Pro array flag is ``-J``; TORQUE uses ``-t`` (like SGE). Streams
        # joined with ``-j oe`` (PBS) cf. SGE's ``-j y``. Otherwise the qsub
        # shape + the ``-v`` comma hazard mirror the SGE branch.
        array_flag = "-J" if self.profile.family == "pbspro" else "-t"
        cmd = [self.profile.submit_bin]
        if array:
            cmd += [array_flag, str(task_range)]
        cmd += [
            "-N",
            job_name,
            "-o",
            self.log_dir,
            "-j",
            "oe",
        ]
        pass_env_keys = getattr(self, "pass_env_keys", ())
        # TASK_OFFSET is a framework-internal var (the per-wave global offset the
        # array template recovers the task id from, #339); transport it whenever
        # present regardless of the user's pass_env_keys allowlist, so a wave
        # submission doesn't depend on the caller having allow-listed it.
        passes = lambda k: k in pass_env_keys or k == _TASK_OFFSET_ENV  # noqa: E731
        bad = [k for k, v in job_env.items() if passes(k) and "," in str(v)]
        if bad:
            raise errors.SpecInvalid(
                "PBS qsub -v cannot transport env values containing "
                f"','; offending keys: {sorted(bad)}. Pre-encode "
                "(base64, space-delimited list, etc.) before submission."
            )
        pass_vars = ",".join(f"{k}={v}" for k, v in job_env.items() if passes(k))
        if pass_vars:
            cmd += ["-v", pass_vars]
        if extra_flags:
            cmd += extra_flags
        cmd.append(self.script)
        return cmd

    def _build_slurm_command(
        self,
        task_range: str | None,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
        array: bool = True,
    ) -> list[str]:
        cmd = [self.profile.submit_bin]
        if getattr(self, "cluster", ""):
            cmd.append(f"--clusters={self.cluster}")
        if array:
            cmd += ["--array", str(task_range)]
        cmd += ["--job-name", job_name]
        if getattr(self, "account", ""):
            cmd += ["--account", self.account]
        # Array jobs interpolate %A (array job id) + %a (array index). A single
        # MPI job (#293) is task 0, so it pins %j (job id) + the literal ``_1``
        # — task 0's 1-based ArrayIndex — so the on-disk name MATCHES what the
        # diagnostic layer resolves via ``stderr_log_path(task_id=0)`` /
        # ``err_log_disk_path``. Without the ``_1`` the canary + status log
        # fetch would look for ``<name>_<jobid>_1.err`` and miss the real log,
        # silently blanking MPI failure classification.
        log_pattern = "%x_%A_%a" if array else "%x_%j_1"
        cmd += [
            "--output",
            f"{self.log_dir}/{log_pattern}.out",
            "--error",
            f"{self.log_dir}/{log_pattern}.err",
        ]
        if job_env:
            # --export uses comma to separate K=V pairs, so a value with a
            # comma silently splits into malformed pairs on the scheduler
            # side. Reject up front rather than silently corrupt the env.
            bad = [k for k, v in job_env.items() if "," in str(v)]
            if bad:
                raise errors.SpecInvalid(
                    "SLURM --export cannot transport env values containing "
                    f"','; offending keys: {sorted(bad)}. Pre-encode "
                    "(base64, space-delimited list, etc.) before submission."
                )
            export_str = ",".join(f"{k}={v}" for k, v in job_env.items())
            cmd += ["--export", f"ALL,{export_str}"]
        if extra_flags:
            cmd += extra_flags
        cmd.append(self.script)
        return cmd

    def _build_sge_command(
        self,
        task_range: str | None,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
        array: bool = True,
    ) -> list[str]:
        cmd = [self.profile.submit_bin]
        if array:
            cmd += ["-t", str(task_range)]
        cmd += [
            "-N",
            job_name,
            "-o",
            self.log_dir,
            "-j",
            "y",
        ]
        pass_env_keys = getattr(self, "pass_env_keys", ())
        # qsub -v uses comma to separate K=V pairs (same hazard as SLURM's
        # --export). Reject comma-bearing values up front. TASK_OFFSET is a
        # framework-internal var (the per-wave global offset, #339) transported
        # regardless of the user's pass_env_keys allowlist.
        passes = lambda k: k in pass_env_keys or k == _TASK_OFFSET_ENV  # noqa: E731
        bad = [k for k, v in job_env.items() if passes(k) and "," in str(v)]
        if bad:
            raise errors.SpecInvalid(
                "SGE qsub -v cannot transport env values containing "
                f"','; offending keys: {sorted(bad)}. Pre-encode "
                "(base64, space-delimited list, etc.) before submission."
            )
        pass_vars = ",".join(f"{k}={v}" for k, v in job_env.items() if passes(k))
        if pass_vars:
            cmd += ["-v", pass_vars]
        if extra_flags:
            cmd += extra_flags
        cmd.append(self.script)
        return cmd

    # ------------------------------------------------------------------
    # B5-PR2 capability hooks — classmethods reading ``cls.profile`` so
    # callers can invoke them off the class.
    # ------------------------------------------------------------------

    @classmethod
    def build_alive_check_cmd(cls, job_ids: list[str]) -> str:
        """Shell command whose stdout lists the live job ids."""
        if not job_ids:
            return "true"
        if cls.profile.family == "slurm":
            # squeue (active states only) so completed/failed jobs don't
            # leak history and make abandoned-run detection useless.
            csv = ",".join(job_ids)
            return f"squeue -j {shlex.quote(csv)} -h -o '%i' 2>/dev/null || true"
        if cls.profile.family in ("pbspro", "torque"):
            # PBS: query the explicit ids (NOT ``qstat -u``). ``-u`` triggers PBS's
            # *wide* alternate listing where the state column is no longer index 4
            # (SessID/NDS/TSK shift it right); passing job ids keeps the default
            # brief format (id col 0, state col 4 — the format parse expects).
            # ``-t`` expands array parents into subjobs; ids that have left the
            # queue print to stderr (discarded) and are simply absent from stdout.
            ids = " ".join(shlex.quote(str(j)) for j in job_ids)
            return f"qstat -t {ids} 2>/dev/null || true"
        # sge: one ``qstat -u $USER`` call regardless of N; filtering happens
        # in parse_alive_output. $USER expands cluster-side.
        return 'qstat -u "$USER" 2>/dev/null || true'

    @classmethod
    def parse_alive_output(cls, stdout: str, job_ids: list[str]) -> set[str]:
        """Filter alive-check stdout to the requested *job_ids*."""
        if cls.profile.family == "slurm":
            alive: set[str] = set()
            wanted = set(job_ids)
            for line in stdout.splitlines():
                token = line.strip()
                if not token:
                    continue
                base = token.split(".")[0].split("_")[0]
                if base in wanted:
                    alive.add(base)
            return alive
        # sge (``qstat -u``) / pbs (``qstat -t <ids>``): both print a 2-line
        # header then rows with the job id in column 0.
        # PBS ids are ``<seq>.<server>`` / ``<seq>[<idx>].<server>`` — strip the
        # ``.server`` / ``[idx]`` to the bare sequence (a no-op for SGE's pure
        # numeric ids, so SGE behaviour is unchanged).
        alive_sge: set[str] = set()
        wanted_sge = {str(j) for j in job_ids}
        for line in stdout.splitlines():
            cols = line.split()
            if not cols:
                continue
            jid = cols[0].strip()
            if not jid or not jid[0].isdigit():
                continue  # header / separator line
            base = jid.split(".")[0].split("[")[0]
            if base in wanted_sge:
                alive_sge.add(base)
        return alive_sge

    @classmethod
    def build_cancel_cmd(cls, job_ids: list[str]) -> str:
        """Shell command that requests cancellation of *job_ids* (kill seam).

        SLURM cancels via ``scancel <id> <id> ...``; SGE and the PBS family
        (pbspro / torque) all cancel via ``qdel <id> <id> ...``. Ids are
        quoted individually (mirroring :meth:`build_alive_check_cmd`'s PBS
        branch). An empty id list short-circuits to a ``true`` no-op — matching
        the alive/state builders — so no bare ``scancel``/``qdel`` with no args
        is ever dispatched. The command only *requests* cancellation: gone-ness
        is confirmed by the alive-check verification, not by its exit code.
        """
        if not job_ids:
            return "true"
        ids = " ".join(shlex.quote(str(j)) for j in job_ids)
        if cls.profile.family == "slurm":
            return f"scancel {ids}"
        # sge / pbspro / torque all cancel via ``qdel <id> <id> ...``.
        return f"qdel {ids}"

    @classmethod
    def build_scheduler_state_cmd(cls, job_ids: list[str]) -> str:
        """Shell command pairing each live job id with its raw state."""
        if not job_ids:
            return "true"
        if cls.profile.family == "slurm":
            csv = ",".join(job_ids)
            return f"squeue -j {shlex.quote(csv)} -h -o '%i %T' 2>/dev/null || true"
        if cls.profile.family in ("pbspro", "torque"):
            # See build_alive_check_cmd: explicit ids (+ ``-t`` for arrays) keep
            # PBS in its brief format so the state token stays at column 4.
            ids = " ".join(shlex.quote(str(j)) for j in job_ids)
            return f"qstat -t {ids} 2>/dev/null || true"
        # sge: qstat -u output already carries the state column.
        return 'qstat -u "$USER" 2>/dev/null || true'

    @classmethod
    def parse_scheduler_states(cls, stdout: str, job_ids: list[str]) -> dict[str, str]:
        """Map each requested job id present in *stdout* to its raw state token."""
        if cls.profile.family == "slurm":
            states: dict[str, str] = {}
            wanted = set(job_ids)
            for line in stdout.splitlines():
                parts = line.split()
                if len(parts) < 2:
                    continue
                base = parts[0].split(".")[0].split("_")[0]
                if base in wanted:
                    states[base] = parts[1].strip()
            return states
        # sge (``qstat -u``) / pbs (``qstat -t <ids>``, brief format): state is the
        # 5th column (index 4); rows guarded on a digit id. PBS ids
        # (``<seq>.<server>`` / ``<seq>[<idx>]...``) are stripped to the bare
        # sequence (no-op for SGE), so this serves both families.
        states_sge: dict[str, str] = {}
        wanted_sge = {str(j) for j in job_ids}
        for line in stdout.splitlines():
            cols = line.split()
            if len(cols) < 5:
                continue
            jid = cols[0].strip()
            if not jid or not jid[0].isdigit():
                continue
            base = jid.split(".")[0].split("[")[0]
            if base not in wanted_sge:
                continue
            states_sge[base] = cols[4].strip()
        return states_sge

    @classmethod
    def classify_scheduler_state(cls, state: str) -> str:
        """Bucket a raw scheduler state token into ``alive`` / ``error`` / ``held``."""
        if cls.profile.family == "slurm":
            s = state.strip().upper()
            # ``sacct`` can emit ``CANCELLED by <uid>`` (trailing text), so match
            # on the leading token against the error vocabulary rather than the
            # whole string (mirrors status._categorize's startswith handling).
            head = s.split()[0] if s else s
            if head in cls.profile.error_states:
                return "error"
            # SUSPENDED / STOPPED are not making progress — bucket as held (matches
            # slurm-drmaa's USER/SYSTEM_SUSPENDED -> held), alongside the hold family.
            if s in {"SUSPENDED", "STOPPED"} or "HOLD" in s or s == "SPECIAL_EXIT":
                return "held"
            return "alive"
        if cls.profile.family in ("pbspro", "torque"):
            # PBS live qstat single-letter states. H/S/U are not progressing
            # -> held; everything else live (Q R E B T W M) -> alive. Finished
            # tokens (F/C/X) don't appear in the live ``qstat -u`` listing, and
            # success-vs-failure is read from Exit_status in the history path,
            # not the live token (so there is no live 'error' bucket here).
            s = state.strip()
            if s in {"H", "S", "U"}:
                return "held"
            return "alive"
        # sge: error states carry an uppercase ``E``; held jobs carry ``h``.
        s = state.strip()
        if "E" in s:
            return "error"
        if "h" in s:
            return "held"
        return "alive"

    @classmethod
    def batch_status(cls, states: dict[str, str]) -> dict[str, str]:
        """Bulk map ``{job_id: raw_state}`` to ``{job_id: TaskStatus.value}``.

        One classification pass over the dict :meth:`parse_scheduler_states`
        produced from a single batched ``qstat``/``squeue`` query — no extra
        scheduler round-trips. Family-aware so a *live* queue token is split
        into ``pending`` (queued/held — waiting, not progressing) vs
        ``running`` (executing) vs ``failed`` (SGE ``Eqw`` / a SLURM error
        state), distinguishing the queued-vs-running boundary that the coarse
        alive/error/held bucketing collapses.

        ``complete`` is never emitted: a finished job leaves the live queue,
        so it is absent from *states* entirely — the caller infers completion
        from absence (cross-checked against result files / history), never
        from a live token. Unrecognized live tokens fall back to ``running``
        (present in the queue ⇒ making progress), matching
        :meth:`classify_scheduler_state`'s conservative ``alive`` default.
        """
        from hpc_agent._kernel.contract.vocabulary import TaskStatus

        out: dict[str, str] = {}
        for job_id, raw in states.items():
            bucket = cls.classify_scheduler_state(raw)
            if bucket == "error":
                out[job_id] = TaskStatus.FAILED.value
                continue
            if bucket == "held":
                # Held jobs are waiting on a dependency/hold, not executing.
                out[job_id] = TaskStatus.PENDING.value
                continue
            # bucket == "alive": split queued vs running on the family token.
            out[job_id] = cls._alive_task_status(raw)
        return out

    @classmethod
    def _alive_task_status(cls, raw: str) -> str:
        """Split a live (non-error/held) scheduler token into running vs pending."""
        from hpc_agent._kernel.contract.vocabulary import TaskStatus

        if cls.profile.family == "slurm":
            s = raw.strip().upper()
            # squeue %T pending family. RUNNING/COMPLETING/CONFIGURING/etc. =>
            # running; anything else live but not yet executing => pending.
            if s in {"PENDING", "REQUEUED", "REQUEUE_HOLD", "RESV_DEL_HOLD"}:
                return TaskStatus.PENDING.value
            return TaskStatus.RUNNING.value
        if cls.profile.family in ("pbspro", "torque"):
            # PBS live single-letter: Q (queued)/W (waiting)/M (moved)/T
            # (transit) are not executing; R/B/E (running/begun/exiting) are.
            if raw.strip() in {"Q", "W", "M", "T"}:
                return TaskStatus.PENDING.value
            return TaskStatus.RUNNING.value
        # sge: ``qw`` (queued waiting) and any ``w``-bearing wait state =>
        # pending; ``r``/``t`` (running/transferring) => running.
        s = raw.strip()
        if "r" not in s and "w" in s:
            return TaskStatus.PENDING.value
        return TaskStatus.RUNNING.value

    @classmethod
    def stderr_log_path(cls, remote_path: str, job_name: str, job_id: str, task_id: int) -> str:
        """Cluster-side path to a single task's stderr log.

        *task_id* is the 0-based id within *job_id*'s OWN array (equal to the
        global ``HpcTaskId`` for a single-array run; a waved batch's caller
        subtracts its ``TASK_OFFSET`` first — the scheduler names the file by
        its local index, ``%a`` / ``$SGE_TASK_ID``). The on-disk filename
        carries the 1-based local ``ArrayIndex`` — recovered here through
        :func:`~hpc_agent._kernel.contract.task_id.to_array_index`, the single
        validated ``±1``.
        """
        base = remote_path.rstrip("/")
        array_idx = int(to_array_index(HpcTaskId(task_id)))
        if cls.profile.family == "slurm":
            # sbatch --error <log_dir>/%x_%A_%a.err -> job_name_jobid_idx.err
            return f"{base}/logs/{job_name}_{job_id}_{array_idx}.err"
        # sge: ``-j y`` merges streams into <job_name>.o<job_id>.<array_idx>
        return f"{base}/logs/{job_name}.o{job_id}.{array_idx}"

    @classmethod
    def err_log_disk_path(
        cls, log_dir: str, scratch_dir: str, job_name: str, job_id: str, task_id: int
    ) -> str:
        """Local-disk path used by ``status.get_err_log_paths``."""
        if cls.profile.family == "slurm":
            return os.path.join(log_dir, f"{job_name}_{job_id}_{task_id}.err")
        return os.path.join(scratch_dir, f"{job_name}.o{job_id}.{task_id}")

    @classmethod
    def query_jobs(
        cls,
        job_ids: list[str],
        *,
        sge_user: str | None = None,
        slurm_cluster: str | None = None,
    ) -> dict[str, Any]:
        """Return per-job state map for *job_ids* via the scheduler's history."""
        if cls.profile.family == "slurm":
            from hpc_agent.infra.backends.query import query_sacct

            return query_sacct(job_ids, cluster=slurm_cluster)
        if cls.profile.family in ("pbspro", "torque"):
            from hpc_agent.infra.backends.query import query_pbs

            return query_pbs(job_ids, fork=cls.profile.family)
        from hpc_agent.infra.backends.query import query_sge

        return query_sge(job_ids, user=sge_user)

    @classmethod
    def inspect_cluster(
        cls,
        cluster_name: str,
        cfg: dict[str, Any],
        *,
        sacct_window_hours: int = 24,
        stress_alloc_mem_pct: float = 0.80,
        stress_cpu_load_frac: float = 0.80,
        runner: Any = None,
    ) -> Any:
        """Return a ``ClusterSnapshot`` for *cluster_name*."""
        if cls.profile.family == "slurm":
            from hpc_agent.infra.inspect.slurm import _slurm_inspect

            return _slurm_inspect(
                cluster_name,
                cfg,
                sacct_window_hours=sacct_window_hours,
                stress_alloc_mem_pct=stress_alloc_mem_pct,
                stress_cpu_load_frac=stress_cpu_load_frac,
                runner=runner,
            )
        if cls.profile.family in ("pbspro", "torque"):
            from hpc_agent.infra.inspect.pbs import _pbs_inspect

            return _pbs_inspect(
                cluster_name,
                cfg,
                scheduler_kind=cls.profile.family,
                stress_alloc_mem_pct=stress_alloc_mem_pct,
                stress_cpu_load_frac=stress_cpu_load_frac,
                runner=runner,
            )
        from hpc_agent.infra.inspect.sge import _sge_inspect

        return _sge_inspect(
            cluster_name,
            cfg,
            stress_alloc_mem_pct=stress_alloc_mem_pct,
            stress_cpu_load_frac=stress_cpu_load_frac,
            runner=runner,
        )


class RemoteProfileBackend(ProfileBackend):
    """A profile-driven backend that submits over SSH.

    Used for *resolved* (non-golden) profiles registered at runtime via
    :func:`hpc_agent.infra.backends.register_profile`. The golden
    ``slurm`` / ``sge`` labels keep their dedicated
    ``RemoteSlurmBackend`` / ``RemoteSGEBackend`` classes for back-compat
    (``remote_factory`` imports those by name); this generic class covers
    everything else. The SSH overrides come from
    :class:`hpc_agent.infra.backends._remote_base.RemoteHPCBackend`,
    mixed in by :func:`build_backend_class` ahead of this in the MRO.
    """

    def __init__(
        self,
        script: str | None = None,
        ssh_run: Callable[[str], subprocess.CompletedProcess[str]] | None = None,
        remote_repo: str | None = None,
        log_dir: str | None = None,
        account: str | None = None,
        cluster: str | None = None,
        pass_env_keys: tuple[str, ...] = (),
    ):
        if script is None:
            raise errors.SpecInvalid(f"{type(self).__name__} requires a 'script' path")
        if ssh_run is None:
            raise errors.SpecInvalid(f"{type(self).__name__} requires an 'ssh_run' callable")
        if remote_repo is None:
            raise errors.SpecInvalid(f"{type(self).__name__} requires a 'remote_repo' path")
        self.script = script
        self.ssh_run = ssh_run
        self.remote_repo = remote_repo
        self.log_dir = log_dir or f"{remote_repo}/logs"
        self.account = account or ""
        self.cluster = cluster or ""
        self.pass_env_keys = pass_env_keys
