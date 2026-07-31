"""Tests for the ONE local OS-scheduler seam (``infra/local_scheduler``).

The 2026-07-30 live class: three ``hpc-agent-doctor-<repo_hash>`` Windows
Scheduled Tasks were firing every 15 minutes with ``LogonType=InteractiveToken``
and NO ``<Hidden>`` element, running console-subsystem ``python.exe`` — a visible
window in the operator's session every quarter hour, per task, for days after
every run they watched had finished.

Three legs are pinned here, one per failure:

* **window hygiene** — the generated task XML carries ``<Hidden>true</Hidden>``
  AND a windowless ``<Command>``; both, because ``Hidden`` is advisory on some
  hosts and a GUI-subsystem interpreter has no console to allocate at all.
* **lifecycle** — install REPLACES (never duplicates), and the idle teardown
  removes the task exactly when no live run remains.
* **staleness** — the sweep finds a task whose target is terminal or whose spec
  is gone, and stays silent on a live one.

No test touches a real scheduler: every spawn goes through the single
``local_scheduler._run`` seam, which is monkeypatched throughout.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest

from hpc_agent.infra import local_scheduler as ls

_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


def _cp(argv: list[str], *, rc: int = 0, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=argv, returncode=rc, stdout=stdout, stderr="")


class _FakeSchtasks:
    """A schtasks stand-in over one in-memory task list (names only)."""

    def __init__(self, tasks: list[str] | None = None) -> None:
        self.tasks: list[str] = list(tasks or [])
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], *, input_text: str | None = None, timeout: int):
        self.calls.append(argv)
        head = argv[:2]
        if head == ["schtasks", "/Query"]:
            if "/FO" in argv:
                body = "".join(f'"\\{n}","N/A","Ready"\n' for n in self.tasks)
                return _cp(argv, rc=0, stdout=body)
            name = argv[argv.index("/TN") + 1]
            return _cp(argv, rc=0 if name in self.tasks else 1)
        if head == ["schtasks", "/Create"]:
            name = argv[argv.index("/TN") + 1]
            self.tasks = [t for t in self.tasks if t != name] + [name]
            return _cp(argv, rc=0)
        if head == ["schtasks", "/Delete"]:
            name = argv[argv.index("/TN") + 1]
            self.tasks = [t for t in self.tasks if t != name]
            return _cp(argv, rc=0)
        raise AssertionError(f"unexpected argv {argv}")


class _FakeCrontab:
    """A crontab stand-in over one in-memory table body."""

    def __init__(self, content: str | None = None) -> None:
        self.content = content
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv: list[str], *, input_text: str | None = None, timeout: int):
        self.calls.append((argv, input_text))
        if argv == ["crontab", "-l"]:
            if self.content is None:
                # Real vixie-cron/cronie phrasing for "this user has no table".
                # rc!=0 with NO output is now deliberately read as UNREADABLE,
                # so the fake must emit what the real binary emits.
                return _cp(argv, rc=1, stdout="no crontab for james")
            return _cp(argv, rc=0, stdout=self.content)
        if argv == ["crontab", "-"]:
            self.content = input_text
            return _cp(argv, rc=0)
        raise AssertionError(f"unexpected argv {argv}")


def _install_windows(
    namespace: Path, *, interval: int = 15, command: str = r"C:\py\pythonw.exe"
) -> tuple[str, Path | None]:
    return ls.install_task(
        task_name="hpc-agent-doctor-abc123abc123",
        namespace=namespace,
        command=command,
        arguments='-m hpc_agent doctor --spec "s.json" --experiment-dir "C:\\CC Allowed\\e"',
        working_dir=r"C:\CC Allowed\e",
        interval_minutes=interval,
        description="watchdog & friends <test>",
        cron_command="unused-on-windows",
    )


# --------------------------------------------------------------------------- #
# (a) WINDOW HYGIENE
# --------------------------------------------------------------------------- #
def test_generated_task_xml_is_hidden_and_windowless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two window-hygiene facts are pinned on the PARSED XML, not a substring.

    ``<Hidden>true</Hidden>`` is unrepresentable through ``schtasks /Create /TR``
    — the shorthand the original installer used — which is why the visible task
    could exist at all. And the action must be the GUI-subsystem interpreter, so
    a host that ignores Hidden still has no console to open.
    """
    fake = _FakeSchtasks()
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)

    status, xml_path = _install_windows(tmp_path, interval=15)
    assert status == "installed"
    assert xml_path is not None

    # Parse the FILE (bytes): the declaration says UTF-16 and the file is written
    # as UTF-16, so a parser that honours the declaration round-trips it.
    root = ElementTree.parse(xml_path).getroot()

    hidden = root.find(f"{_NS}Settings/{_NS}Hidden")
    assert hidden is not None, "the task XML must carry a <Hidden> element"
    assert hidden.text == "true"

    command = root.find(f"{_NS}Actions/{_NS}Exec/{_NS}Command")
    assert command is not None
    assert Path(command.text or "").name.lower() == "pythonw.exe"

    # The cadence rides the XML too (the /TR form could only express it via /MO).
    interval = root.find(f"{_NS}Triggers/{_NS}TimeTrigger/{_NS}Repetition/{_NS}Interval")
    assert interval is not None and interval.text == "PT15M"

    # The registration goes through /XML — never /TR, which cannot hide a window.
    create = next(c for c in fake.calls if c[:2] == ["schtasks", "/Create"])
    assert "/XML" in create
    assert "/TR" not in create


