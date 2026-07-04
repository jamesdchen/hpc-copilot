"""``doctor-install`` — put the ``doctor`` watchdog on the OS scheduler (§5).

Opt-in install verb (design §5, "Decided (James, 2026-07-03)"): the
detection-only ``doctor`` scan is scheduled on Windows Task Scheduler / POSIX
``crontab`` so a missed driver-tick deadline — or an orphaned run left by a dead
session — is caught out of session. The OS scheduler is the bottom of the
watch-the-watcher recursion; it is treated as boring and reliable and is **never
auto-installed**.

The scheduled command is fully non-interactive: this verb writes a durable
``doctor.spec.json`` under the journal home (carrying ``notify=true`` so the
scheduled scan raises an OS notification, not silent JSON) and points the
scheduler at ``hpc-agent doctor --spec <that> --experiment-dir <dir>``.

Idempotent: re-installing with the same params finds the existing task and
returns ``already_installed`` (no duplicate task / cron line). ``uninstall:true``
removes it (and is a no-op if absent). This verb NEVER restarts or re-arms a
run — it only schedules the detector.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.doctor_install import DoctorInstallResult, DoctorInstallSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.state.run_record import journal_dir, repo_hash

_SCHTASKS_TIMEOUT_SEC = 15
_CRONTAB_TIMEOUT_SEC = 15


def _platform() -> str:
    """Return ``"windows"`` or ``"posix"`` (a seam tests monkeypatch)."""
    return "windows" if os.name == "nt" else "posix"


def _task_name(experiment_dir: Path) -> str:
    """Stable scheduler task name / cron marker for *experiment_dir*."""
    return f"hpc-agent-doctor-{repo_hash(experiment_dir)}"


def _write_durable_spec(experiment_dir: Path, *, notify: bool) -> Path:
    """Write the non-interactive doctor spec under the journal home; return its path.

    The scheduled command reads this instead of taking flags, so the firing is
    fully deterministic. ``notify=true`` makes the scheduled scan surface stalls
    as an OS notification (design §5).
    """
    spec_path = journal_dir(experiment_dir) / "doctor.spec.json"
    spec_path.write_text(json.dumps({"notify": notify}, indent=2) + "\n", encoding="utf-8")
    return spec_path


def _scheduled_command(spec_path: Path, experiment_dir: Path) -> str:
    """The exact non-interactive command the scheduler runs each interval.

    Uses ``<python> -m hpc_agent`` (not the bare ``hpc-agent`` console script) so
    the command is durable regardless of PATH state inside the scheduler's
    minimal environment. Paths are quoted for spaces (Windows dirs like
    ``C:\\Users\\...\\CC Allowed`` and the journal home under the profile).
    """
    py = sys.executable
    exp = str(Path(experiment_dir).resolve())
    return f'"{py}" -m hpc_agent doctor --spec "{spec_path}" --experiment-dir "{exp}"'


def _run(
    argv: list[str], *, input_text: str | None = None, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run *argv* capturing text output (utf-8). Raises on spawn failure/timeout."""
    return subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def watchdog_installed(experiment_dir: Path) -> bool:
    """Pure probe: is the §5 doctor watchdog scheduled for *experiment_dir*?

    Read-only — queries the OS scheduler (schtasks / crontab) for this
    experiment's task marker and never installs anything. Consumed by the
    ``submit-s3`` brief so the human learns, at the moment a long unattended
    wait is being armed, whether a dead session would strand the run
    undetected — with ``doctor-install`` as the recommended (opt-in,
    "never auto-installed" — design §5, decided 2026-07-03) remedy.

    A probe failure (no ``schtasks``/``crontab``, timeout) reads as ``False``:
    the fail-safe direction is to recommend an install that turns out to be
    redundant (idempotent: re-install returns ``already_installed``), never to
    hide a missing watchdog behind a probe error.
    """
    task_name = _task_name(experiment_dir)
    if _platform() == "windows":
        return _win_task_exists(task_name)
    return any(task_name in ln for ln in _cron_read_lines())


