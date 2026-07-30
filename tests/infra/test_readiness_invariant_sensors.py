"""The four named invariants as SENSORS (s2-readiness pillar 3).

``scratch`` / ``scheduler`` / ``env`` are one bounded command class each, over
the SAME ``ssh_argv`` + bounded-capture pair every other sensor rides; ``auth``
is a derivation over the connect sensor's own exit/stderr signature and opens
nothing at all. What is under test here is therefore the DERIVATION — which
command class each invariant asks, which subject its atom is filed under, and
which verdict a given answer settles on — not the transport, which is faked.

The no-network tripwire is autouse in this module ON PURPOSE even though these
sensors legitimately probe: every probe seam is patched, so a dial reaching a
real socket would mean the patch is INCOMPLETE and the "faked transport" claim
above is false. It has caught exactly that class before (the 2026-07-30 review's
two surviving mutations).
"""

from __future__ import annotations

import subprocess

import pytest

from hpc_agent.infra import readiness_sensors as rs
from tests._no_network import no_network  # noqa: F401 — autouse zero-network tripwire

TARGET = "user@hoffman2.idre.ucla.edu"
SCRATCH = "/u/scratch/someone"


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    """Route cache + readiness ledger are process-global; isolate every test."""
    rs.clear_route_cache()
    rs.clear_readiness_ledger()


@pytest.fixture(autouse=True)
def _no_local_ssh_version_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``ssh_argv``'s one-off ``ssh -V`` crypto probe.

    ``_local_openssh_supports_gcm`` is ``functools.cache``d, so it shells out at
    most ONCE per process — which makes an unpinned run order-dependent: whichever
    test happens to be first in a worker pays the subprocess and trips the
    tripwire, and the suite runs under ``pytest-randomly``. Pinning it here makes
    every test in this module independently green rather than green-by-luck.
    """
    from hpc_agent.infra import ssh_options

    monkeypatch.setattr(ssh_options, "_local_openssh_supports_gcm", lambda: True)


def _fake_ssh(
    monkeypatch: pytest.MonkeyPatch,
    *,
    rc: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> list[list[str]]:
    """Patch the bounded ssh capture; return the list of argvs it was handed."""
    seen: list[list[str]] = []

    def _run(argv, timeout):  # type: ignore[no-untyped-def]
        seen.append(list(argv))
        return subprocess.CompletedProcess(list(argv), rc, stdout, stderr)

    monkeypatch.setattr(rs, "_run_probe_ssh", _run)
    return seen


def _command(seen: list[list[str]]) -> str:
    """The remote command string of the single recorded ssh invocation."""
    assert len(seen) == 1, f"expected exactly one ssh invocation, saw {len(seen)}"
    return seen[0][-1]


# ── scratch ──────────────────────────────────────────────────────────────────


def test_scratch_asks_the_filesystem_to_answer_not_just_stat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``test -d`` alone passes off a cached stat; ``df`` makes the mount speak.

    A hung NFS/BeeGFS mount is the failure this invariant exists to catch, and it
    is precisely the one a bare existence check misses.
    """
    seen = _fake_ssh(monkeypatch, stdout="Filesystem 1024-blocks Used Available\n")
    atom = rs.sense_scratch(TARGET, SCRATCH)
    command = _command(seen)
    assert "test -d" in command
    assert "df -P" in command, "a bare test -d would pass against a hung mount"
    assert atom.verdict == "ok"


def test_scratch_files_its_atom_under_the_PATH_not_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ledger identity is ``(sensor, route, target)`` — two scratch roots on one
    cluster are two facts, and filing both under the host would collapse them."""
    _fake_ssh(monkeypatch)
    one = rs.sense_scratch(TARGET, SCRATCH)
    two = rs.sense_scratch(TARGET, "/u/project/someone")
    assert one.target == SCRATCH
    assert two.target == "/u/project/someone"
    assert one.target != two.target


def test_scratch_quotes_a_path_with_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unquoted path with a space reaches the remote as two arguments and the
    reading silently describes the wrong directory."""
    seen = _fake_ssh(monkeypatch)
    rs.sense_scratch(TARGET, "/u/scratch/my project")
    assert "'/u/scratch/my project'" in _command(seen)


