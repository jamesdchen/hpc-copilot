"""``stray-sweep`` — login-node process-hygiene probe (run-12 finding 20 LAYER 2).

Covers the pure parser (ps output → total/marked/stray classification), the
reap-flag gating (NEVER kills without the explicit flag, and only marked
over-age PIDs), and the full ssh-mocked flow.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent._wire.queries.stray_sweep import StraySweepSpec
from hpc_agent.ops.recover import stray_sweep as mod

# A realistic ps -o pid=,etimes=,args= dump. Marker rides argv as bash's $0
# (build_remote_command). Two marked+old strays, one marked+young, one unmarked.
_PS = """\
  101 5 sshd: jc_905@pts/0
  202 9000 bash -c reporter HPC_AGENT_OP=verify-canary:1699999999
  303 12 python -m hpc_agent.status
  404 40 timeout -k 10 60s bash -c poll HPC_AGENT_OP=submit-s2:1700000000
  505 8000 bash -c stale HPC_AGENT_OP=monitor:1600000000
not a process row
"""


class TestParsePsOutput:
    def test_counts_total_and_marks_and_strays(self):
        total, marked, strays = mod.parse_ps_output(_PS, max_age_sec=3900)
        assert total == 5  # the non-numeric row is skipped, not counted
        assert {p.pid for p in marked} == {202, 404, 505}
        # Only MARKED processes older than max_age_sec are strays (404 is marked
        # but young → not a stray).
        assert {p.pid for p in strays} == {202, 505}
        assert {p.op for p in strays} == {"verify-canary", "monitor"}

    def test_unmarked_process_never_a_stray_even_when_ancient(self):
        # An unmarked user process (e.g. a login shell) is NEVER classified as a
        # stray, no matter how old — reap must never touch it.
        ps = "  999 999999 bash -l\n"
        total, marked, strays = mod.parse_ps_output(ps, max_age_sec=10)
        assert total == 1
        assert marked == []
        assert strays == []

    def test_empty_and_blank_lines(self):
        total, marked, strays = mod.parse_ps_output("\n   \n", max_age_sec=10)
        assert (total, marked, strays) == (0, [], [])


class _FakeSsh:
    """Records ssh_run calls and returns scripted stdout per command substring."""

    def __init__(self, ps_stdout: str, *, ps_rc: int = 0):
        self.ps_stdout = ps_stdout
        self.ps_rc = ps_rc
        self.calls: list[str] = []

    def __call__(self, cmd, *, ssh_target, op=None, **kw):
        self.calls.append(cmd)
        from types import SimpleNamespace

        if cmd.startswith("ps "):
            return SimpleNamespace(stdout=self.ps_stdout, stderr="", returncode=self.ps_rc)
        # the reap kill
        return SimpleNamespace(stdout="", stderr="", returncode=0)


def _run(monkeypatch, fake, **spec_kw):
    monkeypatch.setattr(mod, "ssh_run", fake, raising=False)
    # ssh_run is imported inside the function body; patch the module attribute it
    # resolves. The function does `from hpc_agent.infra.remote import ssh_run`,
    # so patch there.
    monkeypatch.setattr("hpc_agent.infra.remote.ssh_run", fake)
    spec = StraySweepSpec(ssh_target="jc_905@discovery2", **spec_kw)
    return mod.stray_sweep(spec=spec)


class TestReapGating:
    def test_no_reap_without_the_flag(self, monkeypatch):
        fake = _FakeSsh(_PS)
        out = _run(monkeypatch, fake)  # reap defaults False
        assert out["reaped"] is False
        assert out["reaped_pids"] == []
        # Only the ps probe ran — no kill command.
        assert len(fake.calls) == 1 and fake.calls[0].startswith("ps ")
        assert out["needs_attention"] is True  # strays present
        assert "reap=true" in out["summary"]

    def test_reap_kills_only_marked_over_age_pids(self, monkeypatch):
        fake = _FakeSsh(_PS)
        out = _run(monkeypatch, fake, reap=True)
        assert out["reaped"] is True
        # Exactly the two strays (202, 505) — never the young marked 404, never
        # any unmarked pid.
        assert sorted(out["reaped_pids"]) == [202, 505]
        kill_calls = [c for c in fake.calls if c.startswith("kill ")]
        assert len(kill_calls) == 1
        assert "202" in kill_calls[0] and "505" in kill_calls[0]
        assert "404" not in kill_calls[0]  # marked but young
        assert "303" not in kill_calls[0] and "101" not in kill_calls[0]  # unmarked

    def test_reap_with_no_strays_is_a_noop(self, monkeypatch):
        # All-young table → no strays → reap requested but nothing killed.
        fake = _FakeSsh("  202 5 bash -c x HPC_AGENT_OP=verify-canary:1\n")
        out = _run(monkeypatch, fake, reap=True, max_age_sec=3900)
        assert out["reaped"] is False
        assert out["reaped_pids"] == []
        assert not any(c.startswith("kill ") for c in fake.calls)


class TestFlow:
    def test_warn_threshold_flips_needs_attention_without_strays(self, monkeypatch):
        # Many young unmarked processes, none stray, but total over threshold.
        table = "".join(f"  {900 + i} 5 bash -l\n" for i in range(6))
        fake = _FakeSsh(table)
        out = _run(monkeypatch, fake, warn_threshold=3)
        assert out["total_process_count"] == 6
        assert out["strays"] == []
        assert out["needs_attention"] is True  # over warn_threshold

    def test_all_clear(self, monkeypatch):
        fake = _FakeSsh("  101 5 sshd\n  202 5 bash -l\n")
        out = _run(monkeypatch, fake, warn_threshold=40)
        assert out["needs_attention"] is False
        assert "all clear" in out["summary"]

    def test_ps_failure_raises_remote_command_failed(self, monkeypatch):
        # rc 127 is the REMOTE `ps` failing (e.g. binary missing / fork-quota
        # pressure) over a WORKING transport — the remote-command domain, not
        # unreachable.
        fake = _FakeSsh("", ps_rc=127)
        with pytest.raises(errors.RemoteCommandFailed):
            _run(monkeypatch, fake)


class _RaisingSsh:
    """ssh_run stand-in that raises a scripted exception (the except-conversion leg)."""

    def __init__(self, exc: BaseException):
        self.exc = exc

    def __call__(self, cmd, *, ssh_target, op=None, **kw):
        raise self.exc


class TestExitStatusGate:
    """The rc==255 transport gate (OpenSSH's reserved CLIENT exit) — the only
    non-zero the probe leg may call ``SshUnreachable``."""

    def test_ps_rc255_raises_ssh_unreachable(self, monkeypatch):
        # rc 255 = the ssh CLIENT's own failure (connect/banner/kex) — transport.
        fake = _FakeSsh("", ps_rc=255)
        with pytest.raises(errors.SshUnreachable):
            _run(monkeypatch, fake)

    @pytest.mark.parametrize("rc", [1, 2, 126, 127, 254])
    def test_ps_non255_nonzero_raises_remote_command_failed(self, monkeypatch, rc):
        # Every non-255 non-zero is the REMOTE command's status: the transport
        # demonstrably worked, so SshUnreachable would be a misdiagnosis.
        fake = _FakeSsh("", ps_rc=rc)
        with pytest.raises(errors.RemoteCommandFailed):
            _run(monkeypatch, fake)

    def test_raised_remote_command_failed_propagates_unconverted(self, monkeypatch):
        # The except-conversion leg: a RemoteCommandFailed out of ssh_run is the
        # remote-command domain and must NOT be laundered into SshUnreachable.
        fake = _RaisingSsh(errors.RemoteCommandFailed("remote boom"))
        with pytest.raises(errors.RemoteCommandFailed):
            _run(monkeypatch, fake)

    def test_circuit_open_still_converts_to_unreachable(self, monkeypatch):
        # Typed transport exception (the breaker refusing the attempt): the
        # remote `ps` never ran — client-side, converts as before.
        fake = _RaisingSsh(errors.SshCircuitOpen("circuit open"))
        with pytest.raises(errors.SshUnreachable):
            _run(monkeypatch, fake)

    def test_oserror_still_converts_to_unreachable(self, monkeypatch):
        # Local spawn failure (ssh binary missing): client-side, converts.
        fake = _RaisingSsh(OSError("spawn failed"))
        with pytest.raises(errors.SshUnreachable):
            _run(monkeypatch, fake)
