"""Install hpc-agent's bundled slash commands and skills into ``~/.claude/``.

The CLI surface is ``hpc-agent install-commands`` and lives in
:mod:`hpc_agent.cli.setup`; this module provides the copy logic so a pip-only
install (no repo checkout) can still wire the agent assets into Claude
Code's user-global config directory.

The core asset trees ship as package data inside the
``hpc_agent.slash_commands`` subpackage — ``slash_commands/commands/*.md``,
``slash_commands/skills/<name>/SKILL.md``, and
``slash_commands/agents/<name>.md`` (named subagent definitions; core
ships none since the §6 worker removal — the tree remains for plugins).
Optional plugins may ship their own ``commands/`` + ``skills/`` +
``agents/`` trees via the ``slash_command_assets`` hook on the
``hpc_agent.plugins`` seam; those are installed *after* the core
assets, so a plugin's copy of an asset overrides the core one of the
same name (last writer wins by path).
"""

from __future__ import annotations

import json
import shlex
import shutil
import sys
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any

from hpc_agent.infra.clusters import load_clusters_config

__all__ = ["DEFAULT_CLAUDE_DIR", "install_agent_assets"]


def _hook_python() -> str:
    """Bash-safe path to the current Python interpreter for hook commands.

    Claude Code runs hooks via ``bash -c '<command>'``. Two Windows pitfalls
    the raw ``sys.executable`` walks into:

    * **Backslashes.** ``sys.executable`` is a native backslash path on Windows
      (e.g. ``C:\\Users\\james\\.venv\\Scripts\\python.exe``). Bash treats
      ``\\U``, ``\\j``, ``\\d`` etc. as escape sequences and collapses the
      backslash, turning the path into ``C:Usersjames.venvScriptspython.exe``
      → "command not found". Forward slashes are universally accepted by
      Windows for executable invocation and pass through bash unchanged.
    * **Spaces.** Some interpreter paths contain spaces (e.g.
      ``C:/Program Files/Python311/python.exe`` or a repo dir with a space).
      Without quoting bash splits on the space and tries to run a non-existent
      first token. ``shlex.quote`` wraps the path in single quotes when needed.
    """
    return shlex.quote(sys.executable.replace("\\", "/"))


def _build_hook_command() -> str:
    """The autofetch ``PostToolUse`` hook command, with a bash-level pre-filter.

    ``matcher: "Bash"`` fires this hook after **every** Bash call, and a
    Python interpreter start costs ~300-500ms on Windows (#288). The ``case``
    pre-filter keeps the non-emit common path at bash-builtin cost: only a
    payload mentioning ``emit-skill-return`` reaches Python. (The substring
    scan over the whole payload can false-positive on e.g. an unrelated
    command echoing the verb name — that costs one no-op interpreter start,
    nothing more.)
    """
    return (
        'input=$(cat); case "$input" in *emit-skill-return*) '
        f"printf '%s' \"$input\" | {_hook_python()} "
        "-m hpc_agent._kernel.hooks.skill_return_autofetch;; esac"
    )


def _build_stop_hook_command() -> str:
    """The stop-guard ``Stop`` hook command — no pre-filter.

    Stop fires once per turn (not per tool call), so the interpreter start is
    paid rarely; the guard itself needs the filesystem probe either way.
    """
    return f"{_hook_python()} -m hpc_agent._kernel.hooks.skill_return_stop_guard"


def _build_rendezvous_autofetch_command() -> str:
    """The decision-rendezvous ``PostToolUse`` hook command, with a pre-filter.

    Mirrors :func:`_build_hook_command`: ``matcher: "Bash"`` fires after every
    Bash call, so a ``case`` pre-filter keeps the non-``block-drive`` common
    path at bash-builtin cost — only a payload mentioning ``block-drive``
    reaches Python (which then reads the freshly-parked brief back).
    """
    return (
        'input=$(cat); case "$input" in *block-drive*) '
        f"printf '%s' \"$input\" | {_hook_python()} "
        "-m hpc_agent._kernel.hooks.decision_rendezvous_autofetch;; esac"
    )


def _build_rendezvous_stop_hook_command() -> str:
    """The decision-rendezvous ``Stop`` guard command — no pre-filter.

    Sibling of :func:`_build_stop_hook_command`: Stop fires once per turn, so
    the interpreter start is paid rarely and the guard needs the journal probe
    either way.
    """
    return f"{_hook_python()} -m hpc_agent._kernel.hooks.decision_rendezvous_stop_guard"


# The ``PostToolUse`` hook that auto-fetches a sub-skill's return envelope the
# moment the sub-skill's ``emit-skill-return`` Bash call commits it (see
# :mod:`hpc_agent._kernel.hooks.skill_return_autofetch` for why the trigger is
# the emit Bash call and not the Skill tool — the Skill tool returns *before*
# the sub-skill body runs, so a Skill-matched hook can never see a fresh
# envelope). install-commands merges this entry into
# ``~/.claude/settings.json``'s ``hooks.PostToolUse`` array, additively,
# idempotently, and self-healing on a stale prior install (including the
# pre-0.10.58 ``matcher: "Skill"`` shape).
_HOOK_COMMAND = _build_hook_command()
_AUTOFETCH_NEEDLE = "hpc_agent._kernel.hooks.skill_return_autofetch"
_SKILL_RETURN_HOOK_ENTRY: dict[str, Any] = {
    "matcher": "Bash",
    "hooks": [
        {
            "type": "command",
            "command": _HOOK_COMMAND,
        }
    ],
}

# The ``Stop`` hook that blocks ending the turn while a committed sub-skill
# return envelope sits unfetched (see
# :mod:`hpc_agent._kernel.hooks.skill_return_stop_guard`). Deterministic
# backstop for the advisory hand-back prose at sub-skill composition
# boundaries. Stop entries take no matcher (there is no tool to match).
_STOP_HOOK_COMMAND = _build_stop_hook_command()
_STOP_GUARD_NEEDLE = "hpc_agent._kernel.hooks.skill_return_stop_guard"
_SKILL_RETURN_STOP_ENTRY: dict[str, Any] = {
    "hooks": [
        {
            "type": "command",
            "command": _STOP_HOOK_COMMAND,
        }
    ],
}

