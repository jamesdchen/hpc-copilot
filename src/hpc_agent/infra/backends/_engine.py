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
from hpc_agent.infra.backends.profile import SchedulerProfile, dialect_for
from hpc_agent.infra.backends.profile import render_script as _render_script
from hpc_agent.infra.ssh_validation import split_ack, wrap_with_ack

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable
    from pathlib import Path


# Sentinel-ack transport verdict (docs/design/connection-broker.md, 2026-07-10).
# Every scheduler-liveness / -state query (:meth:`ProfileBackend.build_alive_check_cmd`
# / :meth:`~ProfileBackend.build_scheduler_state_cmd`) ends by echoing this token
# with the scheduler command's own exit code. Its PRESENCE is the affirmative
# proof that the remote shell ran the query to completion; an empty read that
# does NOT carry it is a silently-truncated / never-executed channel — UNKNOWN,
# never "no jobs left the queue". This kills the silence-as-terminal class the
# old ``… || true`` masking created: a scheduler binary that failed (qstat
# missing, slurmctld down) returned rc 0 + empty stdout, indistinguishable from
# an empty queue, so every job read as terminal. See
# :meth:`ProfileBackend.scheduler_query_ran`.
_SCHED_ACK_PREFIX = "__HPC_SCHED_ACK__="

# PBS-family FINISHED single-letter states that linger in the LIVE listing
# (#F38). TORQUE keeps a completed job in plain ``qstat`` as ``C`` for the
# keep_completed window; PBS Pro ``-x`` surfaces ``F`` (finished) and ``X``
# (subjob finished/expired). These are terminal, NOT alive — the row parsers
# skip them for the pbspro/torque families so a finished/qdel'd job reads as
# ABSENT (the same "gone from the live listing" invariant SLURM/SGE have).
# ``E`` (exiting) is deliberately NOT here: it is a job still running its
# epilogue, correctly bucketed alive.
_PBS_TERMINAL_STATES = frozenset({"C", "F", "X"})


def _with_ack(cmd: str) -> str:
    """Suffix *cmd* with the scheduler sentinel-ack echo (see :data:`_SCHED_ACK_PREFIX`).

    Thin alias for the shared :func:`hpc_agent.infra.ssh_validation.wrap_with_ack`
    primitive (the ONE definition of the ack-wrap mechanism); this keeps the
    scheduler prefix as the call-site default.
    """
    return wrap_with_ack(cmd, _SCHED_ACK_PREFIX)


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


# ── SGE/UGE memory semantics (run-14, hoffman2 live evidence 2026-07-16) ────
# ``h_data`` differs from SLURM ``--mem`` on BOTH axes:
#
# * **Per-slot, not per-task.** ``-l h_data=XM -pe shared N`` grants (and
#   queues for) N×X total. Emitting the spec's ``mem_mb`` verbatim made an
#   ``mem_mb=16000, cpus=4`` ask queue as 64G — "queues terribly" — while the
#   same spec on SLURM asked 16G. ``mem_mb`` is defined as the PER-TASK TOTAL
#   (the backend-portable meaning); the SGE emitter divides it across slots.
# * **Enforced against VIRTUAL memory, with a silent SIGKILL.** SLURM accounts
#   resident/cgroup memory; UGE's h_data kills on vmem, which glibc malloc
#   arenas + OpenMP thread arenas + arrow buffers inflate well beyond RSS
#   (observed 3-5× before the MALLOC_ARENA_MAX/thread caps; ~1.5-2× after).
#   A disclosed headroom FACTOR bridges the RSS-intent → vmem-enforced gap so
#   an ask sized from measured RSS is not silently vmem-killed ~1min in with
#   no traceback (two live canaries died exactly so at h_data=8000M).
#
# per-slot h_data = ceil(mem_mb × factor / slots). Factor default 2.0 (the
# post-arena-cap residual gap), operator-tunable via HPC_SGE_VMEM_FACTOR
# (set 1 to disable the headroom). The transformation is DISCLOSED via a
# logger line whenever the emitted number differs from the spec's mem_mb.
_SGE_VMEM_FACTOR_ENV = "HPC_SGE_VMEM_FACTOR"
DEFAULT_SGE_VMEM_FACTOR = 2.0


def sge_vmem_factor() -> float:
    """The SGE vmem-headroom factor: env override, else the 2.0 default.

    Unset / unparseable / non-positive falls back to the default — a
    fat-fingered factor must never zero out a memory ask.
    """
    raw = os.environ.get(_SGE_VMEM_FACTOR_ENV, "").strip()
    if not raw:
        return DEFAULT_SGE_VMEM_FACTOR
    try:
        val = float(raw)
    except ValueError:
        return DEFAULT_SGE_VMEM_FACTOR
    return val if val > 0 else DEFAULT_SGE_VMEM_FACTOR


