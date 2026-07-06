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

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape, SchemaRef
from hpc_agent.infra.backends import backend_requires_ssh
from hpc_agent.infra.clusters import load_clusters_config
from hpc_agent.infra.runtime_preflight import runtime_uv_preflight
from hpc_agent.infra.ssh_agent import agent_available, agent_detail
from hpc_agent.infra.ssh_options import _scp_binary, _ssh_add_binary, _ssh_binary


def _check(name: str, ok: bool, detail: str = "") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail}


def _cluster_ssh_timeout() -> int:
    """Per-probe cluster ssh round-trip timeout in seconds (#295 Fix 1).

    Env-overridable via ``HPC_CLUSTER_SSH_TIMEOUT``; the default is DERIVED
    from :data:`~hpc_agent.infra.remote.SSH_TIMEOUT_SEC` (60s), never a
    tighter restated constant — a probe stricter than the submit/staging ssh
    budget it gates is a false-positive machine, and every false trip feeds
    the per-host circuit breaker (run #8 live, 2026-07-06: a loaded-but-
    healthy hoffman2 failed two 15s ``echo ok`` probes, walking the breaker
    to 2/3 while a 60s-bounded real op would have passed — the
    ``_PREFLIGHT_PROBE_TIMEOUT_SEC`` lesson's uncovered sibling). History:
    the original 5s tripped on a healthy cluster 2026-06-06; the 15s bump
    repeated the same mistake one size up. A non-integer override falls back
    to the derived default rather than erroring out.
    """
    from hpc_agent.infra.remote import SSH_TIMEOUT_SEC

    try:
        return int(os.environ.get("HPC_CLUSTER_SSH_TIMEOUT", str(int(SSH_TIMEOUT_SEC))))
    except ValueError:
        return int(SSH_TIMEOUT_SEC)


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


