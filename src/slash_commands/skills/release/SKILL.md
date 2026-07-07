---
name: release
description: "Release hpc-agent SAFELY. Does every reversible local step — bump pyproject version, run the creds + regen + lint + FULL pytest gates, COMMIT the bump locally (so the wheel fingerprints clean), build the wheel, install across the local/WSL/Hoffman2 envs, refresh ~/.claude commands, update SESSION_HANDOFF.md — then STOPS and prints the destructive/outward checklist (push, tag) for the human to run. Publishing is done by the release.yml GitHub Actions workflow via PyPI trusted publishing: the tag push IS the publish trigger, so there is no token to run or rotate. NEVER publishes, pushes, or tags itself."
allowed-tools: Bash Read Edit Write
execution: inline
category: agent-autonomous
---

`/release [VERSION | major | minor | patch]` — bump type defaults to `patch`.

**Contract:** this skill performs only **safe, local, reversible** release prep, then
**HALTS** and prints the irreversible/outward steps as a checklist. It must never run a
PyPI publish, `git push`, or tag push. The Step-5 local commit is
reversible (`git reset --soft HEAD~1`) and therefore inside the contract; *pushing* it
is not, and stays manual. Run from the work repo (`jamesdchen/hpc-copilot` checkout).
Prepend `.venv/Scripts` to PATH for all tool/regen calls.

**Publishing is CI, not this skill.** The actual PyPI upload is done by the
`.github/workflows/release.yml` GitHub Actions workflow using **PyPI trusted publishing**
(OIDC — no long-lived token). **Pushing the `v<version>` tag IS the publish trigger.**
The local wheel builds + cross-env installs below are for *validation and live use*, not
for upload — the published wheel is rebuilt from the clean CI checkout of the tagged
commit. That is what retires the three long-standing release hazards (no token to rotate,
a creds leak is structurally impossible from a clean checkout, and no stale `build/` can
poison the sha stamp). Decision record + the one-time human setup:
[`docs/internals/release-pipeline.md`](../../../../docs/internals/release-pipeline.md).

This is a **human-run** release procedure: the ssh/scp/`python -c` idioms below are
executed by the human's interactive session, not by an autonomous worker (cited in
`scripts/lint_no_raw_ssh.py` / `scripts/lint_no_blocklisted_commands.py` ALLOWLISTs).

If any step marked **(ABORT)** fails, stop immediately, report the failure, and do NOT
proceed to commit/build/install — never ship a red or creds-leaking tree.

## 0. Preflight + creds gate (ABORT)

1. Confirm repo: `git remote -v` shows `jamesdchen/hpc-copilot`.
   <!-- Remote pinned per the 2026-07-02 pivot: all new work lives on
        jamesdchen/hpc-copilot (origin); jamesdchen/hpc-agent is the frozen
        upstream jumping-off point. If the work repo ever moves again, update
        this check CONSCIOUSLY — a stale remote check here is exactly the drift
        that got this skill ported into the repo. -->
   Else STOP.
2. **Creds gate** — bundled `src/hpc_agent/config/clusters.yaml` ships in the wheel as
   package-data. Grep it for real-cred markers (real usernames like `jamesdc1`,
   `/u/scratch/`, real hostnames). If ANY real cred is present, **ABORT** — real creds
   must never ship (they belong in `demo-hpc/clusters.yaml` / `HPC_CLUSTERS_CONFIG`).
   A `creds` entry in `git stash list` is fine (it stays stashed); just confirm the
   working-tree file is placeholders. `tests/contracts/test_bundled_clusters_placeholders.py`
   is the mechanized backstop (Step 4 runs it).
3. `git status -s` — note any unexpected changes. The bump + regen will be dirty until
   Step 5 commits them; anything else dirty at this point gets swept into the release
   commit by `git add -A`, so resolve unrelated changes NOW (commit, stash, or drop).

## 1. Bump the version

Read `version = "X.Y.Z"` in `pyproject.toml`. Compute the new version from the arg
(`major`/`minor`/`patch`, default `patch`; or an explicit `X.Y.Z`). Edit `pyproject.toml`.
Print `old → new`.

