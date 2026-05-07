"""``check-preflight`` primitive — sanity-check the local environment.

Pure-dispatch primitive: probes ``SSH_AUTH_SOCK``, the ``ssh`` and
``rsync`` binaries on PATH, the parseability of ``clusters.yaml``,
and (optionally) TCP reachability of a named cluster's port 22. No
SSH session is opened — the cluster check is a bare TCP probe.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from typing import Any

from claude_hpc._internal._primitive import primitive
from claude_hpc.infra.clusters import load_clusters_config


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


@primitive(
    name="check-preflight",
    verb="validate",
    side_effects=[],
    idempotent=True,
    cli="hpc-mapreduce preflight [--cluster <name>]",
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
            agent = subprocess.run(["ssh-add", "-l"], capture_output=True, text=True, timeout=5)
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

    # Binaries on PATH
    for binary in ("ssh", "rsync"):
        path = shutil.which(binary)
        checks.append(_check(f"{binary}_on_path", path is not None, path or "not found"))

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
            try:
                with socket.create_connection((host, 22), timeout=3):
                    checks.append(_check("cluster_tcp_22", True, f"{host}:22 open"))
            except OSError as exc:
                checks.append(_check("cluster_tcp_22", False, f"{host}:22 — {exc}"))

    all_ok = all(c["ok"] for c in checks)
    return {"all_ok": all_ok, "checks": checks}
