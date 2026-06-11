"""hpc_agent._kernel.hooks — Claude Code harness hooks shipped by hpc-agent.

These modules are *harness-mediated*: Claude Code runs them as ``command``
hooks wired into ``~/.claude/settings.json`` (see
:func:`hpc_agent.cli.setup.install_commands`), receiving the hook payload
on stdin and emitting the hook-output JSON on stdout. They are not
``@primitive`` CLI verbs — the agent never invokes them directly.

Two hooks live here, both serving the sub-skill return seam:

* :mod:`skill_return_autofetch` — a ``PostToolUse`` hook that auto-reads a
  sub-skill's return envelope the moment the sub-skill's
  ``emit-skill-return`` Bash call commits it, so the parent skill never has
  to remember to chain ``fetch-skill-return``.
* :mod:`skill_return_stop_guard` — a ``Stop`` hook that blocks ending the
  turn while a committed return envelope sits unfetched, turning the
  advisory hand-back prose at sub-skill composition boundaries into a
  deterministic continuation.
"""
