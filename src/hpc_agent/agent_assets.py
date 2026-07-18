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

**Activation profile (harness-activation Wave 2).** WHAT this installer wires —
the hook inventory, the fused Stop dispatcher, and the MCP server invocation — is
now a declarative :data:`CLAUDE_CODE_PROFILE` (a frozen
:class:`~hpc_agent.harness_profile.HarnessProfile` carrying MECHANISM DESCRIPTION
only, never a self-asserted capability claim). Turning that neutral description
into Claude Code's exact ``settings.json`` / ``.claude.json`` layout is
:class:`~hpc_agent.harness_profile.ClaudeCodeProfile` — the FIRST renderer; a
foreign harness ships its own reading the same descriptors. This module is the
Claude-Code install ENGINE (merge / prune / manifest / deny) driving that
renderer; the render is byte-identical to the pre-profile install (pinned by
``tests/cli/test_profile_golden.py``). Installing the profile grants ZERO trust
— capability presence is proven only by BEHAVIOR and read only from the DETECTED
settings-seam (:mod:`hpc_agent.ops.harness_capabilities`); see
:mod:`hpc_agent.harness_profile` for the doctrine.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable, Sequence
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, NamedTuple

from hpc_agent.harness_profile import (
    ClaudeCodeProfile,
    HarnessProfile,
    HookDescriptor,
    HookEvent,
    McpServerDescriptor,
    StopMultiplexDescriptor,
    ToolClass,
)
from hpc_agent.infra.clusters import load_clusters_config
from hpc_agent.infra.io import atomic_write_text

__all__ = [
    "CLAUDE_CODE_PROFILE",
    "DEFAULT_CLAUDE_DIR",
    "install_agent_assets",
    "resolve_claude_dir",
]


# Every hook module lives under this package; the full module path is also the
# NEEDLE that :func:`_find_hook_entry_index` matches an installed entry on. The
# needle is load-bearing: it is written into the command AND used to re-find our
# entry across re-installs (moved venv, changed matcher/pre-filter shape), so it
# must stay byte-stable — a renamed needle orphans an installed hook.
_HOOK_MODULE_PREFIX = "hpc_agent._kernel.hooks."

# Needle constants imported by :mod:`hpc_agent.ops.harness_capabilities` to probe
# an installed settings.json for the capability hooks. Kept as explicit importable
# names, byte-identical to the corresponding needles in :data:`CLAUDE_CODE_PROFILE`.
_UTTERANCE_CAPTURE_NEEDLE = _HOOK_MODULE_PREFIX + "utterance_capture"
_ANSWER_CAPTURE_NEEDLE = _HOOK_MODULE_PREFIX + "answer_capture"
_RELAY_AUDIT_NEEDLE = _HOOK_MODULE_PREFIX + "relay_audit_stop"
_ALERT_COUNT_NEEDLE = _HOOK_MODULE_PREFIX + "alert_count"
# Capability 6 (scheduler-write fence): the ``PreToolUse(Bash)`` hook needle the
# ``harness-capabilities`` verb probes. It IS the needle the scheduler-write-fence
# ``HookDescriptor`` in :data:`CLAUDE_CODE_PROFILE` below carries — an explicit
# importable name so both the profile and the negotiation probe reuse the ONE
# canonical matcher, never a re-derived scan.
_SCHEDULER_WRITE_FENCE_NEEDLE = _HOOK_MODULE_PREFIX + "scheduler_write_fence"

# ── Fused Stop hook (stop_multiplex) ─────────────────────────────────────────
# The three legacy standalone ``Stop`` guards are fused into ONE interpreter start
# by :mod:`hpc_agent._kernel.hooks.stop_multiplex`, so a Stop event costs one
# Python start + one ``hpc_agent`` import instead of three (#288). The multiplex
# entry names the three guard modules explicitly as command arguments, so the
# fused entry's command STILL mentions each legacy needle — the capability probe
# (keyed on ``_RELAY_AUDIT_NEEDLE``) and the re-find matcher
# (:func:`_find_hook_entry_index`, substring on the module path) both keep
# resolving against the fused entry with no change to their needle constants.
_STOP_MULTIPLEX_NEEDLE = _HOOK_MODULE_PREFIX + "stop_multiplex"
_SKILL_RETURN_STOP_NEEDLE = _HOOK_MODULE_PREFIX + "skill_return_stop_guard"
_DECISION_RENDEZVOUS_STOP_NEEDLE = _HOOK_MODULE_PREFIX + "decision_rendezvous_stop_guard"
# The guard dispatch order (also stated in ``stop_multiplex._DEFAULT_GUARDS``);
# these become the fused command's arguments AND the legacy needles the migration
# removes as standalone entries.
_STOP_MULTIPLEX_GUARDS: tuple[str, ...] = (
    _SKILL_RETURN_STOP_NEEDLE,
    _DECISION_RENDEZVOUS_STOP_NEEDLE,
    _RELAY_AUDIT_NEEDLE,
)


