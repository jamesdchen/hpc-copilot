"""``HPCBackend`` that fans hpc-agent task arrays out onto GitHub Actions.

A pure-API ("crowd-compute") backend, per
``docs/proposals/crowd-compute-backend.md``: no SSH, no shared filesystem. The
"scheduler" is the Actions REST API; an "array job of N tasks" is one workflow
run whose matrix has N cells; the "job id" is the Actions run id; results come
back as artifacts rather than over a shared mount.

How it slots into submit-flow's REAL call path
-----------------------------------------------
submit-flow's single-array path (``_make_single_array_submission``) builds a
command with :meth:`_build_command`, runs it with :meth:`_execute_command`, then
parses a job id out of stdout with ``JOB_ID_REGEX``. There is no shell command
to build, so :meth:`_build_command` encodes the dispatch intent and
:meth:`_execute_command` performs it — POST ``workflow_dispatch`` then resolve
the run id — returning a ``CompletedProcess`` whose stdout is the run id. (The
submit override therefore lives in ``_execute_command``, NOT the
``submit_array_tracked`` a marketplace skeleton stubs: that method is not on
submit-flow v1's path.)

The per-task kwargs are NOT shipped in the dispatch. Exactly as the SLURM
dispatcher calls ``resolve(SLURM_ARRAY_TASK_ID)`` on the compute node, the
workflow checks out the repo and each matrix cell resolves its kwargs from the
same ``.hpc/tasks.py`` against ``HPC_TASK_ID``. The dispatch carries only the
array size and the run identity (see ``workflow-template/fan-out.yml``).

Account pool (running out of CI compute)
----------------------------------------
Set ``HPC_GHA_POOL`` to rotate across accounts when one exhausts its Actions
quota. Because the durable campaign state is local (the Optuna study + the
completed-iteration sidecars), switching accounts loses nothing: on a
quota/billing ``403`` the backend advances to the next ``owner/repo=TOKEN_ENV``
entry and re-dispatches, and the next campaign iteration simply lands on the
other account. Liveness and result-pull probe the pool, since a run id is
account-scoped — a batch that ran on account B is polled and pulled from B.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent.infra.backends import BackendBuildContext, HPCBackend, register

from ._api import GitHubActionsAPI, GitHubAPIError

if TYPE_CHECKING:
    from pathlib import Path

# Config env — the API-shaped replacement for the SSH ssh_run / remote_repo pair
# the built-in backends take (consumed by ``from_build_context``).
REPO_ENV = "HPC_GHA_REPO"  # "owner/repo"
WORKFLOW_ENV = "HPC_GHA_WORKFLOW"  # workflow file name, e.g. "fan-out.yml"
REF_ENV = "HPC_GHA_REF"  # git ref the workflow runs on (default "main")
TOKEN_ENV = "GITHUB_TOKEN"  # PAT / Actions token: actions:write + actions:read
POOL_ENV = "HPC_GHA_POOL"  # "owner/a=TOKEN_ENV_A,owner/b=TOKEN_ENV_B" — rotate on quota
DATA_TAG_ENV = "HPC_GHA_DATA_TAG"  # release tag of the dataset asset the runners download

# Sentinel marking a ``_build_command`` payload so ``_execute_command`` knows it
# is an API dispatch request, not a shell argv.
_DISPATCH = "__gha_dispatch__"

# GitHub run statuses that mean "still going" (the alive bucket).
_ALIVE_STATUSES = frozenset(
    {"queued", "in_progress", "requested", "waiting", "pending", "action_required"}
)

# Substrings that mark a 403 as an Actions quota / billing wall (vs. a
# permissions 403), used to decide whether to rotate to the next account.
_QUOTA_SIGNALS = ("minutes", "spending limit", "billing", "quota", "payment", "exceeded")


def _parse_total(task_range: str | None) -> int:
    """Map submit-flow's 1-based ``"1-N"`` task range to the count N.

    ``None`` (a single non-array job, e.g. an MPI run) means one task.
    """
    if not task_range:
        return 1
    _, _, end = task_range.partition("-")
    return int(end or task_range)


def _parse_pool(spec: str) -> list[tuple[str, str]]:
    """Parse ``HPC_GHA_POOL`` (``owner/a=TOK_A,owner/b=TOK_B``) into (repo, token) pairs.

    Tokens are referenced INDIRECTLY by env-var name so each secret stays in its
    own variable rather than being inlined into one string.
    """
    accounts: list[tuple[str, str]] = []
    for raw in spec.split(","):
        entry = raw.strip()
        if not entry:
            continue
        repo, sep, token_env = entry.partition("=")
        repo, token_env = repo.strip(), token_env.strip()
        if not sep or not repo or not token_env:
            raise errors.SpecInvalid(
                f"HPC_GHA_POOL entry {entry!r} must be 'owner/repo=TOKEN_ENV_VAR'"
            )
        token = os.environ.get(token_env)
        if not token:
            raise errors.SpecInvalid(f"HPC_GHA_POOL references unset token env {token_env!r}")
        accounts.append((repo, token))
    if not accounts:
        raise errors.SpecInvalid("HPC_GHA_POOL is set but empty")
    return accounts


@register("github-actions")
class GitHubActionsBackend(HPCBackend):
    """Fan task arrays out onto GitHub Actions runners, across one or more accounts."""

    scheduler_name = "github-actions"
    template_ext = ".yml"  # the deploy unit is the workflow file, not a scheduler script
    supports_test_only_eta = False

    def __init__(
        self,
        repo: str | None = None,
        workflow: str = "",
        ref: str = "main",
        token: str | None = None,
        *,
        pool: list[tuple[str, str]] | None = None,
        data_tag: str | None = None,
    ) -> None:
        self.workflow = workflow
        self.ref = ref
        # Release tag of the dataset asset the runners download (optional; the
        # workflow has its own default). See README "Where the input data lives".
        self.data_tag = data_tag or os.environ.get(DATA_TAG_ENV) or ""
        # No remote log directory on a runner; this only holds fetched copies.
        self.log_dir = os.path.join(".hpc", "gha-logs")
        if pool:
            accounts = list(pool)
        elif repo:
            accounts = [(repo, token or os.environ.get(TOKEN_ENV) or "")]
        else:
            raise errors.SpecInvalid("GitHubActionsBackend needs a repo or a pool")
        self._accounts = [GitHubActionsAPI(r, t) for r, t in accounts]
        self._current = 0

    @property
    def repo(self) -> str:
        """The repo of the account currently accepting dispatches."""
        return self._accounts[self._current].repo

    @property
    def token(self) -> str:
        """The token of the account currently accepting dispatches."""
        return self._accounts[self._current].token

    @classmethod
    def from_build_context(cls, ctx: BackendBuildContext) -> GitHubActionsBackend:
        """Construct from submit-flow's build context, ignoring the SSH fields.

        Reads ``$HPC_GHA_WORKFLOW`` / ``$HPC_GHA_REF`` plus EITHER ``$HPC_GHA_POOL``
        (multi-account rotation) OR ``$HPC_GHA_REPO`` + ``$GITHUB_TOKEN`` (single
        account). Missing required config fails loud with ``SpecInvalid`` (the
        pure-API analogue of a bad ssh_target) rather than dispatching into the void.
        """
        workflow = os.environ.get(WORKFLOW_ENV)
        ref = os.environ.get(REF_ENV, "main")
        pool_spec = os.environ.get(POOL_ENV)
        if pool_spec:
            if not workflow:
                raise errors.SpecInvalid(
                    f"{WORKFLOW_ENV} must be set when using {POOL_ENV} (see the plugin README)."
                )
            return cls(workflow=workflow, ref=ref, pool=_parse_pool(pool_spec))
        repo = os.environ.get(REPO_ENV)
        token = os.environ.get(TOKEN_ENV)
        missing = [
            name
            for name, val in ((REPO_ENV, repo), (WORKFLOW_ENV, workflow), (TOKEN_ENV, token))
            if not val
        ]
        if missing:
            raise errors.SpecInvalid(
                "github-actions backend is missing required configuration: "
                f"{', '.join(missing)} must be set in the environment "
                "(see the plugin README)."
            )
        # Narrowed by the missing-check above; assertions keep the type checker happy.
        assert repo is not None and workflow is not None
        return cls(repo=repo, workflow=workflow, ref=ref, token=token)

    # -- submission: no shell command, so encode the dispatch in _build_command
    #    and perform it in _execute_command (submit-flow's actual path). -------

    def _build_command(
        self,
        task_range: str | None,
        job_name: str,
        job_env: dict[str, str],
        *,
        extra_flags: list[str] | None = None,
        array: bool = True,
    ) -> list[str]:
        return [_DISPATCH, json.dumps({"task_range": task_range, "job_name": job_name})]

    def _execute_command(
        self,
        cmd: list[str],
        job_env: dict[str, str],
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        """Perform the dispatch encoded by :meth:`_build_command`, rotating on quota.

        Tries the current account; on a quota/billing ``403`` it advances to the
        next pooled account and re-dispatches. Returns a ``CompletedProcess``
        whose stdout is the Actions run id (for ``JOB_ID_REGEX``); a non-quota
        failure, or every account exhausted, becomes a non-zero exit so
        submit-flow surfaces ``RemoteCommandFailed``.
        """
        if not cmd or cmd[0] != _DISPATCH:
            return subprocess.CompletedProcess(cmd, 3, "", f"not a gha dispatch payload: {cmd!r}")
        payload = json.loads(cmd[1])
        inputs = {
            "run_id": job_env.get("HPC_RUN_ID", ""),
            "total_tasks": str(_parse_total(payload.get("task_range"))),
            "executor": job_env.get("EXECUTOR", ""),
            "cmd_sha": job_env.get("HPC_CMD_SHA", ""),
            "campaign_id": job_env.get("HPC_CAMPAIGN_ID", ""),
        }
        # Pin the dataset version: the release tag the runners download and cache
        # by (see workflow-template/fan-out.yml). Omitted -> the workflow default.
        if self.data_tag:
            inputs["data_tag"] = self.data_tag
        last: Exception | None = None
        for _ in range(len(self._accounts)):
            api = self._accounts[self._current]
            correlation = uuid.uuid4().hex
            try:
                api.dispatch_workflow(
                    self.workflow, self.ref, {**inputs, "correlation_id": correlation}
                )
            except GitHubAPIError as exc:
                last = exc
                if self._is_quota_error(exc) and len(self._accounts) > 1:
                    self._rotate(api.repo, exc)
                    continue
                return subprocess.CompletedProcess(cmd, 2, "", str(exc))
            try:
                run_id = api.find_run(correlation=correlation)
            except GitHubAPIError as exc:
                return subprocess.CompletedProcess(cmd, 2, "", str(exc))
            return subprocess.CompletedProcess(cmd, 0, run_id, "")
        return subprocess.CompletedProcess(
            cmd,
            2,
            "",
            f"all {len(self._accounts)} GitHub accounts are quota-exhausted; last: {last}",
        )

    def _rotate(self, exhausted_repo: str, exc: Exception) -> None:
        """Advance to the next pooled account, leaving an operator breadcrumb."""
        self._current = (self._current + 1) % len(self._accounts)
        print(
            f"[github-actions] {exhausted_repo} hit a quota/billing wall ({exc}); "
            f"rotating to {self._accounts[self._current].repo}",
            file=sys.stderr,
        )

    @staticmethod
    def _is_quota_error(exc: Exception) -> bool:
        """True when *exc* is a 403 that reads like an Actions quota / billing wall."""
        if getattr(exc, "status", None) != 403:
            return False
        message = str(exc).lower()
        return any(signal in message for signal in _QUOTA_SIGNALS)

    # -- liveness / state (host polls these from status / monitor / reconcile).

    def alive_job_ids(self, job_ids: list[str]) -> list[str]:
        """Subset of *job_ids* (Actions run ids) still running, across the pool.

        A run that is absent (404 on every account) or ``completed`` is dropped;
        the host marks a vanished id ``abandoned`` on reconcile.
        """
        alive: list[str] = []
        for jid in job_ids:
            run = self._get_run_any(jid)
            if run is not None and str(run.get("status")) in _ALIVE_STATUSES:
                alive.append(jid)
        return alive

    def _get_run_any(self, run_id: str) -> dict[str, object] | None:
        """Probe every pooled account for *run_id* (run ids are account-scoped)."""
        for api in self._accounts:
            run = api.get_run(run_id)
            if run is not None:
                return run
        return None

    def _owner_of(self, run_id: str) -> GitHubActionsAPI:
        """Return the pooled account's client that owns *run_id*."""
        for api in self._accounts:
            if api.get_run(run_id) is not None:
                return api
        raise GitHubAPIError(f"run {run_id} not found on any pooled account")

    @staticmethod
    def classify_scheduler_state(state: str) -> str:
        """Bucket a ``"<status>:<conclusion>"`` token into alive / error / held.

        Used by ``verify-submitted`` as a post-dispatch health check. A freshly
        dispatched run is ``queued`` / ``in_progress`` (alive); a finished run is
        bucketed by conclusion. ``cancelled`` maps to ``held`` — the GitHub
        analogue of a job cancelled by a newer dispatch (cf. ``preempted``).
        """
        status, _, conclusion = state.partition(":")
        if status != "completed":
            return "alive"
        if conclusion in {"failure", "startup_failure", "timed_out"}:
            return "error"
        if conclusion in {"cancelled", "action_required", "stale", "neutral", "skipped"}:
            return "held"
        return "alive"  # completed + success — ran cleanly

    # -- results / logs: no shared FS, so these come back over the API. The
    #    SSH path's rsync-pull and stderr-path reads have no backend hook to
    #    override, so these are offered as the building blocks to wire into
    #    aggregate / failures (see README "What still needs bridging"). --------

    def fetch_results(self, run_id: str, dest_dir: str) -> list[str]:
        """Download a run's artifacts into *dest_dir*; return the extracted dirs.

        The shared-filesystem replacement (the proposal's "each task ships data
        in and results out"). Downloads every artifact the run uploaded — the
        per-task ``task-*`` outputs and/or the ``reduced`` aggregate — and
        unzips each under ``dest_dir/<artifact-name>/``. Probes the pool so a
        run that landed on another account is still pulled from it.
        """
        import zipfile

        api = self._owner_of(run_id)
        os.makedirs(dest_dir, exist_ok=True)
        extracted: list[str] = []
        for art in api.list_artifacts(run_id):
            name = str(art.get("name", "artifact"))
            art_id = art.get("id")
            if not isinstance(art_id, int):
                continue
            zip_path = os.path.join(dest_dir, f"{name}.zip")
            api.download_artifact(art_id, zip_path)
            out_dir = os.path.join(dest_dir, name)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(out_dir)
            os.remove(zip_path)
            extracted.append(out_dir)
        return extracted

    def fetch_logs(self, run_id: str, dest_dir: str | None = None) -> str:
        """Download a run's job-logs zip; return its path.

        The instance-method replacement for the ``stderr_log_path`` staticmethod:
        Actions logs need the authenticated client, which a ``@staticmethod``
        can't hold. Probes the pool for the owning account; defaults to writing
        under ``self.log_dir``.
        """
        api = self._owner_of(run_id)
        dest_root = dest_dir or self.log_dir
        os.makedirs(dest_root, exist_ok=True)
        dest = os.path.join(dest_root, f"{run_id}-logs.zip")
        api.download_run_logs(run_id, dest)
        return dest
