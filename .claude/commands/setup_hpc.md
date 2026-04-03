# /setup_hpc — Install claude-hpc commands and package globally

Copy all slash commands from this repo into the global Claude commands directory, then install the Python package in editable mode.

## Steps

1. Copy each `.md` file from `commands/` (repo root) into `~/.claude/commands/`, overwriting existing files
2. Run `pip install -e .` from the repo root
3. List the installed commands and confirm the hpc_mapreduce package is importable