# ── The declarative activation profile ───────────────────────────────────────
# CLAUDE_CODE_PROFILE is the SINGLE SOURCE OF TRUTH for the hook inventory the
# installer wires (retiring the former ``_HOOK_SPECS`` tuple + Stop special-case
# split). It carries harness-NEUTRAL descriptors (module-path needles, neutral
# turn-boundary events, neutral tool-matcher intents, pre-filter verbs) plus the
# MCP invocation and the core asset package — mechanism description only, no
# self-asserted capability claim (frozen, closed field set: see
# :mod:`hpc_agent.harness_profile`). :class:`ClaudeCodeProfile` renders these into
# today's exact settings.json/.claude.json layout.
#
# The one-line rationale per hook (order is load-bearing — entries append per
# event in this order):
#
# * skill_return_autofetch — post-tool/shell: auto-fetches a sub-skill's return
#   envelope the moment its ``emit-skill-return`` Bash call commits it (the Skill
#   tool returns before the sub-skill body runs, so a Skill-matched hook can
#   never see a fresh envelope).
# * decision_rendezvous_autofetch — post-tool/shell: injects the brief a
#   block-drive tick parked (the ``block-drive`` half of the rendezvous pair).
# * scheduler_write_fence — pre-tool/shell: blocks mutating scheduler verbs
#   (qsub/sbatch/qdel/scancel/qmod/qalter, ssh transport included) while
#   read-only probes stay allowed (conduct rule 7; command-position analysis so
#   ``grep qsub log`` passes).
# * alert_count — session-start: prints the unacknowledged watchdog-alert count
#   into session context (proving run #3: detection without delivery is silence).
# * utterance_capture — on-prompt: appends each human prompt to the repo's
#   utterances log so the authorship gate can require human-typed evidence
#   (proving run #4). Silent no-op outside an hpc repo.
# * answer_capture — post-tool/question: captures TYPED selector answer text
#   (never a click on an agent-authored option) as authorship evidence too
#   (proving run #5).
#
# The THREE ``Stop`` guards — skill_return_stop_guard, decision_rendezvous_stop_guard,
# and relay_audit_stop — are NOT descriptors: they are fused into ONE Stop entry
# by :func:`_merge_stop_multiplex_hook` (the ``stop_multiplex`` dispatcher), so a
# Stop event costs one interpreter start, not three (#288).
#
# The registry-projected MCP server (``hpc-agent mcp-serve``) is the preferred,
# shell-free block-invocation surface (design §3, "The tool surface subsumes the
# shell"). Registered venv-pinned via the current interpreter (resolved at render
# time) so it does not depend on ``hpc-agent`` being on PATH. ``--allow-mutations``
# exposes the submit/aggregate verbs (cancel/raw-submit are never registry
# primitives, so they stay unreachable either way); ``--catalog curated``
# advertises exactly the human-amplification block verbs plus the recovery/opt-in
# verbs (doctor, kill, submit-speculate), keeping the rest out of the model's
# context.
CLAUDE_CODE_PROFILE: HarnessProfile = HarnessProfile(
    hook_descriptors=(
        HookDescriptor(
            _HOOK_MODULE_PREFIX + "skill_return_autofetch",
            HookEvent.POST_TOOL,
            ToolClass.SHELL,
            ("emit-skill-return",),
        ),
        HookDescriptor(
            _HOOK_MODULE_PREFIX + "decision_rendezvous_autofetch",
            HookEvent.POST_TOOL,
            ToolClass.SHELL,
            ("block-drive",),
        ),
        HookDescriptor(
            _SCHEDULER_WRITE_FENCE_NEEDLE,
            HookEvent.PRE_TOOL,
            ToolClass.SHELL,
            ("qsub", "sbatch", "qdel", "scancel", "qmod", "qalter"),
        ),
        HookDescriptor(_ALERT_COUNT_NEEDLE, HookEvent.SESSION_START, ToolClass.NONE, ()),
        HookDescriptor(_UTTERANCE_CAPTURE_NEEDLE, HookEvent.ON_PROMPT, ToolClass.NONE, ()),
        HookDescriptor(_ANSWER_CAPTURE_NEEDLE, HookEvent.POST_TOOL, ToolClass.QUESTION, ()),
    ),
    stop_hook=StopMultiplexDescriptor(_STOP_MULTIPLEX_NEEDLE, _STOP_MULTIPLEX_GUARDS),
    mcp_server=McpServerDescriptor(
        "hpc-agent",
        "hpc_agent",
        ("mcp-serve", "--allow-mutations", "--catalog", "curated"),
    ),
    asset_package="hpc_agent.slash_commands",
)