# The ``block-drive`` decision-rendezvous pair (see
# :mod:`hpc_agent._kernel.hooks.decision_rendezvous_autofetch` /
# ``decision_rendezvous_stop_guard``), generalizing the skill-return pair to the
# §5 y/nudge boundary. The PostToolUse autofetch injects the brief a
# ``block-drive`` tick just parked; the Stop guard blocks ending the turn once a
# human ``y`` is committed but the driver has not advanced. Both merge additively
# + idempotently, matched on the module-path needle.
_RENDEZVOUS_AUTOFETCH_COMMAND = _build_rendezvous_autofetch_command()
_RENDEZVOUS_AUTOFETCH_NEEDLE = "hpc_agent._kernel.hooks.decision_rendezvous_autofetch"
_RENDEZVOUS_AUTOFETCH_ENTRY: dict[str, Any] = {
    "matcher": "Bash",
    "hooks": [
        {
            "type": "command",
            "command": _RENDEZVOUS_AUTOFETCH_COMMAND,
        }
    ],
}

_RENDEZVOUS_STOP_COMMAND = _build_rendezvous_stop_hook_command()
_RENDEZVOUS_STOP_NEEDLE = "hpc_agent._kernel.hooks.decision_rendezvous_stop_guard"
_RENDEZVOUS_STOP_ENTRY: dict[str, Any] = {
    "hooks": [
        {
            "type": "command",
            "command": _RENDEZVOUS_STOP_COMMAND,
        }
    ],
}


# The scheduler write-fence (conduct rule 7, proving-run-3 finding (d)):
# mutating scheduler verbs (qsub/sbatch/qdel/scancel/qmod/qalter) are blocked
# from the agent's Bash — including inside an ssh transport — while read-only
# probes (qstat/squeue/qacct, plain ssh) stay allowed ("consequences are gated,
# curiosity isn't" — James, 2026-07-04). PreToolUse + exit-2 blocks the call;
# the bash ``case`` pre-filter keeps every non-matching Bash call at builtin
# cost, and the Python side does command-position analysis so innocent
# mentions (``grep qsub log``) pass.
def _build_write_fence_command() -> str:
    return (
        'input=$(cat); case "$input" in '
        "*qsub*|*sbatch*|*qdel*|*scancel*|*qmod*|*qalter*) "
        f"printf '%s' \"$input\" | {_hook_python()} "
        "-m hpc_agent._kernel.hooks.scheduler_write_fence;; esac"
    )


_WRITE_FENCE_COMMAND = _build_write_fence_command()
_WRITE_FENCE_NEEDLE = "hpc_agent._kernel.hooks.scheduler_write_fence"
_WRITE_FENCE_ENTRY: dict[str, Any] = {
    "matcher": "Bash",
    "hooks": [
        {
            "type": "command",
            "command": _WRITE_FENCE_COMMAND,
        }
    ],
}


# The watchdog alert-count ``SessionStart`` hook (proving run #3: the scheduled
# doctor wrote the stalled-driver alert to doctor.alerts.log and nothing
# delivered it — detection without delivery is silence). Fires once per session
# start (no matcher, no pre-filter — the interpreter start is paid rarely) and
# prints "N unacknowledged hpc-agent watchdog alert(s) ..." to stdout, which
# the harness injects as session context. Notify only: it never re-arms and
# never acknowledges (the status-snapshot watermark owns acknowledgment); the
# alert read is fail-open and non-creating, so a session started in an
# unrelated repo is a clean silent no-op.
def _build_alert_count_command() -> str:
    return f"{_hook_python()} -m hpc_agent._kernel.hooks.alert_count"


_ALERT_COUNT_COMMAND = _build_alert_count_command()
_ALERT_COUNT_NEEDLE = "hpc_agent._kernel.hooks.alert_count"
_ALERT_COUNT_ENTRY: dict[str, Any] = {
    "hooks": [
        {
            "type": "command",
            "command": _ALERT_COUNT_COMMAND,
        }
    ],
}


# The human-utterance capture ``UserPromptSubmit`` hook (proving run #4: the
# authorship gate verified value tokens against journal ``response`` fields the
# agent itself writes — friction, not a lock). Fires on every prompt submit (no
# matcher, no pre-filter — one interpreter start per human prompt is rare) and
# appends the prompt (ts + sha256 + size-capped raw text) to the cwd repo's
# ``<journal home>/<repo_hash>/utterances.jsonl`` — harness-written, so the
# authorship gate can require caller values to derive from text a human
# verifiably typed. No-scaffold: a prompt in a non-hpc repo is a silent no-op,
# and the hook prints nothing (its record must stay out of model context).
def _build_utterance_capture_command() -> str:
    return f"{_hook_python()} -m hpc_agent._kernel.hooks.utterance_capture"


_UTTERANCE_CAPTURE_COMMAND = _build_utterance_capture_command()
_UTTERANCE_CAPTURE_NEEDLE = "hpc_agent._kernel.hooks.utterance_capture"
_UTTERANCE_CAPTURE_ENTRY: dict[str, Any] = {
    "hooks": [
        {
            "type": "command",
            "command": _UTTERANCE_CAPTURE_COMMAND,
        }
    ],
}


# The AskUserQuestion answer-capture ``PostToolUse`` hook (proving run #5:
# answers given through the question selector never pass UserPromptSubmit, so
# a human who TYPED the sweep into the tool's free-text field was invisible
# to the authorship gate). Captures only TYPED answer text — a click on an
# agent-authored option label is never logged (that would reopen the
# laundering channel the utterance lock closes). Matched on the tool name, so
# it fires rarely; no bash pre-filter needed.
def _build_answer_capture_command() -> str:
    return f"{_hook_python()} -m hpc_agent._kernel.hooks.answer_capture"


_ANSWER_CAPTURE_COMMAND = _build_answer_capture_command()
_ANSWER_CAPTURE_NEEDLE = "hpc_agent._kernel.hooks.answer_capture"
_ANSWER_CAPTURE_ENTRY: dict[str, Any] = {
    "matcher": "AskUserQuestion",
    "hooks": [
        {
            "type": "command",
            "command": _ANSWER_CAPTURE_COMMAND,
        }
    ],
}


# The relay-audit ``Stop`` hook (conduct rule 10, staged → active): nothing made
# a driving agent run ``verify-relay``, so an unaudited relay still reached the
# human. Fires once per turn end (no matcher — the interpreter start is paid
# rarely), reads the final assistant text from the transcript, and when it names
# a journaled run, audits it with verify-relay; contradiction mismatches block
# the stop ONCE (loop-safe via stop_hook_active, same as the sibling Stop
# guards) with the itemized summary so the agent corrects the relay. Fail-open:
# no journal / no run mention / clean audit / any error → silent pass.
def _build_relay_audit_command() -> str:
    return f"{_hook_python()} -m hpc_agent._kernel.hooks.relay_audit_stop"


