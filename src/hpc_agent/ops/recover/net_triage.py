"""``net-triage`` — the mechanized connectivity differential (WHY is the cluster dark?).

The 2026-07-05 proving-run incident this verb exists for: hoffman2's SSH
circuit breaker was OPEN and discovery was dark. The driving agent — holding
status-snapshot and doctor output — improvised raw ssh probes, saw two
timeouts, and concluded "network-level problem (VPN not connected)", pausing
for the human. Ground truth was derivable without guessing: the breaker's
durable state file recorded the open circuit and its cooldown deadline, and
ONE bounded control probe would have shown the local network was fine. The
agent guessed because no tool answered "WHY can't I reach the cluster?" —
a differential diagnosis fixed by rules, which per the determinism boundary
(docs/internals/engineering-principles.md) belongs in a verb, not in the LLM.

For each configured cluster host (plus an optional caller host) it gathers,
deterministically and bounded:

(a) the circuit-breaker state, READ from ``_ssh_circuit/<host>.json`` — never
    written: triage must not claim or burn the breaker's single half-open
    probe slot (``ssh_circuit.check_circuit`` claims it under the file lock;
    this module only ever parses the state file);
(b) one control-plane HTTPS probe (shared across hosts) + bounded DNS
    resolution of the host;
(c) ONE bounded direct TCP connect to host:22 — SKIPPED whenever the breaker
    is open (probing an open circuit is exactly the connection the intrusion
    filter would count, and success/failure here must not race the breaker's
    own probe) and when DNS already failed;
(d) a verdict from a fixed precedence table (see :func:`_verdict`), each arm
    carrying deterministic remediation text.

Also home to :func:`open_circuit_lines` — the per-host "ssh circuit OPEN"
one-liners that ``doctor`` and ``status-snapshot`` surface so an agent
holding either output can see the breaker without knowing this verb exists.

Fail-open posture throughout: a missing/corrupt breaker state file reads as
"missing" (healthy), an unloadable clusters.yaml yields no configured hosts —
triage degrades, never raises, on broken local state.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent._wire.queries.net_triage import (
    BreakerState,
    ControlPlaneCheck,
    HostTriage,
    NetTriageResult,
    NetTriageSpec,
    TriageVerdict,
)
from hpc_agent.cli._dispatch import CliShape, SchemaRef
from hpc_agent.infra.ssh_circuit import BASE_COOLDOWN_SEC, OVERRIDE_ENV, circuit_state_path
from hpc_agent.infra.time import utcnow_iso

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["net_triage", "open_circuit_lines", "read_breaker_state"]

#: Stable public endpoint for the control-plane reachability probe. Google's
#: generate_204 exists precisely for connectivity checks (no body, no cache);
#: ANY HTTPS response — any status — proves this machine's egress works.
CONTROL_URL = "https://www.google.com/generate_204"

#: The port the differential probes — SSH, the only port the framework needs.
SSH_PORT = 22


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _float_or(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ── breaker state (read-only) ────────────────────────────────────────────────


def _read_doc(path: Path) -> dict[str, Any] | None:
    """Parse one breaker state file; ``None`` on absent/unreadable/malformed."""
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _breaker_from_doc(doc: dict[str, Any] | None) -> BreakerState:
    """Project a breaker state doc into the wire model (fail-open on gaps)."""
    if doc is None:
        return BreakerState(state="missing")
    state: Literal["closed", "open"] = "open" if doc.get("state") == "open" else "closed"
    cooldown_until: str | None = None
    if state == "open":
        deadline = _float_or(doc.get("opened_at"), time.time()) + _float_or(
            doc.get("cooldown_sec"), BASE_COOLDOWN_SEC
        )
        cooldown_until = _iso(deadline)
    last = doc.get("last_failure")
    last_at = last_detail = None
    if isinstance(last, dict):
        at = last.get("at")
        last_at = _iso(_float_or(at, 0.0)) if at is not None else None
        detail = last.get("detail")
        last_detail = str(detail) if detail else None
    return BreakerState(
        state=state,
        consecutive_failures=int(_float_or(doc.get("consecutive_failures"), 0)),
        cooldown_until=cooldown_until,
        last_failure_at=last_at,
        last_failure_detail=last_detail,
    )


def read_breaker_state(host: str) -> BreakerState:
    """The circuit-breaker state for *host* — a pure file read, never a write."""
    return _breaker_from_doc(_read_doc(circuit_state_path(host)))


def open_circuit_lines() -> list[str]:
    """One human-facing line per host whose SSH circuit is currently OPEN.

    Scans every ``_ssh_circuit/*.json`` state file (fail-open: unreadable
    files and a missing dir yield nothing). ``doctor`` and ``status-snapshot``
    surface these lines so a breaker-dark host is visible on the surfaces an
    agent already reads — the 2026-07-05 incident's missing signal.
    """
    from hpc_agent.state.run_record import _current_homedir

    lines: list[str] = []
    try:
        state_dir = _current_homedir() / "_ssh_circuit"
        paths = sorted(state_dir.glob("*.json")) if state_dir.is_dir() else []
    except OSError:
        return []
    for path in paths:
        doc = _read_doc(path)
        if doc is None or doc.get("state") != "open":
            continue
        host = str(doc.get("host") or path.stem)
        breaker = _breaker_from_doc(doc)
        lines.append(
            f"ssh circuit for {host}: OPEN until {breaker.cooldown_until} "
            f"({breaker.consecutive_failures} failures) — SSH to this host fails fast "
            f"by design; run net-triage before diagnosing the network."
        )
    return lines


# ── bounded probes (module-level so tests monkeypatch them) ─────────────────


def _https_check(url: str, timeout_sec: float) -> tuple[bool, str]:
    """One bounded HTTPS GET; any response (any status) proves local egress."""
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as resp:  # noqa: S310 — fixed https URL
            return True, f"HTTP {resp.status}"
    except Exception as exc:  # bounded probe: every failure is evidence, not an error
        return False, f"{type(exc).__name__}: {exc}"[:200]


def _dns_resolve(host: str, timeout_sec: float) -> tuple[bool, str]:
    """Bounded DNS resolution of *host* (getaddrinfo has no timeout of its own,
    so it runs on a worker thread the caller abandons at the deadline)."""
    import socket
    from concurrent.futures import Future, ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FuturesTimeout

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="net-triage-dns")
    try:
        fut: Future[Any] = pool.submit(socket.getaddrinfo, host, SSH_PORT, type=socket.SOCK_STREAM)
        try:
            infos = fut.result(timeout=timeout_sec)
        except FuturesTimeout:
            return False, f"resolution did not answer within {timeout_sec:.0f}s"
        except socket.gaierror as exc:
            return False, f"gaierror: {exc}"[:200]
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"[:200]
        addrs = sorted({info[4][0] for info in infos})
        return True, f"resolved to {', '.join(addrs[:4])}"
    finally:
        pool.shutdown(wait=False)


def _tcp_connect(host: str, port: int, timeout_sec: float) -> tuple[bool, str]:
    """ONE bounded TCP connect — never more; a retry loop here is exactly the
    connection storm the breaker exists to prevent."""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True, f"tcp connect to {host}:{port} ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:200]


# ── host enumeration ─────────────────────────────────────────────────────────


def _configured_hosts() -> list[tuple[str, str | None]]:
    """``(host, cluster_name)`` for every clusters.yaml entry with a host.

    Fail-open: an unloadable/absent config yields ``[]`` — a broken yaml must
    degrade triage to the caller-supplied host, never crash it.
    """
    try:
        from hpc_agent.infra.clusters import load_clusters_config

        clusters = load_clusters_config()
    except Exception:
        return []
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for name, cfg in clusters.items():
        if not isinstance(cfg, dict):
            continue
        host = str(cfg.get("host") or "").strip()
        if host and host not in seen:
            seen.add(host)
            out.append((host, str(name)))
    return out


# ── the differential ─────────────────────────────────────────────────────────


def _verdict(
    *,
    breaker: BreakerState,
    control_ok: bool,
    dns_ok: bool | None,
    tcp_ok: bool | None,
) -> TriageVerdict:
    """The fixed precedence table — evidence outranks inference at every arm.

    1. A successful TCP connect is direct evidence the host is reachable;
       nothing overrides it.
    2. A failed control probe means THIS machine's network is down — the most
       fundamental cause; every host looks dark when the box is offline.
    3. An open breaker explains SSH failing fast (and the TCP probe was
       skipped, so there is no direct evidence to outrank it).
    4. DNS failure — the name never resolved; nothing host-side was reached.
    5. Control passes but host:22 does not connect — cluster-side outage or a
       border filter on this source IP.
    """
    if tcp_ok is True:
        return "reachable"
    if not control_ok:
        return "local_network_down"
    if breaker.state == "open":
        return "breaker_open_cooling"
    if dns_ok is False:
        return "dns_failure"
    return "host_unreachable_network_ok"


def _remediation(verdict: TriageVerdict, host: str, breaker: BreakerState) -> str:
    """Deterministic remediation text per verdict arm."""
    if verdict == "reachable":
        return (
            f"{host}:{SSH_PORT} accepts TCP — the network path is fine. If SSH still "
            "fails, the problem is auth or ssh config (keys, agent, alias), not "
            "connectivity."
        )
    if verdict == "breaker_open_cooling":
        return (
            f"the SSH circuit breaker for {host} is OPEN after "
            f"{breaker.consecutive_failures} consecutive connection failures — SSH "
            f"fails fast ON PURPOSE (ban-risk protection). Wait until "
            f"{breaker.cooldown_until} for the automatic half-open probe, or — only "
            f"if you know why the failures happened (e.g. a VPN flap you fixed) — "
            f"override for this host with {OVERRIDE_ENV}={host}. Do not probe the "
            f"host yourself meanwhile."
        )
    if verdict == "local_network_down":
        return (
            "the control-plane HTTPS probe failed: THIS machine's network/VPN is "
            "down, so every cluster looks dark. Fix local connectivity first — the "
            "cluster is not implicated by any evidence yet."
        )
    if verdict == "dns_failure":
        return (
            f"'{host}' did not resolve. If it is an OpenSSH alias, its real HostName "
            "lives in ssh config (the alias itself never resolves); otherwise check "
            "DNS / VPN-provided resolvers."
        )
    # host_unreachable_network_ok
    return (
        f"local network is fine (control probe passed) but {host}:{SSH_PORT} did not "
        "accept a TCP connection — a cluster-side outage or a source-IP filter/ban "
        "at their border. A traceroute stalling at the cluster's edge discriminates "
        "border filtering from a dead host. Do NOT retry-storm (repeated probes are "
        "exactly what earns an IP ban); verify out-of-band (cluster status page, "
        "operations mailing list) and wait."
    )


def _triage_host(
    host: str,
    cluster: str | None,
    *,
    control_ok: bool,
    spec: NetTriageSpec,
) -> HostTriage:
    """Run the per-host differential: breaker read → bounded DNS → one TCP connect."""
    breaker = read_breaker_state(host)

    dns_ok, dns_detail = _dns_resolve(host, spec.dns_timeout_sec)

    tcp_ok: bool | None
    if breaker.state == "open":
        # NEVER probe through an open breaker: the connection would be one more
        # the intrusion filter counts, and triage must not race or burn the
        # breaker's single half-open probe slot (claimed by check_circuit).
        tcp_ok, tcp_detail = (
            None,
            (
                "skipped: circuit breaker is open — the half-open probe slot belongs "
                "to the breaker, and one more connection is one more ban-risk count"
            ),
        )
    elif dns_ok is False:
        tcp_ok, tcp_detail = None, "skipped: dns resolution already failed"
    else:
        tcp_ok, tcp_detail = _tcp_connect(host, SSH_PORT, spec.tcp_timeout_sec)

    verdict = _verdict(breaker=breaker, control_ok=control_ok, dns_ok=dns_ok, tcp_ok=tcp_ok)
    return HostTriage(
        host=host,
        cluster=cluster,
        breaker=breaker,
        dns_ok=dns_ok,
        dns_detail=dns_detail,
        tcp_ok=tcp_ok,
        tcp_detail=tcp_detail,
        verdict=verdict,
        remediation=_remediation(verdict, host, breaker),
    )


@primitive(
    name="net-triage",
    verb="query",
    side_effects=[],
    error_codes=[errors.SpecInvalid],
    # Pure diagnosis: reads breaker state files, opens bounded ephemeral
    # probe connections, writes nothing. Re-running is always safe.
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Connectivity differential: WHY can't I reach the cluster? For each "
            "configured cluster host (plus an optional --spec host): circuit-"
            "breaker state (read-only), one control-plane HTTPS probe, bounded "
            "DNS, and ONE bounded TCP connect to host:22 (skipped while the "
            "breaker is open). Verdict per host with remediation — run this "
            "before concluding a network cause; never diagnose with improvised "
            "ssh probes."
        ),
        spec_arg=True,
        # All-optional spec: a bare `hpc-agent net-triage` triages every
        # configured cluster host with the default budgets.
        spec_required=False,
        spec_model=NetTriageSpec,
        schema_ref=SchemaRef(input="net_triage"),
    ),
    agent_facing=True,
)
def net_triage(*, spec: NetTriageSpec | None = None) -> NetTriageResult:
    """Run the connectivity differential for every configured host (+ caller host).

    Deterministic and bounded: one HTTPS control probe (shared), then per host
    a breaker-state file read, a bounded DNS resolution, and at most ONE TCP
    connect to host:22 — skipped entirely while that host's breaker is open
    (the breaker owns the half-open probe; triage never interferes). The
    verdict precedence is fixed (:func:`_verdict`): direct evidence
    (``reachable``) outranks everything; a failed control probe
    (``local_network_down``) outranks host-side conclusions; an open breaker
    (``breaker_open_cooling``) outranks DNS/TCP inference. Fail-open on all
    local state: missing breaker files read as healthy, an unloadable
    clusters.yaml just means no configured hosts.
    """
    spec = spec or NetTriageSpec()  # spec_required=False: bare CLI call → defaults
    now = utcnow_iso()
    control_ok, control_detail = _https_check(CONTROL_URL, spec.control_timeout_sec)
    control = ControlPlaneCheck(https_ok=control_ok, url=CONTROL_URL, detail=control_detail)

    targets = _configured_hosts()
    if spec.host:
        extra = spec.host.rsplit("@", 1)[-1].strip()
        if extra and extra not in {h for h, _ in targets}:
            targets.append((extra, None))

    hosts = [
        _triage_host(host, cluster, control_ok=control_ok, spec=spec) for host, cluster in targets
    ]

    all_reachable = bool(hosts) and all(h.verdict == "reachable" for h in hosts)
    if not hosts:
        summary = "no hosts to triage (no clusters configured and no host supplied)."
    else:
        summary = "; ".join(f"{h.host}: {h.verdict}" for h in hosts)
        if not control_ok:
            summary = f"LOCAL NETWORK DOWN (control probe failed) — {summary}"
    return NetTriageResult(
        now=now,
        control=control,
        hosts=hosts,
        all_reachable=all_reachable,
        summary=summary,
    )
