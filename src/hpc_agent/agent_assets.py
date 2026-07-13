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
from collections.abc import Callable
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, NamedTuple

from hpc_agent.infra.clusters import load_clusters_config
from hpc_agent.infra.io import atomic_write_text

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


def _hook_command(module: str, prefilter: tuple[str, ...] = ()) -> str:
    """The ``bash -c`` command string for a hook that runs ``python -m <module>``.

    Two shapes, selected by *prefilter*:

    * **Bare** (``prefilter=()``): ``<python> -m <module>``. Used by the hooks
      that fire rarely — once per turn (Stop guards, relay audit), once per
      session (alert count), once per prompt (utterance capture), or once per
      matched tool call (answer capture) — where the interpreter start is paid
      seldom and no pre-filter is worth its complexity.
    * **Pre-filtered** (non-empty *prefilter*): a bash ``case`` gate that only
      pipes the payload into Python when it contains one of the *prefilter*
      substrings, keeping the every-Bash-call common path at bash-builtin cost.
      A Python interpreter start costs ~300-500ms on Windows (#288), so the
      ``matcher: "Bash"`` hooks (skill-return / rendezvous autofetch, the
      scheduler write-fence) gate on their trigger verbs. The substring scan can
      false-positive on an unrelated command echoing a verb — that costs one
      no-op interpreter start, nothing more.
    """
    py = _hook_python()
    if not prefilter:
        return f"{py} -m {module}"
    pattern = "|".join(f"*{verb}*" for verb in prefilter)
    return (
        'input=$(cat); case "$input" in '
        f"{pattern}) "
        f"printf '%s' \"$input\" | {py} "
        f"-m {module};; esac"
    )


def _hook_entry(command: str, *, matcher: str | None) -> dict[str, Any]:
    """A single ``settings.json`` hook entry running *command*.

    Emits ``{"matcher": ..., "hooks": [...]}`` when *matcher* is set (the
    tool-matched events — ``Bash``, ``AskUserQuestion``) and the matcher-less
    ``{"hooks": [...]}`` shape otherwise (``Stop`` / ``SessionStart`` /
    ``UserPromptSubmit`` have no tool to match). Key order — matcher before
    hooks — is preserved so the written JSON is byte-stable.
    """
    entry: dict[str, Any] = {}
    if matcher is not None:
        entry["matcher"] = matcher
    entry["hooks"] = [{"type": "command", "command": command}]
    return entry


# Every hook module lives under this package; the full module path is also the
# NEEDLE that :func:`_find_hook_entry_index` matches an installed entry on. The
# needle is load-bearing: it is written into the command AND used to re-find our
# entry across re-installs (moved venv, changed matcher/pre-filter shape), so it
# must stay byte-stable — a renamed needle orphans an installed hook.
_HOOK_MODULE_PREFIX = "hpc_agent._kernel.hooks."

# Needle constants imported by :mod:`hpc_agent.ops.harness_capabilities` to probe
# an installed settings.json for the capability hooks. Kept as explicit importable
# names, byte-identical to the corresponding needles in :data:`_HOOK_SPECS`.
_UTTERANCE_CAPTURE_NEEDLE = _HOOK_MODULE_PREFIX + "utterance_capture"
_ANSWER_CAPTURE_NEEDLE = _HOOK_MODULE_PREFIX + "answer_capture"
_RELAY_AUDIT_NEEDLE = _HOOK_MODULE_PREFIX + "relay_audit_stop"
_ALERT_COUNT_NEEDLE = _HOOK_MODULE_PREFIX + "alert_count"


class _HookSpec(NamedTuple):
    """One hook to merge into ``settings.json``: result key, event, needle, shape.

    *result_key* is the field the merge report lands under in
    :func:`install_agent_assets`'s return dict; *event* the ``settings.json``
    hook event; *needle* the full module path (match key AND the ``-m`` target);
    *matcher* the tool matcher (``None`` for matcher-less events); *prefilter*
    the bash ``case`` trigger substrings (empty → bare invocation).
    """

    result_key: str
    event: str
    needle: str
    matcher: str | None
    prefilter: tuple[str, ...]