_RELAY_AUDIT_COMMAND = _build_relay_audit_command()
_RELAY_AUDIT_NEEDLE = "hpc_agent._kernel.hooks.relay_audit_stop"
_RELAY_AUDIT_ENTRY: dict[str, Any] = {
    "hooks": [
        {
            "type": "command",
            "command": _RELAY_AUDIT_COMMAND,
        }
    ],
}


# The registry-projected MCP server (``hpc-agent mcp-serve``) — the preferred,
# shell-free invocation surface for blocks (design §3, "The tool surface subsumes
# the shell"). Registered venv-pinned via the current interpreter so it does not
# depend on ``hpc-agent`` being on PATH. ``--allow-mutations`` exposes the
# submit/aggregate verbs (cancel/raw-submit are never registry primitives, so they
# stay unreachable either way); ``--catalog curated`` advertises exactly the
# human-amplification block verbs (those returning a next_block) plus the
# recovery/opt-in verbs (doctor, kill, submit-speculate), keeping the rest of the
# catalog out of the model's context.
_MCP_SERVER_NAME = "hpc-agent"
_MCP_SERVER_ENTRY: dict[str, Any] = {
    "type": "stdio",
    "command": sys.executable,
    "args": ["-m", "hpc_agent", "mcp-serve", "--allow-mutations", "--catalog", "curated"],
}


def _mcp_config_path(claude_dir: Path) -> Path:
    """Where Claude Code reads user-global MCP servers from: ``.claude.json``.

    Claude Code discovers user-global MCP servers in ``~/.claude.json`` (the
    top-level ``mcpServers`` object), a SIBLING of the ``~/.claude`` config dir —
    not inside it. So the path is derived from *claude_dir*'s parent, which makes
    it hermetic under a test-supplied ``claude_dir`` (``tmp/.claude`` →
    ``tmp/.claude.json``) while resolving to ``~/.claude.json`` for the default
    install. (A user who relocates the whole config via ``CLAUDE_CONFIG_DIR`` and
    passes that as ``claude_dir`` gets ``.claude.json`` alongside it, which is the
    same sibling relationship.)
    """
    return claude_dir.parent / ".claude.json"


def _register_mcp_server(claude_dir: Path, *, dry_run: bool) -> dict[str, Any]:
    """Additively, idempotently register the ``hpc-agent`` MCP server in
    ``.claude.json``'s ``mcpServers`` object.

    Same contract as :func:`_merge_hook_entry`, targeting ``mcpServers`` in
    ``.claude.json`` rather than ``hooks`` in ``settings.json``: creates an empty
    ``{}`` model when the file is absent/unreadable, refuses to clobber a present
    file that is not a JSON object (``skipped-unparseable``), and only ever adds
    or in-place heals our one ``hpc-agent`` entry — every other server and key is
    preserved verbatim. Idempotent: a byte-equal entry is ``already-present``; a
    stale entry (e.g. a moved venv changing the interpreter path) is ``updated``.

    Returns ``{config_path, action, wrote}`` where ``action`` is ``"added"`` /
    ``"updated"`` / ``"already-present"`` / ``"skipped-unparseable"`` /
    ``"dry-run-would-add"`` / ``"dry-run-would-update"``.
    """
    config_path = _mcp_config_path(claude_dir)

    config: dict[str, Any]
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {
                "config_path": str(config_path),
                "action": "skipped-unparseable",
                "wrote": False,
            }
        if not isinstance(loaded, dict):
            return {
                "config_path": str(config_path),
                "action": "skipped-unparseable",
                "wrote": False,
            }
        config = loaded
    else:
        config = {}

    servers = config.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}

    existing = servers.get(_MCP_SERVER_NAME)
    # A user-set ``env`` on the existing registration (e.g.
    # ``HPC_SSH_ENGINE=asyncssh`` opting the demo server into the connection
    # engine) is the USER'S config, not ours: an install heals OUR keys
    # (command/args after a moved venv) but must never destroy theirs —
    # rewriting the entry wholesale silently un-set the engine flag on every
    # install-commands run.
    desired: dict[str, Any] = dict(_MCP_SERVER_ENTRY)
    if isinstance(existing, dict) and isinstance(existing.get("env"), dict) and existing["env"]:
        desired["env"] = existing["env"]
    if existing == desired:
        return {"config_path": str(config_path), "action": "already-present", "wrote": False}

    if dry_run:
        action = "dry-run-would-update" if existing is not None else "dry-run-would-add"
        return {"config_path": str(config_path), "action": action, "wrote": False}

    servers = dict(servers)
    action = "updated" if existing is not None else "added"
    servers[_MCP_SERVER_NAME] = desired
    config["mcpServers"] = servers

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return {"config_path": str(config_path), "action": action, "wrote": True}


def DEFAULT_CLAUDE_DIR() -> Path:
    """Return ``~/.claude`` (does not create the directory)."""
    return Path.home() / ".claude"


def _find_hook_entry_index(entries: list[Any], needle: str) -> int | None:
    """Return the index of the existing entry matching *needle*, or ``None``.

    Match key: any hook entry whose ``hooks`` list contains a ``command`` hook
    whose command mentions *needle* (a hook module path). We match on the
    module path (not the full command string) so a re-run from a different
    ``sys.executable`` — moved venv, **an upgrade that fixes the command
    encoding**, or one that changes the matcher/pre-filter shape — still finds
    the existing entry instead of appending a duplicate.
    """
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        hooks = entry.get("hooks")
        if not isinstance(hooks, list):
            continue
        for hook in hooks:
            if (
                isinstance(hook, dict)
                and hook.get("type") == "command"
                and isinstance(hook.get("command"), str)
                and needle in hook["command"]
            ):
                return i
    return None


