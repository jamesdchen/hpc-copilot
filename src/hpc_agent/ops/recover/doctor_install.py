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

Idempotent by REPLACEMENT: re-installing rewrites the task definition in place
(``schtasks /Create /F /XML`` on Windows, a marker-keyed cron line rewrite on
POSIX) and reports ``already_installed`` — one task, never a duplicate, and a
task registered by an older build HEALS instead of surviving forever behind an
existence check. ``uninstall:true`` removes it (a no-op if absent). This verb
NEVER restarts or re-arms a run — it only schedules the detector.

Every OS-scheduler mechanic (the hidden-window XML, the windowless interpreter,
the install/remove/enumerate calls) lives in the ONE seam
:mod:`hpc_agent.infra.local_scheduler`, so the three consumers — this verb, the
guaranteed terminal harvest that tears the watchdog down, and ``doctor``'s
stale-watchdog probe — cannot drift apart.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.actions.doctor_install import DoctorInstallResult, DoctorInstallSpec
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra import local_scheduler
from hpc_agent.state.run_record import journal_dir, repo_hash

__all__ = [
    "doctor_install",
    "remove_watchdog_if_idle",
    "stale_watchdog_alert_messages",
    "watchdog_installed",
]

_TASK_DESCRIPTION = (
    "hpc-agent detection-only driver watchdog (design §5). Runs a local, "
    "read-only scan for stalled/orphaned runs and raises an OS notification. "
    "Never restarts or re-arms anything. Remove with `hpc-agent doctor-install "
    "--spec '{\"uninstall\": true}'`."
)


def _platform() -> str:
    """Return ``"windows"`` or ``"posix"`` (forwards to the scheduler seam)."""
    return local_scheduler.platform_kind()


def _task_name(experiment_dir: Path) -> str:
    """Stable scheduler task name / cron marker for *experiment_dir*."""
    return local_scheduler.task_name_for(repo_hash(experiment_dir))


def _write_durable_spec(experiment_dir: Path, *, notify: bool) -> Path:
    """Write the non-interactive doctor spec under the journal home; return its path.

    The scheduled command reads this instead of taking flags, so the firing is
    fully deterministic. ``notify=true`` makes the scheduled scan surface stalls
    as an OS notification (design §5). ``fleet=true`` makes the ONE unattended
    watchdog scan every journaled repo, not just the ``--experiment-dir`` it was
    installed for: the scheduled command still pins a single ``--experiment-dir``
    (that dir owns the parked / dead-worker / version-skew scans), but the §5
    stalled-driver scan then unions across sibling repos so a driver stalled
    under any journaled experiment is caught out of session.
    """
    spec_path = journal_dir(experiment_dir) / "doctor.spec.json"
    spec_path.write_text(
        json.dumps({"notify": notify, "fleet": True}, indent=2) + "\n", encoding="utf-8"
    )
    return spec_path


def _scheduled_argv(spec_path: Path, experiment_dir: Path) -> tuple[str, str]:
    """``(interpreter, argument_string)`` the scheduler runs each interval.

    Uses ``<python> -m hpc_agent`` (not the bare ``hpc-agent`` console script) so
    the command is durable regardless of PATH state inside the scheduler's
    minimal environment. Paths are quoted for spaces (Windows dirs like
    ``C:\\Users\\...\\CC Allowed`` and the journal home under the profile).

    The interpreter is the WINDOWLESS one on Windows (``pythonw.exe``): a
    console-subsystem ``python.exe`` allocates a console host on every firing,
    which is what flashed a window at the operator every 15 minutes for days
    (2026-07-30). On POSIX this is ``sys.executable`` unchanged.
    """
    py = local_scheduler.windowless_interpreter()
    exp = str(Path(experiment_dir).resolve())
    return py, f'-m hpc_agent doctor --spec "{spec_path}" --experiment-dir "{exp}"'


