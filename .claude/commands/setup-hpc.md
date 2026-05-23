# /setup-hpc — Install hpc-agent and verify the cluster environment

One-time setup: install the package, copy the bundled slash commands and skills, then probe each cluster the user plans to submit to. The probe populates the 24h cache marker that `/submit-hpc`'s Step 6b gate reads, so the first submit doesn't have to re-run it. **Environment authority lives here** — runtime workflows assume setup ran successfully; if SSH credentials or a cluster's reachability change later, re-run setup.

## Steps

1. Install the package. From a repo checkout, run `pip install -e .` from the repo root (use `uv pip install -e .` if the venv is uv-managed). A pip-only user installs the published package with `pip install hpc-agent` instead.

2. Run `hpc-agent install-commands` to copy the bundled slash commands and skills into `~/.claude/commands/` and `~/.claude/skills/`. The assets ship inside the package, so this works identically for an editable checkout and a wheel install. Pass `--dry-run` first to preview the file list.

3. **Probe each cluster the user will submit to.** Ask which clusters they plan to use (e.g. `hoffman2`, `discovery`). For each cluster, from inside the experiment directory (so the cache marker lands in the right per-repo journal):

   ```bash
   hpc-agent setup --cluster <name>
   ```

   This runs `install-commands` again (idempotent) plus a `check-preflight` probe. Parse `data.preflight.checks[]` and remediate any failure by check name. **Do not paraphrase failure details — surface the `detail` field verbatim so the user can act on it:**

   - `ssh_auth_sock == false` — `SSH_AUTH_SOCK` is unset or the agent has no keys. Walk the user through:
     ```bash
     eval "$(ssh-agent -s)"
     ssh-add ~/.ssh/<key>     # or whichever key authenticates to the cluster
     ```
     Then re-run `hpc-agent setup --cluster <name>` from the same shell. If the user uses a session manager (tmux, mosh, screen), they must export `SSH_AUTH_SOCK` and `SSH_AGENT_PID` into that session for the agent to be reachable from the runtime workflows.
   - `ssh_on_path == false` — install OpenSSH via the system package manager (`apt install openssh-client`, `brew install openssh`, etc.).
   - `file_transfer_on_path == false` — no file-transfer transport found. Install `rsync` (preferred), or ensure `scp` + `tar` are both on PATH (the runtime falls back to a `tar`/`scp` pipeline when `rsync` is absent — typical for Windows hosts without WSL/MSYS rsync).
   - `clusters_yaml_parses == false` — surface the parse error from `detail` and stop. Point the user at `hpc_agent/config/clusters.yaml` (or whatever `HPC_CLUSTERS_CONFIG` points at).
   - `cluster_known == false` — wrong cluster name; run `hpc-agent clusters-list` and have the user pick from the available names.
   - `cluster_tcp_22 == false` — cluster offline, behind a VPN, or hostname wrong. Verify the user is on the institution's network (or VPN), then re-run.

   On green (`data.preflight.all_ok == true`), the response includes `data.preflight_marker` — the absolute path of the cache marker that was written. Report that path to the user so they know the gate will skip its re-check for the next 24h.

4. **Optional: install the wait-predictor snapshot cron** (skip if not using the pro plugin). Detect whether the `forecasting` extra is installed:

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

   - **On Y**: install the cron line idempotently. First check whether an entry already exists; if so, report and skip.

     Resolve `$CLAUDE_HPC_REPO` to the absolute path of the hpc-agent checkout (e.g. `git rev-parse --show-toplevel` from inside the repo, or hardcode the path you used for `pip install -e`). The cron job runs from `$EXPERIMENT_DIR` but invokes the scripts via their absolute path inside the hpc-agent repo so the user's experiment directory doesn't need a copy of `scripts/`.

     ```bash
     CRON_LINE="*/5 * * * * cd \"$EXPERIMENT_DIR\" && \"$CLAUDE_HPC_REPO/.venv/bin/python\" \"$CLAUDE_HPC_REPO/scripts/snapshot_squeue.py\" --ssh-target \"$SSH_TARGET\" --experiment-dir \"$EXPERIMENT_DIR\" >> .hpc/snapshot_squeue.log 2>&1"
     if crontab -l 2>/dev/null | grep -qF "scripts/snapshot_squeue.py"; then
         echo "snapshot cron already installed; skipping"
     else
         (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
         echo "installed snapshot cron: $CRON_LINE"
     fi
     ```

     Then offer to install the nightly trainer cron too:

     > Also install the nightly trainer (refits the LightGBM model from accumulated snapshots + sacct history)? [Y/n]

     On Y, append a daily cron entry. Same `$CLAUDE_HPC_REPO` resolution applies — point at the absolute path of the hpc-agent checkout (e.g. `git rev-parse --show-toplevel` from inside the repo, or hardcode the path you used for `pip install -e`):

     ```bash
     TRAIN_LINE="0 3 * * * cd \"$EXPERIMENT_DIR\" && \"$CLAUDE_HPC_REPO/.venv/bin/python\" \"$CLAUDE_HPC_REPO/scripts/extract_sacct_history.py\" --ssh-target \"$SSH_TARGET\" --since-days 30 --out completed_jobs.json && \"$CLAUDE_HPC_REPO/.venv/bin/python\" \"$CLAUDE_HPC_REPO/scripts/train_wait_predictor.py\" --completed-jobs completed_jobs.json --slot-counts slot_counts.json --experiment-dir \"$EXPERIMENT_DIR\" >> .hpc/train_wait_predictor.log 2>&1"
     if crontab -l 2>/dev/null | grep -qF "train_wait_predictor"; then
         echo "training cron already installed; skipping"
     else
         (crontab -l 2>/dev/null; echo "$TRAIN_LINE") | crontab -
         echo "installed training cron (runs at 03:00 daily)"
     fi
     ```

   - **On N**: note that the user can install manually by editing crontab themselves; the predictor still works in floor-only mode (no LightGBM residual) when no model has been trained.

   This step is idempotent — re-running `/setup-hpc` after a successful cron install detects the existing entries and skips. To remove either cron, run `crontab -e` and delete the matching line.

5. List the installed commands and confirm the `hpc_agent` package is importable.
