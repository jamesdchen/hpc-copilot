"""hpc_agent._kernel.hooks — Claude Code harness hooks shipped by hpc-agent.

These modules are *harness-mediated*: Claude Code runs them as ``command``
hooks wired into ``~/.claude/settings.json`` (see
:func:`hpc_agent.cli.setup.install_commands`), receiving the hook payload
on stdin and emitting the hook-output JSON on stdout. They are not
``@primitive`` CLI verbs — the agent never invokes them directly.

The sub-skill return seam:

* :mod:`skill_return_autofetch` — a ``PostToolUse`` hook that auto-reads a
  sub-skill's return envelope the moment the sub-skill's
  ``emit-skill-return`` Bash call commits it, so the parent skill never has
  to remember to chain ``fetch-skill-return``.
* :mod:`skill_return_stop_guard` — a ``Stop`` hook that blocks ending the
  turn while a committed return envelope sits unfetched, turning the
  advisory hand-back prose at sub-skill composition boundaries into a
  deterministic continuation.

The permission seam:

* :mod:`scheduler_write_fence` — a ``PreToolUse(Bash)`` hook that BLOCKS a
  mutating scheduler verb in command position (exit 2), leaving read-only
  probes alone.
* :mod:`consent_forward` — a ``PreToolUse(mcp__hpc-agent__.*)`` hook that
  forwards a journaled greenlight / live standing consent to the harness
  permission layer, so a decision the human already typed in-band is not
  re-asked by the auto-mode classifier.

**Fail posture is NOT uniform across this package.** The guards fail OPEN (a
malformed payload leaves the turn running — a missed catch, never a blocked
human), while :mod:`consent_forward` fails CLOSED to "ask", because its
non-firing state is a prompt and its firing state can be ``allow``. Each
module's docstring states and justifies its own direction; do not "normalize"
one to the other.
"""
