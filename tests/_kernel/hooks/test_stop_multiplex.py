"""Tests for the fused ``stop_multiplex`` Stop hook.

The multiplexer replaces the three legacy standalone Stop guards with ONE
interpreter start (#288): it reads the Stop payload once and dispatches the same
parsed mapping to each guard's ``build_hook_output``. These pin:

* the robust shared stdin reader (bytes + utf-8 ``errors="replace"``);
* the syntactic, stdlib-only prefilter and its necessary-condition equivalence
  (prefilter-negative => every guard individually a no-op), incl. the acceptance
  property that a no-op turn never imports the heavy guard dependency chain;
* per-guard isolation (one guard raising still runs the others);
* first-block-wins composition + systemMessage accumulation;
* the end-to-end dispatch over the REAL guards for a pending skill-return.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hpc_agent._kernel.hooks import stop_multiplex as m
from hpc_agent.cli.skill_returns import _committed_path

_KNOWN_SKILL = "hpc-wrap-entry-point"
_GUARDS = (
    "hpc_agent._kernel.hooks.skill_return_stop_guard",
    "hpc_agent._kernel.hooks.decision_rendezvous_stop_guard",
    "hpc_agent._kernel.hooks.relay_audit_stop",
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the journal home at a fresh, NON-existent dir so the default (real)
    ``~/.claude/hpc`` on the box can never leak into the prefilter's skip check."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "_no_home"))


def _stdin_bytes(monkeypatch: pytest.MonkeyPatch, raw: bytes) -> None:
    class _Buf:
        def read(self) -> bytes:
            return raw

    class _Stdin:
        buffer = _Buf()

    monkeypatch.setattr(sys, "stdin", _Stdin())


# ─── read_stdin_payload ──────────────────────────────────────────────────────


def test_reads_valid_json_from_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    _stdin_bytes(monkeypatch, json.dumps({"cwd": "/x"}).encode("utf-8"))
    assert m.read_stdin_payload() == {"cwd": "/x"}


def test_non_utf8_bytes_do_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    # A stray non-utf8 byte inside otherwise-valid JSON text degrades to a
    # replacement char (fail toward running) rather than raising.
    payload = b'{"cwd": "/x", "note": "' + b"\xff" + b'"}'
    _stdin_bytes(monkeypatch, payload)
    out = m.read_stdin_payload()
    assert isinstance(out, dict) and out["cwd"] == "/x"


def test_empty_and_non_json_yield_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _stdin_bytes(monkeypatch, b"   ")
    assert m.read_stdin_payload() is None
    _stdin_bytes(monkeypatch, b"not json")
    assert m.read_stdin_payload() is None


# ─── prefilter_should_run ────────────────────────────────────────────────────


def test_prefilter_skips_clean_non_hpc_turn(tmp_path: Path) -> None:
    # No <cwd>/.hpc and a non-existent journal home → provably no guard can fire.
    assert m.prefilter_should_run({"cwd": str(tmp_path)}) is False


def test_prefilter_runs_when_cwd_has_hpc(tmp_path: Path) -> None:
    (tmp_path / ".hpc").mkdir()
    assert m.prefilter_should_run({"cwd": str(tmp_path)}) is True


def test_prefilter_runs_when_journal_home_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(home))
    assert m.prefilter_should_run({"cwd": str(tmp_path)}) is True


def test_prefilter_skips_non_dict_payload(tmp_path: Path) -> None:
    assert m.prefilter_should_run(None) is False
    assert m.prefilter_should_run("stop") is False


# ─── necessary-condition equivalence: prefilter-negative => each guard no-op ──


def test_prefilter_negative_means_every_guard_is_noop(tmp_path: Path) -> None:
    """The contract: whenever the prefilter says skip, each guard individually
    returns None — so skipping the dispatch cannot suppress a would-be block."""
    from hpc_agent._kernel.hooks import (
        decision_rendezvous_stop_guard,
        relay_audit_stop,
        skill_return_stop_guard,
    )

    payload = {"cwd": str(tmp_path), "transcript_path": str(tmp_path / "t.jsonl")}
    assert m.prefilter_should_run(payload) is False
    assert skill_return_stop_guard.build_hook_output(payload) is None
    assert decision_rendezvous_stop_guard.build_hook_output(payload) is None
    assert relay_audit_stop.build_hook_output(payload) is None


def test_prefilter_positive_when_a_guard_would_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction (a genuine firing condition => prefilter positive):
    a committed skill-return envelope under <cwd>/.hpc makes the prefilter run."""
    committed = _committed_path(tmp_path, _KNOWN_SKILL)
    committed.parent.mkdir(parents=True, exist_ok=True)
    committed.write_text(json.dumps({"ok": True, "skill": _KNOWN_SKILL}), encoding="utf-8")
    payload = {"cwd": str(tmp_path)}
    assert m.prefilter_should_run(payload) is True
    # And the guard really does fire on this payload.
    from hpc_agent._kernel.hooks import skill_return_stop_guard

    monkeypatch.setattr("hpc_agent.cli.skill_returns.known_return_dirs", lambda: [])
    out = skill_return_stop_guard.build_hook_output(payload)
    assert isinstance(out, dict) and out.get("decision") == "block"