def _merge_hook_entry(
    claude_dir: Path, *, event: str, entry: dict[str, Any], needle: str, dry_run: bool
) -> dict[str, Any]:
    """Additively, idempotently wire one hook *entry* into ``settings.json``.

    Reads ``<claude_dir>/settings.json`` (creating an empty ``{}`` model when it
    is absent or unreadable), appends *entry* to ``hooks.<event>`` unless an
    equivalent entry (matched by *needle*, the hook's module path) is already
    present, and writes the merged settings back (pretty-printed, trailing
    newline). Every other key and every other entry under *event* is preserved
    verbatim — the merge only ever *adds* (or in-place heals) our one entry.

    Returns a small report ``{settings_path, action, wrote}`` where ``action``
    is one of ``"added"`` (appended), ``"updated"`` (a stale entry from an
    earlier install — e.g. a moved venv, the pre-0.10.10 backslash-encoded
    Windows path that bash mis-interpreted as escapes, or the pre-0.10.58
    ``matcher: "Skill"`` autofetch shape — replaced in place),
    ``"already-present"`` (byte-equal to the canonical entry; idempotent
    no-op), ``"skipped-unparseable"`` (existing settings.json is not a JSON
    object — we refuse to clobber it), ``"dry-run-would-add"``, or
    ``"dry-run-would-update"``.

    Safety: if ``settings.json`` exists but does not parse as a JSON **object**,
    we do **not** overwrite it — the install reports ``skipped-unparseable`` so
    a human can resolve it rather than risking the loss of hand-written config.
    """
    settings_path = claude_dir / "settings.json"

    settings: dict[str, Any]
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            # Present but unreadable / not JSON. A settings.json is precious
            # user config; refuse to clobber it rather than guess.
            return {
                "settings_path": str(settings_path),
                "action": "skipped-unparseable",
                "wrote": False,
            }
        if not isinstance(loaded, dict):
            return {
                "settings_path": str(settings_path),
                "action": "skipped-unparseable",
                "wrote": False,
            }
        settings = loaded
    else:
        settings = {}

    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        # Absent or wrong-typed ``hooks`` → start a fresh mapping. (A non-dict
        # ``hooks`` would itself be malformed Claude config; replacing it is the
        # only way to wire our entry, and we only do so when adding.)
        hooks = {}
    event_entries = hooks.get(event)
    if not isinstance(event_entries, list):
        event_entries = []

    existing_idx = _find_hook_entry_index(event_entries, needle)
    if existing_idx is not None and event_entries[existing_idx] == entry:
        return {
            "settings_path": str(settings_path),
            "action": "already-present",
            "wrote": False,
        }

    if dry_run:
        return {
            "settings_path": str(settings_path),
            "action": "dry-run-would-update" if existing_idx is not None else "dry-run-would-add",
            "wrote": False,
        }

    event_entries = list(event_entries)
    if existing_idx is not None:
        event_entries[existing_idx] = entry
        action = "updated"
    else:
        event_entries.append(entry)
        action = "added"
    hooks[event] = event_entries
    settings["hooks"] = hooks

    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(settings, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    return {
        "settings_path": str(settings_path),
        "action": action,
        "wrote": True,
    }


def _skill_allow_rule(skill_name: str) -> str:
    """Return the canonical ``permissions.allow`` entry for a Skill grant.

    Mirrors the ``Bash(<prefix>:*)`` parameterised matcher format Claude Code
    uses (the ``Bash(hpc-agent:*)`` grant this installer writes user-globally).
    For Skill, the natural matcher is the skill name, so the entry is
    ``Skill(<name>)`` — narrowest grant per bundled skill rather than a
    blanket ``"Skill"``.
    """
    return f"Skill({skill_name})"


def _merge_skill_permissions(
    claude_dir: Path, skill_names: list[str], *, dry_run: bool
) -> dict[str, Any]:
    """Idempotently add ``Skill(<name>)`` allow rules for every installed skill.

    Without these grants, Claude Code's auto-mode classifier silently denies
    the first ``Skill(<name>)`` call from ``/submit-hpc`` /
    ``/aggregate-hpc`` / ``/monitor-hpc`` / ``/campaign-hpc`` (empirical
    2026-06-06 demo: ``Skill(hpc-submit)`` blocked with ``Denied by auto
    mode classifier`` despite ``skipAutoPermissionPrompt: true`` — the flag
    only suppresses the explicit prompt, the classifier still gates).

    Sibling of :func:`_merge_hook_entry`: same additive + idempotent
    + skip-unparseable + dry-run semantics, but targets
    ``permissions.allow`` rather than ``hooks.PostToolUse``. User-global
    scope here (the bundled skills are user-global; the orchestrator can
    invoke ``/submit-hpc`` from any working directory) — distinct from
    ``ops/memory/interview.py``'s project-scoped Bash grant (#190).

    Returns ``{settings_path, action, added, wrote}`` where ``action`` is:

    * ``"added"`` — at least one new ``Skill(<name>)`` rule appended
    * ``"already-present"`` — every rule already in ``permissions.allow``
    * ``"skipped-unparseable"`` — existing settings.json is not a JSON object
    * ``"dry-run-would-add"`` — would have added rules but ``dry_run=True``

    ``added`` lists the rule strings actually appended (empty on
    ``"already-present"``); on dry-run, lists the strings that *would*
    have been added.
    """
    settings_path = claude_dir / "settings.json"

    settings: dict[str, Any]
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {
                "settings_path": str(settings_path),
                "action": "skipped-unparseable",
                "added": [],
                "wrote": False,
            }
        if not isinstance(loaded, dict):
            return {
                "settings_path": str(settings_path),
                "action": "skipped-unparseable",
                "added": [],
                "wrote": False,
            }
        settings = loaded
    else:
        settings = {}

    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    allow = permissions.get("allow")
    if not isinstance(allow, list):
        allow = []

    canonical_rules = [_skill_allow_rule(name) for name in skill_names]
    missing = [rule for rule in canonical_rules if rule not in allow]

    if not missing:
        return {
            "settings_path": str(settings_path),
            "action": "already-present",
            "added": [],
            "wrote": False,
        }

    if dry_run:
        return {
            "settings_path": str(settings_path),
            "action": "dry-run-would-add",
            "added": missing,
            "wrote": False,
        }

    allow = list(allow) + missing
    permissions["allow"] = allow
    settings["permissions"] = permissions

    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(settings, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    return {
        "settings_path": str(settings_path),
        "action": "added",
        "added": missing,
        "wrote": True,
    }


# Raw-ssh / raw-scp DENY rules (anti-vendor-lockout ruling (a), 2026-07-10;
# NARROWED 2026-07-10 per user: "hpc-agent should be a TOOL and not something
# that takes over the user's entire workspace"). The improvisation class — an
# agent hand-rolling ``ssh <cluster> "<cmd>"`` / ``scp`` instead of a sanctioned
# verb — bypasses the #346 connection-storm guards (ConnectTimeout /
# IdentitiesOnly / the per-host safe_interval throttle in infra/ssh_throttle)
# that only protect the cluster when ALL SSH flows through infra.remote.ssh_run.
# The lint (scripts/lint_no_raw_ssh.py) already removes the affordance from
# agent-facing PROSE; this closes the runtime side: a DENY on the agent's Bash
# tool so a raw ssh/scp the model authors AT RUN TIME dies at the permission
# layer, not in honor-system conduct prose.
#
# The original ruling text said "against cluster hosts" — the first install
# wrote a BLANKET ``Bash(ssh:*)`` / ``Bash(scp:*)`` deny into the user-GLOBAL
# ``~/.claude/settings.json``, which blocked ALL ssh/scp in EVERY project on the
# box (tool-that-takes-over). The narrowing: derive the deny from the CONFIGURED
# CLUSTER HOSTS the install can see (packaged default + user overrides via
# :func:`hpc_agent.infra.clusters.load_clusters_config`) and emit HOST-SCOPED
# rules — ``Bash(ssh *<host>*)`` / ``Bash(scp *<host>*)`` per host. So only ssh
# to a configured cluster is denied; ssh to any other host (a colleague's box, a
# git remote, a VM) is untouched. When no hosts are resolvable at install time we
# install NO deny rules at all — the user-side cluster-ssh confirm-guard hook is
# the backstop. The sanctioned hpc-agent verbs dial ssh INSIDE their own
# subprocesses (never via the agent's Bash tool), so they are unaffected either
# way.
#
# Rule form: the ``Bash(<pat>)`` matcher globs ``*`` anywhere (per the Claude
# Code settings docs, whose own deny example is ``Bash(curl *)``). ``ssh
# *<host>*`` matches the ``ssh`` command with the host token anywhere in the
# argv — as ``ssh user@host``, ``ssh -i key host cmd``, or a bare ``ssh host`` —
# while ``ssh-keygen`` / ``ssh-add`` (distinct command tokens with no cluster
# host) and the identifier forms ``ssh_run`` / ``ssh_target`` do not match a
# real cluster host and are not denied.

# The over-broad BLANKET rules the pre-narrowing install wrote user-globally.
# The installer REMOVES exactly these two on every run so an upgrade heals the
# over-reach — matched by exact string, so no other ``deny`` entry is touched.
_BLANKET_SSH_DENY_RULES: list[str] = ["Bash(ssh:*)", "Bash(scp:*)"]


def _configured_cluster_hosts() -> list[str]:
    """Resolve the SSH host tokens from the clusters config the install can see.

    Uses :func:`hpc_agent.infra.clusters.load_clusters_config`, which searches
    (in order) ``HPC_CLUSTERS_CONFIG`` → ``~/.hpc-agent/clusters.yaml`` → the
    packaged ``config/clusters.yaml`` default — so a user override wins and a
    bare pip install still sees the shipped hoffman2 / discovery hosts. Returns
    the sorted, de-duplicated ``host`` values, skipping empty entries and the
    angle-bracketed ``<...>`` placeholders the bundled template ships (a
    placeholder host is not a real cluster to scope a deny to). Best-effort: a
    bad / missing config yields no hosts (and thus no deny rules) rather than
    breaking the install.
    """
    try:
        config = load_clusters_config()
    except Exception:  # noqa: BLE001 — a bad/missing config must not break install
        return []
    if not isinstance(config, dict):
        return []
    hosts: list[str] = []
    for entry in config.values():
        if not isinstance(entry, dict):
            continue
        host = entry.get("host")
        if not isinstance(host, str):
            continue
        host = host.strip()
        if not host or host.startswith("<"):
            continue
        if host not in hosts:
            hosts.append(host)
    return sorted(hosts)


def _raw_ssh_deny_rules(hosts: list[str]) -> list[str]:
    """Host-scoped raw-ssh/scp DENY rules — two per configured cluster *host*.

    ``Bash(ssh *<host>*)`` + ``Bash(scp *<host>*)`` for each host (see the module
    comment above for the glob semantics and the tool-not-takeover narrowing).
    Empty when *hosts* is empty — no configured cluster means no deny rules.
    """
    rules: list[str] = []
    for host in hosts:
        rules.append(f"Bash(ssh *{host}*)")
        rules.append(f"Bash(scp *{host}*)")
    return rules


def _merge_deny_rules(
    claude_dir: Path,
    deny_rules: list[str],
    *,
    remove_rules: list[str],
    dry_run: bool,
) -> dict[str, Any]:
    """Idempotently host-scope the raw-ssh/scp ``Bash(...)`` DENY rules.

    Two coupled operations on ``permissions.deny``, both narrow:

    * **add** the host-scoped *deny_rules* (``Bash(ssh *<host>*)`` /
      ``Bash(scp *<host>*)`` — see :func:`_raw_ssh_deny_rules`) that aren't
      already present; and
    * **remove** any *remove_rules* still present — the over-broad blanket
      ``Bash(ssh:*)`` / ``Bash(scp:*)`` an earlier install wrote
      (:data:`_BLANKET_SSH_DENY_RULES`), so an upgrade heals the over-reach.

    Sibling of :func:`_merge_skill_permissions`: same skip-unparseable +
    dry-run contract. Removal is by **exact string** against *remove_rules*
    only, so every other ``deny`` entry (``Bash(rm -rf:*)``, a user's own rule)
    and every other permission key (``allow``) is preserved verbatim. Idempotent:
    a settings file already host-scoped and blanket-free is an
    ``"already-present"`` no-op. When *deny_rules* is empty (no configured
    hosts) nothing is added, but a stale blanket rule is still removed — the
    migration heals the over-reach even on a host-less box.

    Returns ``{settings_path, action, added, removed, wrote}`` where ``action``
    is:

    * ``"added"`` — at least one host-scoped rule appended (blanket rules, if
      any, removed in the same write)
    * ``"updated"`` — nothing to add, but a stale blanket rule was removed
    * ``"already-present"`` — nothing to add and no blanket rule to remove
    * ``"skipped-unparseable"`` — existing settings.json is not a JSON object
    * ``"dry-run-would-add"`` — would have written but ``dry_run=True``

    ``added`` lists the rule strings actually appended, ``removed`` the blanket
    rules actually dropped (both empty on ``"already-present"``); on dry-run,
    the strings that *would* have been added / removed.
    """
    settings_path = claude_dir / "settings.json"

    settings: dict[str, Any]
    if settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return {
                "settings_path": str(settings_path),
                "action": "skipped-unparseable",
                "added": [],
                "removed": [],
                "wrote": False,
            }
        if not isinstance(loaded, dict):
            return {
                "settings_path": str(settings_path),
                "action": "skipped-unparseable",
                "added": [],
                "removed": [],
                "wrote": False,
            }
        settings = loaded
    else:
        settings = {}

    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    deny = permissions.get("deny")
    if not isinstance(deny, list):
        deny = []

    missing = [rule for rule in deny_rules if rule not in deny]
    stale = [rule for rule in remove_rules if rule in deny]

    if not missing and not stale:
        return {
            "settings_path": str(settings_path),
            "action": "already-present",
            "added": [],
            "removed": [],
            "wrote": False,
        }

    if dry_run:
        return {
            "settings_path": str(settings_path),
            "action": "dry-run-would-add",
            "added": missing,
            "removed": stale,
            "wrote": False,
        }

    # Drop the blanket rules (exact-string only), keep every other entry, then
    # append the host-scoped rules that were missing.
    deny = [rule for rule in deny if rule not in remove_rules] + missing
    permissions["deny"] = deny
    settings["permissions"] = permissions

    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(settings, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    return {
        "settings_path": str(settings_path),
        "action": "added" if missing else "updated",
        "added": missing,
        "removed": stale,
        "wrote": True,
    }


def _resolve_dir_collision(target: Path, kind_phrase: str, *, dry_run: bool) -> str | None:
    """Resolve a pre-existing non-directory at *target* before mkdir.

    The three install targets (``commands``/``skills``/``agents``) need to
    become directories. A pre-existing path collides; resolution depends on
    what's actually sitting there:

    * Missing or already a directory → nothing to do, returns ``None``.
    * Regular file with **zero bytes** → silently unlinked (or, in dry-run
      mode, just reported). A 0-byte file can't carry meaningful user
      content, and it's the empirically observed shape of stale scaffolding
      artifacts on Windows (touch-then-crash, abandoned old-version
      installs, etc.). Returns the cleared path as a string.
    * Any other non-directory → raises :class:`FileExistsError` with a
      clear remediation message. This is the historical guard preserved
      for the only case where the user might lose real content.

    *kind_phrase* is the inline phrase used in the error message
    (e.g. ``"slash commands"``).
    """
    if not target.exists() or target.is_dir():
        return None
    try:
        is_zero_byte = target.is_file() and target.stat().st_size == 0
    except OSError:
        is_zero_byte = False
    if is_zero_byte:
        if not dry_run:
            target.unlink()
        return str(target)
    raise FileExistsError(
        f"{target} exists but is not a directory — "
        f"hpc-agent setup needs to install {kind_phrase} here. "
        "Move or remove the conflicting file, then re-run."
    )


def _skill_is_internal(skill_dir: Any) -> bool:
    """True when a skill's ``SKILL.md`` frontmatter marks it maintainer-only.

    A skill flagged ``internal: true`` (or ``distribution: maintainer``) in its
    leading ``---``-fenced YAML frontmatter is a maintainer procedure (the
    ``release`` skill bumps versions, commits, and builds wheels) that must never
    be copied into an end user's ``~/.claude/skills`` nor granted an auto-invoke
    ``Skill(...)`` permission (bug-sweep #58). Parsed with a minimal frontmatter
    scan — no yaml dependency, and only the frontmatter block is consulted.
    """
    md = skill_dir / "SKILL.md"
    try:
        text = md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if not sep:
            continue
        k = key.strip().lower()
        v = value.strip().strip('"').strip("'").lower()
        if k == "internal" and v in ("true", "yes", "1"):
            return True
        if k == "distribution" and v == "maintainer":
            return True
    return False


def _install_tree(
    root: Any, target: Path, *, dry_run: bool
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Copy one ``commands/`` + ``skills/`` + ``agents/`` asset tree rooted at *root*.

    *root* is any :mod:`importlib.resources` traversable. Returns
    ``(commands, skills, agents, cleared)`` — the command stems,
    skill-directory names, agent-definition stems found, and any
    pre-existing 0-byte collision paths that were auto-cleared so the
    install could proceed (see :func:`_resolve_dir_collision`). A missing
    ``commands/``, ``skills/`` or ``agents/`` subtree is skipped, so a
    plugin may contribute any subset of the three.
    """
    commands: list[str] = []
    skills: list[str] = []
    agents: list[str] = []
    cleared: list[str] = []

    commands_src = root / "commands"
    if commands_src.is_dir():
        commands_dst = target / "commands"
        cleared_path = _resolve_dir_collision(commands_dst, "slash commands", dry_run=dry_run)
        if cleared_path is not None:
            cleared.append(cleared_path)
        for entry in commands_src.iterdir():
            if not entry.name.endswith(".md"):
                continue
            commands.append(entry.name[:-3])
            if not dry_run:
                commands_dst.mkdir(parents=True, exist_ok=True)
                with as_file(entry) as real:
                    shutil.copy2(real, commands_dst / entry.name)

    skills_src = root / "skills"
    if skills_src.is_dir():
        skills_dst = target / "skills"
        cleared_path = _resolve_dir_collision(skills_dst, "skills", dry_run=dry_run)
        if cleared_path is not None:
            cleared.append(cleared_path)
        for skill in skills_src.iterdir():
            if not skill.is_dir():
                continue
            if _skill_is_internal(skill):
                # Maintainer-only skills (e.g. ``release`` — it bumps versions,
                # commits, builds wheels) are NEVER installed into an end user's
                # ~/.claude, and therefore never granted an auto-invoke
                # ``Skill(...)`` permission (the permission merge below feeds off
                # this returned ``skills`` list). bug-sweep #58.
                continue
            skills.append(skill.name)
            if not dry_run:
                skill_dst = skills_dst / skill.name
                skill_dst.mkdir(parents=True, exist_ok=True)
                for entry in skill.iterdir():
                    if entry.is_dir():
                        continue
                    with as_file(entry) as real:
                        shutil.copy2(real, skill_dst / entry.name)

    # Named subagent definitions — a flat ``agents/*.md`` tree (same shape
    # as ``commands/``). Claude Code discovers these under
    # ``~/.claude/agents/``. Core ships none since the §6 worker removal
    # (the haiku-pinned ``hpc-worker`` went with the spawn transport); the
    # walk remains so plugins can ship their own agent definitions.
    agents_src = root / "agents"
    if agents_src.is_dir():
        agents_dst = target / "agents"
        cleared_path = _resolve_dir_collision(agents_dst, "agent definitions", dry_run=dry_run)
        if cleared_path is not None:
            cleared.append(cleared_path)
        for entry in agents_src.iterdir():
            if not entry.name.endswith(".md"):
                continue
            agents.append(entry.name[:-3])
            if not dry_run:
                agents_dst.mkdir(parents=True, exist_ok=True)
                with as_file(entry) as real:
                    shutil.copy2(real, agents_dst / entry.name)

    return commands, skills, agents, cleared


def install_agent_assets(
    *, claude_dir: Path | None = None, dry_run: bool = False
) -> dict[str, Any]:
    """Copy bundled slash commands, skills, and agent definitions into *claude_dir*.

    Slash commands land in ``<claude_dir>/commands/``, skills in
    ``<claude_dir>/skills/<name>/``, and named subagent definitions in
    ``<claude_dir>/agents/<name>.md``, overwriting any existing files.
    The core ``slash_commands`` assets are installed first; then any
    plugin exposing a ``slash_command_assets`` tree through the
    ``hpc_agent.plugins`` seam is installed over them, so an installed
    plugin's copy of an asset replaces the core one. With
    ``dry_run=True`` nothing is written — the returned dict still
    reports what would have been copied.

    Result shape::

        {
            "claude_dir": "<resolved path>",
            "commands_installed": ["aggregate-hpc", ...],
            "skills_installed": ["hpc-submit", ...],
            "agents_installed": [],
            "cleared_collisions": ["/.../.claude/agents", ...],
            "settings_hook": {"settings_path": "...", "action": "added", "wrote": <bool>},
            "settings_stop_hook": {"settings_path": "...", "action": "added", "wrote": <bool>},
            "settings_rendezvous_hook": {"settings_path": "...", "action": "added",
                                         "wrote": <bool>},
            "settings_rendezvous_stop_hook": {"settings_path": "...", "action": "added",
                                              "wrote": <bool>},
            "settings_alert_count_hook": {"settings_path": "...", "action": "added",
                                          "wrote": <bool>},
            "settings_utterance_hook": {"settings_path": "...", "action": "added",
                                        "wrote": <bool>},
            "settings_relay_audit_hook": {"settings_path": "...", "action": "added",
                                          "wrote": <bool>},
            "settings_permissions": {"settings_path": "...", "action": "added",
                                     "added": ["Skill(hpc-submit)", ...], "wrote": <bool>},
            "settings_deny": {"settings_path": "...", "action": "added",
                              "added": ["Bash(ssh *hoffman2.idre.ucla.edu*)", ...],
                              "removed": ["Bash(ssh:*)", "Bash(scp:*)"], "wrote": <bool>},
            "mcp_server": {"config_path": "...", "action": "added", "wrote": <bool>},
            "wrote": <bool>,
        }

    ``settings_deny`` reports the host-scoped merge of the raw-ssh / raw-scp
    ``Bash(...)`` DENY rules into ``permissions.deny`` — see
    :func:`_merge_deny_rules`. The rules are derived from the CONFIGURED CLUSTER
    HOSTS the install can see (:func:`_configured_cluster_hosts`), so an agent
    hand-rolling raw ssh/scp *to a cluster* dies at the permission layer while
    ssh to any other host is untouched (the 2026-07-10 narrowing: hpc-agent is a
    tool, not a workspace takeover). The same merge REMOVES the over-broad
    blanket ``Bash(ssh:*)`` / ``Bash(scp:*)`` a pre-narrowing install wrote, so an
    upgrade self-heals; ``removed`` lists what was dropped. No configured hosts →
    no deny rules added (the user-side cluster-ssh confirm-guard hook is the
    backstop). The sanctioned verbs dial inside hpc-agent's own processes and are
    unaffected.

    ``mcp_server`` reports the additive, idempotent registration of the
    ``hpc-agent`` MCP server into ``.claude.json``'s ``mcpServers`` — see
    :func:`_register_mcp_server`.

    ``cleared_collisions`` lists any pre-existing 0-byte files at
    ``<claude>/commands``/``skills``/``agents`` that were silently
    removed before mkdir — see :func:`_resolve_dir_collision`. Non-empty
    collisions still raise :class:`FileExistsError`.

    ``settings_hook`` reports the additive, idempotent merge of the
    skill-return autofetch ``PostToolUse`` hook into
    ``<claude>/settings.json``, and ``settings_stop_hook`` the same for the
    skill-return ``Stop`` guard — see :func:`_merge_hook_entry`. Each
    ``action`` is ``"added"`` / ``"already-present"`` / ``"updated"`` /
    ``"skipped-unparseable"`` / ``"dry-run-would-add"`` / ``"dry-run-would-update"``.

    ``settings_permissions`` reports the additive, idempotent merge of
    ``Skill(<name>)`` allow rules for every installed skill into
    ``<claude>/settings.json``'s ``permissions.allow`` — see
    :func:`_merge_skill_permissions`. Its ``action`` is ``"added"`` /
    ``"already-present"`` / ``"skipped-unparseable"`` /
    ``"dry-run-would-add"``, and ``added`` lists the rule strings
    actually appended (or that *would* have been on dry-run).
    """
    target = (claude_dir or DEFAULT_CLAUDE_DIR()).expanduser()

    commands: set[str] = set()
    skills: set[str] = set()
    agents: set[str] = set()
    cleared: list[str] = []

    core_commands, core_skills, core_agents, core_cleared = _install_tree(
        files("hpc_agent.slash_commands"), target, dry_run=dry_run
    )
    commands.update(core_commands)
    skills.update(core_skills)
    agents.update(core_agents)
    cleared.extend(core_cleared)

    # Optional plugins overlay their own assets last — a plugin's
    # skills/<name>/ (or agents/<name>.md) overrides the core copy of the
    # same name.
    from hpc_agent._kernel.registry.plugins import plugin_slash_command_roots

    for root in plugin_slash_command_roots():
        plugin_commands, plugin_skills, plugin_agents, plugin_cleared = _install_tree(
            root, target, dry_run=dry_run
        )
        commands.update(plugin_commands)
        skills.update(plugin_skills)
        agents.update(plugin_agents)
        cleared.extend(plugin_cleared)

    # Wire the skill-return hooks into settings.json — additive + idempotent,
    # never clobbering existing hooks/keys. Two entries: the PostToolUse
    # autofetch (injects the envelope the moment emit-skill-return commits it)
    # and the Stop guard (blocks ending the turn while an envelope sits
    # unfetched — the deterministic backstop for the advisory hand-back prose).
    settings_hook = _merge_hook_entry(
        target,
        event="PostToolUse",
        entry=_SKILL_RETURN_HOOK_ENTRY,
        needle=_AUTOFETCH_NEEDLE,
        dry_run=dry_run,
    )
    settings_stop_hook = _merge_hook_entry(
        target,
        event="Stop",
        entry=_SKILL_RETURN_STOP_ENTRY,
        needle=_STOP_GUARD_NEEDLE,
        dry_run=dry_run,
    )

    # Wire the block-drive decision-rendezvous pair (§5): the PostToolUse
    # autofetch injects the brief a block-drive tick just parked; the Stop guard
    # forces the driver to advance once a human y is committed but unconsumed.
    # Same additive + idempotent merge, matched on their own module-path needles.
    settings_rendezvous_hook = _merge_hook_entry(
        target,
        event="PostToolUse",
        entry=_RENDEZVOUS_AUTOFETCH_ENTRY,
        needle=_RENDEZVOUS_AUTOFETCH_NEEDLE,
        dry_run=dry_run,
    )
    settings_rendezvous_stop_hook = _merge_hook_entry(
        target,
        event="Stop",
        entry=_RENDEZVOUS_STOP_ENTRY,
        needle=_RENDEZVOUS_STOP_NEEDLE,
        dry_run=dry_run,
    )

    # Wire the scheduler write-fence (conduct rule 7): PreToolUse on Bash,
    # blocking mutating scheduler verbs (ssh transport included) while leaving
    # read-only probes untouched. Same additive + idempotent merge.
    settings_write_fence_hook = _merge_hook_entry(
        target,
        event="PreToolUse",
        entry=_WRITE_FENCE_ENTRY,
        needle=_WRITE_FENCE_NEEDLE,
        dry_run=dry_run,
    )

    # Wire the watchdog alert-count SessionStart hook (proving run #3: alert
    # delivery, not just detection) — prints the unacknowledged alert count into
    # session context. Same additive + idempotent merge.
    settings_alert_count_hook = _merge_hook_entry(
        target,
        event="SessionStart",
        entry=_ALERT_COUNT_ENTRY,
        needle=_ALERT_COUNT_NEEDLE,
        dry_run=dry_run,
    )

    # Wire the human-utterance capture (proving run #4: harness-captured
    # authorship evidence for the append-decision gate) — UserPromptSubmit,
    # no matcher. Same additive + idempotent merge.
    settings_utterance_hook = _merge_hook_entry(
        target,
        event="UserPromptSubmit",
        entry=_UTTERANCE_CAPTURE_ENTRY,
        needle=_UTTERANCE_CAPTURE_NEEDLE,
        dry_run=dry_run,
    )

    # Wire the AskUserQuestion answer capture (proving run #5: typed selector
    # answers are human-authored evidence too) — PostToolUse, matched on the
    # tool name. Same additive + idempotent merge.
    settings_answer_capture_hook = _merge_hook_entry(
        target,
        event="PostToolUse",
        entry=_ANSWER_CAPTURE_ENTRY,
        needle=_ANSWER_CAPTURE_NEEDLE,
        dry_run=dry_run,
    )

    # Wire the relay-audit Stop hook (conduct rule 10 staged → active): audits
    # the final assistant text against the journal via verify-relay and blocks
    # the stop once on a contradiction. Same additive + idempotent merge.
    settings_relay_audit_hook = _merge_hook_entry(
        target,
        event="Stop",
        entry=_RELAY_AUDIT_ENTRY,
        needle=_RELAY_AUDIT_NEEDLE,
        dry_run=dry_run,
    )

    # Register the registry-projected MCP server (hpc-agent mcp-serve) as the
    # preferred shell-free block-invocation surface (design §3) — additive +
    # idempotent into .claude.json's mcpServers, never clobbering other servers.
    mcp_server = _register_mcp_server(target, dry_run=dry_run)

    # Grant Skill(<name>) for every installed skill so Claude Code's auto-mode
    # classifier stops silently denying the first /submit-hpc → Skill(hpc-submit)
    # call. Same additive + idempotent + skip-unparseable contract as the hook
    # merge above.
    settings_permissions = _merge_skill_permissions(target, sorted(skills), dry_run=dry_run)

    # DENY raw ssh/scp to the CONFIGURED CLUSTER HOSTS from the agent's Bash tool
    # (anti-vendor-lockout ruling (a), narrowed 2026-07-10): host-scoped rules
    # derived from the clusters config the install can see, so the improvisation
    # class dies at the permission layer for cluster ssh while ssh to any other
    # host is untouched (tool, not takeover). The same run REMOVES the over-broad
    # blanket rules a pre-narrowing install wrote, so an upgrade self-heals. No
    # configured hosts → no deny rules (the confirm-guard hook is the backstop).
    # The sanctioned verbs dial ssh inside hpc-agent's own processes (never via
    # agent Bash) and are unaffected either way.
    settings_deny = _merge_deny_rules(
        target,
        _raw_ssh_deny_rules(_configured_cluster_hosts()),
        remove_rules=_BLANKET_SSH_DENY_RULES,
        dry_run=dry_run,
    )

    return {
        "claude_dir": str(target),
        "commands_installed": sorted(commands),
        "skills_installed": sorted(skills),
        "agents_installed": sorted(agents),
        "cleared_collisions": cleared,
        "settings_hook": settings_hook,
        "settings_stop_hook": settings_stop_hook,
        "settings_rendezvous_hook": settings_rendezvous_hook,
        "settings_rendezvous_stop_hook": settings_rendezvous_stop_hook,
        "settings_write_fence_hook": settings_write_fence_hook,
        "settings_alert_count_hook": settings_alert_count_hook,
        "settings_utterance_hook": settings_utterance_hook,
        "settings_answer_capture_hook": settings_answer_capture_hook,
        "settings_relay_audit_hook": settings_relay_audit_hook,
        "settings_permissions": settings_permissions,
        "settings_deny": settings_deny,
        "mcp_server": mcp_server,
        "wrote": not dry_run,
    }
