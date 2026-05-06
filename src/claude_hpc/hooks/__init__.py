"""Stop hooks bundled with claude-hpc.

These are small Python scripts that Claude Code's hook system invokes
to enforce slash-command exit contracts. The hook scripts themselves
live as modules under this package so they can be invoked with
``python -m claude_hpc.hooks.<name>``; the configuration entry that
wires them into ``~/.claude/settings.json`` is written by
``hpc-mapreduce hook-install``.
"""