# --------------------------------------------------------------------------- #
# Windows — schtasks
# --------------------------------------------------------------------------- #
def _win_task_exists(task_name: str) -> bool:
    try:
        proc = _run(["schtasks", "/Query", "/TN", task_name], timeout=_SCHTASKS_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _win_install(task_name: str, command: str, interval: int) -> str:
    if _win_task_exists(task_name):
        return "already_installed"
    proc = _run(
        [
            "schtasks",
            "/Create",
            "/F",
            "/SC",
            "MINUTE",
            "/MO",
            str(interval),
            "/TN",
            task_name,
            "/TR",
            command,
        ],
        timeout=_SCHTASKS_TIMEOUT_SEC,
    )
    if proc.returncode != 0:
        raise errors.SpecInvalid(
            f"doctor-install: schtasks /Create failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return "installed"


def _win_uninstall(task_name: str) -> str:
    if not _win_task_exists(task_name):
        return "not_installed"
    proc = _run(["schtasks", "/Delete", "/TN", task_name, "/F"], timeout=_SCHTASKS_TIMEOUT_SEC)
    if proc.returncode != 0:
        raise errors.SpecInvalid(
            f"doctor-install: schtasks /Delete failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return "uninstalled"


# --------------------------------------------------------------------------- #
# POSIX — crontab
# --------------------------------------------------------------------------- #
def _cron_read_lines() -> list[str]:
    """Current crontab lines, or ``[]`` when the user has no crontab."""
    try:
        proc = _run(["crontab", "-l"], timeout=_CRONTAB_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        # No crontab installed for the user (crontab -l exits non-zero).
        return []
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _cron_write(lines: list[str]) -> None:
    payload = "\n".join(lines)
    if payload:
        payload += "\n"
    proc = _run(["crontab", "-"], input_text=payload, timeout=_CRONTAB_TIMEOUT_SEC)
    if proc.returncode != 0:
        raise errors.SpecInvalid(
            f"doctor-install: `crontab -` failed (rc={proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )


def _cron_install(task_name: str, command: str, interval: int) -> str:
    lines = _cron_read_lines()
    if any(task_name in ln for ln in lines):
        return "already_installed"
    lines.append(f"*/{interval} * * * * {command} # {task_name}")
    _cron_write(lines)
    return "installed"


def _cron_uninstall(task_name: str) -> str:
    lines = _cron_read_lines()
    kept = [ln for ln in lines if task_name not in ln]
    if len(kept) == len(lines):
        return "not_installed"
    _cron_write(kept)
    return "uninstalled"


@primitive(
    name="doctor-install",
    verb="mutate",
    side_effects=[
        SideEffect("scheduler", "Windows Task Scheduler (schtasks) | POSIX crontab"),
        SideEffect("file_write", "~/.claude/hpc/<repo_hash>/doctor.spec.json"),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="experiment_dir",
    cli=CliShape(
        help=(
            "Install (or uninstall) an OS-scheduled `hpc-agent doctor` scan for "
            "this experiment dir — the out-of-session half of the driver "
            "dead-man's switch (design §5). Opt-in, never auto-installed. Windows "
            "→ Task Scheduler (schtasks); POSIX → crontab. Writes a durable "
            "doctor spec under the journal home and points the scheduler at it "
            "with notify=true, so the scan alerts (OS notification) when it finds "
            "a stalled/orphaned run. Idempotent: re-running never duplicates the "
            "task. It only schedules the DETECTOR — it never re-arms a run."
        ),
        spec_arg=True,
        experiment_dir_arg=True,
        spec_model=DoctorInstallSpec,
        schema_ref=SchemaRef(input="doctor_install"),
    ),
    agent_facing=True,
)
def doctor_install(*, experiment_dir: Path, spec: DoctorInstallSpec) -> DoctorInstallResult:
    """Schedule (or remove) the out-of-session ``doctor`` scan under *experiment_dir*.

    On install: writes the durable ``doctor.spec.json`` (``notify=spec.notify``)
    and registers a scheduler task running every ``spec.interval_minutes``. A
    task with the same name already present → ``already_installed`` (no
    duplicate). On ``uninstall``: removes the task (``not_installed`` if absent).

    Raises :class:`errors.SpecInvalid` if the underlying scheduler command
    (``schtasks`` / ``crontab``) reports a failure.
    """
    experiment_dir = Path(experiment_dir)
    platform = _platform()
    task_name = _task_name(experiment_dir)
    # Spec path is written even on uninstall so the returned command/spec_path
    # stay meaningful; it is harmless (a stale spec no scheduler reads).
    spec_path = _write_durable_spec(experiment_dir, notify=spec.notify)
    command = _scheduled_command(spec_path, experiment_dir)

    if spec.uninstall:
        status = _win_uninstall(task_name) if platform == "windows" else _cron_uninstall(task_name)
    elif platform == "windows":
        status = _win_install(task_name, command, spec.interval_minutes)
    else:
        status = _cron_install(task_name, command, spec.interval_minutes)

    result_status: Any = status
    return DoctorInstallResult(
        status=result_status,
        platform=platform,  # type: ignore[arg-type]
        task_name=task_name,
        command=command,
        interval_minutes=spec.interval_minutes,
        spec_path=str(spec_path),
        notify=spec.notify,
    )
