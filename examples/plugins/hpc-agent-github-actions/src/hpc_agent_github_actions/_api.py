"""Stdlib-only GitHub Actions REST client.

Kept dependency-free (``urllib``, not ``requests``) so installing the plugin
pulls in nothing beyond hpc-agent. Covers exactly the calls the backend needs:
dispatch a workflow, find the resulting run, read a run's state, and download a
run's artifacts / logs.

The network paths are NOT exercised in CI — the build sandbox has no
``GITHUB_TOKEN`` and blocks outbound network — so, matching the host's #269
live-validation discipline, they ship unvalidated until a human runs the smoke
in ``README.md`` against a real repo. The pure logic lives in ``backend.py`` and
is unit-testable without a network.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

_BASE = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"


class GitHubAPIError(RuntimeError):
    """Any non-success GitHub REST response or transport failure."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class _StopRedirect(urllib.request.HTTPRedirectHandler):
    """Turn a 3xx into an ``HTTPError`` instead of following it.

    The artifact-zip and run-logs endpoints 302-redirect to a short-lived signed
    blob URL that must be fetched WITHOUT the ``Authorization`` header; following
    the redirect with urllib's default handler re-sends it and the blob store
    rejects the request. We capture the ``Location`` and fetch it cleanly.
    """

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class GitHubActionsAPI:
    """Minimal GitHub Actions REST client over urllib."""

    def __init__(self, repo: str, token: str, *, timeout: float = 30.0) -> None:
        self.repo = repo
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": _ACCEPT,
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": "hpc-agent-github-actions",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, object] | None = None,
    ) -> object:
        url = f"{_BASE}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers=self._headers())
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise GitHubAPIError(
                f"{method} {path} -> HTTP {exc.code}: {detail}", status=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise GitHubAPIError(f"{method} {path} -> {exc.reason}") from exc
        if not raw:
            return None
        return json.loads(raw)

    def dispatch_workflow(self, workflow: str, ref: str, inputs: dict[str, str]) -> None:
        """POST a ``workflow_dispatch`` (GitHub answers 204 No Content)."""
        self._request(
            "POST",
            f"/repos/{self.repo}/actions/workflows/{workflow}/dispatches",
            body={"ref": ref, "inputs": inputs},
        )

    def find_run(self, *, correlation: str, attempts: int = 20, delay: float = 3.0) -> str:
        """Resolve the run id of a just-dispatched run via its correlation tag.

        ``workflow_dispatch`` returns no run id, so the workflow embeds
        *correlation* in its ``run-name``; we poll the recent runs list until it
        appears and return ``str(run_id)`` for the backend's ``JOB_ID_REGEX``.
        Matching on a unique tag (not a timestamp) sidesteps local/GitHub clock
        skew.
        """
        for _ in range(attempts):
            data = self._request(
                "GET",
                f"/repos/{self.repo}/actions/runs",
                params={"event": "workflow_dispatch", "per_page": "50"},
            )
            runs = data.get("workflow_runs", []) if isinstance(data, dict) else []
            for run in runs:
                if correlation in (run.get("name") or ""):
                    return str(run["id"])
            time.sleep(delay)
        raise GitHubAPIError(f"no workflow_dispatch run surfaced for correlation {correlation!r}")

    def get_run(self, run_id: str) -> dict[str, object] | None:
        """Return the run object, or ``None`` if GitHub no longer knows it (404)."""
        try:
            data = self._request("GET", f"/repos/{self.repo}/actions/runs/{run_id}")
        except GitHubAPIError as exc:
            if exc.status == 404:
                return None
            raise
        return data if isinstance(data, dict) else None

    def list_artifacts(self, run_id: str) -> list[dict[str, object]]:
        """Every artifact a run uploaded (``task-*`` per-task dirs, ``reduced``, …)."""
        data = self._request(
            "GET",
            f"/repos/{self.repo}/actions/runs/{run_id}/artifacts",
            params={"per_page": "100"},
        )
        if not isinstance(data, dict):
            return []
        artifacts = data.get("artifacts", [])
        return artifacts if isinstance(artifacts, list) else []

    def download_artifact(self, artifact_id: int, dest_zip: str) -> None:
        """Download an artifact's zip to *dest_zip* (handles the signed redirect)."""
        self._download_via_redirect(
            f"{_BASE}/repos/{self.repo}/actions/artifacts/{artifact_id}/zip", dest_zip
        )

    def download_run_logs(self, run_id: str, dest_zip: str) -> None:
        """Download a run's job-logs zip to *dest_zip* (same redirect shape)."""
        self._download_via_redirect(
            f"{_BASE}/repos/{self.repo}/actions/runs/{run_id}/logs", dest_zip
        )

    def _download_via_redirect(self, url: str, dest: str) -> None:
        req = urllib.request.Request(url, method="GET", headers=self._headers())
        opener = urllib.request.build_opener(_StopRedirect)
        try:
            opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                raise GitHubAPIError(f"{url} -> HTTP {exc.code}", status=exc.code) from exc
            location = exc.headers["Location"]
        else:  # pragma: no cover - GitHub always redirects these endpoints
            raise GitHubAPIError(f"{url}: expected a redirect to a signed URL")
        # The signed blob URL is pre-authenticated; sending our bearer token to
        # it is what the storage backend rejects, so fetch it bare.
        with urllib.request.urlopen(location, timeout=self.timeout) as resp:
            payload = resp.read()
        with open(dest, "wb") as handle:
            handle.write(payload)