def test_task_xml_escapes_paths_and_survives_parse() -> None:
    """A path or description carrying XML metacharacters must not break the file."""
    xml = ls.task_xml(
        task_name="hpc-agent-doctor-deadbeef",
        command=r"C:\a & b\pythonw.exe",
        arguments='-m hpc_agent doctor --spec "C:\\x<y>\\s.json"',
        working_dir=r"C:\a & b",
        interval_minutes=5,
        description="watch & report <stalls>",
    )
    assert "<Hidden>true</Hidden>" in xml
    root = ElementTree.fromstring(xml.split("\n", 1)[1])  # drop the encoding decl
    args = root.find(f"{_NS}Actions/{_NS}Exec/{_NS}Arguments")
    assert args is not None and "<y>" in (args.text or "")


def test_windowless_interpreter_prefers_the_pythonw_twin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_text("", encoding="utf-8")
    (scripts / "pythonw.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls.sys, "executable", str(scripts / "python.exe"))
    assert Path(ls.windowless_interpreter()).name == "pythonw.exe"


def test_windowless_interpreter_falls_back_when_no_twin_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing ``pythonw.exe`` must never fail the install — Hidden still applies."""
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls.sys, "executable", str(scripts / "python.exe"))
    assert Path(ls.windowless_interpreter()).name == "python.exe"


def test_posix_window_legs_are_an_honest_no_op(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX cron has no window: no XML is written and the interpreter is unchanged.

    The no-op is asserted POSITIVELY (no file, same interpreter) rather than
    assumed, so a future Windows-only change cannot start writing an inert task
    XML into every POSIX journal home.
    """
    fake = _FakeCrontab()
    monkeypatch.setattr(ls, "platform_kind", lambda: "posix")
    monkeypatch.setattr(ls, "_run", fake)
    monkeypatch.setattr(ls.sys, "executable", "/usr/bin/python3")

    assert ls.windowless_interpreter() == "/usr/bin/python3"

    status, xml_path = ls.install_task(
        task_name="hpc-agent-doctor-abc123abc123",
        namespace=tmp_path,
        command="/usr/bin/python3",
        arguments="-m hpc_agent doctor",
        working_dir="/exp",
        interval_minutes=15,
        description="d",
        cron_command="/usr/bin/python3 -m hpc_agent doctor",
    )
    assert status == "installed"
    assert xml_path is None
    assert not ls.task_xml_path(tmp_path).exists()
    assert (fake.content or "").startswith("*/15 * * * *")


# --------------------------------------------------------------------------- #
# (b) LIFECYCLE
# --------------------------------------------------------------------------- #
def test_duplicate_install_collapses_to_one_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-install REPLACES: one task, current definition — never a second entry."""
    fake = _FakeSchtasks()
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)

    first, _ = _install_windows(tmp_path, interval=15)
    second, xml_path = _install_windows(tmp_path, interval=45)
    assert (first, second) == ("installed", "already_installed")
    assert fake.tasks == ["hpc-agent-doctor-abc123abc123"]

    # The REPLACE is what heals an older build's definition: the on-disk XML the
    # second /Create registered carries the NEW cadence, not the first one's.
    assert xml_path is not None
    root = ElementTree.parse(xml_path).getroot()
    interval = root.find(f"{_NS}Triggers/{_NS}TimeTrigger/{_NS}Repetition/{_NS}Interval")
    assert interval is not None and interval.text == "PT45M"


def test_posix_duplicate_install_collapses_to_one_cron_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _FakeCrontab("0 3 * * * /usr/bin/backup\n")
    monkeypatch.setattr(ls, "platform_kind", lambda: "posix")
    monkeypatch.setattr(ls, "_run", fake)

    for interval in (15, 45):
        ls.install_task(
            task_name="hpc-agent-doctor-abc123abc123",
            namespace=tmp_path,
            command="/usr/bin/python3",
            arguments="-m hpc_agent doctor",
            working_dir="/exp",
            interval_minutes=interval,
            description="d",
            cron_command="/usr/bin/python3 -m hpc_agent doctor",
        )
    body = fake.content or ""
    assert body.count("hpc-agent-doctor-abc123abc123") == 1
    assert "*/45 * * * *" in body
    assert "/usr/bin/backup" in body  # unrelated lines survive


def test_posix_install_refuses_to_write_when_the_table_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient ``crontab -l`` failure must never be read as an EMPTY table.

    Install now rewrites the marker line on EVERY call (idempotent-by-
    replacement), so the read-modify-write runs far more often than before — and
    bug-sweep #45 is exactly what happens when a blip's empty read is written
    back: the user's entire crontab is replaced by our single line. The guard
    fires by REFUSING, and the proof is that no ``crontab -`` write was attempted.
    """
    calls: list[list[str]] = []

    def _flaky(argv, *, input_text=None, timeout):  # noqa: ANN001, ANN202
        calls.append(argv)
        if argv == ["crontab", "-l"]:
            return _cp(argv, rc=1, stdout="crontab: cannot connect to NIS server")
        raise AssertionError("the write path must not be reached")

    monkeypatch.setattr(ls, "platform_kind", lambda: "posix")
    monkeypatch.setattr(ls, "_run", _flaky)

    with pytest.raises(RuntimeError, match="refusing to write"):
        ls.install_task(
            task_name="hpc-agent-doctor-abc123abc123",
            namespace=tmp_path,
            command="/usr/bin/python3",
            arguments="-m hpc_agent doctor",
            working_dir="/exp",
            interval_minutes=15,
            description="d",
            cron_command="/usr/bin/python3 -m hpc_agent doctor",
        )
    assert ["crontab", "-"] not in calls


def test_posix_silent_nonzero_crontab_read_also_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero `crontab -l` with NO output must refuse too (L3).

    The dangerous case is the quiet one: a permission denial or a broken
    installation can exit non-zero without printing anything a captured pipe
    sees, and "no output" was previously read as "empty table" — i.e. write. The
    branch is allow-listed now: only a POSITIVELY recognized empty-table message
    permits the write.
    """
    calls: list[list[str]] = []

    def _silent(argv, *, input_text=None, timeout):  # noqa: ANN001, ANN202
        calls.append(argv)
        if argv == ["crontab", "-l"]:
            return _cp(argv, rc=1, stdout="")
        raise AssertionError("the write path must not be reached")

    monkeypatch.setattr(ls, "platform_kind", lambda: "posix")
    monkeypatch.setattr(ls, "_run", _silent)

    readable, lines = ls.cron_read()
    assert (readable, lines) == (False, [])
    with pytest.raises(RuntimeError, match="refusing to write"):
        ls.install_task(
            task_name="hpc-agent-doctor-abc123abc123",
            namespace=tmp_path,
            command="/usr/bin/python3",
            arguments="-m hpc_agent doctor",
            working_dir="/exp",
            interval_minutes=15,
            description="d",
            cron_command="/usr/bin/python3 -m hpc_agent doctor",
        )
    assert ["crontab", "-"] not in calls


def test_posix_rewrite_preserves_blank_lines_and_comments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installing a watchdog must not reformat the operator's crontab (L4).

    The table is rebuilt from the lines we read back, so dropping "empty" lines
    on read silently strips every blank separator and leaves the user's file
    visibly rearranged by an unrelated action.
    """
    original = "# nightly backups\n\n0 3 * * * /usr/bin/backup\n\n# end\n"
    fake = _FakeCrontab(original)
    monkeypatch.setattr(ls, "platform_kind", lambda: "posix")
    monkeypatch.setattr(ls, "_run", fake)

    ls.install_task(
        task_name="hpc-agent-doctor-abc123abc123",
        namespace=tmp_path,
        command="/usr/bin/python3",
        arguments="-m hpc_agent doctor",
        working_dir="/exp",
        interval_minutes=15,
        description="d",
        cron_command="/usr/bin/python3 -m hpc_agent doctor",
    )
    body = fake.content or ""
    assert body.startswith(original.rstrip("\n"))
    assert "\n\n0 3 * * * /usr/bin/backup\n\n# end\n" in body


def test_posix_empty_table_still_installs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The recognized "no crontab for <user>" empty table is NOT a read failure.

    The other side of the guard above — verify it can actually distinguish, or
    the fail-closed rule would simply block every first install.
    """
    written: list[str | None] = []

    def _empty_table(argv, *, input_text=None, timeout):  # noqa: ANN001, ANN202
        if argv == ["crontab", "-l"]:
            return _cp(argv, rc=1, stdout="no crontab for james")
        written.append(input_text)
        return _cp(argv, rc=0)

    monkeypatch.setattr(ls, "platform_kind", lambda: "posix")
    monkeypatch.setattr(ls, "_run", _empty_table)

    status, _ = ls.install_task(
        task_name="hpc-agent-doctor-abc123abc123",
        namespace=tmp_path,
        command="/usr/bin/python3",
        arguments="-m hpc_agent doctor",
        working_dir="/exp",
        interval_minutes=15,
        description="d",
        cron_command="/usr/bin/python3 -m hpc_agent doctor",
    )
    assert status == "installed"
    assert written and "hpc-agent-doctor-abc123abc123" in (written[0] or "")


def _mark_installed(namespace: Path) -> None:
    """Leave the durable install marker the scheduler gate keys on.

    Both the sweep and the teardown skip the scheduler entirely when no marker
    exists, so a test that wants to exercise the scheduler path must look like a
    namespace where `doctor-install` really ran.
    """
    (Path(namespace) / "doctor.spec.json").write_text('{"notify": true}', encoding="utf-8")


def _write_run(namespace: Path, run_id: str, status: str) -> None:
    runs = namespace / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{run_id}.json").write_text(
        json.dumps({"run_id": run_id, "status": status}), encoding="utf-8"
    )


def test_remove_watchdog_if_idle_holds_while_a_run_is_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watchdog is per-REPO: a live sibling run still needs the dead-man's switch."""
    from hpc_agent.state.run_record import journal_dir, repo_hash

    exp = tmp_path / "exp"
    exp.mkdir()
    namespace = journal_dir(exp)
    task = ls.task_name_for(repo_hash(exp))
    _mark_installed(namespace)
    _write_run(namespace, "done", "complete")
    _write_run(namespace, "still-going", "in_flight")

    fake = _FakeSchtasks([task])
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)

    record = ls.remove_watchdog_if_idle(exp)
    assert record["removed"] is False
    assert record["live_runs"] == 1
    assert fake.tasks == [task]
    assert not any(c[:2] == ["schtasks", "/Delete"] for c in fake.calls)