def test_scratch_without_a_configured_path_is_skipped_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No scratch declared is an ABSENCE of evidence. Reading it as ``ok`` would
    grant readiness for an invariant nobody looked at."""
    seen = _fake_ssh(monkeypatch)
    atom = rs.sense_scratch(TARGET, "   ")
    assert atom.verdict == "skipped"
    assert atom.ok is None
    assert seen == [], "a skipped sensor must not dial"


def test_scratch_nonzero_exit_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ssh(monkeypatch, rc=1, stderr="df: /u/scratch/someone: No such file or directory")
    atom = rs.sense_scratch(TARGET, SCRATCH)
    assert atom.verdict == "down"
    assert "No such file" in atom.detail


# ── scheduler ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("family", "fragment"),
    [("slurm", "squeue"), ("sge", "qstat"), ("pbspro", "qstat"), ("torque", "qstat")],
)
def test_every_known_family_has_a_cheap_touch(
    monkeypatch: pytest.MonkeyPatch, family: str, fragment: str
) -> None:
    seen = _fake_ssh(monkeypatch, stdout="slurm 23.02.7\n")
    atom = rs.sense_scheduler(TARGET, family)
    assert fragment in _command(seen)
    assert atom.verdict == "ok"
    assert atom.target == family


def test_the_touch_never_enumerates_the_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every touch is a version/usage banner. A bare ``qstat`` / ``squeue`` would
    make the readiness sweep a per-refresh load on the scheduler daemon, which is
    how a diagnosis layer becomes the problem it diagnoses."""
    for family, command in rs.SCHEDULER_TOUCH_COMMANDS.items():
        assert "-" in command, f"{family}'s touch {command!r} takes no flag — it would query"
        assert any(flag in command for flag in ("--version", "-help")), command


def test_an_unknown_family_is_skipped_never_a_guessed_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guessing a submit binary for an unrecognized family reports a broken
    scheduler on a working cluster."""
    seen = _fake_ssh(monkeypatch)
    atom = rs.sense_scheduler(TARGET, "lsf")
    assert atom.verdict == "skipped"
    assert "lsf" in atom.detail
    assert seen == []


def test_a_usage_exit_that_printed_a_banner_still_counts_as_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``qstat -help`` exits non-zero on several Grid Engine builds while printing
    its usage. The CLI ANSWERED — which is the fact this sensor reads — and
    calling it ``down`` would report a broken scheduler at every such site.

    The exit code stays disclosed in ``detail``: nothing is hidden, only
    classified.
    """
    _fake_ssh(monkeypatch, rc=1, stdout="usage: qstat [options]\n", stderr="")
    atom = rs.sense_scheduler(TARGET, "sge")
    assert atom.verdict == "ok"
    assert "exit 1" in atom.detail
    assert "usage: qstat" in atom.detail


def test_a_nonzero_exit_with_no_output_is_still_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """The leniency above is bounded to "it answered". Silence is not an answer."""
    _fake_ssh(monkeypatch, rc=127, stdout="", stderr="qstat: command not found")
    atom = rs.sense_scheduler(TARGET, "sge")
    assert atom.verdict == "down"
    assert "command not found" in atom.detail


def test_the_output_leniency_does_not_leak_into_other_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the scheduler touch passes ``accept_output``. A preamble that printed
    something and exited non-zero is a FAILED preamble."""
    _fake_ssh(monkeypatch, rc=1, stdout="Loading module...\n", stderr="conda: not found")
    atom = rs.sense_command_class(TARGET, "module load x", kind="preamble")
    assert atom.verdict == "down"


def test_scheduler_family_resolves_from_a_clusters_yaml_entry() -> None:
    assert rs.scheduler_family_for_cluster({"scheduler": "SGE"}) == "sge"
    assert rs.scheduler_family_for_cluster({"scheduler": "slurm"}) == "slurm"
    # A registered-backend name with a pinned profile resolves via the profile.
    assert (
        rs.scheduler_family_for_cluster(
            {"scheduler": "hoffman2_sge", "scheduler_profile": {"family": "sge"}}
        )
        == "sge"
    )
    # Unresolvable → "" (the sensor then skips), never a guess.
    assert rs.scheduler_family_for_cluster({"scheduler": "something_new"}) == ""
    assert rs.scheduler_family_for_cluster({}) == ""


# ── env ──────────────────────────────────────────────────────────────────────


def test_env_reuses_the_release_flows_command_class(monkeypatch: pytest.MonkeyPatch) -> None:
    """One command class for "which wheel is over there", so a sensed fingerprint
    is comparable to the one the release flow reads on every target env."""
    seen = _fake_ssh(monkeypatch, stdout="hpc-agent 0.11.4+g766c293d\n")
    rs.sense_env(TARGET, "module load python && conda activate hpc")
    command = _command(seen)
    assert command.endswith(rs.ENV_FINGERPRINT_COMMAND)
    assert command.startswith("module load python && conda activate hpc &&")


def test_env_carries_the_FINGERPRINT_into_the_atom_not_just_exit_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fact is WHICH wheel, not that a command succeeded. An ``ok`` atom whose
    detail read "exit 0" would satisfy the ledger while answering nothing."""
    _fake_ssh(monkeypatch, stdout="hpc-agent 0.11.4+g766c293d\nnoise on a second line\n")
    atom = rs.sense_env(TARGET, "")
    assert atom.verdict == "ok"
    assert atom.detail == "hpc-agent 0.11.4+g766c293d"


