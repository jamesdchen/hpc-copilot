# /setup_hpc — Install claude-hpc commands and package globally

Copy all slash commands from this repo into the global Claude commands directory, install the Python package in editable mode, and (with explicit consent) wire up bundled Stop hooks that enforce slash-command exit contracts.

## Steps

1. Copy each `.md` file from `src/slash_commands/commands/` into `~/.claude/commands/`, overwriting existing files.

2. Run `pip install -e .` from the repo root (use `uv pip install -e .` if the venv is uv-managed).

3. **Preview the bundled Stop hooks** by running `hpc-mapreduce hook-install --dry-run`. Show the user the JSON envelope it would write — specifically the `added` list (e.g. `["monitor-armed"]`) and the `settings_path`. Explain in one sentence what each hook does:

   - `monitor-armed` — blocks `/monitor-hpc` from finishing without an `armed:` line. This is what makes cron-arming behavior reliable; without it the agent's compliance is best-effort.

4. **Ask for consent** before modifying `~/.claude/settings.json`:

   > Install the Stop hooks now? They take agent compliance with /monitor-hpc out of discretion (Claude Code re-prompts the agent if it tries to finish without arming a follow-up tick). [Y/n]

5. **On Y**, run `hpc-mapreduce hook-install` (no flags). Report the result envelope's `wrote` and `added` fields back to the user. **On N**, note that the user can install later by running `hpc-mapreduce hook-install` themselves.

6. List the installed commands and confirm the `claude_hpc` package is importable.
