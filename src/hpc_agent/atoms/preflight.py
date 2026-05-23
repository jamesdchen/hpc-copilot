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

from hpc_agent._internal.primitive import primitive
from hpc_agent.infra.clusters import load_clusters_config


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


@primitive(
    name="check-preflight",
    verb="validate",
    side_effects=[],
    idempotent=True,
    cli="hpc-agent preflight [--cluster <name>]",
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

    # SSH agent
    sock = os.environ.get("SSH_AUTH_SOCK")
    if not sock:
        checks.append(_check("ssh_auth_sock", False, "SSH_AUTH_SOCK is not set"))
    else:
        try:
            agent = subprocess.run(
                ["ssh-add", "-l"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
            )
            has_keys = agent.returncode == 0 and bool(agent.stdout.strip())
            checks.append(
                _check(
                    "ssh_auth_sock",
                    has_keys,
                    "ssh-agent has no keys" if not has_keys else f"agent at {sock}",
                )
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            checks.append(_check("ssh_auth_sock", False, f"ssh-add failed: {exc}"))

    # ssh is mandatory for every remote operation.
    ssh_path = shutil.which("ssh")
    checks.append(_check("ssh_on_path", ssh_path is not None, ssh_path or "not found"))

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
            f"no file-transfer transport — need rsync, or scp+tar (missing: {', '.join(missing)})"
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
            checks.append(_check("cluster_known", False, f"{cluster!r} not in clusters.yaml"))
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
                    checks.append(_check("cluster_tcp_22", False, f"{host}:22 — {exc}"))

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
    from hpc_agent._internal.io import atomic_write_json
    from hpc_agent._internal.layout import JournalLayout

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