# The Claude-Code install-report field each hook descriptor's merge result lands
# under in :func:`install_agent_assets`'s return dict — a Claude-Code report-shape
# detail, keyed by the neutral descriptor's needle (a foreign renderer produces
# its own report shape, so this mapping is the renderer's, not the profile's).
_HOOK_REPORT_KEYS: dict[str, str] = {
    _HOOK_MODULE_PREFIX + "skill_return_autofetch": "settings_hook",
    _HOOK_MODULE_PREFIX + "decision_rendezvous_autofetch": "settings_rendezvous_hook",
    _HOOK_MODULE_PREFIX + "scheduler_write_fence": "settings_write_fence_hook",
    _ALERT_COUNT_NEEDLE: "settings_alert_count_hook",
    _UTTERANCE_CAPTURE_NEEDLE: "settings_utterance_hook",
    _ANSWER_CAPTURE_NEEDLE: "settings_answer_capture_hook",
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

    Under ``CLAUDE_CONFIG_DIR`` relocation (:func:`resolve_claude_dir`) this
    parent-sibling registration is BEST-EFFORT (premortem D2): MCP is a ruled
    NON-load-bearing projection, so if a given Claude Code build stores the
    relocated user config somewhere other than this parent-sibling, registration
    degrades to MCP-absent (drive the CLI directly) — no guarantee is lost. We do
    not assert the relocated ``.claude.json`` location as a verified fact.
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


def _register_mcp_server(
    claude_dir: Path,
    *,
    dry_run: bool,
    mcp: McpServerDescriptor | None = None,
    executable: str | None = None,
) -> dict[str, Any]:
    """Additively, idempotently register the ``hpc-agent`` MCP server in
    ``.claude.json``'s ``mcpServers`` object.

    Same contract as :func:`_merge_hook_entry`, targeting ``mcpServers`` in
    ``.claude.json`` rather than ``hooks`` in ``settings.json``: creates an empty
    ``{}`` model when the file is absent/unreadable, refuses to clobber a present
    file that is not a JSON object (``skipped-unparseable``), and only ever adds
    or in-place heals our one ``hpc-agent`` entry — every other server and key is
    preserved verbatim. Idempotent: a byte-equal entry is ``already-present``; a
    stale entry (e.g. a moved venv changing the interpreter path) is ``updated``.

    The desired entry is RENDERED from the profile's MCP descriptor (*mcp*,
    default :data:`CLAUDE_CODE_PROFILE`'s) via :class:`ClaudeCodeProfile`, pinning
    the interpreter to *executable* (default the live ``sys.executable``) at render
    time rather than at import — so a moved venv heals and the golden can inject a
    hermetic interpreter.

    Returns ``{config_path, action, wrote}`` where ``action`` is ``"added"`` /
    ``"updated"`` / ``"already-present"`` / ``"skipped-unparseable"`` /
    ``"dry-run-would-add"`` / ``"dry-run-would-update"``.
    """
    if mcp is None:
        mcp = CLAUDE_CODE_PROFILE.mcp_server
    if executable is None:
        executable = sys.executable
    desired_base = ClaudeCodeProfile.render_mcp_entry(mcp, executable)

    def plan(config: dict[str, Any]) -> _MergeOutcome:
        servers = config.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}

        existing = servers.get(mcp.name)
        # A user-set ``env`` on the existing registration (e.g.
        # ``HPC_SSH_ENGINE=asyncssh`` opting the demo server into the connection
        # engine) is the USER'S config, not ours: an install heals OUR keys
        # (command/args after a moved venv) but must never destroy theirs —
        # rewriting the entry wholesale silently un-set the engine flag on every
        # install-commands run.
        desired: dict[str, Any] = dict(desired_base)
        if isinstance(existing, dict) and isinstance(existing.get("env"), dict) and existing["env"]:
            desired["env"] = existing["env"]
        if existing == desired:
            return _MergeOutcome(False, config, "", "", {}, {})

        servers = dict(servers)
        servers[mcp.name] = desired
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
    """Return the LITERAL default harness config dir ``~/.claude``.

    The no-override default only — it deliberately does NOT read
    ``CLAUDE_CONFIG_DIR``; the env-honoring resolution lives in the ONE shared
    :func:`resolve_claude_dir`, which falls back here. Does not create the
    directory.
    """
    return Path.home() / ".claude"


