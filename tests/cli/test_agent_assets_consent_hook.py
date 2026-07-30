"""The consent-forwarding ``PreToolUse`` hook's INSTALL discipline.

``install_agent_assets`` wires
:mod:`hpc_agent._kernel.hooks.consent_forward` into
``<claude_dir>/settings.json``'s ``hooks.PreToolUse`` with matcher
``mcp__hpc-agent__.*``. The sibling ``test_agent_assets_settings_hook.py``
covers the shared merge contract; what is pinned HERE is what is specific to
this hook:

* the matcher is hpc-agent's OWN MCP tool surface (never ``Bash`` — the CLI
  form would need a second command parser this hook deliberately does not
  build);
* the pre-filter is DERIVED from ``block_chain.GATED_BLOCKS`` — a newly gated
  block must be covered with no edit to the installer, or its consent is
  silently never forwarded;
* the **exact-match removal discipline (539c1cdc)**: a prior version's entry is
  replaced IN PLACE and any duplicate is dropped, so two hooks never race on
  one event;
* :func:`consent_forward_hook_status` — the ``doctor`` check's substrate —
  reads that install back and reports absent / stale / duplicated.
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent.agent_assets import consent_forward_hook_status, install_agent_assets

_MODULE = "hpc_agent._kernel.hooks.consent_forward"
_MATCHER = "mcp__hpc-agent__.*"


def _settings(claude_dir: Path) -> dict:
    loaded = json.loads((claude_dir / "settings.json").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _entries(claude_dir: Path) -> list:
    pre_tool = _settings(claude_dir)["hooks"].get("PreToolUse", [])
    return [e for e in pre_tool if _MODULE in e["hooks"][0]["command"]]


# ─── the installed shape ────────────────────────────────────────────────────


def test_installed_with_the_mcp_tool_matcher(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_consent_forward_hook"]["action"] == "added"
    assert result["settings_consent_forward_hook"]["wrote"] is True

    entries = _entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["matcher"] == _MATCHER
    assert entries[0]["hooks"][0]["type"] == "command"
    # The needle-embed obligation: the probe / re-find both key on it.
    assert _MODULE in entries[0]["hooks"][0]["command"]


def test_prefilter_derives_from_the_gated_census(tmp_path: Path) -> None:
    """Every ``GATED_BLOCKS`` member + every always-ask verb is in the pre-filter.

    Derived, never a hand list: a block newly added to the census pays the
    interpreter start (and gets its consent forwarded) with no edit here.
    """
    from hpc_agent._kernel.hooks.consent_forward import ALWAYS_ASK_VERBS
    from hpc_agent.infra.block_chain import GATED_BLOCKS

    install_agent_assets(claude_dir=tmp_path)
    command = _entries(tmp_path)[0]["hooks"][0]["command"]
    for verb in GATED_BLOCKS | ALWAYS_ASK_VERBS:
        assert f"*{verb}*" in command, f"pre-filter does not cover {verb}"


def test_the_scheduler_fence_entry_is_untouched(tmp_path: Path) -> None:
    """Both PreToolUse hooks coexist — different matchers, different jobs."""
    install_agent_assets(claude_dir=tmp_path)
    pre_tool = _settings(tmp_path)["hooks"]["PreToolUse"]
    matchers = [e.get("matcher") for e in pre_tool]
    assert matchers == ["Bash", _MATCHER]


# ─── idempotency + the exact-match removal discipline (539c1cdc) ────────────


def test_rerun_does_not_duplicate(tmp_path: Path) -> None:
    install_agent_assets(claude_dir=tmp_path)
    second = install_agent_assets(claude_dir=tmp_path)
    assert second["settings_consent_forward_hook"]["action"] == "already-present"
    assert second["settings_consent_forward_hook"]["wrote"] is False
    assert len(_entries(tmp_path)) == 1


def test_stale_entry_is_replaced_in_place(tmp_path: Path) -> None:
    """The realistic upgrade: an older release's narrower pre-filter, moved venv.

    Exact-match on the module-path needle finds it and repoints it — the entry
    is REPLACED, never appended beside.
    """
    stale = {
        "matcher": _MATCHER,
        "hooks": [
            {
                "type": "command",
                "command": (
                    'input=$(cat); case "$input" in *submit-s2*) printf \'%s\' "$input" '
                    f"| /moved/venv/bin/python -m {_MODULE};; esac"
                ),
            }
        ],
    }
    (tmp_path / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [stale]}}), encoding="utf-8"
    )

    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_consent_forward_hook"]["action"] == "updated"

    entries = _entries(tmp_path)
    assert len(entries) == 1
    assert entries[0] != stale
    # The widened pre-filter now covers the whole census.
    assert "*aggregate-run*" in entries[0]["hooks"][0]["command"]


def test_entry_with_a_changed_matcher_is_repointed(tmp_path: Path) -> None:
    """Re-find is by NEEDLE, not by shape — a differently-matched prior entry heals."""
    (tmp_path / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "mcp__hpc-agent__submit-s2",
                            "hooks": [{"type": "command", "command": f"/old/python -m {_MODULE}"}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_consent_forward_hook"]["action"] == "updated"
    entries = _entries(tmp_path)
    assert len(entries) == 1
    assert entries[0]["matcher"] == _MATCHER


def test_duplicate_entries_are_collapsed_to_one(tmp_path: Path) -> None:
    """The 539c1cdc shape: a settings.json carrying our hook TWICE heals to one.

    A merge that only ever looked at the first match would heal that one and
    leave the second live — two hooks deciding every MCP call, forever. The
    surviving entry keeps the FIRST one's position (order under an event is
    load-bearing).
    """
    install_agent_assets(claude_dir=tmp_path)
    settings_path = tmp_path / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    ours = _entries(tmp_path)[0]
    other = {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo hi"}]}
    stale = json.loads(json.dumps(ours))
    stale["hooks"][0]["command"] = f"/moved/python -m {_MODULE}"
    settings["hooks"]["PreToolUse"] = [ours, other, stale]
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    result = install_agent_assets(claude_dir=tmp_path)
    assert result["settings_consent_forward_hook"]["removed_duplicates"] == 1

    pre_tool = _settings(tmp_path)["hooks"]["PreToolUse"]
    assert len(_entries(tmp_path)) == 1
    # The unrelated entry survives verbatim, after ours (the first position).
    assert other in pre_tool
    assert pre_tool.index(ours) < pre_tool.index(other)


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    result = install_agent_assets(claude_dir=tmp_path, dry_run=True)
    assert result["settings_consent_forward_hook"]["action"] == "dry-run-would-add"
    assert result["settings_consent_forward_hook"]["wrote"] is False
    assert not (tmp_path / "settings.json").exists()


# ─── the status read-back (the doctor check's substrate) ────────────────────


def test_status_round_trips_a_fresh_install(tmp_path: Path) -> None:
    absent = consent_forward_hook_status(claude_dir=tmp_path)
    assert absent == {
        "settings_path": str(tmp_path / "settings.json"),
        "hpc_hooks_present": False,
        "installed": False,
        "current": False,
        "duplicates": 0,
    }

    install_agent_assets(claude_dir=tmp_path)
    fresh = consent_forward_hook_status(claude_dir=tmp_path)
    assert fresh["hpc_hooks_present"] is True
    assert fresh["installed"] is True
    assert fresh["current"] is True
    assert fresh["duplicates"] == 0


def test_status_is_silent_on_a_foreign_settings_json(tmp_path: Path) -> None:
    """Someone else's settings.json is not OUR install — nothing to report.

    This is what keeps the ``doctor`` check quiet on a machine where hpc-agent
    was never installed into the harness, rather than nagging every run.
    """
    (tmp_path / "settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"command": "ruff check"}]}]}}),
        encoding="utf-8",
    )
    status = consent_forward_hook_status(claude_dir=tmp_path)
    assert status["hpc_hooks_present"] is False
    assert status["installed"] is False


def test_status_flags_a_stale_entry(tmp_path: Path) -> None:
    install_agent_assets(claude_dir=tmp_path)
    settings_path = tmp_path / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    for entry in settings["hooks"]["PreToolUse"]:
        if _MODULE in entry["hooks"][0]["command"]:
            entry["hooks"][0]["command"] = f"/moved/python -m {_MODULE}"
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    status = consent_forward_hook_status(claude_dir=tmp_path)
    assert status["hpc_hooks_present"] is True
    assert status["installed"] is True
    assert status["current"] is False

    # …and a re-install heals it.
    install_agent_assets(claude_dir=tmp_path)
    assert consent_forward_hook_status(claude_dir=tmp_path)["current"] is True


def test_status_counts_duplicates(tmp_path: Path) -> None:
    install_agent_assets(claude_dir=tmp_path)
    settings_path = tmp_path / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    ours = _entries(tmp_path)[0]
    settings["hooks"]["PreToolUse"] = [ours, json.loads(json.dumps(ours))]
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    assert consent_forward_hook_status(claude_dir=tmp_path)["duplicates"] == 1
    install_agent_assets(claude_dir=tmp_path)
    assert consent_forward_hook_status(claude_dir=tmp_path)["duplicates"] == 0


def test_status_never_raises_on_a_broken_settings_json(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{ not json", encoding="utf-8")
    status = consent_forward_hook_status(claude_dir=tmp_path)
    assert status["hpc_hooks_present"] is False
    assert status["installed"] is False