def _scheduled_command(spec_path: Path, experiment_dir: Path) -> str:
    """The exact non-interactive command the scheduler runs each interval."""
    py, args = _scheduled_argv(spec_path, experiment_dir)
    return f'"{py}" {args}'


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
    redundant (idempotent: re-install replaces in place), never to hide a
    missing watchdog behind a probe error.
    """
    return local_scheduler.task_exists(_task_name(experiment_dir))


#: Terminal-seat teardown — "a finished run must never leave a headless tick
#: behind". Re-exported from the scheduler seam so the ops-side name stays
#: reachable, but the RULE has exactly one definition
#: (:func:`hpc_agent.infra.local_scheduler.remove_watchdog_if_idle`): the
#: guaranteed terminal harvest lives in a different ops subject and must reach
#: the same decision without cross-importing this module.
remove_watchdog_if_idle = local_scheduler.remove_watchdog_if_idle


def stale_watchdog_alert_messages() -> list[str]:
    """Human-facing alert lines for every watchdog that outlived its reason to exist.

    The ``doctor`` surface for this class: any ``hpc-agent-*`` scheduled task
    whose durable spec is missing, or whose journal namespace holds run records
    with NONE of them live, is a tick firing into the void — the 2026-07-30
    signature (three tasks, every 15 minutes, for days after the runs finished).
    Each line names the task, WHY it reads stale, and the exact removal command.

    Never raises and never mutates: like the other ``doctor`` probes it rides the
    ``alerts`` list and does not flip ``needs_attention`` — a stale watchdog is
    operator noise plus a wasted tick, not a stalled driver.
    """
    messages: list[str] = []
    for finding in local_scheduler.scan_stale_watchdogs():
        if finding.get("reason") == "spec_missing":
            why = (
                "its durable doctor spec is GONE (the journal namespace "
                f"{finding.get('namespace')} no longer carries doctor.spec.json), so every "
                "firing does nothing"
            )
        else:
            target = finding.get("experiment_dir") or finding.get("namespace")
            why = (
                f"every run under its target ({target}) has reached a terminal state, so "
                "it has nothing left to watch"
            )
        messages.append(
            f"stale local watchdog {finding.get('task_name')}: {why}. Remove it: "
            f"{finding.get('removal_command')}"
        )
    return messages


@primitive(
    name="doctor-install",
    verb="mutate",
    side_effects=[
        SideEffect("scheduler", "Windows Task Scheduler (schtasks) | POSIX crontab"),
        SideEffect(
            "file_write",
            "~/.claude/hpc/<repo_hash>/doctor.spec.json + doctor.task.xml (Windows)",
        ),
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
    and registers a scheduler task running every ``spec.interval_minutes``. On
    Windows the registration goes through a generated Task Scheduler XML carrying
    ``<Hidden>true</Hidden>`` and a ``pythonw.exe`` action, so the tick can never
    flash a console window; a task with the same name already present is
    REPLACED (status ``already_installed`` — one task, current definition). On
    ``uninstall``: removes the task (``not_installed`` if absent).

    Raises :class:`errors.SpecInvalid` if the underlying scheduler command
    (``schtasks`` / ``crontab``) is absent or reports a failure.
    """
    experiment_dir = Path(experiment_dir)
    platform = _platform()
    task_name = _task_name(experiment_dir)
    # Spec path is written even on uninstall so the returned command/spec_path
    # stay meaningful; it is harmless (a stale spec no scheduler reads).
    spec_path = _write_durable_spec(experiment_dir, notify=spec.notify)
    namespace = spec_path.parent
    py, arguments = _scheduled_argv(spec_path, experiment_dir)
    command = _scheduled_command(spec_path, experiment_dir)

    try:
        if spec.uninstall:
            status = local_scheduler.remove_task(task_name=task_name, namespace=namespace)
        else:
            status, _xml_path = local_scheduler.install_task(
                task_name=task_name,
                namespace=namespace,
                command=py,
                arguments=arguments,
                working_dir=str(Path(experiment_dir).resolve()),
                interval_minutes=spec.interval_minutes,
                description=_TASK_DESCRIPTION,
                cron_command=command,
            )
    except FileNotFoundError as exc:
        # The read-only probes treat an absent scheduler binary as "not
        # installed" (fail-safe False). The mutating paths cannot silently
        # no-op, but must not crash the envelope as error_code='internal'
        # either (bug-sweep #41): report the declared SpecInvalid naming the
        # absent scheduler.
        raise errors.SpecInvalid(
            f"doctor-install: scheduler binary not found on this host ({exc}); the OS "
            "scheduler (schtasks / crontab) is required to (un)install the "
            "out-of-session doctor scan."
        ) from exc
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise errors.SpecInvalid(f"doctor-install: {exc}") from exc

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
