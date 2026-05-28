"""``check-preflight`` primitive — sanity-check the local environment.

Pure-dispatch primitive: probes ``SSH_AUTH_SOCK``, the ``ssh`` binary
and a file-transfer transport on PATH, the parseability of
``clusters.yaml``, and (optionally) TCP reachability of a named
cluster's port 22. No SSH session is opened — the cluster check is a
bare TCP probe.

File transfer is satisfied by ``rsync`` *or* the ``scp``+``tar`` pair
(``infra.remote`` falls back to a ``tar c | ssh tar x`` push / ``scp
-r`` pull pipeline when rsync is absent — typically Windows hosts
without WSL/MSYS rsync), so a missing ``rsync`` alone does not fail
preflight.

Also exposes :func:`write_preflight_marker`, the one-line helper that
writes the per-cluster 24h cache marker consumed by ``/submit-hpc``'s
Step 6b gate. Called by ``hpc-agent setup --cluster <name>`` after a
green probe; the gate skips its re-check while the marker is fresh.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape
from hpc_agent.infra.clusters import load_clusters_config
from hpc_agent.infra.ssh_agent import agent_available, agent_detail


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def _placeholder_fields(entry: dict[str, Any]) -> list[str]:
    """Keys in a cluster entry whose value still holds a ``<your_...>`` token.

    The packaged ``clusters.yaml`` ships placeholders (``<your_user>``,
    ``<your_scratch>``, ``<your_env>``, ``<your_account>``) the user must
    replace. Left in, they fail at submit time with confusing cluster-side
    errors (auth to ``<your_user>@host``, a scratch dir that doesn't exist,
    ``conda activate <your_env>``). Catch them at preflight instead.
    """
    bad: list[str] = []
    for key, val in entry.items():
        candidates = val if isinstance(val, list) else [val]
        if any(isinstance(v, str) and "<your_" in v for v in candidates):
            bad.append(key)
    return sorted(bad)


@primitive(
    name="check-preflight",
    verb="validate",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help="Health check: SSH agent, ssh/rsync on PATH, clusters.yaml parses.",
        verb="preflight",
        args=(
            CliArg(
                "--cluster",
                type=str,
                default=None,
                help="Optional cluster name to TCP-probe on :22.",
            ),
        ),
    ),
    agent_facing=True,
)
def check_preflight(*, cluster: str | None = None) -> dict[str, Any]:
    """Run all preflight checks; return a dict with ``all_ok`` and ``checks``.

    *cluster*: optional cluster name; when supplied, adds a
    ``cluster_known`` check (membership in clusters.yaml) and a
    ``cluster_tcp_22`` check (TCP probe on the cluster's host:22 with
    a 3s timeout). When omitted, those checks are skipped.

    Returns ``{"all_ok": bool, "checks": list[dict]}``. The CLI adapter
    maps ``all_ok=False`` to the cluster-error exit code.
    """
    checks: list[dict[str, Any]] = []

    # SSH agent — name kept as ``ssh_auth_sock`` for backwards-compat with
    # downstream consumers (tests, JSON output schema). On Windows the
    # named-pipe agent doesn't set the env var; ``agent_available`` probes
    # the pipe directly there.
    sock = os.environ.get("SSH_AUTH_SOCK")
    if not agent_available():
        checks.append(
            _check(
                "ssh_auth_sock",
                False,
                "SSH_AUTH_SOCK is not set — start the agent and load a key: "
                "`eval $(ssh-agent -s); ssh-add ~/.ssh/<your-key>`, then re-run from "
                "the same shell. In tmux/screen/mosh, export SSH_AUTH_SOCK and "
                "SSH_AGENT_PID into that session. "
                "On Windows: `Start-Service ssh-agent; ssh-add ~/.ssh/<your-key>`.",
            )
        )
    elif sock:
        # Unix path: env-var is the signal — verify a key is actually loaded.
        try:
            agent = subprocess.run(
                ["ssh-add", "-l"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
            )
            has_keys = agent.returncode == 0 and bool(agent.stdout.strip())
            if has_keys:
                detail = f"agent at {sock}"
            else:
                detail = "ssh-agent has no keys loaded — run `ssh-add ~/.ssh/<your-key>` to add one"
            checks.append(_check("ssh_auth_sock", has_keys, detail))
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            checks.append(
                _check(
                    "ssh_auth_sock",
                    False,
                    f"ssh-add failed: {exc} — install openssh-client "
                    "(`apt install openssh-client` / `brew install openssh`)",
                )
            )
    else:
        # Windows named-pipe path: ``agent_available`` already verified
        # the pipe is reachable (rc 0 or 1). Emit OK with the detail
        # string; the rc=1 ("no keys loaded") state still passes here
        # because the pipe is reachable, and the detail surfaces it.
        checks.append(_check("ssh_auth_sock", True, agent_detail()))

    # ssh is mandatory for every remote operation.
    ssh_path = shutil.which("ssh")
    ssh_detail = (
        ssh_path
        if ssh_path
        else (
            "not found — install openssh-client "
            "(`apt install openssh-client` / `brew install openssh`)"
        )
    )
    checks.append(_check("ssh_on_path", ssh_path is not None, ssh_detail))

    # File transfer: rsync is preferred, but infra.remote falls back to a
    # ``tar c | ssh tar x`` push + ``scp -r`` pull pipeline when rsync is
    # absent (typically Windows without WSL/MSYS rsync). The capability is
    # satisfied by rsync OR the scp+tar pair — don't fail preflight just
    # because rsync is missing when the fallback transport is available.
    rsync_path = shutil.which("rsync")
    scp_path = shutil.which("scp")
    tar_path = shutil.which("tar")
    fallback_ok = scp_path is not None and tar_path is not None
    transfer_ok = rsync_path is not None or fallback_ok
    if rsync_path is not None:
        transfer_detail = f"rsync at {rsync_path}"
    elif fallback_ok:
        transfer_detail = f"rsync not found; scp/tar fallback available ({scp_path}, {tar_path})"
    else:
        missing = [
            name
            for name, found in (("rsync", rsync_path), ("scp", scp_path), ("tar", tar_path))
            if found is None
        ]
        transfer_detail = (
            f"no file-transfer transport — install rsync (preferred), "
            f"or ensure scp+tar are both on PATH (missing: {', '.join(missing)})"
        )
    checks.append(_check("file_transfer_on_path", transfer_ok, transfer_detail))

    # Clusters config parseable
    try:
        clusters = load_clusters_config()
        checks.append(_check("clusters_yaml_parses", True, f"{len(clusters)} clusters defined"))
    except (OSError, Exception) as exc:  # noqa: BLE001
        clusters = {}
        checks.append(_check("clusters_yaml_parses", False, str(exc)))

    # If a cluster name was passed, attempt a TCP probe on port 22.
    if cluster:
        if cluster not in clusters:
            checks.append(
                _check(
                    "cluster_known",
                    False,
                    f"{cluster!r} not in clusters.yaml — run `hpc-agent clusters list` "
                    "and pick from the available names",
                )
            )
        else:
            host = clusters[cluster].get("host")
            if not host:
                # ``socket.create_connection((None, 22))`` does not raise —
                # Python treats a None host as loopback, so a misconfigured
                # cluster would falsely probe (and possibly pass) localhost.
                checks.append(
                    _check("cluster_tcp_22", False, f"{cluster!r} has no 'host' in clusters.yaml")
                )
            else:
                try:
                    with socket.create_connection((host, 22), timeout=3):
                        checks.append(_check("cluster_tcp_22", True, f"{host}:22 open"))
                except OSError as exc:
                    checks.append(
                        _check(
                            "cluster_tcp_22",
                            False,
                            f"{host}:22 — {exc} — cluster may be offline or behind a VPN; "
                            "verify connectivity from your network",
                        )
                    )

            # Reject un-customized placeholders: a clusters.yaml entry still
            # carrying <your_user> / <your_scratch> / <your_env> would pass
            # the TCP probe but fail every task at submit time.
            placeholders = _placeholder_fields(clusters[cluster])
            if placeholders:
                checks.append(
                    _check(
                        "cluster_config_customized",
                        False,
                        f"{cluster!r} still has placeholder value(s) in {placeholders} — "
                        "replace the <your_...> tokens (username / scratch path / conda "
                        "env / account) with your real values in clusters.yaml (or point "
                        "HPC_CLUSTERS_CONFIG at a customized copy) before submitting.",
                    )
                )
            else:
                checks.append(
                    _check("cluster_config_customized", True, "no placeholder values remain")
                )

    all_ok = all(c["ok"] for c in checks)
    return {"all_ok": all_ok, "checks": checks}


def write_preflight_marker(*, cluster: str, experiment_dir: Path | None = None) -> Path:
    """Write the per-cluster preflight cache marker; return its path.

    Populates the 24h cache that ``/submit-hpc``'s Step 6b gate reads
    so the first submit in an experiment doesn't re-run the SSH probe.
    Called by ``hpc-agent setup --cluster <name>`` after a green
    :func:`check_preflight` on the same cluster.

    The marker is scoped to *experiment_dir* (default: ``Path.cwd()``)
    because the gate reads from ``JournalLayout(experiment_dir)`` —
    the marker must land in the same per-repo journal the gate
    consults. Setup is therefore typically run from inside the
    experiment directory.
    """
    from hpc_agent._kernel.contract.layout import JournalLayout
    from hpc_agent.infra.io import atomic_write_json

    layout = JournalLayout(experiment_dir or Path.cwd())
    marker = layout.preflight_marker(cluster)
    marker.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        marker,
        {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "all_ok": True,
            "cluster": cluster,
        },
    )
    return marker