def test_remove_watchdog_if_idle_fires_at_the_last_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hpc_agent.state.run_record import journal_dir, repo_hash

    exp = tmp_path / "exp"
    exp.mkdir()
    namespace = journal_dir(exp)
    task = ls.task_name_for(repo_hash(exp))
    _write_run(namespace, "a", "complete")
    _write_run(namespace, "b", "failed")
    ls.write_task_xml(namespace, "<Task/>")

    fake = _FakeSchtasks([task])
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)

    record = ls.remove_watchdog_if_idle(exp)
    assert record["removed"] is True
    assert record["status"] == "uninstalled"
    assert fake.tasks == []
    # The generated XML goes with it — nothing inert left under the journal home.
    assert not ls.task_xml_path(namespace).exists()


def test_remove_watchdog_if_idle_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is called from a terminal path: a broken scheduler must not mask the cause."""
    from hpc_agent.state.run_record import journal_dir

    exp = tmp_path / "exp"
    exp.mkdir()
    _mark_installed(journal_dir(exp))
    _write_run(journal_dir(exp), "a", "complete")

    def _boom(argv, *, input_text=None, timeout):  # noqa: ANN001, ANN202
        raise OSError("scheduler exploded")

    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", _boom)
    record = ls.remove_watchdog_if_idle(exp)
    # The read-only probe fails safe to "not installed", so this is a clean no-op
    # rather than an error — either way, no exception escapes.
    assert record["removed"] is False
    assert record["status"] in {"not_installed", "error"}


# --------------------------------------------------------------------------- #
# The MARKER GATE — what keeps `schtasks` off the common path
# --------------------------------------------------------------------------- #
def test_teardown_spawns_nothing_when_no_watchdog_was_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The B1/H2 gate: no install marker → no subprocess, at all.

    `schtasks /Query` cost 24.6 s cold on the incident machine, and the terminal
    harvest calls this on EVERY finished run — the overwhelming majority of which
    belong to experiments that never installed a watchdog. The proof is negative
    and must stay negative: the seam is replaced with a fake that FAILS the test
    if it is called at all.
    """
    from hpc_agent.state.run_record import journal_dir

    exp = tmp_path / "exp"
    exp.mkdir()
    journal_dir(exp)  # a real journal namespace — but no install marker in it
    _write_run(journal_dir(exp), "a", "complete")

    def _must_not_spawn(argv, *, input_text=None, timeout):  # noqa: ANN001, ANN202
        raise AssertionError(f"no subprocess may be spawned without a marker: {argv}")

    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", _must_not_spawn)

    record = ls.remove_watchdog_if_idle(exp)
    assert record["removed"] is False
    assert record["status"] == "not_installed"
    assert "marker" in record["reason"]


