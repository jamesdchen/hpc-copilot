"""hpc_agent._kernel.hooks — Claude Code harness hooks shipped by hpc-agent.

These modules are *harness-mediated*: Claude Code runs them as ``command``
hooks wired into ``~/.claude/settings.json`` (see
:func:`hpc_agent.cli.setup.install_commands`), receiving the hook payload
on stdin and emitting the hook-output JSON on stdout. They are not
``@primitive`` CLI verbs — the agent never invokes them directly.

Currently one hook lives here:

* :mod:`skill_return_autofetch` — a ``PostToolUse`` hook that auto-reads a
  sub-skill's return envelope after a composed ``Skill(<sub>)`` call so the
  parent skill never has to remember to chain ``fetch-skill-return``.
"""
