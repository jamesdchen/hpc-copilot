# Release pipeline: build + publish in CI via PyPI trusted publishing

Status: **SHIPPED** (2026-07-07, `.github/workflows/release.yml`). The `release`
skill (`src/slash_commands/skills/release/SKILL.md`) still does all the local,
reversible prep and STOPS; the actual PyPI upload moved off the human's laptop
into GitHub Actions, triggered by pushing the `v<version>` tag. This document is
the decision record: the hazards it retires, the trusted-publishing model, and
the **one-time human setup** that only a repo/PyPI owner can do.

## Problem — three standing release hazards

The pre-CI release path built the wheel and ran `uv publish` from the developer's
Windows box. Three failure classes accumulated around that, each caught by hand
(or not) more than once:

1. **A long-lived PyPI token.** Publishing needed `UV_PUBLISH_TOKEN`, which leaks
   into the terminal transcript, so every release ended with a "rotate the token"
   step. That rotation item had been open since run #4 — a standing liability for
   as long as any token exists.
2. **A creds-leak hazard in package-data.** `src/hpc_agent/config/clusters.yaml`
   ships verbatim inside the wheel. A developer testing against real clusters
   edits it with real usernames/scratch paths, so every local `uv build` was
   preceded by stashing those creds out of the working tree — a manual gate that
   was forgotten twice (`release_clusters_yaml_hazard`).
3. **A stale `build/` dir poisoning the sha stamp.** `setup.py` stamps the git
   commit sha into the wheel (`hpc_agent/_build_info.py`, surfaced by
   `hpc-agent --version` as `<version>+g<sha>`). setuptools merges a leftover
   in-tree `build/` into the wheel, so a stale `build/` silently shipped an
   old — or wrong — `BUILD_SHA` for weeks until it was root-caused (the
   `chore: ignore setuptools build artifacts` commit).

## Decision — publish from a clean CI checkout, OIDC-authenticated

`.github/workflows/release.yml`, triggered on a `v*` tag push, builds and
publishes. This structurally retires all three hazards:

1. **No token to rotate.** The `publish` job uses **PyPI trusted publishing**:
   `pypa/gh-action-pypi-publish@release/v1` with **no `password:`**. GitHub mints
   a short-lived OIDC id-token for the job (`permissions: id-token: write`); PyPI
   verifies it against a pre-registered trusted publisher and issues a
   single-use, minutes-long upload credential. No long-lived secret is stored in
   the repo, GitHub, or a transcript — so there is nothing to leak and nothing to
   rotate.
2. **A creds leak is structurally impossible.** CI checks out the committed tree
   (a clean `actions/checkout`), which carries only the placeholder
   `clusters.yaml` — a developer's local real-creds edit lives only on their box
   and is never in the checkout. Belt-and-suspenders: the build job reads the
   packaged `clusters.yaml` **out of the wheel zip** and greps it for the
   `<your_user>` placeholder; a missing placeholder fails the build before
   publish. (`tests/contracts/test_bundled_clusters_placeholders.py` is the same
   gate at pytest time.)
3. **No stale `build/` to poison the stamp.** Every run is a fresh checkout with
   no prior `build/`, so the merge-in-stale-artifacts class cannot occur. And the
   build job **proves** the stamp landed: it installs the wheel into a scratch
   venv and asserts `hpc-agent --version` contains `g<sha8>` for the tagged
   commit. If the stamp did not land (e.g. a future refactor builds the wheel
   from the sdist, which has no `.git`), the build fails loudly instead of
   shipping an untraceable wheel. That assertion is the exact failure class this
   pipeline exists to kill.

### Workflow shape

- **Triggers.** `push` of a `v*` tag (the real publish path) and
  `workflow_dispatch` (build + verify only — the `publish` job is gated
  `if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')`, so a
  manual dispatch always runs the gates and never uploads).
