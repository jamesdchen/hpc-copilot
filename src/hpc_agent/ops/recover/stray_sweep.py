"""``stray-sweep`` — login-node process-hygiene probe (run-12 finding 20, LAYER 2).

The connection storm's REMOTE cost: orphaned remote halves of killed ssh
commands (a bash+python pair per poll) accumulate on the login node until the
user's per-process fork quota is exhausted and sshd can no longer fork a login
shell — a state self-service recovery cannot clear. LAYER 1
(:func:`hpc_agent.infra.remote.build_remote_command`) bounds each remote
command's lifetime so orphans self-destruct; this verb is the LAYER-2
observability + bounded-cleanup half.

ONE fork-minimal ssh command lists the user's processes
(``ps -u "$USER" -o pid=,etimes=,args=``); the parser counts the total (fork
pressure), identifies the MARKED framework processes (their argv carries the
``HPC_AGENT_OP`` marker), and flags marked processes older than the max
legitimate deadline as strays — the observability gap the incident exposed
(47 strays would have been visible days before the quota wedged). Only with an
explicit ``reap`` flag does a SECOND ssh ``kill`` exactly those stray PIDs —
never an unmarked user process.

Home rationale: this verb does SSH, so it does NOT live on ``doctor`` (whose
contract is "pure local filesystem read — no SSH, no scheduler") nor on
``net-triage`` (which deliberately opens NO ssh — it must not burn a host's
single half-open breaker probe). A small dedicated primitive keeps both those
contracts intact.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent._wire.queries.stray_sweep import StrayProcess, StraySweepResult, StraySweepSpec
from hpc_agent.cli._dispatch import CliArg, CliShape, SchemaRef
from hpc_agent.infra.remote import OP_MARKER_PREFIX

__all__ = ["stray_sweep", "parse_ps_output"]

# ``HPC_AGENT_OP=<label>:<epoch>`` anywhere in the argv line marks a framework
# process (:func:`hpc_agent.infra.remote.build_remote_command` puts it in argv
# as bash's $0). The label is captured for the human-facing note.
_MARKER_RE = re.compile(rf"{re.escape(OP_MARKER_PREFIX)}=([A-Za-z0-9._-]+):\d+")


def parse_ps_output(
    text: str, *, max_age_sec: int
) -> tuple[int, list[StrayProcess], list[StrayProcess]]:
    """Parse ``ps -o pid=,etimes=,args=`` output into ``(total, marked, strays)``.

    Each non-empty line is ``<pid> <etimes> <args...>``. Returns the total
    process count, the MARKED subset (argv carries the ``HPC_AGENT_OP`` marker),
    and the STRAY subset (marked AND ``etimes`` older than *max_age_sec* — an
    orphan the LAYER-1 timeout wrapper did not reap). A line that does not parse
    (no leading integer pid/etimes) is counted toward neither total nor marked —
    it is skipped, never mistaken for a process to kill.
    """
    total = 0
    marked: list[StrayProcess] = []
    strays: list[StrayProcess] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        total += 1
        pid = int(parts[0])
        etimes = int(parts[1])
        args = parts[2] if len(parts) > 2 else ""
        m = _MARKER_RE.search(args)
        if m is None:
            continue
        proc = StrayProcess(pid=pid, etimes=etimes, op=m.group(1), args=args)
        marked.append(proc)
        if etimes > max_age_sec:
            strays.append(proc)
    return total, marked, strays


# Fork-minimal: ONE exec of ps, no conda/module preamble (a starved login node
# fails a heavy poll's many forks but can still run a single ps — the probe
# pattern validated live, run-12 finding 20). ``pid=,etimes=,args=`` with empty
# headers suppresses the header row portably (SGE/Slurm login nodes alike).
_PS_CMD = 'ps -u "$USER" -o pid=,etimes=,args='


def _reap_cmd(pids: list[int]) -> str:
    """The ``kill`` for exactly *pids* — never a pattern, never an unmarked process."""
    return "kill -TERM " + " ".join(str(p) for p in pids)


@primitive(
    name="stray-sweep",
    verb="query",
    side_effects=[
        SideEffect("ssh", "<login-node> (ps -u $USER; kill only marked strays when reap=true)"),
    ],
    error_codes=[errors.SpecInvalid, errors.SshUnreachable],
    # Detection re-runs freely; a reap is idempotent by PID (a dead PID's kill
    # is a harmless no-op), so re-running never double-acts on a live process.
    idempotent=True,
    idempotency_key=None,
    cli=CliShape(
        help=(
            "Login-node process-hygiene probe (run-12 finding 20). One "
            "fork-minimal `ps -u $USER` over ssh: counts total processes, "
            "identifies MARKED framework processes (HPC_AGENT_OP) older than the "
            "max legitimate deadline as strays, and — only with --reap — kills "
            "exactly those PIDs (never an unmarked process). Surfaces the stray "
            "count the quota-wedge incident lacked."
        ),
        spec_arg=True,
        spec_model=StraySweepSpec,
        schema_ref=SchemaRef(input="stray_sweep"),
        args=(
            CliArg(
                "--ssh-target",
                type=str,
                required=True,
                help="Login node to sweep (user@host or an OpenSSH alias).",
            ),
            CliArg(
                "--reap",
                action="store_true",
                help="Kill the strays found (only marked, over-age PIDs). Default: detect only.",
            ),
            CliArg(
                "--max-age-sec",
                type=int,
                default=3900,
                help="Age (s) beyond which a MARKED process is a stray (default 3900).",
            ),
            CliArg(
                "--warn-threshold",
                type=int,
                default=40,
                help="Total process count above which needs_attention flips (default 40).",
            ),
        ),
    ),
    agent_facing=True,
)
def stray_sweep(*, spec: StraySweepSpec) -> dict[str, Any]:
    """Sweep the login node for framework process strays; optionally reap them.

    Runs ONE ``ps`` over ssh, parses the process table
    (:func:`parse_ps_output`), and — only when *spec.reap* is True AND strays
    were found — runs a SECOND ssh ``kill`` targeting exactly the stray PIDs.
    Never kills an unmarked process, and never a marked process younger than
    *spec.max_age_sec*.

    Returns a :class:`StraySweepResult`-shaped dict: the total process count
    (fork-quota pressure), the marked count, the strays, the reaped PIDs, and a
    ``needs_attention`` flag (total over ``warn_threshold`` OR any stray).

    Raises :class:`errors.SshUnreachable` when the ps probe itself cannot run.
    """
    from hpc_agent.infra.remote import ssh_run

    try:
        probe = ssh_run(_PS_CMD, ssh_target=spec.ssh_target, op="stray-sweep")
    except (errors.RemoteCommandFailed, errors.SshCircuitOpen, OSError) as exc:
        raise errors.SshUnreachable(
            f"stray-sweep could not run `ps` on {spec.ssh_target!r}: {exc}"
        ) from exc
    if probe.returncode != 0:
        raise errors.SshUnreachable(
            f"stray-sweep `ps` on {spec.ssh_target!r} exited {probe.returncode}: "
            f"{(probe.stderr or '').strip()[:200]}"
        )

    total, marked, strays = parse_ps_output(probe.stdout or "", max_age_sec=spec.max_age_sec)

    reaped_pids: list[int] = []
    reaped = False
    if spec.reap and strays:
        reaped = True
        pids = [p.pid for p in strays]
        # Best-effort: a kill that partially fails (a PID already gone) still
        # reports the PIDs we targeted; the next sweep re-checks the truth.
        with contextlib.suppress(errors.RemoteCommandFailed, errors.SshCircuitOpen, OSError):
            ssh_run(_reap_cmd(pids), ssh_target=spec.ssh_target, op="stray-sweep-reap")
        reaped_pids = pids

    needs_attention = total > spec.warn_threshold or bool(strays)
    if needs_attention:
        verdict = (
            f"NEEDS ATTENTION: {total} processes, {len(marked)} marked, {len(strays)} stray(s)"
        )
        if reaped:
            verdict += f"; reaped {len(reaped_pids)}"
        elif strays:
            verdict += " — re-run with reap=true to kill them"
    else:
        verdict = f"all clear: {total} processes, {len(marked)} marked, 0 strays"

    result = StraySweepResult(
        ssh_target=spec.ssh_target,
        total_process_count=total,
        marked_count=len(marked),
        strays=strays,
        reaped_pids=reaped_pids,
        reaped=reaped,
        needs_attention=needs_attention,
        summary=verdict,
    )
    dumped: dict[str, Any] = result.model_dump(mode="json")
    return dumped
