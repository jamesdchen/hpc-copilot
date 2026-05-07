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

6. **Optional: install the wait-predictor snapshot cron.** Detect whether the `forecasting` extra is installed:

   ```bash
   python -c "import lightgbm" 2>/dev/null && echo "lightgbm: installed" || echo "lightgbm: not installed (skipping cron offer)"
   ```

   If `lightgbm` is NOT installed, skip this step entirely. Otherwise:

   - Ask the user for the cluster they'll be using and the experiment directory:

     > The wait-time predictor (`predict-start-time`) needs squeue snapshots to fit its residual model. Snapshot every 5 minutes via cron? (Recommended if you'll use the LightGBM-residual predictor; the model needs ~7-14 days of history before it's useful.)
     >
     > - SSH target (e.g. `alice@cluster.example.edu`):
     > - Experiment directory (defaults to cwd):
     > [Y/n]

   - **On Y**: install the cron line idempotently. First check whether an entry already exists; if so, report and skip:

     ```bash
     CRON_LINE="*/5 * * * * cd \"$EXPERIMENT_DIR\" && python -m scripts.snapshot_squeue --ssh-target \"$SSH_TARGET\" --experiment-dir \"$EXPERIMENT_DIR\" >> .hpc/snapshot_squeue.log 2>&1"
     if crontab -l 2>/dev/null | grep -qF "scripts.snapshot_squeue"; then
         echo "snapshot cron already installed; skipping"
     else
         (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
         echo "installed snapshot cron: $CRON_LINE"
     fi
     ```

     Then offer to install the nightly trainer cron too:

     > Also install the nightly trainer (refits the LightGBM model from accumulated snapshots + sacct history)? [Y/n]

     On Y, append a daily cron entry:

     ```bash
     TRAIN_LINE="0 3 * * * cd \"$EXPERIMENT_DIR\" && python -m scripts.extract_sacct_history --ssh-target \"$SSH_TARGET\" --since-days 30 --out completed_jobs.json && python -m scripts.train_wait_predictor --completed-jobs completed_jobs.json --slot-counts slot_counts.json --experiment-dir \"$EXPERIMENT_DIR\" >> .hpc/train_wait_predictor.log 2>&1"
     if crontab -l 2>/dev/null | grep -qF "train_wait_predictor"; then
         echo "training cron already installed; skipping"
     else
         (crontab -l 2>/dev/null; echo "$TRAIN_LINE") | crontab -
         echo "installed training cron (runs at 03:00 daily)"
     fi
     ```

   - **On N**: note that the user can install manually by editing crontab themselves; the predictor still works in floor-only mode (no LightGBM residual) when no model has been trained.

   This step is idempotent — re-running `/setup_hpc` after a successful cron install detects the existing entries and skips. To remove either cron, run `crontab -e` and delete the matching line.

7. List the installed commands and confirm the `claude_hpc` package is importable.
