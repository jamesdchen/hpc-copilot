"""``watcher-install`` — install/uninstall/status a cluster-side heartbeat watcher.

Design §5 hybrid monitor: a cluster-side watcher survives the laptop. This verb
installs it via an **install-time probe ladder** — never encoded site policy —
over the throttled SSH spine (:func:`hpc_agent.infra.remote.ssh_run`), trying in
order:

  1. user ``crontab``       — viable when present and not cron.deny-blocked;
  2. ``scrontab`` (Slurm)   — when the run's scheduler is Slurm (decided through
                              the backend seam, never a concrete-backend import)
                              and ``scrontab`` is viable;
  3. self-resubmitting job  — a minimal watcher job submitted through the backend
                              seam (:func:`build_remote_backend`), whose submit
                              binary comes from the scheduler profile;
  4. none available         — install NOTHING and say so LOUDLY.

Install ships the stdlib-only watcher script
(:mod:`...templates.watcher.hpc_watcher`, which never imports ``hpc_agent``) to
``<remote>/.hpc/watcher/`` and registers the cron/scrontab line (idempotent,
marker-comment keyed on ``run_id``) or submits the job. Uninstall reverses it;
status reports what is installed.

The client half (``record_status`` stamping ``.hpc_last_read`` and surfacing the
watcher's ``.hpc_watcher_ALARM``) lives in :mod:`hpc_agent.ops.monitor.status`.
"""

from __future__ import annotations

import base64
import shlex
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.watcher_install import WatcherInstallResult, WatcherInstallSpec
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.remote import ssh_run
from hpc_agent.state.journal import load_run

# Remote layout under the project root (the "run dir" the watcher is pointed at).
_WATCHER_DIR_REL = ".hpc/watcher"
_WATCHER_SCRIPT_REL = f"{_WATCHER_DIR_REL}/hpc_watcher.py"
_WATCHER_JOB_REL = f"{_WATCHER_DIR_REL}/hpc_watcher_job.sh"


def _marker(run_id: str) -> str:
    """Idempotency marker comment appended to the cron/scrontab line for *run_id*."""
    return f"# hpc-agent-watcher run_id={run_id}"


def _watcher_script_bytes() -> bytes:
    """Read the shipped watcher template's bytes from the installed package."""
    import hpc_agent

    path = (
        Path(hpc_agent.__file__).parent
        / "execution"
        / "mapreduce"
        / "templates"
        / "watcher"
        / "hpc_watcher.py"
    )
    return path.read_bytes()


def _check(proc: Any, what: str) -> Any:
    """Raise :class:`RemoteCommandFailed` if *proc* exited non-zero."""
    if proc.returncode != 0:
        detail = ((proc.stderr or "") + " " + (proc.stdout or "")).strip()[:300]
        raise errors.RemoteCommandFailed(
            f"watcher-install: {what} failed (rc={proc.returncode}): {detail}"
        )
    return proc


