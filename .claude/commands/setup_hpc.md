# /setup_hpc — Install claude-hpc commands and package globally

Copy all slash commands from this repo into the global Claude commands directory, then install the Python package in editable mode.

## Steps

1. Copy each `.md` file from `src/slash_commands/commands/` into `~/.claude/commands/`, overwriting existing files
2. Run `pip install -e .` from the repo root (use `uv pip install -e .` if the venv is uv-managed)
3. List the installed commands and confirm the `claude_hpc` package is importable