# ─── dispatch: per-guard isolation ───────────────────────────────────────────


def _fake_guard(name: str, out: object | Exception) -> types.ModuleType:
    mod = types.ModuleType(name)

    def build_hook_output(_payload: object) -> object:
        if isinstance(out, Exception):
            raise out
        return out

    mod.build_hook_output = build_hook_output  # type: ignore[attr-defined]
    return mod


def test_dispatch_isolates_a_raising_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    mods = {
        "g.a": _fake_guard("g.a", RuntimeError("boom")),
        "g.b": _fake_guard("g.b", {"decision": "block", "reason": "b-blocks"}),
        "g.c": _fake_guard("g.c", {"systemMessage": "c-note"}),
    }

    def fake_import(path: str) -> types.ModuleType:
        calls.append(path)
        return mods[path]

    monkeypatch.setattr(m.importlib, "import_module", fake_import)
    outputs = m.dispatch({"cwd": "/x"}, ("g.a", "g.b", "g.c"))
    # A raised despite raising, B and C still ran (isolation).
    assert calls == ["g.a", "g.b", "g.c"]
    assert outputs[0] is None
    assert outputs[1] == {"decision": "block", "reason": "b-blocks"}
    assert outputs[2] == {"systemMessage": "c-note"}


# ─── compose_output: first-block-wins + systemMessage accumulation ───────────


def test_compose_first_block_wins_and_messages_accumulate() -> None:
    outputs = [
        {"systemMessage": "note-1"},
        {"decision": "block", "reason": "first-reason", "systemMessage": "note-2"},
        {"decision": "block", "reason": "second-reason"},
    ]
    composed = m.compose_output(outputs)
    assert composed is not None
    assert composed["decision"] == "block"
    assert composed["reason"] == "first-reason"  # first block wins
    assert composed["systemMessage"] == "note-1\n\nnote-2"  # accumulated in order


def test_compose_systemmessage_only() -> None:
    assert m.compose_output([{"systemMessage": "n"}]) == {"systemMessage": "n"}


def test_compose_all_none_is_none() -> None:
    assert m.compose_output([None, None, {}]) is None


# ─── end-to-end over the REAL guards ─────────────────────────────────────────


def test_main_blocks_on_pending_skill_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    committed = _committed_path(tmp_path, _KNOWN_SKILL)
    committed.parent.mkdir(parents=True, exist_ok=True)
    committed.write_text(json.dumps({"ok": True, "skill": _KNOWN_SKILL}), encoding="utf-8")
    monkeypatch.setattr("hpc_agent.cli.skill_returns.known_return_dirs", lambda: [])
    _stdin_bytes(monkeypatch, json.dumps({"cwd": str(tmp_path)}).encode("utf-8"))

    rc = m.main(list(_GUARDS))
    assert rc == 0
    raw = capsys.readouterr().out
    out = json.loads(raw)
    assert out["decision"] == "block"
    # The skill is named somewhere in the composed output — in the rejector's
    # reason, or (when the harness declares the completer) its systemMessage.
    assert _KNOWN_SKILL in raw