def _cron_schedule(interval_min: int) -> str:
    """A cron schedule string for an every-*interval_min* cadence."""
    if interval_min < 60:
        return f"*/{max(1, interval_min)} * * * *"
    hours = max(1, interval_min // 60)
    return f"0 */{hours} * * *"


# ── backend-seam reads (never import a concrete backend) ─────────────────────


def _scheduler_family(scheduler: str) -> str:
    """Resolve the scheduler's structural family through the backend seam."""
    from hpc_agent.infra.backends import get_backend_class

    cls = get_backend_class(scheduler)
    prof = getattr(cls, "profile", None)
    if prof is not None:
        return str(prof.family)
    # Golden hand-written backends carry ``scheduler_name`` but no ``profile``.
    return str(getattr(cls, "scheduler_name", "") or scheduler)


def _is_slurm(scheduler: str) -> bool:
    """Whether *scheduler* is the Slurm family (gates the scrontab rung)."""
    return _scheduler_family(scheduler) == "slurm"


def _resolve_submit_bin(scheduler: str) -> str | None:
    """The scheduler's submit binary via the seam (profile-driven or golden)."""
    from hpc_agent.infra.backends import get_backend_class

    cls = get_backend_class(scheduler)
    prof = getattr(cls, "profile", None)
    if prof is not None:
        return str(prof.submit_bin)
    # Golden backends: match a golden profile by family/name (profile.py is the
    # scheduler-data seam remote_factory itself reads, not a concrete backend).
    from hpc_agent.infra.backends.profile import (
        PBSPRO_PROFILE,
        SGE_PROFILE,
        SLURM_PROFILE,
        TORQUE_PROFILE,
    )

    fam = _scheduler_family(scheduler)
    for p in (SLURM_PROFILE, SGE_PROFILE, PBSPRO_PROFILE, TORQUE_PROFILE):
        if p.family == fam or p.name == scheduler:
            return str(p.submit_bin)
    return None


# ── remote helpers over the throttled spine ──────────────────────────────────


def _resolve_remote_python(ssh_target: str) -> str:
    """Absolute path of the login node's python3 (fallback: bare ``python3``)."""
    proc = ssh_run("command -v python3 || command -v python || echo python3", ssh_target=ssh_target)
    for line in (proc.stdout or "").splitlines():
        if line.strip():
            return line.strip()
    return "python3"


def _ship_file(
    ssh_target: str, *, remote_dir: str, remote_path: str, data: bytes, executable: bool = False
) -> None:
    """Ship *data* to *remote_path* (base64 over one ssh call; mkdir -p first)."""
    b64 = base64.b64encode(data).decode("ascii")
    chmod = f" && chmod +x {shlex.quote(remote_path)}" if executable else ""
    cmd = (
        f"mkdir -p {shlex.quote(remote_dir)} && "
        f"printf %s {shlex.quote(b64)} | base64 -d > {shlex.quote(remote_path)}{chmod}"
    )
    _check(ssh_run(cmd, ssh_target=ssh_target), f"ship {remote_path}")


def _probe_cron_binary(ssh_target: str, binary: str) -> tuple[bool, str]:
    """Probe whether *binary* (``crontab``/``scrontab``) is usable for this user.

    Viable = the command exists and the user is not blocked from it (cron.deny).
    A "no crontab for user" result is viable (the table is simply empty/writable).
    """
    proc = ssh_run(f"{binary} -l 2>&1 || true", ssh_target=ssh_target)
    blob = (proc.stdout or "").lower()
    for bad in ("not allowed", "command not found", "not found", "cannot use this program"):
        if bad in blob:
            return False, blob.strip()[:200] or f"{binary} unavailable"
    return True, "viable"


def _register_cron_line(ssh_target: str, *, binary: str, cron_line: str, marker: str) -> None:
    """Idempotently install *cron_line* under *binary*, keyed on *marker*.

    Reads the current table, strips any prior line carrying this run's marker,
    appends the fresh line, and reinstalls — so a re-install never duplicates.
    """
    cmd = (
        f"( {binary} -l 2>/dev/null | grep -vF {shlex.quote(marker)} ; "
        f"printf '%s\\n' {shlex.quote(cron_line)} ) | {binary} -"
    )
    _check(ssh_run(cmd, ssh_target=ssh_target), f"register {binary} line")


def _remove_cron_line(ssh_target: str, *, binary: str, marker: str) -> None:
    """Best-effort removal of this run's *marker* line from *binary*'s table."""
    cmd = (
        f"{binary} -l 2>/dev/null | grep -vF {shlex.quote(marker)} | {binary} - 2>/dev/null || true"
    )
    ssh_run(cmd, ssh_target=ssh_target)


def _cron_has_marker(ssh_target: str, *, binary: str, marker: str) -> bool:
    """Whether *binary*'s table currently carries this run's marker line."""
    proc = ssh_run(
        f"{binary} -l 2>/dev/null | grep -Fq {shlex.quote(marker)} && echo YES || echo NO",
        ssh_target=ssh_target,
    )
    return "YES" in (proc.stdout or "")


def _watcher_invocation(
    *, py: str, script: str, run_dir: str, stale_sec: int, job_name: str
) -> str:
    """The ``python hpc_watcher.py --run-dir ...`` command run each firing."""
    return (
        f"{py} {shlex.quote(script)} --run-dir {shlex.quote(run_dir)} "
        f"--stale-sec {int(stale_sec)} --job-name {shlex.quote(job_name)}"
    )


# ── the ladder ───────────────────────────────────────────────────────────────


def _install(
    *,
    spec: WatcherInstallSpec,
    ssh_target: str,
    remote_path: str,
    job_name: str,
) -> WatcherInstallResult:
    """Run the probe ladder and install the first viable watcher rung."""
    run_dir = remote_path.rstrip("/")
    marker = _marker(spec.run_id)
    probes: dict[str, str] = {}

    # Ship the watcher script first — every rung needs it on the cluster.
    remote_dir = f"{run_dir}/{_WATCHER_DIR_REL}"
    script_path = f"{run_dir}/{_WATCHER_SCRIPT_REL}"
    _ship_file(
        ssh_target,
        remote_dir=remote_dir,
        remote_path=script_path,
        data=_watcher_script_bytes(),
        executable=True,
    )
    py = _resolve_remote_python(ssh_target)
    invocation = _watcher_invocation(
        py=py, script=script_path, run_dir=run_dir, stale_sec=spec.stale_sec, job_name=job_name
    )

    # Rung 1 — user crontab.
    cron_ok, cron_detail = _probe_cron_binary(ssh_target, "crontab")
    probes["crontab"] = "viable" if cron_ok else f"unavailable: {cron_detail}"
    if cron_ok:
        cron_line = f"{_cron_schedule(spec.interval_min)} {invocation} {marker}"
        _register_cron_line(ssh_target, binary="crontab", cron_line=cron_line, marker=marker)
        return WatcherInstallResult(
            run_id=spec.run_id,
            action="install",
            installed=True,
            mechanism="cron",
            reason=(
                f"cluster-side watcher installed via user crontab ({spec.interval_min}m cadence)."
            ),
            detail=cron_line,
            probes=probes,
        )

    # Rung 2 — scrontab (Slurm only, decided through the seam).
    if _is_slurm(spec.scheduler):
        scron_ok, scron_detail = _probe_cron_binary(ssh_target, "scrontab")
        probes["scrontab"] = "viable" if scron_ok else f"unavailable: {scron_detail}"
        if scron_ok:
            cron_line = f"{_cron_schedule(spec.interval_min)} {invocation} {marker}"
            _register_cron_line(ssh_target, binary="scrontab", cron_line=cron_line, marker=marker)
            return WatcherInstallResult(
                run_id=spec.run_id,
                action="install",
                installed=True,
                mechanism="scrontab",
                reason=(
                    f"cluster-side watcher installed via Slurm scrontab "
                    f"({spec.interval_min}m cadence)."
                ),
                detail=cron_line,
                probes=probes,
            )
    else:
        probes["scrontab"] = f"skipped: scheduler {spec.scheduler!r} is not Slurm"

    # Rung 3 — self-resubmitting minimal watcher job (through the backend seam).
    submit_bin = _resolve_submit_bin(spec.scheduler)
    if submit_bin:
        wrapper_path = f"{run_dir}/{_WATCHER_JOB_REL}"
        sleep_sec = int(spec.interval_min) * 60
        wrapper = (
            "#!/bin/sh\n"
            f"{invocation}\n"
            f"sleep {sleep_sec}\n"
            f"{submit_bin} {shlex.quote(wrapper_path)}\n"
        )
        _ship_file(
            ssh_target,
            remote_dir=remote_dir,
            remote_path=wrapper_path,
            data=wrapper.encode("utf-8"),
            executable=True,
        )
        try:
            job_id = _submit_watcher_job(
                scheduler=spec.scheduler,
                ssh_target=ssh_target,
                remote_path=run_dir,
                wrapper_path=wrapper_path,
                job_name=f"{job_name}-watch",
            )
        except (errors.HpcError, RuntimeError) as exc:
            probes["job"] = f"submit failed: {str(exc)[:200]}"
        else:
            probes["job"] = "submitted"
            return WatcherInstallResult(
                run_id=spec.run_id,
                action="install",
                installed=True,
                mechanism="job",
                reason=(
                    f"cluster-side watcher installed as a self-resubmitting {submit_bin} job "
                    f"(job {job_id}, {spec.interval_min}m cadence)."
                ),
                detail=f"job_id={job_id}",
                probes=probes,
            )
    else:
        probes["job"] = f"unavailable: backend seam exposes no submit binary for {spec.scheduler!r}"

    # Rung 4 — nothing took. Loud.
    return WatcherInstallResult(
        run_id=spec.run_id,
        action="install",
        installed=False,
        mechanism="none",
        reason=(
            "NO CLUSTER-SIDE WATCHER INSTALLED. crontab, scrontab, and a self-resubmitting "
            "job all failed the probe ladder. OVERNIGHT BLINDNESS PERSISTS: the laptop is "
            "the only monitor, and if it sleeps the run goes unwatched until it wakes."
        ),
        detail="; ".join(f"{k}={v}" for k, v in probes.items()),
        probes=probes,
    )


def _submit_watcher_job(
    *, scheduler: str, ssh_target: str, remote_path: str, wrapper_path: str, job_name: str
) -> str:
    """Submit the self-resubmitting watcher wrapper through the backend seam."""
    from hpc_agent.infra.backends.remote_factory import build_remote_backend

    backend = build_remote_backend(
        backend_name=scheduler,
        script=wrapper_path,
        ssh_target=ssh_target,
        remote_path=remote_path,
        pass_env_keys=None,
        job_env_keys=(),
    )
    return backend.submit_one(None, job_name, {}, array=False, cwd=Path.cwd())


def _uninstall(
    *, spec: WatcherInstallSpec, ssh_target: str, remote_path: str
) -> WatcherInstallResult:
    """Reverse an install: strip cron/scrontab markers + remove watcher files."""
    run_dir = remote_path.rstrip("/")
    marker = _marker(spec.run_id)
    _remove_cron_line(ssh_target, binary="crontab", marker=marker)
    if _is_slurm(spec.scheduler):
        _remove_cron_line(ssh_target, binary="scrontab", marker=marker)
    # Remove the shipped scripts + the per-run marker/heartbeat/alarm files.
    rm = " ".join(
        shlex.quote(f"{run_dir}/{rel}")
        for rel in (
            _WATCHER_SCRIPT_REL,
            _WATCHER_JOB_REL,
            ".hpc_watcher_status.json",
            ".hpc_watcher_ALARM",
            ".hpc_last_read",
        )
    )
    ssh_run(f"rm -f {rm} 2>/dev/null || true", ssh_target=ssh_target)
    return WatcherInstallResult(
        run_id=spec.run_id,
        action="uninstall",
        installed=False,
        mechanism="none",
        reason="cluster-side watcher uninstalled (cron/scrontab markers stripped, files removed).",
        detail=marker,
        probes={},
    )


def _status(*, spec: WatcherInstallSpec, ssh_target: str) -> WatcherInstallResult:
    """Report which rung (if any) is currently installed for this run."""
    marker = _marker(spec.run_id)
    probes: dict[str, str] = {}
    mechanism = "none"
    installed = False
    if _cron_has_marker(ssh_target, binary="crontab", marker=marker):
        mechanism, installed = "cron", True
        probes["crontab"] = "installed"
    else:
        probes["crontab"] = "absent"
    if not installed and _is_slurm(spec.scheduler):
        if _cron_has_marker(ssh_target, binary="scrontab", marker=marker):
            mechanism, installed = "scrontab", True
            probes["scrontab"] = "installed"
        else:
            probes["scrontab"] = "absent"
    reason = (
        f"cluster-side watcher present via {mechanism}."
        if installed
        else "no cluster-side watcher marker found for this run (crontab/scrontab)."
    )
    return WatcherInstallResult(
        run_id=spec.run_id,
        action="status",
        installed=installed,
        mechanism=mechanism,  # type: ignore[arg-type]
        reason=reason,
        detail=marker,
        probes=probes,
    )


@primitive(
    name="watcher-install",
    verb="mutate",
    side_effects=[
        SideEffect("ssh", "<cluster>"),
        SideEffect("scheduler-submit", "<cluster> (job rung only)"),
    ],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable, errors.RemoteCommandFailed],
    idempotent=True,
    idempotency_key="run_id",
    cli=CliShape(
        help=(
            "Install/uninstall/status a cluster-side heartbeat watcher for a run via "
            "an install-time probe ladder (crontab -> scrontab -> self-resubmitting "
            "job -> none, loud). The watcher survives the laptop and alarms when the "
            "client stops reading status."
        ),
        experiment_dir_arg=True,
        requires_ssh=True,
        args=(
            CliArg("--run-id", type=str, required=True),
            CliArg(
                "--action",
                type=str,
                default="install",
                choices=("install", "uninstall", "status"),
                help="install (default) / uninstall / status.",
            ),
            CliArg(
                "--scheduler",
                type=str,
                required=True,
                help="Backend/scheduler name (gates scrontab; supplies the job submit binary).",
            ),
            CliArg(
                "--stale-sec",
                type=int,
                default=1800,
                help="Alarm when the client's .hpc_last_read marker is older than this.",
            ),
            CliArg(
                "--interval-min",
                type=int,
                default=10,
                help="Watcher firing cadence in minutes.",
            ),
        ),
    ),
    agent_facing=True,
)
def watcher_install(
    *,
    experiment_dir: Path,
    run_id: str,
    scheduler: str,
    action: str = "install",
    stale_sec: int = 1800,
    interval_min: int = 10,
) -> dict[str, Any]:
    """Install/uninstall/report a cluster-side heartbeat watcher for *run_id*.

    Loads the run's journal record for ``ssh_target`` + ``remote_path`` (like
    ``kill``), then dispatches on *action*. Install runs the probe ladder and
    returns the mechanism that took (or a loud ``installed: false`` when none is
    available). Raises :class:`errors.SpecInvalid` if no journal record exists.
    """
    experiment_dir = Path(experiment_dir)
    spec = WatcherInstallSpec(
        run_id=run_id,
        action=action,  # type: ignore[arg-type]
        scheduler=scheduler,
        stale_sec=stale_sec,
        interval_min=interval_min,
    )
    record = load_run(experiment_dir, spec.run_id)
    if record is None:
        raise errors.SpecInvalid(f"watcher-install: no journal record for run_id {spec.run_id!r}")

    if spec.action == "status":
        result = _status(spec=spec, ssh_target=record.ssh_target)
    elif spec.action == "uninstall":
        result = _uninstall(spec=spec, ssh_target=record.ssh_target, remote_path=record.remote_path)
    else:
        result = _install(
            spec=spec,
            ssh_target=record.ssh_target,
            remote_path=record.remote_path,
            job_name=record.job_name,
        )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped
