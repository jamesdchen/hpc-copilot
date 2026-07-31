"""The ONE local OS-scheduler seam: Windows Task Scheduler / POSIX ``crontab``.

Every local *watchdog* the framework installs — today only the §5 detection-only
``doctor`` scan (:mod:`hpc_agent.ops.recover.doctor_install`) — is created,
probed, and REMOVED through this module. It lives in ``infra`` (substrate, not a
subject) precisely so the three consumers can all reach it without a
cross-subject import: the installer verb (``ops/recover``), the guaranteed
terminal harvest that must tear the watchdog down (``ops/monitor``), and the
``doctor`` stale-watchdog probe (``ops/recover``).

Three invariants this module owns, all three learned the hard way
(2026-07-30, live): three ``hpc-agent-doctor-<repo_hash>`` Scheduled Tasks were
found firing every 15 minutes with ``LogonType=InteractiveToken``, no
``<Hidden>`` element, and ``python.exe`` as the action — a console window
flashing in the operator's session every quarter hour, per task, for DAYS after
every run they were installed for had finished.

1. **WINDOW HYGIENE.** A Windows task is registered from a generated Task
   Scheduler XML carrying ``<Hidden>true</Hidden>`` AND running the *windowless*
   interpreter (``pythonw.exe``). Belt and braces: ``Hidden`` is advisory (some
   hosts and some ``ExecutionPolicy``/console configurations ignore it), while a
   GUI-subsystem interpreter has no console to allocate in the first place.
   ``schtasks /Create /TR`` cannot express ``Hidden`` at all — which is exactly
   why the original install was visible — so the XML form is the only form.

2. **LIFECYCLE.** :func:`remove_watchdog_if_idle` is wired at the guaranteed
   terminal harvest, and :func:`install_task` is idempotent by REPLACEMENT
   (``/Create /F /XML``), never by "already there, leave it": a task registered
   by an older build (visible window, wrong cadence) must HEAL on re-install,
   not survive forever behind an existence check. The harvest is not the ONLY
   terminal — the bulk/never-actuated closure paths
   (``reconcile_stale._close_record``, ``reconcile._never_actuated_abandon``,
   ``reconcile._safe_resubmit``, ``ops/supersession``) mark a run terminal
   WITHOUT harvesting, and deliberately so. Those are covered by
   :func:`scan_stale_watchdogs`, the ``doctor`` backstop that finds any task
   whose namespace has gone all-terminal however it got there.

3. **NO REAL SCHEDULER IN TESTS.** Every scheduler process spawn goes through the
   single :func:`_run` seam, so tests inject a fake ``schtasks``/``crontab``
   instead of mutating the CI machine's real task list.

**Marker-gated spawns.** ``schtasks`` is expensive — a cold ``/Query`` measured
**24.6 s** on the incident machine — and both the staleness sweep and the
terminal teardown would otherwise pay it on every call, including the
overwhelmingly common case where this machine has never installed a watchdog at
all. Both are therefore gated on a LOCAL MARKER first
(:func:`watchdog_marker_hashes` / :func:`namespace_has_marker`): a pure
filesystem probe for the durable ``doctor.spec.json`` / ``doctor.task.xml`` the
installer writes under the journal namespace. No marker anywhere → **zero
subprocess**, so a ``doctor`` run on a watchdog-free machine (and every test that
never installed one) never spawns a scheduler process. The marker gate is also
what makes the timeout honest: :data:`SCHTASKS_TIMEOUT_SEC` is sized ABOVE the
measured cold cost rather than below it, because a sweep that silently times out
after paying 15 s is worse than either honest choice.

POSIX is an honest no-op for the window legs: a ``crontab`` line has no console
to hide and no windowless interpreter to pick, so :func:`windowless_interpreter`
returns ``sys.executable`` unchanged and no XML is written. The lifecycle and
staleness legs are fully live on POSIX.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

__all__ = [
    "TASK_NAME_PREFIX",
    "compose_removal_command",
    "cron_lines",
    "cron_read",
    "install_task",
    "installed_task_names",
    "live_run_count",
    "namespace_for_task",
    "namespace_has_marker",
    "remove_task",
    "remove_watchdog_if_idle",
    "scan_stale_watchdogs",
    "task_exists",
    "task_name_for",
    "task_xml",
    "task_xml_path",
    "watchdog_marker_hashes",
    "windowless_interpreter",
    "write_task_xml",
]

#: Sized ABOVE the measured cold cost of ``schtasks /Query`` on the incident
#: machine (**24.6 s** — the Task Scheduler service's first-call warm-up), not
#: below it. The previous 15 s budget was a silent-failure generator: the sweep
#: would pay the full 15 s and then time out having learned nothing, which reads
#: identically to "no stale watchdogs". Spawns are marker-gated (see the module
#: docstring), so this budget is only ever paid on a machine that actually has a
#: watchdog installed.
SCHTASKS_TIMEOUT_SEC = 40
CRONTAB_TIMEOUT_SEC = 15

#: Every local watchdog task/cron marker this framework registers starts here,
#: so the stale-watchdog sweep can enumerate them without knowing which verb
#: installed which. ``hpc-agent-doctor-<repo_hash>`` is the only member today.
TASK_NAME_PREFIX = "hpc-agent-"
_DOCTOR_PREFIX = "hpc-agent-doctor-"

#: File name of the generated Task Scheduler XML, written beside the durable
#: doctor spec under the journal namespace so ``/Create /XML`` has a stable path
#: and a human can read exactly what was registered.
TASK_XML_NAME = "doctor.task.xml"

#: A StartBoundary safely in the past so the repetition begins at registration
#: rather than waiting for a future first firing. The repetition itself carries
#: no ``<Duration>``, which Task Scheduler reads as "repeat indefinitely".
_START_BOUNDARY = "2020-01-01T00:00:00"


def platform_kind() -> str:
    """Return ``"windows"`` or ``"posix"`` (a seam tests monkeypatch)."""
    return "windows" if os.name == "nt" else "posix"


def _run(
    argv: list[str], *, input_text: str | None = None, timeout: int
) -> subprocess.CompletedProcess[str]:
    """Run *argv* capturing text output (utf-8) — the ONE scheduler spawn seam.

    Raises on spawn failure / timeout; callers decide whether that reads as
    "not installed" (the read-only probes, fail-safe) or as a structured error
    (the mutating paths).
    """
    return subprocess.run(
        argv,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
    )


def task_name_for(repo_hash_value: str) -> str:
    """The stable watchdog task name / cron marker for a journal repo hash."""
    return f"{_DOCTOR_PREFIX}{repo_hash_value}"


# --------------------------------------------------------------------------- #
# Local markers — the gate that keeps `schtasks` off the common path
# --------------------------------------------------------------------------- #
#: The durable files a watchdog install leaves under its journal namespace. Both
#: are checked because they cover different eras: the CURRENT installer writes
#: the task XML, while a task registered by the pre-2026-07-30 build left only
#: ``doctor.spec.json`` — and those are exactly the stale tasks the sweep exists
#: to find, so gating on the XML alone would make the sweep blind to the very
#: incident that motivated it.
_MARKER_NAMES: tuple[str, ...] = ("doctor.spec.json", TASK_XML_NAME)


def namespace_has_marker(namespace: Path) -> bool:
    """Does this journal namespace carry a watchdog install marker? (pure FS read)"""
    base = Path(namespace)
    return any((base / name).is_file() for name in _MARKER_NAMES)


def watchdog_marker_hashes() -> list[str]:
    """Journal namespaces (repo-hash dir names) carrying a watchdog marker.

    The cheap gate in front of every ``schtasks``/``crontab`` enumeration: a few
    ``stat`` calls over the journal home instead of a subprocess that costs
    ~25 s cold. An EMPTY result means this machine has never installed a
    watchdog, so there is provably nothing for the sweep to find and the
    scheduler is never invoked.

    Note the gate is machine-wide, not per-namespace: once ANY namespace carries
    a marker the full task list is enumerated, so a task whose own
    ``doctor.spec.json`` was deleted (the ``spec_missing`` signature) is still
    discovered. Fail-safe on an unreadable journal home: ``[]`` (no spawn), which
    is the same answer a machine with no watchdogs gives.
    """
    try:
        children = sorted(p for p in _journal_home().iterdir() if p.is_dir())
    except OSError:
        return []
    return [p.name for p in children if namespace_has_marker(p)]


# --------------------------------------------------------------------------- #
# Window hygiene
# --------------------------------------------------------------------------- #
def windowless_interpreter() -> str:
    """Interpreter for a scheduled tick that must never open a console window.

    On Windows, ``python.exe`` is a CONSOLE-subsystem binary: every firing of a
    Scheduled Task running it allocates a console host, and that host is what the
    operator saw flash every 15 minutes. Its GUI-subsystem twin ``pythonw.exe``
    ships beside it in every CPython install and venv ``Scripts/`` dir and has no
    console to allocate — so it stays dark even on the hosts that ignore the
    task's ``<Hidden>`` flag.

    Returns the sibling ``<stem>w<suffix>`` when it exists on disk, the current
    interpreter when it is already windowless, and — fail-open — the current
    interpreter when no twin is found (``<Hidden>true</Hidden>`` still applies;
    a missing ``pythonw.exe`` must never make the install fail).

    On POSIX there is no window and no twin: this returns ``sys.executable``
    verbatim, so the cron leg is byte-identical to before.
    """
    if platform_kind() != "windows":
        # Returned VERBATIM (not through ``Path``): there is nothing to resolve,
        # and round-tripping a POSIX path through a Windows ``Path`` would rewrite
        # its separators.
        return sys.executable
    exe = Path(sys.executable)
    if exe.stem.lower().endswith("w"):
        return str(exe)
    candidate = exe.with_name(f"{exe.stem}w{exe.suffix}")
    return str(candidate) if candidate.is_file() else str(exe)


def task_xml(
    *,
    task_name: str,
    command: str,
    arguments: str,
    working_dir: str,
    interval_minutes: int,
    description: str,
) -> str:
    """Compose the Task Scheduler XML registering a HIDDEN, repeating task.

    ``<Hidden>true</Hidden>`` is the whole point of using the XML form: the
    ``schtasks /Create /TR "<cmd>"`` shorthand has no flag for it, so a task
    created that way is visible by construction. ``LogonType`` stays
    ``InteractiveToken`` (the task must run as the logged-on user to read their
    journal home and raise their OS notifications) — Hidden + a windowless
    ``<Command>`` are what make that safe rather than intrusive.

    The repetition carries no ``<Duration>``, which Task Scheduler reads as
    "indefinitely", and ``<StartBoundary>`` sits in the past so the first
    repetition is due immediately after registration.
    """
    esc = _xml_escape
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Description>{esc(description)}</Description>\n"
        f"    <URI>\\{esc(task_name)}</URI>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <TimeTrigger>\n"
        f"      <StartBoundary>{_START_BOUNDARY}</StartBoundary>\n"
        "      <Enabled>true</Enabled>\n"
        "      <Repetition>\n"
        f"        <Interval>PT{int(interval_minutes)}M</Interval>\n"
        "        <StopAtDurationEnd>false</StopAtDurationEnd>\n"
        "      </Repetition>\n"
        "    </TimeTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <Hidden>true</Hidden>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <Enabled>true</Enabled>\n"
        "    <ExecutionTimeLimit>PT10M</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{esc(command)}</Command>\n"
        f"      <Arguments>{esc(arguments)}</Arguments>\n"
        f"      <WorkingDirectory>{esc(working_dir)}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def task_xml_path(namespace: Path) -> Path:
    """Where the generated task XML lives for a journal *namespace* dir."""
    return Path(namespace) / TASK_XML_NAME


def write_task_xml(namespace: Path, xml: str) -> Path:
    """Write *xml* as UTF-16 (with BOM) and return its path.

    ``schtasks /Create /XML`` is documented for Unicode task files and rejects
    some UTF-8 payloads with a bare "the task XML contains a value which is
    incorrectly formatted" — so the encoding is pinned, and the XML declaration
    written by :func:`task_xml` says ``UTF-16`` to match. An XML parser reading
    the FILE (bytes) therefore round-trips; only a parser handed the decoded
    ``str`` would object to the declaration.
    """
    path = task_xml_path(namespace)
    path.write_text(xml, encoding="utf-16")
    return path


# --------------------------------------------------------------------------- #
# Windows — schtasks
# --------------------------------------------------------------------------- #
def _win_task_exists(task_name: str) -> bool:
    try:
        proc = _run(["schtasks", "/Query", "/TN", task_name], timeout=SCHTASKS_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _win_list_task_names() -> list[str]:
    """Every registered task name (best-effort; ``[]`` on any probe failure)."""
    try:
        proc = _run(
            ["schtasks", "/Query", "/FO", "CSV", "/NH"],
            timeout=SCHTASKS_TIMEOUT_SEC,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    names: list[str] = []
    for line in (proc.stdout or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        first = raw.split(",")[0].strip().strip('"')
        if not first or first.lower() == "taskname":
            continue
        names.append(first.lstrip("\\"))
    return names


def _win_delete(task_name: str) -> subprocess.CompletedProcess[str]:
    return _run(["schtasks", "/Delete", "/TN", task_name, "/F"], timeout=SCHTASKS_TIMEOUT_SEC)


def _win_create_from_xml(task_name: str, xml_path: Path) -> subprocess.CompletedProcess[str]:
    # ``/F`` makes this a REPLACE, so a re-install heals a task registered by an
    # older build (visible window / stale cadence) instead of leaving it in place.
    return _run(
        ["schtasks", "/Create", "/F", "/TN", task_name, "/XML", str(xml_path)],
        timeout=SCHTASKS_TIMEOUT_SEC,
    )


# --------------------------------------------------------------------------- #
# POSIX — crontab
# --------------------------------------------------------------------------- #
#: The ONLY non-zero ``crontab -l`` outputs that positively mean "this user has
#: an EMPTY table" (vixie-cron / cronie / BSD phrasings). Everything else on a
#: non-zero exit — a permission denial, an NIS/LDAP/NFS blip, or NO OUTPUT AT
#: ALL — is treated as unreadable, because a read-modify-write that guesses
#: "empty" destroys every entry the user has. See :func:`cron_read`.
_EMPTY_TABLE_SIGNATURES: tuple[str, ...] = ("no crontab for", "no crontab set")


def cron_read() -> tuple[bool, list[str]]:
    """``(readable, lines)`` — the FAIL-CLOSED crontab read the write paths use.

    ``crontab -l`` exits non-zero in two very different situations: the user
    simply has no crontab (an EMPTY table — proceed, output is empty or the
    recognized "no crontab for <user>" message), and a genuine read failure
    (NIS/LDAP/NFS blip, permissions). Collapsing both to ``[]`` is bug-sweep #45:
    a read-modify-write that treats a transient failure as an empty table
    REPLACES the user's entire crontab with just our line.

    That distinction matters more now than it did: install rewrites the marker
    line on EVERY call (idempotent-by-replacement), so the write path runs even
    when the table already carries our marker. ``readable=False`` means "do not
    write" — the caller leaves the table untouched.

    The non-zero branch is **allow-listed, not deny-listed**: a non-zero exit is
    treated as an empty table ONLY when the output positively matches a
    recognized empty-table message. Anything else — including a SILENT non-zero
    exit with no output at all — refuses. That direction is deliberate: a silent
    ``crontab -l`` failure is indistinguishable from a permission denial
    (``you (x) are not allowed to use this program`` is not guaranteed to be
    printed to a captured pipe), and the cost of guessing wrong is destroying
    every cron entry the user has. The cost of guessing conservatively is a LOUD
    refusal naming the reason — including, on an exotic locale whose empty-table
    message we do not recognize, a first install that refuses instead of
    proceeding. Loud refusal beats silent destruction; the error text tells the
    human exactly what to do.

    Lines are returned VERBATIM — blanks and comments included — because the
    caller rebuilds the table from them. Filtering "empty" lines here would
    silently reformat the user's crontab as a side effect of installing a
    watchdog (L4).
    """
    try:
        proc = _run(["crontab", "-l"], timeout=CRONTAB_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError):
        return False, []
    if proc.returncode != 0:
        blob = ((proc.stdout or "") + " " + (proc.stderr or "")).strip().lower()
        if any(sig in blob for sig in _EMPTY_TABLE_SIGNATURES):
            return True, []  # positively recognized empty table — safe to proceed
        return False, []  # unreadable, refused, or silent — never clobber the table
    # rc == 0: the table was read successfully. An empty stdout here IS an empty
    # (but existing) table, which is unambiguous and safe.
    return True, proc.stdout.splitlines()


def cron_lines() -> list[str]:
    """Current crontab lines, or ``[]`` when unreadable (READ-ONLY callers only).

    The lossy projection of :func:`cron_read` for probes that only ask "is our
    marker there?" — where an unreadable table honestly reads as "not visible".
    Never use it to build a table you are about to WRITE: see :func:`cron_read`.
    """
    _readable, lines = cron_read()
    return lines


def _cron_write(lines: list[str]) -> subprocess.CompletedProcess[str]:
    payload = "\n".join(lines)
    if payload:
        payload += "\n"
    return _run(["crontab", "-"], input_text=payload, timeout=CRONTAB_TIMEOUT_SEC)


# --------------------------------------------------------------------------- #
# The seam's public verbs
# --------------------------------------------------------------------------- #
def task_exists(task_name: str) -> bool:
    """Read-only: is *task_name* registered with the local OS scheduler?

    A probe failure (no ``schtasks``/``crontab``, timeout) reads as ``False``:
    the fail-safe direction is to recommend an install that turns out to be
    redundant (install is idempotent), never to hide a missing watchdog behind a
    probe error.
    """
    if platform_kind() == "windows":
        return _win_task_exists(task_name)
    return any(task_name in ln for ln in cron_lines())


def installed_task_names() -> list[str]:
    """Every registered ``hpc-agent-*`` watchdog name (best-effort, read-only).

    The enumeration the stale sweep walks. Windows reads the CSV task list;
    POSIX extracts the marker comment from each crontab line. Any probe failure
    yields ``[]`` — a sweep that cannot see the scheduler reports nothing rather
    than inventing staleness.
    """
    if platform_kind() == "windows":
        return [n for n in _win_list_task_names() if n.startswith(TASK_NAME_PREFIX)]
    names: list[str] = []
    for line in cron_lines():
        for token in line.replace("#", " ").split():
            if token.startswith(TASK_NAME_PREFIX) and token not in names:
                names.append(token)
    return names


def install_task(
    *,
    task_name: str,
    namespace: Path,
    command: str,
    arguments: str,
    working_dir: str,
    interval_minutes: int,
    description: str,
    cron_command: str,
) -> tuple[str, Path | None]:
    """Register (or REPLACE) the watchdog task; return ``(status, xml_path)``.

    ``status`` is ``"installed"`` when nothing was registered before and
    ``"already_installed"`` when a task of the same name was found — but BOTH
    paths write the current definition, because idempotence here means "one task,
    matching the current build", not "leave whatever is there alone". The old
    existence-check-and-return left the 2026-07-30 visible-window tasks alive
    across every subsequent re-install.

    Raises :class:`OSError` / :class:`subprocess.SubprocessError` from the seam,
    and returns a non-zero-rc process to the caller via a raised
    :class:`RuntimeError` carrying the scheduler's own diagnostic — the calling
    verb converts that into its declared structured error.
    """
    existed = task_exists(task_name)
    if platform_kind() == "windows":
        xml = task_xml(
            task_name=task_name,
            command=command,
            arguments=arguments,
            working_dir=working_dir,
            interval_minutes=interval_minutes,
            description=description,
        )
        xml_path = write_task_xml(namespace, xml)
        proc = _win_create_from_xml(task_name, xml_path)
        if proc.returncode != 0:
            raise RuntimeError(
                f"schtasks /Create /XML failed (rc={proc.returncode}): "
                f"{(proc.stderr or '').strip() or (proc.stdout or '').strip()}"
            )
        return ("already_installed" if existed else "installed"), xml_path

    # POSIX: rewrite the marker line in place (strip any prior, append fresh) so
    # a re-install replaces rather than duplicates, exactly like /Create /F.
    # FAIL-CLOSED (bug-sweep #45): a transient `crontab -l` failure must NOT read
    # as an empty table here — writing what we then build would replace the
    # user's whole crontab with our single line.
    readable, current = cron_read()
    if not readable:
        raise RuntimeError(
            "`crontab -l` could not be read, so the current table is unknown; "
            "refusing to write (a blind write would replace every other cron entry "
            "with this one line)."
        )
    lines = [ln for ln in current if task_name not in ln]
    lines.append(f"*/{int(interval_minutes)} * * * * {cron_command} # {task_name}")
    proc = _cron_write(lines)
    if proc.returncode != 0:
        raise RuntimeError(
            f"`crontab -` failed (rc={proc.returncode}): "
            f"{(proc.stderr or '').strip() or (proc.stdout or '').strip()}"
        )
    return ("already_installed" if existed else "installed"), None


def remove_task(*, task_name: str, namespace: Path | None = None) -> str:
    """Remove the watchdog task; ``"uninstalled"`` or ``"not_installed"``.

    Removing an absent task is a no-op by contract — every terminal seat calls
    this unconditionally, so "there was nothing to remove" must be a normal
    answer rather than an error. Raises :class:`RuntimeError` when the scheduler
    itself reports a failure.
    """
    if platform_kind() == "windows":
        if not _win_task_exists(task_name):
            return "not_installed"
        proc = _win_delete(task_name)
        if proc.returncode != 0:
            raise RuntimeError(
                f"schtasks /Delete failed (rc={proc.returncode}): "
                f"{(proc.stderr or '').strip() or (proc.stdout or '').strip()}"
            )
        if namespace is not None:
            # The registration is gone; a leftover XML is inert, so its removal
            # is a tidy-up, never a failure mode.
            with contextlib.suppress(OSError):
                task_xml_path(namespace).unlink()
        return "uninstalled"

    # Same fail-closed read on the remove path: an unreadable table means the
    # marker's presence is unknown, and "not_installed" would be a lie that also
    # skips the write — which is the safe half. Reporting it is the honest half.
    readable, lines = cron_read()
    if not readable:
        raise RuntimeError(
            "`crontab -l` could not be read, so the current table is unknown; "
            "refusing to rewrite it to remove the watchdog marker."
        )
    kept = [ln for ln in lines if task_name not in ln]
    if len(kept) == len(lines):
        return "not_installed"
    proc = _cron_write(kept)
    if proc.returncode != 0:
        raise RuntimeError(
            f"`crontab -` failed (rc={proc.returncode}): "
            f"{(proc.stderr or '').strip() or (proc.stdout or '').strip()}"
        )
    return "uninstalled"


def remove_watchdog_if_idle(experiment_dir: Path) -> dict[str, Any]:
    """THE terminal-seat teardown: remove this repo's watchdog once nothing is live.

    ONE definition, three callers: the guaranteed terminal harvest
    (``ops/monitor/harvest_guard``), the ``doctor-install`` verb's re-export, and
    any future terminal seat. It lives here — in the scheduler seam, not in the
    installer verb — precisely so a subject that must not cross-import
    ``ops/recover`` can still reach the ONE rule instead of re-deriving "is this
    experiment idle?" locally (the drift that lets two seats disagree about when
    a watchdog may die).

    This rule and the harness-cron ``arm="none"`` rule are two applications of
    one principle ("a finished run must never leave a headless tick behind"), NOT
    copies of one another — they share no code, no vocabulary, and no data, and
    they deliberately differ where the watchdogs differ: the harness cron is
    per-RUN, so it dies when its run ends, while this task is namespaced per
    REPO, so its terminal is "no live run remains in this experiment's journal
    namespace". Tearing it down on the per-run trigger would blind the
    dead-man's switch for a sibling run still in flight, which is the failure the
    watchdog exists to prevent. Nothing here needs to stay in sync with
    ``decide_monitor_arm``; only the principle is shared.

    NEVER raises: it is called from a ``finally``-reachable terminal path, so a
    refused scheduler, an unreadable journal, or a host with no
    ``schtasks``/``crontab`` is reported in the returned record instead.

    Two gates keep the common path free of a subprocess, in order: no install
    MARKER under this namespace → nothing was ever scheduled here, return
    immediately (this is what keeps the terminal harvest — and every test that
    never installed a watchdog — from spawning a ~25 s ``schtasks`` query); then
    the live-run count, a local journal read. The scheduler is touched only when
    a watchdog really was installed AND the namespace is genuinely quiet.

    Returns ``{"removed", "status", "reason", "task_name", "live_runs"}``.
    """
    record: dict[str, Any] = {
        "removed": False,
        "status": "skipped",
        "reason": "",
        "task_name": "",
        "live_runs": 0,
    }
    try:
        # L1: imported INSIDE the try — this function's contract is "never
        # raises", and an import is as capable of raising as anything below it.
        from hpc_agent.state.run_record import journal_root_if_exists, repo_hash

        task_name = task_name_for(repo_hash(experiment_dir))
        record["task_name"] = task_name
        namespace = journal_root_if_exists(experiment_dir)
        if not namespace_has_marker(namespace):
            record["status"] = "not_installed"
            record["reason"] = (
                "no watchdog install marker under this experiment's journal "
                "namespace — nothing was ever scheduled here, so the scheduler is "
                "not queried."
            )
            return record
        live = live_run_count(namespace)
        record["live_runs"] = live
        if live:
            record["reason"] = (
                f"{live} live run(s) remain in this experiment's journal namespace — "
                "the watchdog still has something to watch."
            )
            return record
        status = remove_task(task_name=task_name, namespace=namespace)
        record["status"] = status
        record["removed"] = status == "uninstalled"
        record["reason"] = (
            "no live run remains — the out-of-session watchdog was removed so no "
            "headless tick outlives the work that armed it."
            if record["removed"]
            else "no watchdog was scheduled for this experiment dir."
        )
    except Exception as exc:  # noqa: BLE001 — a teardown must never mask a terminal cause
        record["status"] = "error"
        record["reason"] = f"watchdog teardown failed ({type(exc).__name__}: {exc})"
    return record


def compose_removal_command(task_name: str, *, experiment_dir: str | None) -> str:
    """The exact command a human can paste to remove *task_name*.

    Prefers the framework verb when the target experiment dir is still on disk
    (it also tidies the durable spec + generated XML); falls back to the raw
    scheduler command when the dir is gone — which is precisely the case where
    the verb could not be pointed at it anyway.

    QUOTING IS PER-SHELL and this string cannot satisfy every shell at once, so
    it targets the shell each branch's tool actually lives in: the Windows
    fallback uses ``"..."`` (cmd.exe / PowerShell both accept it), the POSIX
    fallback uses ``'...'`` (sh/bash), and the verb form embeds a single-quoted
    JSON spec — which is correct for sh/bash but NOT for cmd.exe, where the
    operator must swap the outer quotes. It is a paste-ready HINT for a human,
    never a string any code execs; nothing here shells out on the operator's
    behalf, so a mis-quoted paste fails visibly at their prompt rather than
    running something unintended.
    """
    if experiment_dir and Path(experiment_dir).exists():
        return (
            f'hpc-agent doctor-install --experiment-dir "{experiment_dir}" '
            "--spec '{\"uninstall\": true}'"
        )
    if platform_kind() == "windows":
        return f'schtasks /Delete /TN "{task_name}" /F'
    return f"crontab -l | grep -vF '{task_name}' | crontab -"


# --------------------------------------------------------------------------- #
# Staleness — "is anything still watching a run that is over?"
# --------------------------------------------------------------------------- #
def _journal_home() -> Path:
    from hpc_agent.state.run_record import current_homedir

    return current_homedir()


def namespace_for_task(task_name: str) -> Path | None:
    """The journal namespace dir a ``hpc-agent-doctor-<repo_hash>`` task watches."""
    if not task_name.startswith(_DOCTOR_PREFIX):
        return None
    repo_hash_value = task_name[len(_DOCTOR_PREFIX) :]
    if not repo_hash_value:
        return None
    return _journal_home() / repo_hash_value


def live_run_count(namespace: Path) -> int:
    """Number of NON-terminal run records in a journal *namespace* (0 if absent).

    Read-only and NON-CREATING: it takes the namespace dir directly rather than
    an ``experiment_dir``, so it can answer for a namespace whose experiment dir
    has been deleted, and it never scaffolds a ghost namespace on read (the F46
    ``journal_dir``-creates-what-it-tests trap).
    """
    from hpc_agent._kernel.contract.vocabulary import TERMINAL_STATUSES

    terminal = {str(s) for s in TERMINAL_STATUSES}
    runs = Path(namespace) / "runs"
    if not runs.is_dir():
        return 0
    live = 0
    for path in sorted(runs.glob("*.json")):
        if path.name.endswith(".last_status.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("status") or "in_flight") not in terminal:
            live += 1
    return live


def _namespace_experiment_dir(namespace: Path) -> str | None:
    try:
        payload = json.loads((Path(namespace) / "repo.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("experiment_dir")
    return value if isinstance(value, str) and value else None


def scan_stale_watchdogs() -> list[dict[str, Any]]:
    """Every registered watchdog whose reason to exist is gone.

    Two staleness signatures, both read from LOCAL state only (no SSH, no
    mutation):

    * **spec missing** — the durable ``doctor.spec.json`` the scheduled command
      reads is not on disk (the namespace was cleaned up, or the journal home
      moved). The tick still fires; it just does nothing useful.
    * **target terminal** — the namespace holds run records and NONE of them is
      live. This is the 2026-07-30 signature: the runs finished, the operator
      moved on, and the 15-minute tick kept firing for days.

    A namespace with a readable spec and at least one live run is NOT reported.
    Neither is a namespace that has never held a run record (a freshly installed
    watchdog on a repo that has not submitted yet is doing exactly its job).

    Never raises: an unreadable scheduler, journal home, or namespace yields no
    findings rather than a broken ``doctor`` scan.
    """
    findings: list[dict[str, Any]] = []
    # MARKER GATE: no watchdog was ever installed on this machine → provably
    # nothing to sweep, and we skip the ~25 s cold `schtasks /Query` entirely.
    # This is what keeps `doctor` free of a scheduler subprocess on the common
    # path (and what keeps every test that never installed a watchdog from
    # spawning one).
    if not watchdog_marker_hashes():
        return []
    try:
        names = installed_task_names()
    except Exception:  # noqa: BLE001 — a probe must never break the doctor scan
        return []
    for name in names:
        try:
            namespace = namespace_for_task(name)
            if namespace is None:
                continue
            experiment_dir = _namespace_experiment_dir(namespace)
            spec_present = (namespace / "doctor.spec.json").is_file()
            if not spec_present:
                findings.append(
                    {
                        "task_name": name,
                        "namespace": str(namespace),
                        "experiment_dir": experiment_dir,
                        "reason": "spec_missing",
                        "live_runs": 0,
                        "removal_command": compose_removal_command(
                            name, experiment_dir=experiment_dir
                        ),
                    }
                )
                continue
            runs_dir_path = namespace / "runs"
            if not runs_dir_path.is_dir():
                continue
            recorded = [
                p for p in runs_dir_path.glob("*.json") if not p.name.endswith(".last_status.json")
            ]
            if not recorded:
                continue
            live = live_run_count(namespace)
            if live:
                continue
            findings.append(
                {
                    "task_name": name,
                    "namespace": str(namespace),
                    "experiment_dir": experiment_dir,
                    "reason": "target_terminal",
                    "live_runs": 0,
                    "removal_command": compose_removal_command(name, experiment_dir=experiment_dir),
                }
            )
        except Exception:  # noqa: BLE001 — one torn namespace must not blind the sweep
            continue
    return findings