## 2. Regen generated artifacts

Run the regen **--check** gates; on any drift, run the `--write` form so the release
carries current artifacts: `scripts/build_schemas.py`, `build_primitive_frontmatter.py`,
`build_primitive_index.py`, `build_operations_index.py`, `bake_operations_json.py`.

## 3. Pre-commit gate (ABORT)

On changed `.py` (in parallel): `ruff check --fix`, `ruff format`,
`python -m mypy --ignore-missing-imports`. Fix trivial issues; ABORT on a real failure.

## 4. Full test suite (ABORT on red)

`python -m pytest -q; echo "EXIT=$?"` — do NOT pipe to `tail` (it masks pytest's exit
code). The full suite is THE release gate. Any failure ⇒ **ABORT**.

## 5. Commit the bump locally (BEFORE building)

`git add -A && git commit -m "release: <new>"`

The commit comes BEFORE the build so the wheel fingerprints **clean**:
`setup.py` embeds the HEAD sha into the wheel, and a dirty tree stamps
`<new>+g<sha>.dirty` (2026-07-04 incident: 0.11.0 was built with the bump
uncommitted, `.dirty` wheels got installed into all four environments, and the
post-commit rebuild+reinstall had to redo every install). A *local* commit is
reversible — `git reset --soft HEAD~1` unwinds it if the release aborts after
this point — so it stays inside the safety contract; **pushing** it is the
human's Step-9 call.

Verify: `git status -s` is clean, `git log --oneline -1` shows `release: <new>`.

## 6. Build the wheel