def test_env_with_no_activation_degenerates_to_the_bare_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _fake_ssh(monkeypatch, stdout="hpc-agent 0.11.4\n")
    rs.sense_env(TARGET, "")
    assert _command(seen) == rs.ENV_FINGERPRINT_COMMAND


def test_env_activation_that_already_ends_in_and_is_not_doubled() -> None:
    assert rs.env_fingerprint_command("module load x &&") == (
        f"module load x && {rs.ENV_FINGERPRINT_COMMAND}"
    )


# ── auth: the derivation that costs no probe ─────────────────────────────────


def _connect(verdict: rs.SensorVerdict, detail: str) -> rs.VerdictAtom:
    return rs.VerdictAtom(
        sensor="connect", target=TARGET, verdict=verdict, detail=detail, route="effective"
    )


def test_a_command_that_ran_proves_the_credentials_were_accepted() -> None:
    """The one place in the system that can honestly say ``auth: ok``. The circuit
    breaker cannot: its SUCCESS verdict means "reached the host", which an auth
    rejection also satisfies."""
    atom = rs.auth_atom(_connect("ok", "exit 0"))
    assert (atom.sensor, atom.verdict, atom.route) == ("auth", "ok", "effective")
    assert atom.target == TARGET


@pytest.mark.parametrize(
    "stderr",
    [
        "user@host: Permission denied (publickey,gssapi-keyex).",
        "Received disconnect from 1.2.3.4 port 22:2: Too many authentication failures",
        "Permission denied, please try again.",
        "Host key verification failed.",
    ],
)
def test_an_auth_rejection_reads_down(stderr: str) -> None:
    atom = rs.auth_atom(_connect("down", f"exit 255: {stderr}"))
    assert atom.verdict == "down"
    assert "REFUSED" in atom.detail


@pytest.mark.parametrize(
    "stderr",
    [
        "ssh: connect to host h port 22: Connection refused",
        "ssh: connect to host h port 22: Connection timed out",
        "ssh: Could not resolve hostname h: Name or service not known",
        "ssh: connect to host h port 22: Network is unreachable",
    ],
)
def test_never_reaching_the_host_is_unknown_not_a_rejection(stderr: str) -> None:
    """THE discrimination. An unreachable host tells us NOTHING about auth, and
    recording ``down`` would send the human to their keys while the network is
    what is broken."""
    atom = rs.auth_atom(_connect("down", f"exit 255: {stderr}"))
    assert atom.verdict == "unknown"
    assert atom.ok is None
    assert "NOT an auth rejection" in atom.detail


def test_a_timeout_reads_unknown_not_a_rejection() -> None:
    atom = rs.auth_atom(_connect("timeout", "timed out after 30s running 'true'"))
    assert atom.verdict == "unknown"


def test_auth_rejection_outranks_a_connection_closed_phrase() -> None:
    """OpenSSH emits ``Connection closed by <host> port 22`` immediately AFTER
    ``Too many authentication failures``. Testing the transport markers first
    would classify the flagship auth failure as unreachable — the exact
    conflation this leg exists to remove.
    """
    detail = (
        "exit 255: Received disconnect from 5.6.7.8 port 22:2: Too many "
        "authentication failures\r\nConnection reset by 5.6.7.8 port 22"
    )
    assert rs.auth_signature(detail) == "rejected"
    assert rs.auth_atom(_connect("down", detail)).verdict == "down"


def test_an_unrecognized_failure_is_unsettled_never_assumed_healthy() -> None:
    atom = rs.auth_atom(_connect("down", "exit 3: something nobody has seen before"))
    assert atom.verdict == "unknown"
    assert "no auth or transport signature" in atom.detail