# The nine hooks install-commands wires into ``~/.claude/settings.json``,
# additively + idempotently + self-healing (see :func:`_merge_hook_entry`).
# Order is load-bearing: each hook is merged in sequence, so within one event
# the entries append in this order. The one-line rationale per hook:
#
# * skill_return_autofetch — PostToolUse: auto-fetches a sub-skill's return
#   envelope the moment its ``emit-skill-return`` Bash call commits it (the Skill
#   tool returns before the sub-skill body runs, so a Skill-matched hook can
#   never see a fresh envelope).
# * skill_return_stop_guard — Stop: blocks ending the turn while a committed
#   envelope sits unfetched (deterministic backstop for the hand-back prose).
# * decision_rendezvous_autofetch / _stop_guard — the ``block-drive`` pair
#   generalizing the skill-return pair to the §5 y/nudge boundary: PostToolUse
#   injects the brief a block-drive tick parked; Stop blocks the turn once a
#   human ``y`` is committed but the driver has not advanced.
# * scheduler_write_fence — PreToolUse: blocks mutating scheduler verbs
#   (qsub/sbatch/qdel/scancel/qmod/qalter, ssh transport included) while
#   read-only probes stay allowed (conduct rule 7; command-position analysis so
#   ``grep qsub log`` passes).
# * alert_count — SessionStart: prints the unacknowledged watchdog-alert count
#   into session context (proving run #3: detection without delivery is silence).
# * utterance_capture — UserPromptSubmit: appends each human prompt to the repo's
#   utterances log so the authorship gate can require human-typed evidence
#   (proving run #4). Silent no-op outside an hpc repo.
# * answer_capture — PostToolUse(AskUserQuestion): captures TYPED selector answer
#   text (never a click on an agent-authored option) as authorship evidence too
#   (proving run #5).
# * relay_audit_stop — Stop: audits the final assistant text against the journal
#   via verify-relay, blocking the stop once on a contradiction (conduct rule 10).
_HOOK_SPECS: tuple[_HookSpec, ...] = (
    _HookSpec(
        "settings_hook",
        "PostToolUse",
        _HOOK_MODULE_PREFIX + "skill_return_autofetch",
        "Bash",
        ("emit-skill-return",),
    ),
    _HookSpec(
        "settings_stop_hook",
        "Stop",
        _HOOK_MODULE_PREFIX + "skill_return_stop_guard",
        None,
        (),
    ),
    _HookSpec(
        "settings_rendezvous_hook",
        "PostToolUse",
        _HOOK_MODULE_PREFIX + "decision_rendezvous_autofetch",
        "Bash",
        ("block-drive",),
    ),
    _HookSpec(
        "settings_rendezvous_stop_hook",
        "Stop",
        _HOOK_MODULE_PREFIX + "decision_rendezvous_stop_guard",
        None,
        (),
    ),
    _HookSpec(
        "settings_write_fence_hook",
        "PreToolUse",
        _HOOK_MODULE_PREFIX + "scheduler_write_fence",
        "Bash",
        ("qsub", "sbatch", "qdel", "scancel", "qmod", "qalter"),
    ),
    _HookSpec(
        "settings_alert_count_hook",
        "SessionStart",
        _ALERT_COUNT_NEEDLE,
        None,
        (),
    ),
    _HookSpec(
        "settings_utterance_hook",
        "UserPromptSubmit",
        _UTTERANCE_CAPTURE_NEEDLE,
        None,
        (),
    ),
    _HookSpec(
        "settings_answer_capture_hook",
        "PostToolUse",
        _ANSWER_CAPTURE_NEEDLE,
        "AskUserQuestion",
        (),
    ),
    _HookSpec(
        "settings_relay_audit_hook",
        "Stop",
        _RELAY_AUDIT_NEEDLE,
        None,
        (),
    ),
)


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