def resolve_claude_dir() -> Path:
    """The ONE harness CONFIG-dir resolver — shared by the install WRITE path
    (:func:`install_agent_assets`) and the capability READ probe
    (:func:`hpc_agent.ops.harness_capabilities._claude_dir`).

    Resolution: ``CLAUDE_CONFIG_DIR`` (Claude Code's documented relocation knob)
    if set and non-empty → ``expanduser``; else ``~/.claude``
    (:func:`DEFAULT_CLAUDE_DIR`). Does not create the directory. Collapsing the
    two former CONFIG-dir definitions here (the write path used to ignore the
    env) closes a latent read/write asymmetry: a relocated config used to get
    capabilities WRITTEN to ``~/.claude`` where the env-honoring probe never
    LOOKED.

    **Fenced to the harness-config surface ONLY (premortem D1).** This is NOT
    the journal home. The journal home — RunRecords, submit-locks, monitor
    sidecars, the run index — is a SEPARATE axis keyed on ``HPC_JOURNAL_DIR``
    (:func:`hpc_agent.state.run_record.current_homedir` /
    :func:`hpc_agent.state._homedir.journal_homedir` /
    :class:`hpc_agent._kernel.contract.layout.JournalLayout`), default
    ``~/.claude/hpc``. That resolver MUST NOT delegate here: folding the two
    axes together would RELOCATE every existing ``CLAUDE_CONFIG_DIR`` user's
    entire run history on upgrade (their live-run journal would vanish from
    where every reader looks). ``~/.claude/hpc`` staying literal-home under a
    relocated config is INTENTIONAL, not a bug to heal.

    **Upgrade semantics, stated honestly (premortem D3).** For a user who had
    already set ``CLAUDE_CONFIG_DIR``, closing the asymmetry is a HEAL that MOVES
    the install write target: the write path previously wrote to ``~/.claude``,
    which Claude Code — following the env — never read (so the hooks never fired
    and the capabilities were absent); it now writes to the relocated dir Claude
    Code actually reads. Files left at the old ``~/.claude`` are INERT leftovers,
    not a hazard:

    * no double-fire — Claude Code follows ``CLAUDE_CONFIG_DIR`` and never reads
      the old location, so the orphaned hooks never fire;
    * litter, not harm — :func:`_prune_stale_assets` reads the manifest at the
      NEW location, so manifest-prune never revisits (or cleans) the old dir;
    * the one honest ambiguity — a Claude Code FORK that reads ``~/.claude``
      UNCONDITIONALLY while the user also set ``CLAUDE_CONFIG_DIR`` for another
      harness could see BOTH locations; the installer cannot adjudicate that
      multi-harness config and does not try.
    """
    override = (os.environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_CLAUDE_DIR()


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


def _entry_mentions(entry: Any, needle: str) -> bool:
    """True when *entry* has a ``command`` hook whose command mentions *needle*."""
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return False
    for hook in hooks:
        if (
            isinstance(hook, dict)
            and isinstance(hook.get("command"), str)
            and needle in hook["command"]
        ):
            return True
    return False


def _is_legacy_standalone_stop(entry: Any) -> bool:
    """True when *entry* is a LEGACY standalone Stop guard (pre-fusion), not the fused one.

    A legacy entry mentions one of the three guard needles but NOT the
    ``stop_multiplex`` needle. The fused entry mentions all three guard needles AND
    the multiplex needle (its command lists them as args), so it is never
    classified legacy — the migration removes only the pre-fusion standalone
    entries, never the fused one it is installing.
    """
    if _entry_mentions(entry, _STOP_MULTIPLEX_NEEDLE):
        return False
    return any(_entry_mentions(entry, needle) for needle in _STOP_MULTIPLEX_GUARDS)


def _merge_stop_multiplex_hook(
    claude_dir: Path, *, entry: dict[str, Any], dry_run: bool
) -> dict[str, Any]:
    """Install the fused ``Stop`` hook *entry*, removing the three legacy standalone entries.

    ONE atomic write on ``settings.json``'s ``hooks.Stop`` array (F2 — the
    539c1cdc regression zone the memo names): the legacy standalone Stop guards
    (``skill_return_stop_guard`` / ``decision_rendezvous_stop_guard`` /
    ``relay_audit_stop``) are dropped and the single ``stop_multiplex`` *entry*
    (rendered by :class:`ClaudeCodeProfile` from the profile's stop descriptor) is
    added/healed in the same write, so an upgrade from the three-entry shape can
    never leave a duplicate Stop guard behind. Every other ``Stop`` entry (a
    user's own hook) and every other key is preserved verbatim.

    Idempotent: with the fused entry already present byte-equal and no legacy left,
    it is ``already-present``. Self-healing: a stale fused entry (moved venv) is
    ``updated`` in place; a leftover legacy entry is removed and reported under
    ``removed_legacy``.

    Returns ``{settings_path, action, removed_legacy, wrote}`` where ``action`` is
    ``"added"`` (fused entry newly appended) / ``"updated"`` (healed in place or a
    legacy entry removed) / ``"already-present"`` / ``"skipped-unparseable"`` /
    ``"dry-run-would-add"`` / ``"dry-run-would-update"``, and ``removed_legacy``
    lists the legacy guard needles dropped.
    """

    def plan(settings: dict[str, Any]) -> _MergeOutcome:
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            hooks = {}
        stop_entries = hooks.get("Stop")
        if not isinstance(stop_entries, list):
            stop_entries = []

        kept = [e for e in stop_entries if not _is_legacy_standalone_stop(e)]
        removed = [e for e in stop_entries if _is_legacy_standalone_stop(e)]
        removed_needles = sorted(
            {
                needle
                for e in removed
                for needle in _STOP_MULTIPLEX_GUARDS
                if _entry_mentions(e, needle)
            }
        )

        existing_idx = _find_hook_entry_index(kept, _STOP_MULTIPLEX_NEEDLE)
        multiplex_present_equal = existing_idx is not None and kept[existing_idx] == entry

        if multiplex_present_equal and not removed:
            return _MergeOutcome(False, settings, "", "", {}, {"removed_legacy": []})

        new_stop = list(kept)
        if existing_idx is None:
            new_stop.append(entry)
            write_action = "added"
            dryrun_action = "dry-run-would-add"
        else:
            new_stop[existing_idx] = entry
            write_action = "updated"
            dryrun_action = "dry-run-would-update"
        # A removal-only change (fused entry already correct) is an update.
        if multiplex_present_equal and removed:
            write_action = "updated"
            dryrun_action = "dry-run-would-update"

        hooks["Stop"] = new_stop
        settings["hooks"] = hooks
        return _MergeOutcome(
            True,
            settings,
            write_action,
            dryrun_action,
            {"removed_legacy": removed_needles},
            {"removed_legacy": []},
        )

    return _merge_json(
        claude_dir / "settings.json",
        path_key="settings_path",
        unparseable_extra={"removed_legacy": []},
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
    invoke ``/submit-hpc`` from any working directory). This
    :func:`install_agent_assets` path is the ONLY settings.json writer in the
    tree — ``ops/memory/interview.py`` no longer writes any settings / permission
    grant (the historical project-scoped Bash grant #190 is gone).

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


# ── manifest-stamped pruning of removed/renamed assets (#F34) ────────────────
#
# ``_install_tree`` is COPY-ONLY: it adds/overwrites but never removes. So an
# asset a release DELETED or RENAMED (e.g. the §6 worker removal dropped the
# ``hpc-worker`` agent) stayed installed forever — a pre-§6 install keeps
# ``~/.claude/agents/hpc-worker.md`` and its ``Skill(...)`` grant, and Claude
# Code still discovers + routes to the stale skill/agent whose procedure drives
# verbs the upgraded CLI refuses. The deny-rule and hook classes already
# self-heal (``_merge_deny_rules`` REMOVES stale blanket rules); this is the
# equivalent removal step for the three copied trees. We stamp a manifest of the
# names THIS install owns and, on the next install, prune the owned names the
# current tree no longer ships — never touching a name we did not stamp, so a
# user's own hand-added skill/command is safe.

_ASSET_MANIFEST_NAME = ".hpc-agent-manifest.json"
_MANIFEST_KINDS: tuple[tuple[str, str, str | None], ...] = (
    # (kind, subdir, suffix) — skills are per-name DIRECTORIES (suffix None);
    # commands / agents are flat ``<name>.md`` files.
    ("commands", "commands", ".md"),
    ("skills", "skills", None),
    ("agents", "agents", ".md"),
)

# Pre-manifest orphans — asset names an install shipped BEFORE ownership was
# tracked (the manifest stamp landed with #F34). ``_prune_stale_assets`` derives
# "stale" as ``previous_manifest[kind] - current[kind]``, so a name that no
# manifest EVER owned is invisible to that subtraction and can never be swept.
# This curated set closes the gap for the known assets retired before ownership
# tracking existed, so the next ``install-commands`` finally prunes them.
#
# Incident (the preflight → hpc-preflight dead-skill orphan, 2026-07-18): a fresh
# session ran ``/preflight``; the installed ``commands/preflight.md`` orphan told
# it to "Invoke the ``hpc-preflight`` skill" — a skill retired when
# environment-authority work moved to ``hpc-agent setup`` (see
# ``scripts/lint_skill_command_sync.py``). Neither the dead command nor the dead
# skill was ever manifest-owned, so the pruner had never removed them.
#
# The legacy sweep keeps the pruner's safety posture: it acts ONLY when the file
# is actually present AND the name is not owned by the current install (a name
# the current tree re-ships is never touched), and a missing file is fine.
_LEGACY_OWNED: dict[str, frozenset[str]] = {
    "commands": frozenset({"preflight", "sync", "validate-campaign", "hpc-axes-init"}),
    "skills": frozenset({"hpc-preflight"}),
    "agents": frozenset(),
}


# hpc-agent authorship sentinel — the guard the LEGACY sweep needs (the sync.md
# collision, 2026-07-18). The manifest-stale sweep deletes only names THIS project
# stamped, so authorship is PROVEN. The legacy sweep, by contrast, deletes by NAME
# with no manifest proof: a user's own hand-authored ``~/.claude/commands/sync.md``
# (``sync`` is a perfectly ordinary name a user might pick) would be destroyed on
# first install — violating the module doctrine that "a user's own hand-added
# skill/command is safe". Before unlinking a legacy-owned name we therefore READ
# the file and require a conservative hpc-agent authorship marker in its content.
#
# The retired assets are known content — they drive hpc-agent machinery, so they
# NAME it. ``hpc-agent`` (the hyphenated package name) is the anchor: no generic
# user-authored ``sync`` / ``preflight`` note would plausibly contain that exact
# hyphenated token, whereas EVERY generated command/skill does (it references the
# CLI, the skills, the hook module path). The skill-invocation idiom
# ("Invoke the ``hpc-…`` skill") and the retired skill ids are equally
# hpc-specific corroborators. A name-matched file WITHOUT any marker is treated as
# a user collision: KEPT in place and reported (``legacy_name_skipped``) so the
# human learns of the clash without losing their file.
_HPC_AUTHORSHIP_MARKERS: tuple[str, ...] = (
    "hpc-agent",  # the hyphenated package name — the primary, high-confidence anchor
    "hpc-preflight",  # a retired-skill id the dead ``preflight`` command still names
    "invoke the `hpc-",  # the generated skill-invocation idiom (backtick-fenced)
    "hpc-submit",  # a shipped skill id every generated command surface references
    "hpc-status",
    "hpc-aggregate",
    "hpc-campaign",
)


def _is_hpc_authored(text: str) -> bool:
    """True when *text* carries a conservative hpc-agent authorship marker.

    Keyed on strings no user-authored generic file would plausibly contain — the
    hyphenated package name ``hpc-agent`` is the anchor (see
    :data:`_HPC_AUTHORSHIP_MARKERS`). Case-insensitive. Deliberately conservative:
    a legacy-owned name whose on-disk content fails this test is treated as a
    user's own collision (kept + reported), never deleted — a false delete of a
    user's file is far worse than leaving one stale orphan in place.
    """
    low = text.lower()
    return any(marker in low for marker in _HPC_AUTHORSHIP_MARKERS)


def _read_asset_text(path: Path) -> str | None:
    """Read a legacy asset's text for the authorship check (``None`` if unreadable).

    Commands/agents are ``<name>.md`` FILES read directly; a skill is a DIRECTORY
    whose authorship lives in its ``SKILL.md``. Any read/decode error → ``None``
    so the caller fails OPEN (skips, never deletes): an asset we cannot read is
    never assumed hpc-authored.
    """
    try:
        target = path / "SKILL.md" if path.is_dir() else path
        return target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _asset_manifest_path(claude_dir: Path) -> Path:
    """Path to the install manifest stamping which asset names hpc-agent owns."""
    return claude_dir / _ASSET_MANIFEST_NAME


def _read_asset_manifest(claude_dir: Path) -> dict[str, list[str]]:
    """Read the previous install's owned-asset manifest (empty on any absence/parse error)."""
    try:
        data = json.loads(_asset_manifest_path(claude_dir).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, list[str]] = {}
    for kind, _subdir, _suffix in _MANIFEST_KINDS:
        val = data.get(kind)
        out[kind] = [x for x in val if isinstance(x, str)] if isinstance(val, list) else []
    return out


def _prune_stale_assets(
    claude_dir: Path, *, current: dict[str, set[str]], dry_run: bool
) -> dict[str, list[str]]:
    """Delete assets a PRIOR install owned that the CURRENT tree no longer ships.

    Reads the manifest (:func:`_asset_manifest_path`) the previous install wrote
    and, per ``commands`` / ``skills`` / ``agents``, removes the on-disk asset for
    every name the manifest OWNED but the current install did not re-copy. Only
    manifest-owned names are touched (a user's own asset was never stamped, so it
    is never pruned). Returns ``{commands, skills, agents}`` of the names pruned
    from ownership (a stale skill's ``Skill(...)`` grant is dropped for these).

    Additionally sweeps the curated :data:`_LEGACY_OWNED` set — pre-manifest
    orphans that no manifest ever owned, so the manifest subtraction above can
    never reach them (the preflight → hpc-preflight incident). These are swept
    conservatively: only when the file is actually present, the name is not owned
    by the current install (a name the current tree re-ships is spared), AND the
    on-disk content passes the hpc-agent authorship check (:func:`_is_hpc_authored`)
    — because a legacy NAME is not proof WE wrote the file. A name-matched file
    that is unreadable is left in place (fail-open); one that is readable but
    UNAUTHORED (a user's own same-named file) is left in place and recorded under
    the ``legacy_name_skipped`` result key, so the human learns of the collision
    without losing their file (the sync.md collision).
    """
    previous = _read_asset_manifest(claude_dir)
    pruned: dict[str, list[str]] = {kind: [] for kind, _s, _x in _MANIFEST_KINDS}
    # A user's own file at a legacy-owned name — kept, not deleted, and surfaced
    # here as ``"<kind>/<name>"`` so the install result discloses the clash.
    pruned["legacy_name_skipped"] = []

    def _remove(subdir: str, suffix: str | None, name: str) -> None:
        leaf = name if suffix is None else f"{name}{suffix}"
        target = claude_dir / subdir / leaf
        if not dry_run and target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()

    for kind, subdir, suffix in _MANIFEST_KINDS:
        current_owned = current[kind]
        # Manifest-owned names the current tree drops: recorded regardless of
        # whether the file survives, so the manifest stamp and (for skills) the
        # Skill(...) grant are cleared even if the file was hand-deleted.
        manifest_stale = sorted(set(previous.get(kind, ())) - current_owned)
        for name in manifest_stale:
            _remove(subdir, suffix, name)
            pruned[kind].append(name)
        # Legacy pre-manifest orphans: never stamped, so unreachable above. Swept
        # only when the file exists AND the current install does not own the name
        # (and not already handled as manifest-stale). A missing orphan is a
        # no-op — nothing to prune and no grant to drop.
        legacy_stale = sorted(
            (_LEGACY_OWNED.get(kind, frozenset()) - current_owned) - set(manifest_stale)
        )
        for name in legacy_stale:
            leaf = name if suffix is None else f"{name}{suffix}"
            asset_path = claude_dir / subdir / leaf
            if not asset_path.exists():
                continue
            # Authorship gate (the sync.md collision): a legacy-owned NAME proves
            # nothing about who wrote the FILE. Read it and require an hpc-agent
            # authorship marker before deleting. Unreadable → skip (fail-open).
            # Readable-but-unauthored → a user's own same-named file: keep it and
            # report the collision, never delete.
            text = _read_asset_text(asset_path)
            if text is None:
                continue  # fail-open: never delete what we could not read
            if not _is_hpc_authored(text):
                pruned["legacy_name_skipped"].append(f"{kind}/{name}")
                continue
            _remove(subdir, suffix, name)
            pruned[kind].append(name)
    return pruned


def _write_asset_manifest(
    claude_dir: Path,
    *,
    commands: set[str],
    skills: set[str],
    agents: set[str],
    version: str | None = None,
    dry_run: bool,
) -> dict[str, Any]:
    """Stamp the names + package *version* this install owns (skipped on dry-run).

    *version* is injectable (premortem D5 — the install path passes it so the
    hermetic golden is not tied to the live wheel version); it defaults to the
    live :data:`hpc_agent.__version__` when omitted.
    """
    if version is None:
        from hpc_agent import __version__

        version = __version__
    path = _asset_manifest_path(claude_dir)
    payload = {
        "version": version,
        "commands": sorted(commands),
        "skills": sorted(skills),
        "agents": sorted(agents),
    }
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return {"manifest_path": str(path), "wrote": not dry_run, **payload}


def _prune_skill_permissions(
    claude_dir: Path, skill_names: list[str], *, dry_run: bool
) -> dict[str, Any]:
    """Drop the ``Skill(<name>)`` allow rules for pruned skills (mirror of the add merge).

    Sibling of :func:`_merge_skill_permissions`, but REMOVING: a skill a release
    deleted keeps its auto-invoke grant otherwise. Same skip-unparseable +
    dry-run contract; reports ``removed`` (like :func:`_merge_deny_rules`). Every
    other allow entry — a user's own rule, a surviving skill's grant — is kept.
    """

    def plan(settings: dict[str, Any]) -> _MergeOutcome:
        permissions = settings.get("permissions")
        if not isinstance(permissions, dict):
            return _MergeOutcome(False, settings, "", "", {}, {"removed": []})
        allow = permissions.get("allow")
        if not isinstance(allow, list):
            return _MergeOutcome(False, settings, "", "", {}, {"removed": []})
        rules = {_skill_allow_rule(name) for name in skill_names}
        present = [rule for rule in allow if rule in rules]
        if not present:
            return _MergeOutcome(False, settings, "", "", {}, {"removed": []})
        permissions["allow"] = [rule for rule in allow if rule not in rules]
        settings["permissions"] = permissions
        return _MergeOutcome(
            True, settings, "removed", "dry-run-would-remove", {"removed": present}, {"removed": []}
        )

    return _merge_json(
        claude_dir / "settings.json",
        path_key="settings_path",
        unparseable_extra={"removed": []},
        plan=plan,
        dry_run=dry_run,
    )


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

    The public entrypoint: resolves the live install inputs (the current
    interpreter, the wheel version, the configured cluster hosts, and the core +
    plugin asset roots) and drives :func:`_install_from_profile` over
    :data:`CLAUDE_CODE_PROFILE`. The declarative WHAT-to-wire lives in that
    profile; :class:`ClaudeCodeProfile` renders it into the exact layout below.

    Result shape::

        {
            "claude_dir": "<resolved path>",
            "commands_installed": ["aggregate-hpc", ...],
            "skills_installed": ["hpc-submit", ...],
            "agents_installed": [],
            "cleared_collisions": ["/.../.claude/agents", ...],
            "settings_hook": {"settings_path": "...", "action": "added", "wrote": <bool>},
            "settings_rendezvous_hook": {"settings_path": "...", "action": "added",
                                         "wrote": <bool>},
            "settings_alert_count_hook": {"settings_path": "...", "action": "added",
                                          "wrote": <bool>},
            "settings_utterance_hook": {"settings_path": "...", "action": "added",
                                        "wrote": <bool>},
            "settings_stop_multiplex_hook": {"settings_path": "...", "action": "added",
                                             "removed_legacy": ["...relay_audit_stop", ...],
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
    ``<claude>/settings.json`` — see :func:`_merge_hook_entry`. Each
    ``action`` is ``"added"`` / ``"already-present"`` / ``"updated"`` /
    ``"skipped-unparseable"`` / ``"dry-run-would-add"`` / ``"dry-run-would-update"``.
    The full set of non-Stop hooks wired (in order) is the profile's
    ``hook_descriptors`` (:data:`CLAUDE_CODE_PROFILE`).

    ``settings_stop_multiplex_hook`` reports the fused ``Stop`` hook merge — the
    single ``stop_multiplex`` entry that dispatches all three Stop guards in one
    interpreter start, installed while removing the three legacy standalone Stop
    entries in the same write (see :func:`_merge_stop_multiplex_hook`). Its
    ``removed_legacy`` lists the legacy guard needles dropped on an upgrade.

    ``settings_permissions`` reports the additive, idempotent merge of
    ``Skill(<name>)`` allow rules for every installed skill into
    ``<claude>/settings.json``'s ``permissions.allow`` — see
    :func:`_merge_skill_permissions`. Its ``action`` is ``"added"`` /
    ``"already-present"`` / ``"skipped-unparseable"`` /
    ``"dry-run-would-add"``, and ``added`` lists the rule strings
    actually appended (or that *would* have been on dry-run).
    """
    from hpc_agent import __version__
    from hpc_agent._kernel.registry.plugins import plugin_slash_command_roots

    profile = CLAUDE_CODE_PROFILE
    # Core asset tree first, then plugin overlays (last writer wins by path).
    asset_roots: list[Any] = [files(profile.asset_package), *plugin_slash_command_roots()]
    return _install_from_profile(
        profile,
        claude_dir=claude_dir,
        dry_run=dry_run,
        executable=sys.executable,
        version=__version__,
        cluster_hosts=_configured_cluster_hosts(),
        asset_roots=asset_roots,
    )


def _install_from_profile(
    profile: HarnessProfile,
    *,
    claude_dir: Path | None,
    dry_run: bool,
    executable: str,
    version: str,
    cluster_hosts: Sequence[str],
    asset_roots: Sequence[Any],
) -> dict[str, Any]:
    """Render *profile* into Claude Code config under INJECTED inputs.

    The pure, hermetic-input core of :func:`install_agent_assets` — every source
    of install non-determinism the golden pins is a PARAMETER, never captured
    live (premortem D5):

    * ``executable`` — embedded in every hook command AND the MCP server command;
    * ``version`` — stamped into the asset manifest;
    * ``cluster_hosts`` — the host-scoped raw-ssh/scp deny rules;
    * ``asset_roots`` — the ``commands/skills/agents`` trees to distribute, in
      install order (core first, plugins overlaid).

    The public wrapper supplies the live values; ``tests/cli/test_profile_golden``
    injects hermetic ones to pin ``ClaudeCodeProfile``'s render byte-for-byte.
    """
    target = (claude_dir or resolve_claude_dir()).expanduser()

    commands: set[str] = set()
    skills: set[str] = set()
    agents: set[str] = set()
    cleared: list[str] = []

    for root in asset_roots:
        tree_commands, tree_skills, tree_agents, tree_cleared = _install_tree(
            root, target, dry_run=dry_run
        )
        commands.update(tree_commands)
        skills.update(tree_skills)
        agents.update(tree_agents)
        cleared.extend(tree_cleared)

    # Wire every hook descriptor from the profile into settings.json — additive +
    # idempotent, never clobbering existing hooks/keys, matched on each hook's
    # module-path needle. Order is load-bearing (entries append per event in
    # descriptor order); the renderer maps the neutral event/matcher/prefilter to
    # Claude Code's strings and the report key comes from _HOOK_REPORT_KEYS.
    hook_reports: dict[str, Any] = {}
    for descriptor in profile.hook_descriptors:
        hook_reports[_HOOK_REPORT_KEYS[descriptor.needle]] = _merge_hook_entry(
            target,
            event=ClaudeCodeProfile.event_string(descriptor.event),
            entry=ClaudeCodeProfile.render_hook_entry(descriptor, executable),
            needle=descriptor.needle,
            dry_run=dry_run,
        )

    # Install the fused Stop hook (stop_multiplex), removing the three legacy
    # standalone Stop entries in the same write (F2). This is the ONLY Stop-event
    # writer — the three guards are dispatched by the one fused entry.
    hook_reports["settings_stop_multiplex_hook"] = _merge_stop_multiplex_hook(
        target,
        entry=ClaudeCodeProfile.render_stop_entry(profile.stop_hook, executable),
        dry_run=dry_run,
    )

    # Register the registry-projected MCP server (hpc-agent mcp-serve) as the
    # preferred shell-free block-invocation surface (design §3) — additive +
    # idempotent into .claude.json's mcpServers, never clobbering other servers.
    mcp_server = _register_mcp_server(
        target, mcp=profile.mcp_server, executable=executable, dry_run=dry_run
    )

    # Prune assets a prior install owned that this tree no longer ships (#F34) —
    # the removal step _install_tree lacks. Runs against the manifest the last
    # install stamped, then re-stamps below with the current ownership. Only
    # manifest-owned names are removed; a user's own asset is never touched.
    assets_pruned = _prune_stale_assets(
        target,
        current={"commands": commands, "skills": skills, "agents": agents},
        dry_run=dry_run,
    )
    settings_permissions_pruned = _prune_skill_permissions(
        target, assets_pruned["skills"], dry_run=dry_run
    )
    asset_manifest = _write_asset_manifest(
        target, commands=commands, skills=skills, agents=agents, version=version, dry_run=dry_run
    )

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
        _raw_ssh_deny_rules(list(cluster_hosts)),
        remove_rules=_BLANKET_SSH_DENY_RULES,
        dry_run=dry_run,
    )

    return {
        "claude_dir": str(target),
        "commands_installed": sorted(commands),
        "skills_installed": sorted(skills),
        "agents_installed": sorted(agents),
        "cleared_collisions": cleared,
        "assets_pruned": assets_pruned,
        "asset_manifest": asset_manifest,
        **hook_reports,
        "settings_permissions": settings_permissions,
        "settings_permissions_pruned": settings_permissions_pruned,
        "settings_deny": settings_deny,
        "mcp_server": mcp_server,
        "wrote": not dry_run,
    }