**6a. Purge stale build artifacts FIRST** — setuptools merges an in-tree `build/` +
`src/hpc_agent.egg-info/` into the wheel, so files deleted from the source tree
re-appear in the build (2026-07-04 incident: the §6-deleted worker transport shipped
in a fresh wheel until `build/` was cleared; `uv build`'s isolation covers
*dependencies*, not the source tree's `build/` dir):

```
python -c "import shutil; [shutil.rmtree(p, ignore_errors=True) for p in ('build', 'src/hpc_agent.egg-info')]"
```

**6b.** `uv build --wheel`. Capture the `dist/hpc_agent-<new>-*.whl` path.

**6c. Fingerprint gate (ABORT on `.dirty`)** — the wheel's embedded fingerprint must
be `+g<sha>` of the Step-5 release commit, with NO `.dirty` suffix. Check the wheel's
`hpc_agent/_build_info.py` (`BUILD_SHA` set, `BUILD_DIRTY` False) or install into a
scratch env and read `hpc-agent --version`. A `.dirty` fingerprint means the tree
changed after Step 5 — find out why, recommit, rebuild.

**6d. Content gate (ABORT on failure)** — every file in the wheel must be
git-tracked in the source tree; a hit means stale artifacts leaked despite 6a:

```
python - <<'EOF'
import pathlib, subprocess, sys, zipfile
# Newest by mtime — dist/ accumulates old wheels, and lexical sort picks
# 0.10.9 over 0.10.65. (Validated 2026-07-04: flags a stale wheel, passes a
# clean one.)
whl = max(pathlib.Path('dist').glob('hpc_agent-*.whl'), key=lambda p: p.stat().st_mtime)
tracked = set(subprocess.run(['git','ls-files','src'], capture_output=True, text=True).stdout.split())
leaks = [n for n in zipfile.ZipFile(whl).namelist()
         if '.dist-info/' not in n and not n.endswith('/')
         and f'src/{n}' not in tracked]
if leaks:
    sys.exit(f"ABORT — untracked files in wheel (stale build/ leak): {leaks[:10]}")
print(f"wheel content gate OK ({whl.name})")
EOF
```

Re-confirm the creds gate held (the built wheel must not contain real clusters.yaml
creds).

## 7. Install across the environments

Using the Step-6 wheel. Local CLI and cluster wheel MUST end on the SAME build (a
divergent local build caused the ridge_imp exit-127).

**Never rename a wheel file.** pip requires the canonical wheel filename to parse
name/version/tags — a renamed wheel fails with "Invalid wheel filename" (hit
2026-07-04). Transfer and install by the ORIGINAL basename, always.

- **Local CLI (uv tool):** `uv tool install --force <wheel>`
- **Demo venv (Windows):** `uv pip install --python C:\Users\james\demo-hpc\.venv\Scripts\python.exe --reinstall <wheel>`
- **WSL (~/.local):** `wsl.exe -- bash -lc 'pip install --user --force-reinstall --no-deps --no-cache-dir --break-system-packages "<wheel as /mnt/... wsl path>"'`
- **Hoffman2 (hpc-pi env):** needs SSH — use native Windows ssh (`/c/Windows/System32/OpenSSH/ssh.exe`, NOT bare `ssh`). Copy the wheel keeping its basename: `scp dist/hpc_agent-<new>-py3-none-any.whl hoffman2:~/` then `ssh hoffman2 '~/.conda/envs/hpc-pi/bin/pip install --force-reinstall --no-deps --no-cache-dir ~/hpc_agent-<new>-py3-none-any.whl'`. If the cluster is unreachable or SSH would prompt indefinitely, do NOT abort — record "Hoffman2 install pending" in the Step-9 checklist instead.
- Refresh the harness: `hpc-agent install-commands`.
- **Verify FINGERPRINTS, not bare versions:** every wheel-installed env must report
  the identical `hpc-agent --version` string `<new>+g<sha>` — same `+g<sha>` suffix,
  no `.dirty` (the fingerprint is what catches a stale wheel; two envs both saying
  `<new>` can still be days of commits apart). The dev checkout itself reports
  `<new>+dev.g<sha>` — its `<sha>` must match the wheels' `<sha>`; the `dev.` prefix
  just marks it as the source tree.

## 8. Update SESSION_HANDOFF.md

Edit `C:\Users\james\SESSION_HANDOFF.md` (outside the repo — does not dirty the
release commit): bump the "Current state" date + the pyproject version line, and add a
one-line entry — new version + fingerprint, "committed locally + installed
local/WSL/Hoffman2, **NOT yet pushed/published**". Keep it to live-state facts only.

## 9. STOP — print the manual destructive/outward checklist (DO NOT RUN)

Emit exactly this for the human, filled in with the real version/wheel/branch, then HALT:

```
RELEASE <new> prepped (suite green, bump committed locally as "release: <new>",
wheel built + fingerprint-verified <new>+g<sha>, installed local/WSL[/Hoffman2],
handoff updated).
Remaining steps are yours to run — irreversible/outward, NOT run by /release:

  0. (only if ABANDONING the release) unwind the local commit:
       git reset --soft HEAD~1
  1. branch+push (branch first if on main):  git push origin <branch>
  2. tag + push — THE TAG PUSH IS THE PUBLISH TRIGGER (fires release.yml, which
     builds a clean wheel from this commit and publishes to PyPI via trusted
     publishing — no token to enter, none to rotate):
       git tag v<new>
       git push origin v<new>
  3. WATCH the release workflow to green (build gate = stamp + creds; publish job
     = PyPI trusted-publish + attach dist to the GitHub release). List the run,
     then watch it by the id the list prints (two plain commands, deliberately
     not chained through a substitution — the harness classifier blocks those):
       gh run list --repo jamesdchen/hpc-copilot --workflow release.yml --limit 1
       gh run watch --repo jamesdchen/hpc-copilot <run-id-from-the-list>
     If the publish job fails with an OIDC / "trusted publisher" error, the
     one-time PyPI + GitHub-environment setup isn't done — see
     docs/internals/release-pipeline.md §"One-time user setup".
  4. (only if Step 7 deferred it) Hoffman2 install, keeping the wheel basename:
       scp dist/hpc_agent-<new>-py3-none-any.whl hoffman2:~/
       /c/Windows/System32/OpenSSH/ssh.exe hoffman2 '~/.conda/envs/hpc-pi/bin/pip install --force-reinstall --no-deps --no-cache-dir ~/hpc_agent-<new>-py3-none-any.whl'
```

Do not push or tag — those are the human's call. There is no publish command to run
and no token to rotate: the tag push in step 2 triggers CI, which publishes.