# Sentinel distinguishing "file present but not a JSON object" (refuse to
# clobber) from "file absent" (start an empty ``{}`` model) in _load_json_object.
_UNPARSEABLE = object()


def _load_json_object(path: Path) -> Any:
    """Load the JSON **object** at *path* for an additive merge.

    Returns ``{}`` when *path* is absent (start a fresh model), the parsed
    ``dict`` when it is a readable JSON object, or the :data:`_UNPARSEABLE`
    sentinel when it exists but is unreadable / not a JSON object — precious
    user config we refuse to clobber, so the caller reports
    ``skipped-unparseable`` instead of overwriting it.
    """
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return _UNPARSEABLE
    if not isinstance(loaded, dict):
        return _UNPARSEABLE
    return loaded


def _write_json_object(path: Path, obj: dict[str, Any]) -> None:
    """Atomically write *obj* to *path*, pretty-printed with a trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(obj, indent=2, sort_keys=False) + "\n")


class _MergeOutcome(NamedTuple):
    """A merge plan's decision, fed back to :func:`_merge_json`.

    *changed* is ``False`` for an idempotent no-op (report ``already-present``
    with *present_extra*). When ``True``, *config* is the mutated object to write;
    *write_action* / *dryrun_action* are the ``action`` strings for the real vs.
    ``dry_run`` branch, and *change_extra* the extra report fields (e.g. the
    ``added`` / ``removed`` rule lists) shared by both.
    """

    changed: bool
    config: dict[str, Any]
    write_action: str
    dryrun_action: str
    change_extra: dict[str, Any]
    present_extra: dict[str, Any]


def _merge_json(
    path: Path,
    *,
    path_key: str,
    unparseable_extra: dict[str, Any],
    plan: Callable[[dict[str, Any]], _MergeOutcome],
    dry_run: bool,
) -> dict[str, Any]:
    """The generic additive/idempotent JSON-config merge core.

    Loads the JSON object at *path* (:func:`_load_json_object`), refusing to
    clobber an existing non-object (``skipped-unparseable``); otherwise hands the
    loaded config to *plan*, which computes the mutation decision as a
    :class:`_MergeOutcome`. An unchanged outcome is a no-op (``already-present``);
    a changed outcome is written atomically unless *dry_run*.

    Returns ``{<path_key>, "action", ...extra, "wrote"}`` — *path_key* is
    ``"settings_path"`` or ``"config_path"``; the extra report fields come from
    *unparseable_extra* / the outcome's extras (each caller's report shape).
    """
    config = _load_json_object(path)
    if config is _UNPARSEABLE:
        return {
            path_key: str(path),
            "action": "skipped-unparseable",
            **unparseable_extra,
            "wrote": False,
        }

    outcome = plan(config)
    if not outcome.changed:
        return {
            path_key: str(path),
            "action": "already-present",
            **outcome.present_extra,
            "wrote": False,
        }

    if dry_run:
        return {
            path_key: str(path),
            "action": outcome.dryrun_action,
            **outcome.change_extra,
            "wrote": False,
        }

    _write_json_object(path, outcome.config)
    return {
        path_key: str(path),
        "action": outcome.write_action,
        **outcome.change_extra,
        "wrote": True,
    }


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

    def plan(config: dict[str, Any]) -> _MergeOutcome:
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
            return _MergeOutcome(False, config, "", "", {}, {})

        servers = dict(servers)
        servers[_MCP_SERVER_NAME] = desired
        config["mcpServers"] = servers
        return _MergeOutcome(
            True,
            config,
            "updated" if existing is not None else "added",
            "dry-run-would-update" if existing is not None else "dry-run-would-add",
            {},
            {},
        )

    return _merge_json(
        _mcp_config_path(claude_dir),
        path_key="config_path",
        unparseable_extra={},
        plan=plan,
        dry_run=dry_run,
    )


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

    def plan(settings: dict[str, Any]) -> _MergeOutcome:
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            # Absent or wrong-typed ``hooks`` → start a fresh mapping. (A non-dict
            # ``hooks`` would itself be malformed Claude config; replacing it is
            # the only way to wire our entry, and we only do so when adding.)
            hooks = {}
        event_entries = hooks.get(event)
        if not isinstance(event_entries, list):
            event_entries = []

        existing_idx = _find_hook_entry_index(event_entries, needle)
        if existing_idx is not None and event_entries[existing_idx] == entry:
            return _MergeOutcome(False, settings, "", "", {}, {})

        event_entries = list(event_entries)
        if existing_idx is not None:
            event_entries[existing_idx] = entry
            write_action = "updated"
        else:
            event_entries.append(entry)
            write_action = "added"
        hooks[event] = event_entries
        settings["hooks"] = hooks
        return _MergeOutcome(
            True,
            settings,
            write_action,
            "dry-run-would-update" if existing_idx is not None else "dry-run-would-add",
            {},
            {},
        )

    return _merge_json(
        claude_dir / "settings.json",
        path_key="settings_path",
        unparseable_extra={},
        plan=plan,
        dry_run=dry_run,
    )


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

    def plan(settings: dict[str, Any]) -> _MergeOutcome:
        permissions = settings.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
        allow = permissions.get("allow")
        if not isinstance(allow, list):
            allow = []

        canonical_rules = [_skill_allow_rule(name) for name in skill_names]
        missing = [rule for rule in canonical_rules if rule not in allow]
        if not missing:
            return _MergeOutcome(False, settings, "", "", {}, {"added": []})

        allow = list(allow) + missing
        permissions["allow"] = allow
        settings["permissions"] = permissions
        return _MergeOutcome(
            True, settings, "added", "dry-run-would-add", {"added": missing}, {"added": []}
        )

    return _merge_json(
        claude_dir / "settings.json",
        path_key="settings_path",
        unparseable_extra={"added": []},
        plan=plan,
        dry_run=dry_run,
    )


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

    def plan(settings: dict[str, Any]) -> _MergeOutcome:
        permissions = settings.get("permissions")
        if not isinstance(permissions, dict):
            permissions = {}
        deny = permissions.get("deny")
        if not isinstance(deny, list):
            deny = []

        missing = [rule for rule in deny_rules if rule not in deny]
        stale = [rule for rule in remove_rules if rule in deny]
        if not missing and not stale:
            return _MergeOutcome(False, settings, "", "", {}, {"added": [], "removed": []})

        # Drop the blanket rules (exact-string only), keep every other entry,
        # then append the host-scoped rules that were missing.
        deny = [rule for rule in deny if rule not in remove_rules] + missing
        permissions["deny"] = deny
        settings["permissions"] = permissions
        return _MergeOutcome(
            True,
            settings,
            "added" if missing else "updated",
            "dry-run-would-add",
            {"added": missing, "removed": stale},
            {"added": [], "removed": []},
        )

    return _merge_json(
        claude_dir / "settings.json",
        path_key="settings_path",
        unparseable_extra={"added": [], "removed": []},
        plan=plan,
        dry_run=dry_run,
    )


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
    The full set of hooks wired (in order) is enumerated in :data:`_HOOK_SPECS`.

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

    # Wire every hook in _HOOK_SPECS into settings.json — additive + idempotent,
    # never clobbering existing hooks/keys, matched on each hook's module-path
    # needle. Order is load-bearing (entries append per event in spec order), so
    # iterate the tuple as-is and key each merge report by the spec's result_key.
    hook_reports: dict[str, Any] = {}
    for spec in _HOOK_SPECS:
        hook_reports[spec.result_key] = _merge_hook_entry(
            target,
            event=spec.event,
            entry=_hook_entry(_hook_command(spec.needle, spec.prefilter), matcher=spec.matcher),
            needle=spec.needle,
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
        **hook_reports,
        "settings_permissions": settings_permissions,
        "settings_deny": settings_deny,
        "mcp_server": mcp_server,
        "wrote": not dry_run,
    }