def test_main_noop_turn_prints_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin_bytes(monkeypatch, json.dumps({"cwd": str(tmp_path)}).encode("utf-8"))
    rc = m.main(list(_GUARDS))
    assert rc == 0
    assert capsys.readouterr().out == ""


# ─── acceptance: a no-op turn never imports the heavy guard chain ────────────


def test_noop_turn_does_not_import_guard_modules(tmp_path: Path) -> None:
    """A prefilter-skip exits without importing ANY of the three Stop guards (and
    thus none of their heavy dependency chain: block_drive / verify_relay / …)."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    script = (
        "import sys, json\n"
        "from hpc_agent._kernel.hooks import stop_multiplex as m\n"
        f"rc = m.main({list(_GUARDS)!r})\n"
        "guards = ["
        "'hpc_agent._kernel.hooks.skill_return_stop_guard',"
        "'hpc_agent._kernel.hooks.decision_rendezvous_stop_guard',"
        "'hpc_agent._kernel.hooks.relay_audit_stop',"
        "'hpc_agent._kernel.lifecycle.block_drive']\n"
        "loaded = [g for g in guards if g in sys.modules]\n"
        "print(json.dumps({'rc': rc, 'loaded': loaded}))\n"
    )
    env = {
        **_os_environ(),
        "HPC_JOURNAL_DIR": str(tmp_path / "_no_home"),
    }
    proc = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps({"cwd": str(cwd)}).encode("utf-8"),
        cwd=str(cwd),
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    result = json.loads(proc.stdout.decode())
    assert result["rc"] == 0
    assert result["loaded"] == [], f"guard chain imported on a no-op turn: {result['loaded']}"


def test_noop_turn_does_not_import_pathlib(tmp_path: Path) -> None:
    """The dry no-op hook path imports NO ``pathlib`` (hook-floor unit, 2026-07-17).

    The installed Stop hook runs ``python -m …stop_multiplex`` 3×/turn; the ruling
    made the prefilter ``os.path``-only AND dropped ``pathlib`` from the root
    ``hpc_agent.__init__`` module scope, so a prefilter-skip turn — the common case —
    loads no ``pathlib`` at all. Subprocess-isolated so a sibling test that imported
    ``pathlib`` earlier cannot mask the regression (base CPython does not preload it).
    Re-adding a module-scope ``from pathlib import Path`` to either the prefilter or
    the root ``__init__`` reds this."""
    cwd = tmp_path / "proj"
    cwd.mkdir()
    script = (
        "import sys, json\n"
        "from hpc_agent._kernel.hooks import stop_multiplex as m\n"
        f"rc = m.main({list(_GUARDS)!r})\n"
        "print(json.dumps({'rc': rc, 'pathlib': 'pathlib' in sys.modules}))\n"
    )
    # Scrub coverage's subprocess-start hook: under `pytest --cov` the child
    # would run `coverage.process_startup()` at interpreter init, and coverage
    # itself imports `pathlib` — polluting the child's sys.modules BEFORE our
    # code runs and false-failing this measurement (the c41c7d24 3.12-with-
    # coverage red; local runs have no --cov so never saw it).
    env = {
        k: v for k, v in _os_environ().items() if not (k.startswith("COV") or k == "PYTHONSTARTUP")
    }
    env["HPC_JOURNAL_DIR"] = str(tmp_path / "_no_home")
    proc = subprocess.run(
        [sys.executable, "-c", script],
        input=json.dumps({"cwd": str(cwd)}).encode("utf-8"),
        cwd=str(cwd),
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    result = json.loads(proc.stdout.decode())
    assert result["rc"] == 0
    assert result["pathlib"] is False, (
        "pathlib was imported on a no-op Stop turn — the prefilter / root __init__ "
        "must stay pathlib-free (hook-floor unit, 2026-07-17)"
    )


def _os_environ() -> dict[str, str]:
    import os

    return dict(os.environ)