def _runtime_uv_check(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """The #275 runtime-``uv`` probe as a self-contained check (or None when N/A).

    Returns the ``runtime_uv`` check dict when *spec* is a built submit-flow
    spec asking for ``HPC_RUNTIME=uv`` with an ``ssh_target`` — the same
    ``command -v uv`` probe ``submit-flow`` runs — else None (no probe, no ssh
    round-trip). One ssh round-trip, fannable with the cluster_ssh_echo probe
    (#289). Raises are surfaced as a failed check, never an exception, so the
    envelope stays uniform.
    """
    if not isinstance(spec, dict):
        return None
    job_env = spec.get("job_env") or {}
    spec_ssh_target = spec.get("ssh_target")
    if not (isinstance(job_env, dict) and job_env.get("HPC_RUNTIME") == "uv" and spec_ssh_target):
        return None
    try:
        runtime_uv_preflight(str(spec_ssh_target), job_env=dict(job_env), skip=False)
        return _check(
            "runtime_uv",
            True,
            f"uv present on PATH after cluster env activation on {spec_ssh_target}",
        )
    except errors.SpecInvalid as exc:
        return _check("runtime_uv", False, str(exc))
    except (TimeoutError, OSError) as exc:
        return _check(
            "runtime_uv",
            False,
            f"runtime uv probe to {spec_ssh_target} could not complete: {exc}",
        )


def _cluster_ssh_echo_check(host: str) -> dict[str, Any]:
    """The functional ``ssh <host> echo ok`` round-trip as a self-contained check.

    Runs through the same production ssh path a real submit takes (the fix for
    the 2026-06-04 bare-TCP-passed-but-rsync-failed demo). One ssh round-trip,
    fannable with the runtime_uv probe (#289); the caller gates this on a
    passing tcp:22 probe.
    """
    from hpc_agent.infra.remote import ssh_run

    try:
        result = ssh_run("echo ok", ssh_target=host, timeout=_cluster_ssh_timeout())
    except (TimeoutError, OSError) as exc:
        return _check("cluster_ssh_echo", False, f"{host} ssh round-trip raised: {exc}")
    if result.returncode == 0 and (result.stdout or "").strip() == "ok":
        return _check("cluster_ssh_echo", True, f"{host} ssh round-trip succeeded")
    stderr_tail = (result.stderr or "")[-200:]
    return _check(
        "cluster_ssh_echo",
        False,
        f"{host} ssh round-trip failed (exit {result.returncode}): {stderr_tail!r} — "
        "production submits will hit the same failure",
    )


def _cluster_combined_probe(ssh_target: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Echo round-trip + runtime-uv probe over ONE ssh connection (#295 Fix 2).

    #289 fanned these two independent cluster probes onto a ``ThreadPoolExecutor``
    so their wall-clock was one RTT, not two — but with named-pipe ControlMaster
    multiplexing broken (the empirical Windows case) each concurrent connection
    still pays a full cold TCP+SSH handshake. Collapsing both into a single
    multi-command round-trip eliminates one handshake per submit preflight, a win
    that lands whether or not multiplexing works.

    Targets the spec's ``ssh_target`` (``user@host``) for BOTH legs — the exact
    endpoint the real submit (rsync/qsub) uses. The standalone echo probe targets
    the bare clusters.yaml ``host``; routing the merged echo through ``ssh_target``
    is strictly more production-faithful (it exercises the same explicit user the
    canary will, catching a wrong-default-user mismatch the bare-host probe would
    miss).

    Robust to activation noise: the command emits unique sentinel tokens and the
    parser checks for their *presence*, so ``module load`` / ``conda activate``
    chatter interleaved in stdout can't corrupt the verdict. Activation stderr is
    not suppressed, so a uv-missing failure still surfaces the cluster's diagnostic
    tail. Returns ``[echo_check, uv_check]``.
    """
    from hpc_agent.infra.remote import ssh_run
    from hpc_agent.infra.runtime_preflight import uv_activation_prefix, uv_missing_message

    job_env = dict(spec.get("job_env") or {})
    prefix = uv_activation_prefix(job_env)
    uv_cond = f"{prefix} && command -v uv" if prefix else "command -v uv"
    # Echo first (proves the round-trip), then the activation-aware uv check
    # reduced to a single OK/MISSING token. ``>/dev/null`` drops uv's path (we
    # only need presence); activation stdout/stderr still flow for diagnostics.
    cmd = (
        "echo __HPC_ECHO_OK__; "
        f"if {uv_cond} >/dev/null 2>&1; then echo __HPC_UV_OK__; "
        "else echo __HPC_UV_MISSING__; fi"
    )
    try:
        result = ssh_run(cmd, ssh_target=ssh_target, timeout=_cluster_ssh_timeout())
    except (TimeoutError, OSError) as exc:
        detail = f"{ssh_target} ssh round-trip raised: {exc}"
        return [
            _check("cluster_ssh_echo", False, detail),
            _check("runtime_uv", False, f"runtime uv probe could not complete: {detail}"),
        ]

    out = result.stdout or ""
    # No echo token means the shell never ran cleanly — the round-trip itself
    # failed (auth/host-key/etc.). Report both legs against that shared cause
    # rather than mislabeling it as "uv missing".
    if "__HPC_ECHO_OK__" not in out:
        stderr_tail = (result.stderr or "")[-200:]
        detail = (
            f"{ssh_target} ssh round-trip failed (exit {result.returncode}): "
            f"{stderr_tail!r} — production submits will hit the same failure"
        )
        return [
            _check("cluster_ssh_echo", False, detail),
            _check("runtime_uv", False, f"runtime uv probe could not complete: {detail}"),
        ]

    echo_check = _check("cluster_ssh_echo", True, f"{ssh_target} ssh round-trip succeeded")
    if "__HPC_UV_OK__" in out:
        uv_check = _check(
            "runtime_uv", True, f"uv present on PATH after cluster env activation on {ssh_target}"
        )
    else:
        uv_check = _check(
            "runtime_uv",
            False,
            uv_missing_message(
                ssh_target,
                conda_env=str(job_env.get("CONDA_ENV") or "").strip(),
                cmd=uv_cond,
                returncode=result.returncode,
                stderr=result.stderr or "",
            ),
        )
    return [echo_check, uv_check]


@primitive(
    name="check-preflight",
    verb="validate",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Health check: SSH agent, ssh/rsync on PATH, clusters.yaml parses; "
            "with --cluster also runs a TCP :22 probe + an actual ssh round-trip; "
            "with --spec <built submit-flow spec> also runs the runtime (uv) "
            "probe submit-flow would, so a uv-on-a-uv-less-cluster spec is caught "
            "here, before qsub (#275)."
        ),
        verb="preflight",
        # Optional submit-flow spec (#275): when supplied, check-preflight runs
        # the same ``command -v uv`` runtime probe submit-flow runs, reusing the
        # built spec's ssh_target + job_env. ``spec_required=False`` keeps the
        # bare ``--cluster`` (and no-arg) invocations working unchanged.
        spec_arg=True,
        spec_required=False,
        schema_ref=SchemaRef(input="submit_flow"),
        args=(
            CliArg(
                "--cluster",
                type=str,
                default=None,
                help=(
                    "Optional cluster name to probe: TCP :22 + a real ``ssh "
                    "<host> echo ok`` round-trip through the production "
                    "ssh_argv / multiplex / crypto path."
                ),
            ),
        ),
        # Conditional: only fires when ``--cluster`` is supplied. The
        # contract is conservative — declare requires_ssh so the
        # ssh-touching-primitives contract test (#WS4) does not flag the
        # ssh_run call added for the cluster_ssh_echo functional probe.
        requires_ssh=True,
    ),
    agent_facing=True,
)
def check_preflight(
    *, cluster: str | None = None, spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Run all preflight checks; return a dict with ``all_ok`` and ``checks``.

    *spec*: optional built submit-flow spec (#275). When supplied AND its
    ``job_env`` declares ``HPC_RUNTIME=uv``, adds a ``runtime_uv`` check that
    runs the SAME ``command -v uv`` probe submit-flow runs (via the shared
    :func:`hpc_agent.ops.submit_flow._preflight_runtime_check`), using the
    spec's ``ssh_target`` + activation ``job_env``. This closes the #275 gap:
    the SKILL.md flow ran check-preflight (no spec) then submit-flow, whose uv
    guard was skippable, so a ``runtime=uv`` spec on a uv-less cluster sailed
    past preflight and doomed every task with ``HPC_RUNTIME=uv but 'uv' not on
    PATH``. No spec (or a non-uv runtime) leaves the check absent — no extra
    ssh round-trip.

    *cluster*: optional cluster name; when supplied, adds the
    ``cluster_known`` check (membership in clusters.yaml), a
    ``cluster_tcp_22`` check (TCP probe on the cluster's host:22 with a
    3s timeout), and a ``cluster_ssh_echo`` check (a real ``ssh <host>
    echo ok`` round-trip through the same production machinery — added
    after the 2026-06-04 bare-TCP-probe-passed-but-rsync-failed demo
    surfaced the inert-guard mismatch). When omitted, all three are
    skipped. The SSH round-trip is skipped when the TCP probe fails (no
    point burning the (HPC_CLUSTER_SSH_TIMEOUT, default 15s) ssh timeout on an
    unreachable host).

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
                [_ssh_add_binary(), "-l"],
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

    # ssh is mandatory for every remote operation. Probe the *same* binary
    # production invokes (``_ssh_binary()``), not a bare ``"ssh"``: on Windows
    # production prefers native ``C:\\Windows\\System32\\OpenSSH\\ssh.exe`` (the
    # binary that reaches the ssh-agent named-pipe), so a bare-``ssh`` probe
    # would report green for Git Bash's agent-blind ``ssh`` that production
    # never runs. ``HPC_SSH_BINARY`` pins it on any platform.
    ssh_binary = _ssh_binary()
    ssh_path = shutil.which(ssh_binary)
    ssh_detail = (
        ssh_path
        if ssh_path
        else (
            f"{ssh_binary} not found — install openssh-client "
            "(`apt install openssh-client` / `brew install openssh`)"
        )
    )
    checks.append(_check("ssh_on_path", ssh_path is not None, ssh_detail))

    # File transfer: rsync is preferred, but infra.remote falls back to a
    # ``tar c | ssh tar x`` push + ``scp -r`` pull pipeline when rsync is
    # absent (typically Windows without WSL/MSYS rsync). The capability is
    # satisfied by rsync OR the scp+tar pair — don't fail preflight just
    # because rsync is missing when the fallback transport is available.
    # Probe the same ``scp`` binary the fallback pipeline invokes
    # (``_scp_binary()`` — native OpenSSH ``scp.exe`` on Windows), not a bare
    # ``"scp"`` that could resolve to Git Bash's. ``HPC_SCP_BINARY`` pins it.
    rsync_path = shutil.which("rsync")
    scp_binary = _scp_binary()
    scp_path = shutil.which(scp_binary)
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

    # Runtime-binary probe (#275) — DEFERRED so it can ride the same ssh
    # round-trip as cluster_ssh_echo. When a built submit-flow spec asks for
    # ``runtime=uv``, the probe verifies ``uv`` is on PATH after the cluster env
    # activates (the SAME probe submit-flow runs) — closing the gap where a
    # uv-less cluster's array died with ``HPC_RUNTIME=uv but 'uv' not on PATH``.
    # #289 fanned it concurrently with cluster_ssh_echo (one RTT wall-clock);
    # #295 Fix 2 goes further and collapses BOTH into ONE ssh connection
    # (``_cluster_combined_probe``) when both fire — the documented
    # ``check-preflight --cluster X --spec <uv-spec>`` submit path (submit.md
    # Step 7) — so a broken ControlMaster pays one handshake, not two.
    # ``uv_pending`` marks the probe owed; it runs inside the cluster block
    # (merged) or standalone afterwards (no --cluster, or tcp:22 unreachable).
    uv_pending = (
        isinstance(spec, dict)
        and isinstance(spec.get("job_env"), dict)
        and (spec.get("job_env") or {}).get("HPC_RUNTIME") == "uv"
        and bool(spec.get("ssh_target"))
    )

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
        # A pure-API backend (``requires_ssh=False``) has no login node, so the
        # whole transport block — TCP :22, the ssh ``echo ok`` round-trip, and
        # the merged uv probe — is meaningless and must issue ZERO ssh (#337
        # Class B). Gate on the cluster's ``scheduler`` capability via
        # ``backend_requires_ssh`` (core dispatches on the capability, never
        # branches on the name). Emit structured skipped checks in place of the
        # probes so the envelope shape is preserved. An unregistered/unknown
        # scheduler conservatively reports ``True`` (the safe SSH default).
        elif not backend_requires_ssh(str(clusters[cluster].get("scheduler") or "")):
            checks.append(
                _check("cluster_tcp_22", True, "skipped: pure-API backend (no login node)")
            )
            checks.append(
                _check("cluster_ssh_echo", True, "skipped: pure-API backend (no login node)")
            )
            if uv_pending:
                # The #275 uv probe also rides ssh; skip it for the same reason
                # rather than letting it run standalone below.
                checks.append(
                    _check("runtime_uv", True, "skipped: pure-API backend (no login node)")
                )
                uv_pending = False
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
                    tcp_ok = True
                except OSError as exc:
                    checks.append(
                        _check(
                            "cluster_tcp_22",
                            False,
                            f"{host}:22 — {exc} — cluster may be offline or behind a VPN; "
                            "verify connectivity from your network",
                        )
                    )
                    tcp_ok = False

                # Functional SSH probe: the TCP check above is necessary
                # but not sufficient. Port 22 open says nothing about
                # whether the production SSH path actually works — the
                # 2026-06-04 demo failed mid-submit with rsync hitting
                # ``getsockname failed: Not a socket`` even though
                # preflight had passed (the bare TCP probe never exercised
                # the named-pipe ControlMaster bind, the Git-Bash vs
                # native-OpenSSH binary resolution, or the ssh-agent
                # reachability path). Run a real ``ssh <host> echo ok``
                # round-trip through the same ``ssh_argv("ssh")`` /
                # multiplex / crypto / runtime-fallback machinery
                # production uses, so a green here means the production
                # path is actually green. Skipped when the TCP probe
                # failed (no point burning 5s on an unreachable host).
                if tcp_ok:
                    # ``uv_pending`` already implies ``spec`` is a dict with an
                    # ssh_target (see its definition above); the ``isinstance``
                    # re-states that invariant so the type checker narrows ``spec``
                    # from ``dict | None`` here.
                    if uv_pending and isinstance(spec, dict):
                        # #295 Fix 2: collapse the two independent cluster probes
                        # (echo round-trip + the #275 uv probe) into ONE ssh
                        # round-trip. #289 fanned them concurrently (one RTT
                        # wall-clock), but each still paid its own handshake — so
                        # with named-pipe ControlMaster broken (Windows) this saves
                        # a full cold handshake per submit preflight. Routed through
                        # the spec's ssh_target (the production submit endpoint).
                        checks.extend(_cluster_combined_probe(str(spec["ssh_target"]), spec))
                        uv_pending = False
                    else:
                        checks.append(_cluster_ssh_echo_check(host))

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

    # The runtime_uv probe runs standalone when it could NOT be fanned with a
    # cluster_ssh_echo round-trip — no --cluster, or tcp:22 unreachable (#289).
    if uv_pending:
        standalone_uv = _runtime_uv_check(spec)
        if standalone_uv is not None:
            checks.append(standalone_uv)

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