- **`build` job** (ubuntu, `permissions: contents: read`): `actions/checkout` with
  `fetch-depth: 0` (full history so the sha stamp resolves), `python -m build
  --sdist --wheel` (**both flags on purpose** — that builds each artifact directly
  from the source checkout so `.git` is present for the stamp; a bare
  `python -m build` builds the wheel from the unpacked sdist and would ship it
  UNSTAMPED), the two verify gates above, then `actions/upload-artifact` of
  `dist/`.
- **`publish` job** (`needs: build`, `environment: pypi`, `permissions:
  id-token: write` + `contents: write`): downloads the verified `dist/`, publishes
  via `pypa/gh-action-pypi-publish@release/v1` (no password), then attaches the
  sdist + wheel to the GitHub release with `softprops/action-gh-release@v2`.

The `release` skill's Step-9 checklist no longer contains a `uv publish` line or a
token-rotation step; they are replaced by `git push origin v<version>` (the tag
push IS the publish trigger) plus a `gh run watch` step to follow the workflow.

### The version number vs the fingerprint

The wheel's PyPI version is the clean `pyproject.toml` value (e.g. `0.11.0`) —
PyPI rejects PEP 440 local-version segments, so the `+g<sha>` fingerprint is NOT
in the package version or the wheel filename. The sha travels inside the stamped
`_build_info.py` and surfaces only at runtime via `full_version()` /
`hpc-agent --version` / MCP `serverInfo` / the `doctor` skew check. The verify
gate greps the `--version` string, not the filename.

## One-time user setup (only a human owner can do this)

Until BOTH of these are done, the `publish` job fails with a clear OIDC /
"trusted publisher" error (the upload is refused because PyPI has no publisher to
match the presented id-token). This is by design — a misconfigured release fails
closed, it does not fall back to an insecure path.

1. **PyPI — register the trusted publisher.**
   On https://pypi.org, project `hpc-agent` → **Manage** → **Publishing** → **Add
   a new pending/trusted publisher** → **GitHub**, with EXACTLY:
   - **Owner:** `jamesdchen`
   - **Repository:** `hpc-copilot`
   - **Workflow name:** `release.yml`
   - **Environment:** `pypi`

   (If the project does not exist on PyPI yet, add it as a *pending* publisher of
   the same shape — the first successful run creates the project.)

2. **GitHub — create the `pypi` deployment environment.**
   Repo `jamesdchen/hpc-copilot` → **Settings** → **Environments** → **New
   environment** named exactly `pypi`. Optionally add **Required reviewers** —
   that turns the environment into the final manual gate: after a tag push, the
   `publish` job pauses until a named reviewer approves, giving a human the last
   look before anything reaches PyPI. (The build + verify gates have already run
   by then, so the reviewer is approving a wheel that is proven stamped and
   creds-clean.)

The environment name, workflow filename, owner, and repo in `release.yml` and in
the PyPI publisher registration must match **character for character** — a
mismatch is the most common "it fails with OIDC error" cause.

## Verification / how to exercise it safely

- **Dry run (no publish):** Actions → `release` → **Run workflow**. A manual
  dispatch runs `build` + both verify gates and skips `publish` entirely — use it
  to confirm a candidate commit builds and stamps cleanly before tagging.
- **Real release:** the `release` skill preps locally and stops; the human runs
  `git tag v<new> && git push origin v<new>`, which fires the workflow.

## Why not the alternatives

- **Keep publishing from the laptop with a scoped token.** Still a long-lived
  secret to store and rotate, still requires the manual creds-stash before every
  build, and still exposed to a stale local `build/`. Trusted publishing + a clean
  checkout removes all three at once.
- **A GitHub Actions secret holding a PyPI token (`secrets.PYPI_API_TOKEN`).**
  Removes the transcript-leak but keeps a long-lived credential in the repo's
  secret store — exactly the standing-liability class hazard #1 is about. OIDC has
  no stored secret.
- **Build the wheel locally and only upload from CI.** Reintroduces the stale-
  `build/` and creds-stash hazards on the local build; the whole point is that the
  *published* artifact comes from a clean, verified CI checkout.