def sge_h_data_mb(mem_mb: int, slots: int | None) -> int:
    """Per-slot ``h_data`` MB from a PER-TASK-TOTAL *mem_mb* (the one definition).

    ``ceil(mem_mb × vmem_factor / slots)`` — every SGE mem emitter (the engine's
    single-node + MPI paths, recover-flow's override renderer) routes through
    this so the per-slot division and the vmem headroom can never drift apart.
    """
    import math

    n = max(1, int(slots)) if slots else 1
    return max(1, math.ceil(int(mem_mb) * sge_vmem_factor() / n))


def _sge_mem_flags(mem_mb: int | None, slots: int | None = None) -> list[str]:
    """SGE ``-l h_data=`` flags for a PER-TASK-TOTAL *mem_mb* across *slots*.

    Discloses the translation (per-slot division + vmem headroom) via a logger
    warning whenever the emitted per-slot number differs from the spec's
    mem_mb, so the operator can audit what the scheduler was actually asked.
    """
    if not mem_mb:
        return []
    per_slot = sge_h_data_mb(int(mem_mb), slots)
    if per_slot != int(mem_mb):
        import logging

        n = max(1, int(slots)) if slots else 1
        logging.getLogger(__name__).warning(
            "SGE mem translation: mem_mb=%d (per-task total) -> h_data=%dM per slot "
            "(x%d slots = %dM total vmem cap; vmem headroom factor %g, "
            "%s=<f> to tune). h_data is PER-SLOT and enforced against VIRTUAL "
            "memory on UGE/SGE.",
            int(mem_mb),
            per_slot,
            n,
            per_slot * n,
            sge_vmem_factor(),
            _SGE_VMEM_FACTOR_ENV,
        )
    return ["-l", f"h_data={per_slot}M"]


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
            # h_data is PER-SLOT: divide the per-task-total mem_mb across the
            # ``-pe shared`` slots (+ the vmem headroom) — see sge_h_data_mb.
            flags += _sge_mem_flags(mem_mb, cpus)
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
            # h_data is PER-SLOT and the PE grants ``ranks`` slots: divide the
            # per-job-total mem_mb across them (+ vmem headroom).
            flags += _sge_mem_flags(mem_mb, ranks)
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
        concurrency_cap: int | None = None,
    ) -> list[str]:
        """Assemble the submit command.

        *array* defaults True (the fan-out shape: one array of ``task_range``
        elements). A single multi-rank MPI job (#293) is submitted with
        ``array=False`` and ``task_range=None`` — one job whose internal
        parallelism is the rank count, not a scheduler array.

        *concurrency_cap* (#339 item 16) is the scheduler-native in-array
        concurrency limit — how many array TASKS may run at once — spelled
        per-family (SLURM ``--array=<range>%N``, SGE ``qsub -tc N``, PBS
        ``-J/-t <range>%N``). It is emitted ONLY for an array submission
        (``array`` True) with a positive cap; a ``None`` / non-positive cap or a
        non-array (single MPI) job leaves the command byte-identical to the
        pre-item-16 output. The native cap gives perfect back-fill inside ONE
        array (no ``afterany`` wave boundary that drains to ~zero while
        stragglers finish), so it is the concurrency-bounding mechanism for a
        single-array sweep; the wave chain is kept only where waves carry
        semantics (per-wave combine checkpoints, staged canary gates).
        """
        if self.profile.family == "slurm":
            cmd = self._build_slurm_command(
                task_range,
                job_name,
                job_env,
                extra_flags=extra_flags,
                array=array,
                concurrency_cap=concurrency_cap,
            )
        elif self.profile.family in ("pbspro", "torque"):
            cmd = self._build_pbs_command(
                task_range,
                job_name,
                job_env,
                extra_flags=extra_flags,
                array=array,
                concurrency_cap=concurrency_cap,
            )
        else:
            cmd = self._build_sge_command(
                task_range,
                job_name,
                job_env,
                extra_flags=extra_flags,
                array=array,
                concurrency_cap=concurrency_cap,
            )
        return self._weave_correlation_flags(cmd, job_env)

    def _weave_correlation_flags(self, cmd: list[str], job_env: dict[str, str]) -> list[str]:
        """Inject the U3-c ``run_id#attempt`` correlation flag before the script arg.

        DOUBLE-GATED exactly like the jobmap dispatch weave (``_dispatch_core``):
        ``HPC_SUBMIT_ONCE`` set AND an ``HPC_RUN_ID`` in *job_env*. Flag OFF (or no
        run_id, or a family with no comment field) ⇒ the returned command is
        BYTE-IDENTICAL to the family builder's output — the same regression pin
        the marker weave carries (test_correlation_key + the golden command
        tests). The flag is inserted before the LAST arg (the script, which every
        family appends last) so the qsub grammar is undisturbed.
        """
        from hpc_agent.infra.jobmap import submit_once_enabled

        run_id = job_env.get("HPC_RUN_ID", "")
        if not (submit_once_enabled() and run_id and cmd):
            return cmd
        try:
            attempt = int(job_env.get("HPC_SUBMIT_ATTEMPT", "0"))
        except ValueError:
            attempt = 0
        flags = type(self).build_correlation_flags(run_id, attempt)
        if not flags:
            return cmd
        return [*cmd[:-1], *flags, cmd[-1]]

    def submit_non_contiguous(
        self,
        task_range: str | None,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
        cwd: Path | None = None,
        array: bool = True,
        setup_log_dir: bool = True,
        concurrency_cap: int | None = None,
    ) -> list[str]:
        """Submit a possibly non-contiguous array expression, family-aware (#6).

        recover-flow's :func:`compact_task_ids` packs the exact failed ids into
        a comma-bearing expression (``"4,8,13-15"``). SLURM ``--array`` and
        TORQUE ``-t`` accept that verbatim — one submission, one job id. SGE/UGE
        ``qsub -t`` and PBS Pro ``qsub -J`` accept only a SINGLE ``n[-m[:s]]``
        range, so for those families this splits the expression on commas
        (:func:`compact_task_ids`'s output is already one contiguous run per
        comma-delimited segment) and submits ONE array job per run via
        :meth:`submit_one`, accumulating every resulting job id.

        Returning the full id list — rather than the single-id
        :meth:`submit_one` contract — is deliberate: a scattered resubmit that
        fans out into N arrays produces N job ids, and dropping the tail would
        leave those arrays untracked by monitor/kill (silent orphans). Callers
        that thread resubmit ids (recover-flow's submit loop) extend their
        ``submitted_ids`` with the whole list so partial-resume semantics hold.

        The log dir is created once here (``setup_log_dir=True``) and each
        per-run :meth:`submit_one` skips its own idempotent ``mkdir``.
        """
        one_shot = (
            not array
            or task_range is None
            or "," not in str(task_range)
            or dialect_for(self.profile.family).supports_comma_array_ranges
        )
        if one_shot:
            return [
                self.submit_one(
                    task_range,
                    job_name,
                    job_env,
                    extra_flags=extra_flags,
                    cwd=cwd,
                    array=array,
                    setup_log_dir=setup_log_dir,
                    concurrency_cap=concurrency_cap,
                )
            ]

        runs = [seg.strip() for seg in str(task_range).split(",") if seg.strip()]
        if setup_log_dir:
            self._setup_log_dir()
        job_ids: list[str] = []
        for run in runs:
            job_ids.append(
                self.submit_one(
                    run,
                    job_name,
                    job_env,
                    extra_flags=extra_flags,
                    cwd=cwd,
                    array=array,
                    setup_log_dir=False,
                    concurrency_cap=concurrency_cap,
                )
            )
        return job_ids

    def _build_pbs_command(
        self,
        task_range: str | None,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
        array: bool = True,
        concurrency_cap: int | None = None,
    ) -> list[str]:
        # PBS Pro array flag is ``-J``; TORQUE uses ``-t`` (like SGE). Streams
        # joined with ``-j oe`` (PBS) cf. SGE's ``-j y``. Otherwise the qsub
        # shape + the ``-v`` comma hazard mirror the SGE branch.
        array_flag = "-J" if self.profile.family == "pbspro" else "-t"
        cmd = [self.profile.submit_bin]
        if array:
            if (
                not dialect_for(self.profile.family).supports_comma_array_ranges
                and task_range is not None
                and "," in str(task_range)
            ):
                # PBS Pro ``qsub -J`` accepts only a single ``X-Y[:Z]`` range —
                # no comma lists (TORQUE's ``-t`` does; its dialect sets
                # ``supports_comma_array_ranges=True``). A non-contiguous
                # resubmit expression must be split into one array per contiguous
                # run via :meth:`submit_non_contiguous`; a comma reaching the
                # builder would emit an invalid qsub, so fail loudly here (#6).
                raise errors.SpecInvalid(
                    f"PBS Pro qsub {array_flag} accepts only a single "
                    f"'X-Y[:Z]' range, not a comma list ({str(task_range)!r}). "
                    "Route non-contiguous task ranges through "
                    "submit_non_contiguous, which splits them into one array "
                    "job per contiguous run."
                )
            # PBS in-array concurrency cap (#339 item 16, #32): the two forks
            # diverge and must NOT share one rule. TORQUE ``-t`` accepts the
            # ``%N`` slot-limit suffix on the array range
            # (cap_style="range_suffix"), but PBS Pro ``-J`` REJECTS it
            # (``qsub: illegal -J value``) and caps running subjobs via the
            # separate ``-l max_run_subjobs=N`` attribute
            # (cap_style="max_run_subjobs"). Read the emission style off the
            # dialect so PBS Pro can't silently inherit TORQUE's suffix rule. A
            # None/non-positive cap leaves the range bare AND emits no attribute,
            # so the command stays byte-identical to the pre-item-16 output.
            array_spec = str(task_range)
            cap_attr: list[str] = []
            if concurrency_cap and concurrency_cap > 0:
                cap_style = dialect_for(self.profile.family).cap_style
                if cap_style == "range_suffix":
                    array_spec = f"{array_spec}%{int(concurrency_cap)}"
                elif cap_style == "max_run_subjobs":
                    cap_attr = ["-l", f"max_run_subjobs={int(concurrency_cap)}"]
            cmd += [array_flag, array_spec]
            cmd += cap_attr
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
        concurrency_cap: int | None = None,
    ) -> list[str]:
        cmd = [self.profile.submit_bin]
        if getattr(self, "cluster", ""):
            cmd.append(f"--clusters={self.cluster}")
        if array:
            # SLURM in-array concurrency cap (#339 item 16): the ``%N`` suffix on
            # the array range (``--array=1-100%20``) limits simultaneously
            # running tasks, back-filling as they finish. Only meaningful for an
            # array; a None/non-positive cap leaves the range bare, so the
            # command is byte-identical to the pre-item-16 output.
            array_spec = str(task_range)
            if concurrency_cap and concurrency_cap > 0:
                array_spec = f"{array_spec}%{int(concurrency_cap)}"
            cmd += ["--array", array_spec]
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
        concurrency_cap: int | None = None,
    ) -> list[str]:
        cmd = [self.profile.submit_bin]
        if array:
            if task_range is not None and "," in str(task_range):
                # SGE/UGE ``qsub -t`` grammar is a SINGLE ``n[-m[:s]]`` range —
                # no comma lists (only SLURM/TORQUE accept those). A
                # non-contiguous resubmit expression ("4,8,13-15") must be split
                # into one array per contiguous run via
                # :meth:`submit_non_contiguous`; reaching the builder with a
                # comma still present would emit an invalid qsub that the
                # scheduler rejects with an opaque error, so fail loudly and
                # diagnosably here instead (#6).
                raise errors.SpecInvalid(
                    "SGE qsub -t accepts only a single 'n[-m[:s]]' range, not a "
                    f"comma list ({str(task_range)!r}). Route non-contiguous "
                    "task ranges through submit_non_contiguous, which splits "
                    "them into one array job per contiguous run."
                )
            cmd += ["-t", str(task_range)]
            # UGE/SGE in-array concurrency cap (#339 item 16): ``-tc N`` limits
            # how many array tasks run at once, back-filling as they finish. Only
            # meaningful for an array; a None/non-positive cap emits nothing so
            # the command is byte-identical to the pre-item-16 output.
            if concurrency_cap and concurrency_cap > 0:
                cmd += ["-tc", str(int(concurrency_cap))]
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
    def build_alive_check_cmd(cls, job_ids: list[str], *, cluster: str | None = None) -> str:
        """Shell command whose stdout lists the live job ids.

        *cluster* (#F37) is the federated-SLURM cluster NAME the jobs were
        submitted to (the ``slurm_cluster`` value, i.e. the ``sbatch
        --clusters=`` argument — NOT the hpc-agent clusters.yaml config key).
        When set it emits ``squeue -M <cluster>`` so the liveness probe queries
        the SAME federation member ``submit`` routed to; a plain ``squeue``
        against the login node's default cluster prints nothing for a
        foreign-cluster job, which ``scheduler_query_ran`` reads as an empty
        queue and reconcile mis-settles as abandoned. Default ``None`` (the
        non-federated case, and every current call site until the value is
        threaded from the run's persisted ``slurm_cluster`` — see the class
        note) leaves the command byte-identical. Only the slurm family emits
        ``-M``; PBS/SGE ignore it (they carry no cross-cluster routing here).
        """
        if not job_ids:
            return "true"
        if cls.profile.family == "slurm":
            # squeue (active states only) so completed/failed jobs don't
            # leak history and make abandoned-run detection useless.
            csv = ",".join(job_ids)
            m = f"-M {shlex.quote(cluster)} " if cluster else ""
            return _with_ack(f"squeue {m}-j {shlex.quote(csv)} -h -o '%i' 2>/dev/null")
        if cls.profile.family in ("pbspro", "torque"):
            # PBS: query the explicit ids (NOT ``qstat -u``). ``-u`` triggers PBS's
            # *wide* alternate listing where the state column is no longer index 4
            # (SessID/NDS/TSK shift it right); passing job ids keeps the default
            # brief format (id col 0, state col 4 — the format parse expects).
            # ``-t`` expands array parents into subjobs; ids that have left the
            # queue print to stderr (discarded) and are simply absent from stdout.
            ids = " ".join(shlex.quote(str(j)) for j in job_ids)
            return _with_ack(f"qstat -t {ids} 2>/dev/null")
        # sge: one ``qstat -u $USER`` call regardless of N; filtering happens
        # in parse_alive_output. $USER expands cluster-side.
        return _with_ack('qstat -u "$USER" 2>/dev/null')

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
        # PBS ids are ``<seq>.<server>`` / ``<seq>[<idx>].<server>`` — normalise
        # the row id to the bare sequence for MATCHING (a no-op for SGE's pure
        # numeric ids, so SGE behaviour is unchanged). #F36: a stored PBS array
        # id is now ``<seq>[]`` (bracket-preserving regex), so the requested ids
        # are normalised to the bare sequence too, and the ORIGINAL requested id
        # — bracketed or bare — is what we return, so the caller's
        # ``[j for j in job_ids if j not in alive]`` set-membership holds across
        # mixed old-bare / new-bracketed sidecars.
        alive_sge: set[str] = set()
        wanted_by_base: dict[str, str] = {}
        for j in job_ids:
            wanted_by_base.setdefault(str(j).split(".")[0].split("[")[0], str(j))
        for line in stdout.splitlines():
            cols = line.split()
            if not cols:
                continue
            jid = cols[0].strip()
            if not jid or not jid[0].isdigit():
                continue  # header / separator line
            # #F38: a TORQUE job qdel'd (or normally finished) lingers in plain
            # ``qstat`` as state 'C' (also 'F'/'X' on PBS Pro ``-x``) for the
            # keep_completed window. Counting that row as alive made kill
            # verification report "still alive" after a successful qdel and
            # blocked reconcile from settling a finished run until the record
            # aged out. Skip terminal-state rows for the PBS families so a
            # finished job reads as ABSENT — the same "finished ⇒ gone from the
            # live listing" invariant SLURM/SGE get for free.
            if (
                cls.profile.family in ("pbspro", "torque")
                and len(cols) >= 5
                and cols[4].strip() in _PBS_TERMINAL_STATES
            ):
                continue
            base = jid.split(".")[0].split("[")[0]
            orig = wanted_by_base.get(base)
            if orig is not None:
                alive_sge.add(orig)
        return alive_sge

    @classmethod
    def build_cancel_cmd(
        cls, job_ids: list[str], task_range: str | None = None, *, cluster: str | None = None
    ) -> str:
        """Shell command that requests cancellation of *job_ids* (kill seam).

        SLURM cancels via ``scancel <id> <id> ...``; SGE and the PBS family
        (pbspro / torque) all cancel via ``qdel <id> <id> ...``. Ids are
        quoted individually (mirroring :meth:`build_alive_check_cmd`'s PBS
        branch), so a PBS array id ``12345[]`` (#F36) arrives single-quoted and
        addresses the real array rather than a non-existent bare ``12345``. An
        empty id list short-circuits to a ``true`` no-op — matching the
        alive/state builders — so no bare ``scancel``/``qdel`` with no args is
        ever dispatched. The command only *requests* cancellation: gone-ness is
        confirmed by the alive-check verification, not by its exit code.

        *task_range* (the submit-side array-index grammar ``"4,8,13-15"``, the
        SAME expression :meth:`submit_one` accepts, so submit and cancel speak
        ONE range vocabulary) scopes the cancel to those array indices of each
        id — SGE ``qdel <id> -t <range>``, SLURM ``scancel <id>_[<range>]``.
        ``None`` cancels the whole array/job (byte-identical to the pre-range
        command). A range cancel is a PARTIAL cancel by construction: the array
        job stays in the queue with its remaining tasks, so ``kill`` never
        settles a range cancel through reconcile. The range is emitted verbatim
        (already validated by :class:`~hpc_agent._wire.actions.kill.KillSpec`);
        it carries no shell metacharacters (digits / ``,`` / ``-`` / ``:`` are
        all shell-safe) so it needs no quoting.

        **Comma decomposition for the single-range families (SGE / PBS Pro).**
        SLURM ``scancel <id>_[<indices>]`` accepts a comma index LIST verbatim
        (``scancel 12_[4,8,13-15]`` is valid), so it stays ONE call. SGE/UGE
        ``qdel -t`` — like ``qsub -t`` — accepts only a SINGLE ``n[-m[:s]]``
        range, NOT a comma list: a whole-set ``qdel <id> -t 4,8,13-15`` cancels
        at most the leading task and leaves the rest running (the reported
        defect). The submit path can *refuse* a comma-bearing ``-t`` (see
        :meth:`_build_sge_command`), but a cancel CANNOT refuse — it must cancel
        every undone task — so for a family whose dialect lacks
        ``supports_comma_array_ranges`` a non-contiguous range is DECOMPOSED
        into one ``qdel <ids> -t <run>`` per contiguous comma segment (recover's
        :func:`~hpc_agent.ops.recover.batching.compact_task_ids` already emits
        one contiguous run per segment, so the comma split IS the
        decomposition). The per-segment commands are sequenced with ``;`` — NOT
        ``&&`` — so a non-zero qdel on an already-gone leading task still runs
        the remaining segments: the emitted command cancels EXACTLY the undone
        set, never a leading-range-only subset and never a task outside it. A
        single-segment range ("42" / "13-15") collapses to the original single
        ``qdel -t`` (byte-identical), so only the multi-segment case changes.

        *cluster* (#F37) mirrors :meth:`build_alive_check_cmd`: a federated
        ``scancel -M <cluster>`` so the cancel reaches the member ``sbatch
        --clusters=`` submitted to. Default ``None`` leaves the command
        byte-identical; only the slurm family emits ``-M``.
        """
        if not job_ids:
            return "true"
        ids = " ".join(shlex.quote(str(j)) for j in job_ids)
        if cls.profile.family == "slurm":
            m = f"-M {shlex.quote(cluster)} " if cluster else ""
            if task_range is not None:
                # Per-id ``<id>_[<indices>]`` — scancel's array-subscript form.
                targets = " ".join(f"{shlex.quote(str(j))}_[{task_range}]" for j in job_ids)
                return f"scancel {m}{targets}"
            return f"scancel {m}{ids}"
        # sge / pbspro / torque all cancel via ``qdel <id> <id> ...``.
        if task_range is not None:
            # SGE addresses array tasks with ``-t <range>`` (the submit ``-t``
            # dialect); the PBS families reuse it in this codepath (no PBS range
            # cancel is exercised today).
            if not dialect_for(cls.profile.family).supports_comma_array_ranges:
                # SGE/UGE ``qdel -t`` accepts one ``n[-m[:s]]`` range only, so a
                # non-contiguous set must be decomposed into one qdel per
                # contiguous comma segment (see the docstring). ``;`` — never
                # ``&&`` — so every segment is cancelled even if an earlier one
                # errors on an already-gone task. A single segment collapses to
                # the original single ``qdel -t`` string.
                segments = [seg.strip() for seg in str(task_range).split(",") if seg.strip()]
                return " ; ".join(f"qdel {ids} -t {seg}" for seg in segments)
            return f"qdel {ids} -t {task_range}"
        return f"qdel {ids}"

    @classmethod
    def build_scheduler_state_cmd(cls, job_ids: list[str], *, cluster: str | None = None) -> str:
        """Shell command pairing each live job id with its raw state.

        *cluster* (#F37) emits ``squeue -M <cluster>`` for a federated SLURM
        job, same contract as :meth:`build_alive_check_cmd`; default ``None``
        leaves the command byte-identical and PBS/SGE ignore it.
        """
        if not job_ids:
            return "true"
        if cls.profile.family == "slurm":
            csv = ",".join(job_ids)
            m = f"-M {shlex.quote(cluster)} " if cluster else ""
            return _with_ack(f"squeue {m}-j {shlex.quote(csv)} -h -o '%i %T' 2>/dev/null")
        if cls.profile.family in ("pbspro", "torque"):
            # See build_alive_check_cmd: explicit ids (+ ``-t`` for arrays) keep
            # PBS in its brief format so the state token stays at column 4.
            ids = " ".join(shlex.quote(str(j)) for j in job_ids)
            return _with_ack(f"qstat -t {ids} 2>/dev/null")
        # sge: qstat -u output already carries the state column.
        return _with_ack('qstat -u "$USER" 2>/dev/null')

    @classmethod
    def scheduler_query_ran(cls, stdout: str) -> tuple[str, bool]:
        """Split the sentinel-ack line off *stdout*; return ``(clean_stdout, ran_ok)``.

        The positive-evidence transport verdict for a liveness / state query
        (sentinel-ack ruling, docs/design/connection-broker.md). ``ran_ok`` is a
        claim about whether the QUERY executed — never about the remote command
        succeeding — so a caller can distinguish "the scheduler answered (its
        answer may be an empty queue)" from "the channel returned nothing / was
        truncated" (UNKNOWN) instead of reading silence as "all jobs terminal".

        Rules:

        * **Ack absent** → ``ran_ok=False`` for every family: the remote shell
          never reached the trailing echo (empty / silently-truncated read).
          This is the class the ruling exists to kill.
        * **Ack present, whole-queue query (SGE)** → ``ran_ok`` iff the recorded
          rc is ``0``. SGE's ``qstat -u $USER`` lists the whole user queue and
          exits 0 on an empty queue, so a non-zero rc is the scheduler binary
          ITSELF failing (missing / server down) — exactly the case the old
          ``|| true`` masked into a spurious "no jobs".
        * **Ack present, explicit-id query (SLURM / PBS Pro / TORQUE)** →
          ``ran_ok=True`` EXCEPT for rc ``126``/``127``. These families query
          EXPLICIT ids (``squeue -j`` / ``qstat -t <ids>``) and exit non-zero
          once a queried id has left the queue (SLURM "invalid job id", PBS
          "Unknown Job Id" / "job has finished"), indistinguishable from a
          genuine binary failure by rc alone — so they lean on ack PRESENCE (the
          channel-silence guard) and defer the completed-vs-failed decision to
          the reporter's positive task evidence
          (``ops/monitor/classify.settle``), never to this query's emptiness.
          Reading a *finished-id* non-zero rc as "query failed" is the G9 bug
          that pinned every finished PBS run at UNKNOWN (#5): once one job leaves
          the queue the rc-0 rule can never settle the run terminal — so the
          finished-id rcs (1 / 35 / 153) MUST stay ``ran_ok=True``.

          BUT rc ``127`` (command not found) and ``126`` (found, not
          executable) are the shell's OWN "the scheduler binary never ran"
          codes — a finished/absent job id never produces them (its rc is
          1/35/153). This is the exact "``squeue`` missing / non-login shell
          lacks the module" class the ack header (:data:`_SCHED_ACK_PREFIX`)
          claims to kill but, for the explicit-id families, could not: a missing
          binary echoed the ack with rc 127 and empty stdout, read as "queue
          empty", and a healthy campaign settled abandoned (#F35, reproduced
          in-sandbox at rc 127). Excluding 126/127 restores that guard's fire
          path without touching the finished-id rcs the #5 fix depends on.

          NOT covered here (deliberately deferred, gated on the live-cluster
          check in ``critic-gaps.json``): the daemon-down rc=1 case (a live
          ``squeue`` against a down slurmctld returns rc 1 with a FATAL stderr,
          not 127). Distinguishing it from a benign finished-id rc=1 needs
          stderr capture with a known-FATAL allowlist — and, per the Fable
          panel, a stderr rule MUST default ``ran_ok=True`` on unrecognised
          messages (allowlist FATAL daemon-down strings, never benign ones) or
          it re-opens the G9/#5 UNKNOWN-forever regression. That arm changes the
          query command shape and rests on a per-scheduler stderr convention not
          verifiable in this sandbox, so it is left for the live-cluster follow-up.

          The family capability is read off :class:`FamilyDialect`
          (``explicit_id_liveness_query``), not hardcoded per branch, so a family
          the dev loop doesn't exercise can't inherit SGE's whole-queue rule.

        The ack line is stripped from the returned stdout so the family parsers
        see only real scheduler rows (they already skip a non-digit-led line,
        but stripping keeps the contract explicit).
        """
        clean, rc = split_ack(stdout, _SCHED_ACK_PREFIX)
        if rc is None:
            return clean, False
        if dialect_for(cls.profile.family).explicit_id_liveness_query:
            # rc 126/127 = the shell could not run the scheduler binary at all
            # (missing / not executable); a finished-or-absent id never yields
            # those (1/35/153), so this cleanly flips the missing-binary case to
            # UNKNOWN without re-breaking the finished-id rule (#5 / #F35).
            return clean, rc not in (126, 127)
        return clean, rc == 0

    # ------------------------------------------------------------------
    # U3-c — the run+attempt correlation key (submit-once Δ2/OPEN-1(i)).
    # The token ``run_id#attempt`` rides a length-unconstrained scheduler
    # CONTEXT/COMMENT field (never job_name), emitted at submit and read back
    # by U3-d rung-1b to discover an orphan's job id when the marker append
    # never landed. Per-scheduler shape lives HERE (B5-PR2), keyed off
    # ``cls.profile.family`` like every other query classmethod above.
    # ------------------------------------------------------------------

    @classmethod
    def build_correlation_flags(cls, run_id: str, attempt: int) -> list[str]:
        """Submit-argv fragment carrying the ``run_id#attempt`` token (Δ2).

        Slurm ``--comment <token>``; SGE/UGE ``-ac HPC_TOKEN=<token>`` (a job
        CONTEXT variable, visible via ``qstat -j``). The token is NEVER put in
        ``job_name`` — SGE caps names at 15 chars and ``job_name`` is consumed
        byte-for-byte by log paths + canary naming (the whole reason OPEN-1(iii)
        was rejected). Returns ``[]`` for families with no clean submit-time
        comment field (PBS Pro / TORQUE): the cluster-durable jobmap MARKER
        (``infra.jobmap``) stays the authoritative id binding, so a family that
        cannot carry the key degrades to marker-only recovery — never a
        duplicate. The caller injects the fragment before the script arg ONLY
        under the ``HPC_SUBMIT_ONCE`` flag with a run_id in hand, so flag-off is
        byte-identical (see :meth:`_build_command`).
        """
        from hpc_agent.infra.jobmap import CORRELATION_KEY_ENV, jobmap_token

        token = jobmap_token(run_id, attempt)
        if cls.profile.family == "slurm":
            return ["--comment", token]
        if cls.profile.family == "sge":
            return ["-ac", f"{CORRELATION_KEY_ENV}={token}"]
        return []

    @classmethod
    def build_token_query_cmd(cls, *, user: str | None = None) -> str:
        """Ack-gated query pairing each live job's correlation token with its id.

        The U3-d rung-1b fallback: when a ``submitting`` orphan's marker is
        ``pending`` with no id (``qsub`` accepted the array but SIGKILL cut the
        stdout before the id reached the client AND before the marker append),
        the id can still be recovered from the scheduler by the token the submit
        stamped (Δ2). Slurm: ``squeue -o '%i|%k'`` (job id | comment). SGE: the
        context lives only in ``qstat -j <id>``, so the user's live queue is
        enumerated and each job's detail dumped. PBS carries no token → a ``true``
        no-op (recovery falls back to the marker alone). Ack-wrapped so a severed
        / binary-missing query reads UNKNOWN (:meth:`scheduler_query_ran`), never
        "token absent" — the positive-evidence discipline the whole ladder rests
        on.
        """
        if cls.profile.family == "slurm":
            u = f"-u {shlex.quote(user)}" if user else '-u "$USER"'
            return _with_ack(f"squeue {u} -h -o '%i|%k' 2>/dev/null")
        if cls.profile.family == "sge":
            u = shlex.quote(user) if user else '"$USER"'
            # Enumerate the user's live job ids, then dump each job's detail
            # (``qstat -j`` is the only place the ``-ac`` context surfaces). The
            # ``awk`` keeps only digit-led id rows (skips the 2-line header).
            enum = (
                f"qstat -u {u} 2>/dev/null | awk 'NR>2 && $1 ~ /^[0-9]+$/ {{print $1}}' | sort -u"
            )
            return _with_ack(f'for __hpc_j in $({enum}); do qstat -j "$__hpc_j" 2>/dev/null; done')
        return _with_ack("true")

    @classmethod
    def parse_token_query(cls, stdout: str) -> dict[str, str]:
        """Map correlation-token → base job id from :meth:`build_token_query_cmd`.

        The ack line is assumed already stripped by
        :meth:`scheduler_query_ran`. Slurm rows are ``<jobid>|<comment>`` (the
        comment IS the token); the id is normalised to its base sequence
        (``12345_7`` / ``12345.batch`` → ``12345``). SGE output is concatenated
        ``qstat -j`` blocks: track ``job_number:`` and read the token off the
        ``context:`` line (``HPC_TOKEN=<token>[,other=…]``). First occurrence of
        a token wins (a token is run+attempt-unique by construction, so a second
        hit would be a re-used attempt the adopt gate already forbids).
        """
        from hpc_agent.infra.jobmap import CORRELATION_KEY_ENV

        out: dict[str, str] = {}
        if cls.profile.family == "slurm":
            for line in stdout.splitlines():
                line = line.strip()
                if "|" not in line:
                    continue
                jid, comment = line.split("|", 1)
                token = comment.strip()
                base = jid.strip().split(".")[0].split("_")[0]
                if token and base:
                    out.setdefault(token, base)
            return out
        if cls.profile.family == "sge":
            current: str | None = None
            for raw in stdout.splitlines():
                line = raw.strip()
                if line.startswith("job_number:"):
                    current = line.split(":", 1)[1].strip()
                elif line.startswith("context:") and current:
                    ctx = line.split(":", 1)[1].strip()
                    for kv in ctx.split(","):
                        key, _, val = kv.partition("=")
                        if key.strip() == CORRELATION_KEY_ENV and val.strip():
                            out.setdefault(val.strip(), current)
            return out
        return out

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
        # (``<seq>.<server>`` / ``<seq>[<idx>]...``) are normalised to the bare
        # sequence for MATCHING (no-op for SGE), so this serves both families;
        # the ORIGINAL requested id — bracketed ``12345[]`` (#F36) or bare — is
        # the dict key so callers keying off ``job_ids`` line up across mixed
        # old-bare / new-bracketed sidecars.
        states_sge: dict[str, str] = {}
        wanted_by_base: dict[str, str] = {}
        for j in job_ids:
            wanted_by_base.setdefault(str(j).split(".")[0].split("[")[0], str(j))
        for line in stdout.splitlines():
            cols = line.split()
            if len(cols) < 5:
                continue
            jid = cols[0].strip()
            if not jid or not jid[0].isdigit():
                continue
            state_tok = cols[4].strip()
            # #F38: skip a TORQUE 'C' (or PBS Pro 'F'/'X') finished row — it
            # lingers in the live listing for keep_completed but is terminal, so
            # it must be ABSENT from the live-state map (else batch_status maps
            # it RUNNING and reconcile can't settle the finished run).
            if cls.profile.family in ("pbspro", "torque") and state_tok in _PBS_TERMINAL_STATES:
                continue
            base = jid.split(".")[0].split("[")[0]
            orig = wanted_by_base.get(base)
            if orig is None:
                continue
            states_sge[orig] = state_tok
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
            # -> held; everything else live (Q R E B T W M) -> alive.
            # Finished tokens (F/C/X) are TERMINAL and are dropped UPSTREAM by
            # the row parsers (:meth:`parse_alive_output` /
            # :meth:`parse_scheduler_states`) for the PBS families (#F38) — a
            # TORQUE job lingers as 'C' for keep_completed but must read as
            # ABSENT, matching the "gone from the live listing ⇒ complete"
            # contract SLURM/SGE get for free — so classify never receives one
            # here (no inert F/C/X branch: this bucketer's only callers consume
            # the already-filtered parser output). Success-vs-failure is read
            # from Exit_status in the history path, not the live token (so there
            # is no live 'error' bucket here).
            s = state.strip()
            if s in {"H", "S", "U"}:
                return "held"
            return "alive"
        # sge: error states carry an uppercase ``E``; held jobs carry ``h``.
        s = state.strip()
        if "E" in s:
            return "error"
        # #F40: SUSPENDED tokens (routine under subordinate-queue preemption:
        # 's'/'ts' user-suspend, 'S'/'tS' queue-suspend, 'T'/'tT' threshold
        # suspend) are NOT progressing — bucket held so batch_status reports
        # PENDING, matching the SLURM branch's SUSPENDED/STOPPED -> held. Checked
        # on membership of s/S/T so lowercase 't' (transferring — a RUNNING
        # substate) stays alive. Placed before the 'h' check; both return held.
        if any(c in s for c in ("s", "S", "T")):
            return "held"
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
