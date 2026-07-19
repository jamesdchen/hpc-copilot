"""Cluster-side jobmap marker protocol (U3-b, SUBMIT-ONCE-DESIGN §3.2, premortem
Δ2/Δ4/Δ5).

Pins the token/keying, the pre/post-dispatch shell fragments, and the ack-gated
recovery read + parse round-trip — all WITHOUT SSH (the fragments are pure shell
strings; the round-trip runs them in a local ``bash``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hpc_agent.infra import jobmap


def test_flag_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default flipped ON in 0.11.3 (run-15 live-fire evidence): unset ⇒ enabled.
    monkeypatch.delenv(jobmap.SUBMIT_ONCE_FLAG, raising=False)
    assert jobmap.submit_once_enabled() is True


def test_flag_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only an explicit "0" disables; every other value (incl. "1") is ON.
    monkeypatch.setenv(jobmap.SUBMIT_ONCE_FLAG, "1")
    assert jobmap.submit_once_enabled() is True
    monkeypatch.setenv(jobmap.SUBMIT_ONCE_FLAG, "0")
    assert jobmap.submit_once_enabled() is False


def test_token_is_run_id_hash_attempt() -> None:
    # Δ2: run_id#attempt, NOT a job-name hash.
    assert jobmap.jobmap_token("pi-train-d363e2a3", 2) == "pi-train-d363e2a3#2"


def test_wave_key_distinct_from_canary() -> None:
    # Δ5: a canary is never confused with wave-0.
    assert jobmap.wave_key(0) == "wave-0"
    assert jobmap.wave_key(3) == "wave-3"
    assert jobmap.wave_key(0) != jobmap.CANARY_WAVE_KEY


def test_marker_paths_under_hpc_submit() -> None:
    assert jobmap.jobmap_dir("/home/u/demo/") == "/home/u/demo/.hpc/submit"
    assert jobmap.jobmap_marker_path("/home/u/demo", "r1") == "/home/u/demo/.hpc/submit/r1.jobmap"
    assert (
        jobmap.wave_id_marker_path("/home/u/demo", "r1", "wave-0")
        == "/home/u/demo/.hpc/submit/r1.jobmap.wave-0.id"
    )


def test_pre_dispatch_fragment_shape() -> None:
    frag = jobmap.build_pre_dispatch_shell(
        remote_path="/home/u/demo", run_id="r1", attempt=0, wkey="wave-0"
    )
    # mkdir -p rides the same leg (OPEN-4); atomic temp+mv; token carried as a
    # printf %s arg (the token value is a separate shell word).
    assert "mkdir -p" in frag
    assert ".hpc/submit" in frag
    assert '"token":"%s"' in frag
    assert "'r1#0'" in frag  # the shlex-quoted token arg
    assert '"state":"pending"' in frag
    assert "mv -f" in frag


def test_post_dispatch_fragment_records_rc_first() -> None:
    frag = jobmap.build_post_dispatch_shell(remote_path="/home/u/demo", run_id="r1", wkey="wave-0")
    # Δ4: rc persisted alongside the id; rc FIRST as the clean single token.
    assert 'printf \'%s %s\\n\' "$__hpc_rc" "$__hpc_jid"' in frag
    assert "r1.jobmap.wave-0.id" in frag
    assert "mv -f" in frag


def test_severed_read_is_not_present() -> None:
    # No ack ⇒ present=False (UNKNOWN / never-dispatched) — never "no marker".
    r = jobmap.parse_jobmap_read("some truncated bytes\nno ack here")
    assert r.present is False
    assert r.waves == {}


def test_parse_empty_but_present_dir() -> None:
    # A genuinely empty-but-present submit dir: ack seen, no marker/wave lines.
    r = jobmap.parse_jobmap_read("__HPC_JOBMAP_ACK__\n")
    assert r.present is True
    assert r.token is None
    assert r.waves == {}


def _run_bash(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    # Write the script to a FILE and run ``bash <file>`` rather than
    # ``bash -c "<huge string>"`` — on Windows, subprocess joins argv via
    # ``list2cmdline`` (Windows quoting), which mangles the embedded shell quotes
    # / ``$(...)`` of a complex one-liner before bash ever parses it. A file
    # sidesteps the host arg-quoting entirely (the fragments are unchanged).
    import uuid

    name = f"_jobmap_test_{uuid.uuid4().hex}.sh"
    (cwd / name).write_text(script, encoding="utf-8")
    # Pass the BASENAME (cwd is the script's dir) — an absolute Windows path is
    # not resolvable by the host's git-bash mount layout.
    return subprocess.run(
        ["bash", name],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize(
    ("scheduler_stdout", "expected_id"),
    [
        ('Your job 987654 ("job") has been submitted', "987654"),  # SGE/UGE
        ("Submitted batch job 12345", "12345"),  # Slurm
    ],
)
def test_marker_write_read_roundtrip(
    tmp_path: Path, scheduler_stdout: str, expected_id: str
) -> None:
    """End-to-end (local bash, no SSH): the pre-fragment writes ``pending``, a
    faked dispatch persists ``"<rc> <raw stdout>"``, and the ack-gated read +
    parse recover token/attempt/state + the raw blob (rc==0). Both scheduler
    stdout dialects round-trip and the backend regex extracts the id."""
    if not _bash_available():
        pytest.skip("bash unavailable")
    # Relative remote_path so the fragments' POSIX shell is exercised WITHOUT the
    # Windows-drive path translation that the local git-bash mount layout mangles
    # (the fragments themselves are cluster-path-agnostic — this only isolates the
    # shell mechanics under test from the host's path translation).
    remote = "exp"
    pre = jobmap.build_pre_dispatch_shell(
        remote_path=remote, run_id="run-x", attempt=1, wkey="wave-0"
    )
    post = jobmap.build_post_dispatch_shell(remote_path=remote, run_id="run-x", wkey="wave-0")
    # Fake the dispatch: __hpc_jid=<stdout>, __hpc_rc=0, then the post fragment.
    script = f"{pre}; __hpc_jid={_sq(scheduler_stdout)}; __hpc_rc=0; {post}"
    assert _run_bash(script, tmp_path).returncode == 0

    # pending marker is valid JSON with the token+attempt
    marker = tmp_path / jobmap.jobmap_marker_path(remote, "run-x")
    obj = json.loads(marker.read_text())
    assert obj["token"] == "run-x#1"
    assert obj["attempt"] == 1
    assert obj["state"] == "pending"

    read = jobmap.build_read_shell(remote_path=remote, run_id="run-x")
    out = _run_bash(read, tmp_path)
    parsed = jobmap.parse_jobmap_read(out.stdout)
    assert parsed.present is True
    assert parsed.token == "run-x#1"
    assert parsed.attempt == 1
    blob, rc = parsed.waves["wave-0"]
    assert rc == 0
    # the SAME id-parsing source the client uses on the happy path
    from hpc_agent.infra.backends.sge import SGEBackend
    from hpc_agent.infra.backends.slurm import SlurmBackend

    matched = SGEBackend.JOB_ID_REGEX.search(blob) or SlurmBackend.JOB_ID_REGEX.search(blob)
    assert matched is not None and matched.group(1) == expected_id


def test_failed_dispatch_marker_carries_nonzero_rc(tmp_path: Path) -> None:
    """Δ4: a qsub that FAILED (rc≠0) still persists the marker with that rc, so
    the U3-d adopt rung can refuse to adopt (confirmed failed dispatch)."""
    if not _bash_available():
        pytest.skip("bash unavailable")
    remote = "exp"  # relative — isolate shell mechanics from host path translation
    pre = jobmap.build_pre_dispatch_shell(
        remote_path=remote, run_id="run-f", attempt=0, wkey="wave-0"
    )
    post = jobmap.build_post_dispatch_shell(remote_path=remote, run_id="run-f", wkey="wave-0")
    # rc=3, empty id (a failed dispatch that printed nothing parseable).
    script = f"{pre}; __hpc_jid=''; __hpc_rc=3; {post}"
    assert _run_bash(script, tmp_path).returncode == 0
    read = jobmap.build_read_shell(remote_path=remote, run_id="run-f")
    parsed = jobmap.parse_jobmap_read(_run_bash(read, tmp_path).stdout)
    blob, rc = parsed.waves["wave-0"]
    assert rc == 3
    assert blob == ""


def test_read_absent_marker_dir_is_not_present(tmp_path: Path) -> None:
    """rung-2 substrate: an absent ``.hpc/submit/`` ⇒ ``cd`` fails ⇒ no ack ⇒
    present=False (never a spurious 'no marker' that could mis-settle)."""
    if not _bash_available():
        pytest.skip("bash unavailable")
    read = jobmap.build_read_shell(remote_path="never-created", run_id="never")
    parsed = jobmap.parse_jobmap_read(_run_bash(read, tmp_path).stdout)
    assert parsed.present is False


def _sq(s: str) -> str:
    import shlex

    return shlex.quote(s)


def _bash_available() -> bool:
    try:
        return (
            subprocess.run(
                ["bash", "-c", "true"], capture_output=True, check=False, timeout=30
            ).returncode
            == 0
        )
    except (OSError, ValueError):
        return False