def test_the_preamble_rung_emits_the_auth_leg_alongside_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auth atom rides an existing rung, adding no ssh invocation of its own —
    that is what makes it affordable at all."""
    seen = _fake_ssh(monkeypatch)
    atoms = rs.sense_preamble(TARGET, "module load python")
    kinds = [a.sensor for a in atoms]
    assert kinds == ["connect", "auth", "preamble"]
    assert len(seen) == 2, "connect + preamble only — auth must add NO probe"


def test_a_dead_connect_still_emits_auth_and_skips_the_preamble(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _fake_ssh(monkeypatch, rc=255, stderr="Permission denied (publickey).")
    atoms = rs.sense_preamble(TARGET, "module load python")
    by_kind = {a.sensor: a for a in atoms}
    assert by_kind["connect"].verdict == "down"
    assert by_kind["auth"].verdict == "down"
    assert by_kind["preamble"].verdict == "skipped"
    assert len(seen) == 1, "the preamble must not run through a dead connect"


# ── the invariant rungs are OPT-IN and never a PATH verdict ──────────────────


def _resolution(monkeypatch: pytest.MonkeyPatch, *, proxyjump: str | None = None) -> None:
    lines = ["hostname h.example.edu", "user someone", "port 22"]
    if proxyjump is not None:
        lines.append(f"proxyjump {proxyjump}")

    def _run(argv, timeout):  # type: ignore[no-untyped-def]
        return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")

    monkeypatch.setattr(rs, "_run_route_resolution", _run)


def _ok_connector(host: str, port: int, timeout: float) -> tuple[bool, str]:
    return True, f"tcp connect to {host}:{port} ok"


def test_the_invariant_rungs_do_not_run_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each rung is one more connection. The S2 pre-detach gate asks the PATH
    question and must not pay for storage to get its answer."""
    _resolution(monkeypatch)
    seen = _fake_ssh(monkeypatch)
    readiness = rs.read_path_readiness(
        "h.example.edu", activation="module load x", connect=_ok_connector, record=False
    )
    kinds = {a.sensor for a in readiness.atoms}
    assert kinds & {"scratch", "scheduler", "env"} == set()
    assert len(seen) == 2, "connect + preamble only"


def test_asking_for_the_rungs_adds_their_atoms(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolution(monkeypatch)
    _fake_ssh(monkeypatch, stdout="hpc-agent 0.11.4\n")
    readiness = rs.read_path_readiness(
        "h.example.edu",
        activation="module load x",
        connect=_ok_connector,
        scratch=SCRATCH,
        scheduler_family="sge",
        env_fingerprint=True,
        record=False,
    )
    by_kind = {a.sensor: a for a in readiness.atoms}
    assert by_kind["scratch"].target == SCRATCH
    assert by_kind["scheduler"].target == "sge"
    assert by_kind["env"].verdict == "ok"


def test_a_broken_invariant_never_makes_the_PATH_read_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full scratch disk is not a reason to call the path dead. The S2 path gate
    refuses on ``PathReadiness.ok``, so letting a storage atom into ``_classify``
    would turn a quota problem into a transport refusal naming the wrong cause.
    """
    _resolution(monkeypatch)

    def _run(argv, timeout):  # type: ignore[no-untyped-def]
        command = argv[-1]
        if "df -P" in command:
            return subprocess.CompletedProcess(argv, 1, "", "No space left on device")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(rs, "_run_probe_ssh", _run)

    readiness = rs.read_path_readiness(
        "h.example.edu",
        activation="module load x",
        connect=_ok_connector,
        scratch=SCRATCH,
        record=False,
    )
    scratch = next(a for a in readiness.atoms if a.sensor == "scratch")
    assert scratch.verdict == "down"
    assert readiness.cause == "path_ok"
    assert readiness.ok is True, "the PATH is fine; only storage is not"


def test_the_rungs_are_skipped_through_a_route_that_did_not_carry_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sensing storage through a route that just failed to carry ``true`` yields
    an atom about the TRANSPORT wearing a storage label — the conflation this
    whole substrate exists to end."""
    _resolution(monkeypatch)
    seen = _fake_ssh(
        monkeypatch, rc=255, stderr="ssh: connect to host h port 22: Connection refused"
    )
    readiness = rs.read_path_readiness(
        "h.example.edu",
        activation="module load x",
        connect=_ok_connector,
        scratch=SCRATCH,
        scheduler_family="sge",
        env_fingerprint=True,
        record=False,
    )
    kinds = {a.sensor for a in readiness.atoms}
    assert kinds & {"scratch", "scheduler", "env"} == set()
    assert all("df -P" not in argv[-1] for argv in seen)


def test_the_rungs_are_skipped_behind_a_dead_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    _resolution(monkeypatch, proxyjump="usc-discovery")
    seen = _fake_ssh(monkeypatch)

    def _connect(host: str, port: int, timeout: float) -> tuple[bool, str]:
        return (host != "usc-discovery"), "ConnectionRefusedError: refused"

    readiness = rs.read_path_readiness(
        "h.example.edu",
        activation="module load x",
        connect=_connect,
        scratch=SCRATCH,
        scheduler_family="sge",
        env_fingerprint=True,
        record=False,
    )
    kinds = {a.sensor for a in readiness.atoms}
    assert kinds & {"scratch", "scheduler", "env"} == set()
    assert seen == [], "nothing may be sensed through a hop that is down"