def test_sweep_spawns_nothing_when_no_watchdog_was_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same gate on the doctor sweep — the readiness-style no-spawn proof."""

    def _must_not_spawn(argv, *, input_text=None, timeout):  # noqa: ANN001, ANN202
        raise AssertionError(f"no subprocess may be spawned without a marker: {argv}")

    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", _must_not_spawn)
    assert ls.watchdog_marker_hashes() == []
    assert ls.scan_stale_watchdogs() == []


def test_marker_gate_opens_on_either_durable_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both eras count: the CURRENT installer's XML and the OLD build's bare spec.

    Gating on the task XML alone would make the sweep blind to exactly the tasks
    it exists to find — the pre-fix build wrote only `doctor.spec.json`.
    """
    from hpc_agent.state.run_record import current_homedir

    home = current_homedir()
    (home / "aaaaaaaaaaaa").mkdir(parents=True)
    (home / "bbbbbbbbbbbb").mkdir(parents=True)
    (home / "cccccccccccc").mkdir(parents=True)  # no marker at all
    (home / "aaaaaaaaaaaa" / "doctor.spec.json").write_text("{}", encoding="utf-8")
    ls.write_task_xml(home / "bbbbbbbbbbbb", "<Task/>")

    assert ls.watchdog_marker_hashes() == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
    assert ls.namespace_has_marker(home / "cccccccccccc") is False


