"""Contract test: pin the {tasks, errors} shape of query_sacct / query_sge.

These tests focus on the invariant, not on specific states -- they assert
the top-level keys, types, and that every error entry is a {code, detail}
dict of strings, regardless of success or failure path.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from hpc_agent.infra.backends import query as qmod


def _cp(stdout="", stderr="", returncode=0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _assert_contract(out: dict) -> None:
    assert isinstance(out, dict)
    assert set(out.keys()) == {"tasks", "errors"}
    assert isinstance(out["tasks"], dict)
    assert isinstance(out["errors"], list)
    for e in out["errors"]:
        assert isinstance(e, dict)
        assert "code" in e and isinstance(e["code"], str)
        assert "detail" in e and isinstance(e["detail"], str)


class TestSacctContract:
    def test_empty_input(self, monkeypatch):
        # Should not even call subprocess, but contract still holds.
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp())
        _assert_contract(qmod.query_sacct([]))

    def test_happy_path(self, monkeypatch):
        stdout = "123_1|COMPLETED|0:0\n"
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(stdout=stdout))
        out = qmod.query_sacct(["123"])
        _assert_contract(out)
        assert out["errors"] == []

    def test_timeout_path(self, monkeypatch):
        def raiser(cmd, *a, **kw):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

        monkeypatch.setattr(subprocess, "run", raiser)
        out = qmod.query_sacct(["111"])
        _assert_contract(out)
        assert out["tasks"] == {}
        assert len(out["errors"]) >= 1

    def test_file_not_found_path(self, monkeypatch):
        def raiser(cmd, *a, **kw):
            raise FileNotFoundError("sacct missing")

        monkeypatch.setattr(subprocess, "run", raiser)
        out = qmod.query_sacct(["111"])
        _assert_contract(out)

    def test_nonzero_exit_path(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **kw: _cp(stdout="", returncode=1, stderr="nope")
        )
        out = qmod.query_sacct(["111"])
        _assert_contract(out)
        assert len(out["errors"]) >= 1


class TestSgeContract:
    def test_empty_input(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp())
        _assert_contract(qmod.query_sge([]))

    def test_happy_path(self, monkeypatch):
        def responder(cmd, *a, **kw):
            if cmd[0] == "qstat":
                return _cp(stdout="42 0.5 myjob u r 04/17/2026 12:00:00 all.q 1 1-2:1\n")
            block = (
                f"=====\njobnumber    {cmd[-1]}\ntaskid       3\nexit_status  0\nfailed       0\n"
            )
            return _cp(stdout=block)

        monkeypatch.setattr(subprocess, "run", responder)
        out = qmod.query_sge(["42"], user="u")
        _assert_contract(out)
        assert out["errors"] == []

    def test_all_tools_fail(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _cp(returncode=1))
        out = qmod.query_sge(["111"], user="u")
        _assert_contract(out)
        assert out["tasks"] == {}
        assert len(out["errors"]) >= 1

    def test_timeout_propagates_error(self, monkeypatch):
        def raiser(cmd, *a, **kw):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=30)

        monkeypatch.setattr(subprocess, "run", raiser)
        out = qmod.query_sge(["111"], user="u")
        _assert_contract(out)
        assert len(out["errors"]) >= 1