def test_schtasks_budget_exceeds_the_measured_cold_cost() -> None:
    """A timeout below the cold cost is a silent-failure generator, not a budget.

    Measured 24.6 s for a cold `schtasks /Query` on the incident machine. At the
    original 15 s the sweep would pay the full budget and then time out having
    learned nothing — which is indistinguishable, in the envelope, from "no stale
    watchdogs found". Pinned so a future latency tidy-up cannot quietly restore
    the silent failure.
    """
    assert ls.SCHTASKS_TIMEOUT_SEC > 24.6


def test_remove_watchdog_if_idle_reports_a_refusing_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduler that REFUSES the delete is recorded, never raised and never silent."""
    from hpc_agent.state.run_record import journal_dir, repo_hash

    exp = tmp_path / "exp"
    exp.mkdir()
    task = ls.task_name_for(repo_hash(exp))
    _mark_installed(journal_dir(exp))
    _write_run(journal_dir(exp), "a", "complete")

    def _refuse(argv, *, input_text=None, timeout):  # noqa: ANN001, ANN202
        if argv[:2] == ["schtasks", "/Query"]:
            return _cp(argv, rc=0)
        return _cp(argv, rc=1, stdout="ERROR: Access is denied.")

    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", _refuse)
    record = ls.remove_watchdog_if_idle(exp)
    assert record["removed"] is False
    assert record["status"] == "error"
    assert "Access is denied" in record["reason"]
    assert record["task_name"] == task


# --------------------------------------------------------------------------- #
# (c) STALENESS SWEEP
# --------------------------------------------------------------------------- #
def _seed_namespace(tmp_path: Path, name: str, *, statuses: list[str], spec: bool) -> Path:
    from hpc_agent.state.run_record import current_homedir

    namespace = current_homedir() / name
    namespace.mkdir(parents=True, exist_ok=True)
    (namespace / "repo.json").write_text(
        json.dumps({"experiment_dir": str(tmp_path / name)}), encoding="utf-8"
    )
    (tmp_path / name).mkdir(exist_ok=True)
    if spec:
        (namespace / "doctor.spec.json").write_text('{"notify": true}', encoding="utf-8")
    else:
        # spec_missing signature: the task XML marker still proves an install
        # happened here, so the sweep's machine-wide gate opens and the missing
        # spec is REPORTED rather than silently skipped.
        ls.write_task_xml(namespace, "<Task/>")
    for i, status in enumerate(statuses):
        _write_run(namespace, f"r{i}", status)
    return namespace


def test_stale_sweep_flags_terminal_target_and_missing_spec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_namespace(tmp_path, "aaaaaaaaaaaa", statuses=["complete", "failed"], spec=True)
    _seed_namespace(tmp_path, "bbbbbbbbbbbb", statuses=["complete"], spec=False)

    fake = _FakeSchtasks(
        [
            "hpc-agent-doctor-aaaaaaaaaaaa",
            "hpc-agent-doctor-bbbbbbbbbbbb",
            "SomeVendorUpdater",  # not ours — never reported
        ]
    )
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)

    findings = {f["task_name"]: f for f in ls.scan_stale_watchdogs()}
    assert set(findings) == {
        "hpc-agent-doctor-aaaaaaaaaaaa",
        "hpc-agent-doctor-bbbbbbbbbbbb",
    }
    assert findings["hpc-agent-doctor-aaaaaaaaaaaa"]["reason"] == "target_terminal"
    assert findings["hpc-agent-doctor-bbbbbbbbbbbb"]["reason"] == "spec_missing"
    # Each finding carries the paste-ready removal, pointed at the live dir.
    assert "doctor-install" in findings["hpc-agent-doctor-aaaaaaaaaaaa"]["removal_command"]
    assert "uninstall" in findings["hpc-agent-doctor-aaaaaaaaaaaa"]["removal_command"]
    # The sweep is READ-ONLY: it never deletes what it reports.
    assert not any(c[:2] == ["schtasks", "/Delete"] for c in fake.calls)


def test_stale_sweep_is_silent_on_a_live_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live run, and a never-submitted repo, are both doing exactly their job."""
    _seed_namespace(tmp_path, "cccccccccccc", statuses=["complete", "in_flight"], spec=True)
    _seed_namespace(tmp_path, "dddddddddddd", statuses=[], spec=True)

    fake = _FakeSchtasks(["hpc-agent-doctor-cccccccccccc", "hpc-agent-doctor-dddddddddddd"])
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", fake)

    assert ls.scan_stale_watchdogs() == []


def test_stale_sweep_reads_cron_markers_on_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_namespace(tmp_path, "eeeeeeeeeeee", statuses=["complete"], spec=True)
    fake = _FakeCrontab(
        "0 3 * * * /usr/bin/backup\n"
        "*/15 * * * * /usr/bin/python3 -m hpc_agent doctor # hpc-agent-doctor-eeeeeeeeeeee\n"
    )
    monkeypatch.setattr(ls, "platform_kind", lambda: "posix")
    monkeypatch.setattr(ls, "_run", fake)

    findings = ls.scan_stale_watchdogs()
    assert [f["task_name"] for f in findings] == ["hpc-agent-doctor-eeeeeeeeeeee"]
    assert findings[0]["reason"] == "target_terminal"


def test_stale_sweep_is_silent_when_the_scheduler_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sweep that cannot see the scheduler reports nothing — never invents staleness."""

    def _boom(argv, *, input_text=None, timeout):  # noqa: ANN001, ANN202
        raise OSError("no schtasks on this host")

    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    monkeypatch.setattr(ls, "_run", _boom)
    assert ls.scan_stale_watchdogs() == []


def test_removal_command_falls_back_when_the_experiment_dir_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ls, "platform_kind", lambda: "windows")
    cmd = ls.compose_removal_command("hpc-agent-doctor-ffffffffffff", experiment_dir=None)
    assert cmd.startswith("schtasks /Delete")
    monkeypatch.setattr(ls, "platform_kind", lambda: "posix")
    cmd = ls.compose_removal_command(
        "hpc-agent-doctor-ffffffffffff", experiment_dir="/gone/for/good"
    )
    assert cmd.startswith("crontab -l")
