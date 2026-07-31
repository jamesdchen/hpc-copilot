"""On-cluster combiner / reduce invocations over SSH.

Thin wrappers that run ``.hpc/_hpc_combiner.py`` on the login node — per-wave
combine, its checked ``(ok, stdout, stderr)`` variant, and the cross-wave final
reduce. Each funnels through :func:`hpc_agent.infra.remote.ssh_run`, so they
inherit its bounded-capture discipline; the transfer/deploy engine proper lives
in :mod:`hpc_agent.infra.transport.__init__`.
"""

from __future__ import annotations

import re
import shlex
import subprocess

from hpc_agent.errors import CombinerMissing
from hpc_agent.execution.mapreduce.deployed_artifact import (
    COMBINER_REL,
    combiner_absent_in,
    combiner_guard_snippet,
)
from hpc_agent.infra.remote import ssh_run

from ._disclose import run_with_stage_heartbeat
from ._shared import _DEFAULT

# ── P4 tier-1: sentinel-framed multi-wave combine (one ssh exec per burst) ────
# A burst of N newly-complete waves used to cost N cold SSH round-trips (one
# ``run_combiner`` per wave — the serial-wave-combines head-of-line stall). The
# batch runner fuses them into ONE ssh exec that invokes the SAME shipped
# ``.hpc/_hpc_combiner.py`` once per wave (the cluster-side reducer is untouched
# — one-definition), each invocation framed by a BEGIN/END sentinel pair so the
# per-wave stdout+stderr (folded ``2>&1``) and exit code are recoverable
# individually. A trailing ``__HPC_BATCH_END__`` proves the whole fused stream
# arrived intact: its ABSENCE (a NAT/reaper truncation) is positive evidence the
# output was cut, so the caller degrades to per-wave calls rather than
# parse-and-trust a truncated batch (E3 — never adopt a partial as authoritative).
_WAVE_BEGIN_SENTINEL = "__HPC_WAVE_BEGIN__"
_WAVE_END_SENTINEL = "__HPC_WAVE_END__"
#: Emitted last, only if every prior statement (activation + every wave) ran to
#: completion. Its absence ⇒ the batch stream was truncated ⇒ per-wave fallback.
BATCH_END_SENTINEL = "__HPC_BATCH_END__"

_WAVE_BEGIN_RE = re.compile(r"^" + _WAVE_BEGIN_SENTINEL + r" (\d+)$")
_WAVE_END_RE = re.compile(r"^" + _WAVE_END_SENTINEL + r" (\d+) rc=(-?\d+)$")


# ── U5: never invoke a combiner that isn't there ─────────────────────────────
# Every runner below composes ``cd <root> && [activation] python3
# .hpc/_hpc_combiner.py …``. Until this guard, a deploy root with no combiner
# produced whatever the login shell says about a missing script — which the
# control plane read as an ordinary non-zero combine, journaled into
# ``failed_waves``, and retried. The 2026-07-30 incident is the shape that
# costs: the artifact's absence was never named, and the first visible symptom
# was the cross-wave reduce's ``[combiner] ERROR: no _combiner/<run_id>/
# wave_*.json`` — a message about wave PARTIALS, hours downstream of the cause.
#
# The guard is one ``test -f`` INSIDE the ssh the runner was making anyway
# (:func:`combiner_guard_snippet`) — no new round-trip class — and it exits
# with a machine-recognisable sentinel so the absence is positive evidence
# rather than an inference from shell prose.


def _raise_if_combiner_absent(
    *,
    proc_or_streams: tuple[str, str],
    ssh_target: str,
    remote_path: str,
) -> None:
    """Raise :class:`CombinerMissing` when the guard fired; else return.

    ONE definition of the absence verdict, shared by every checked runner so a
    new combine entry point cannot quietly skip it. The remediation is composed
    from the recovery registry (kind ``combiner_missing``) rather than written
    here, so the redeploy command this raise names and the one
    ``recoveries-show`` prints are the same string.

    The registry import is function-local: this module is on the transport hot
    path and the registry pulls pydantic, which a combine that succeeds should
    never pay for.
    """
    stdout, stderr = proc_or_streams
    if not combiner_absent_in(stdout, stderr):
        return
    from hpc_agent.recovery.registry import remediation_for

    raise CombinerMissing(
        f"the deployed combiner {COMBINER_REL} is absent at {ssh_target}:{remote_path} — "
        "no combine ran. The per-task outputs are untouched; the framework artifact "
        "that reduces them was never deployed there, or vanished after it was.",
        remediation=remediation_for("combiner_missing"),
    )


def run_combiner(
    *,
    ssh_target: str,
    remote_path: str,
    wave: int,
    run_id: str,
    force: bool = False,
    timeout: float | None = _DEFAULT,
    remote_activation: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run the on-cluster combiner on the login node for a specific wave.

    Executes ``.hpc/_hpc_combiner.py`` on the remote host via SSH. The
    combiner accepts both CLI flags (preferred) and ``HPC_WAVE`` /
    ``HPC_RUN_ID`` env vars; we pass both.

    Parameters
    ----------
    ssh_target, remote_path:
        SSH target and remote project root.
    wave:
        Wave number (0-based) to combine.
    run_id:
        Run identifier — locates the per-run sidecar at
        ``.hpc/runs/<run_id>.json`` from which the combiner reads
        ``wave_map`` and ``result_dir_template``.
    force:
        If True, pass ``--force`` so the combiner overwrites any existing
        ``_combiner/wave_N.json`` output.
    timeout:
        Per-call subprocess timeout in seconds, threaded through to
        :func:`ssh_run`. Defaults to :data:`SSH_TIMEOUT_SEC` when omitted.
    """
    force_flag = " --force" if force else ""
    run_id_q = shlex.quote(run_id)
    # The presence guard sits AFTER the ``cd`` (so the relative
    # ``.hpc/_hpc_combiner.py`` resolves against the deploy root) and BEFORE the
    # activation (an absent artifact is not worth a conda activate).
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        f"{combiner_guard_snippet()}"
        f"{remote_activation}"
        f"HPC_WAVE={wave} HPC_RUN_ID={run_id_q} "
        f"python3 {COMBINER_REL} --wave {wave} --run-id {run_id_q}{force_flag}"
    )

    def _do() -> subprocess.CompletedProcess[str]:
        if timeout is _DEFAULT:
            return ssh_run(cmd, ssh_target=ssh_target)
        return ssh_run(cmd, ssh_target=ssh_target, timeout=timeout)

    return run_with_stage_heartbeat(f"combine: wave {wave}", ssh_target, _do)


def run_combiner_checked(
    *,
    ssh_target: str,
    remote_path: str,
    wave: int,
    run_id: str,
    force: bool = False,
    timeout: float | None = _DEFAULT,
    remote_activation: str = "",
) -> tuple[bool, str, str]:
    """Run the combiner and return ``(ok, stdout, stderr)``.

    Thin wrapper around :func:`run_combiner` that collapses
    ``CompletedProcess`` into a simple tuple. ``ok`` is ``True`` iff the
    remote combiner exited with returncode ``0``. A timeout propagates
    as :class:`TimeoutError`, not ``ok=False``.

    A DEPLOY dropout is not a combine failure and does not come back as
    ``ok=False``: when the presence guard reports ``.hpc/_hpc_combiner.py``
    absent this raises :class:`~hpc_agent.errors.CombinerMissing` instead, so
    the caller cannot journal it into ``failed_waves`` and retry it forever
    against a root that has no combiner (U5).
    """
    if timeout is _DEFAULT:
        result = run_combiner(
            ssh_target=ssh_target,
            remote_path=remote_path,
            wave=wave,
            run_id=run_id,
            force=force,
            remote_activation=remote_activation,
        )
    else:
        result = run_combiner(
            ssh_target=ssh_target,
            remote_path=remote_path,
            wave=wave,
            run_id=run_id,
            force=force,
            timeout=timeout,
            remote_activation=remote_activation,
        )
    _raise_if_combiner_absent(
        proc_or_streams=(result.stdout or "", result.stderr or ""),
        ssh_target=ssh_target,
        remote_path=remote_path,
    )
    return (
        result.returncode == 0,
        result.stdout or "",
        result.stderr or "",
    )


def run_final_reduce(
    *,
    ssh_target: str,
    remote_path: str,
    run_id: str,
    force: bool = False,
    timeout: float | None = _DEFAULT,
    remote_activation: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run the cluster-side FINAL cross-wave reduce on the login node (#254).

    Invokes ``.hpc/_hpc_combiner.py --final --run-id <id>`` over SSH. The
    combiner merges every ``_combiner/wave_*.json`` into a single
    ``_aggregated/<run_id>/metrics_aggregate.json`` on the cluster, so the
    caller pulls one kilobyte-scale file instead of hundreds of wave partials.
    Mirrors :func:`run_combiner` (same activation + timeout contract); pass
    ``force=True`` to overwrite an existing aggregate.
    """
    force_flag = " --force" if force else ""
    run_id_q = shlex.quote(run_id)
    cmd = (
        f"cd {shlex.quote(remote_path)} && "
        f"{combiner_guard_snippet()}"
        f"{remote_activation}"
        f"HPC_RUN_ID={run_id_q} "
        f"python3 {COMBINER_REL} --final --run-id {run_id_q}{force_flag}"
    )

    def _do() -> subprocess.CompletedProcess[str]:
        if timeout is _DEFAULT:
            return ssh_run(cmd, ssh_target=ssh_target)
        return ssh_run(cmd, ssh_target=ssh_target, timeout=timeout)

    result = run_with_stage_heartbeat("final reduce", ssh_target, _do)
    # U5 item 3: the aggregate path's own combine leg. On a combiner-absent
    # cluster this used to come back as a plain non-zero CompletedProcess and
    # the caller re-presented the raw stderr line — the 2026-07-30 shape.
    # Raise the discriminated cause with the registry remediation instead, so
    # the aggregate refuses with the redeploy command rather than with prose.
    _raise_if_combiner_absent(
        proc_or_streams=(result.stdout or "", result.stderr or ""),
        ssh_target=ssh_target,
        remote_path=remote_path,
    )
    return result


def _build_batch_command(
    *,
    remote_path: str,
    run_id: str,
    wave_forces: list[tuple[int, bool]],
    remote_activation: str,
) -> str:
    """Build the POSIX-sh command that combines every wave in ONE ssh exec.

    Each wave's combiner call is framed by a ``__HPC_WAVE_BEGIN__ <w>`` /
    ``__HPC_WAVE_END__ <w> rc=<rc>`` sentinel pair (the combiner's own
    stdout+stderr folded via ``2>&1`` ride between them), and the whole run ends
    with a single ``__HPC_BATCH_END__``. The activation prefix (which ends in
    `` && ``) guards a **brace group** — a group, not a subshell, so a
    ``conda activate`` / ``export PATH`` persists across every wave's python —
    and its ``&&`` means a failed activation runs NOTHING, so the batch emits no
    ``__HPC_BATCH_END__`` and the caller degrades to per-wave (E3). ``$?`` is read
    immediately after each python call, so the END line carries that wave's true
    exit code regardless of the preceding BEGIN printf.
    """
    run_id_q = shlex.quote(run_id)
    blocks: list[str] = []
    for wave, force in wave_forces:
        w = int(wave)
        force_flag = " --force" if force else ""
        blocks.append(
            f"printf '{_WAVE_BEGIN_SENTINEL} %s\\n' {w}; "
            f"HPC_WAVE={w} HPC_RUN_ID={run_id_q} "
            f"python3 {COMBINER_REL} --wave {w} --run-id {run_id_q}{force_flag} 2>&1; "
            f"printf '{_WAVE_END_SENTINEL} %s rc=%s\\n' {w} \"$?\""
        )
    body = "; ".join(blocks)
    # The U5 presence guard rides ahead of the activation and the brace group,
    # sharing their ``&&`` chain: an absent artifact exits before ANY wave runs
    # and before ``__HPC_BATCH_END__`` is printed. That is deliberate — the
    # missing sentinel routes the caller to its E3 per-wave degrade, and the
    # absence sentinel on stderr then makes the per-wave path raise the named
    # refusal instead of N shell errors.
    return (
        f"cd {shlex.quote(remote_path)} && {combiner_guard_snippet()}{remote_activation}"
        f"{{ {body}; printf '{BATCH_END_SENTINEL}\\n'; }}"
    )


def _parse_batch_output(stdout: str) -> tuple[bool, dict[int, tuple[int, str]]]:
    """Split the sentinel-framed batch stdout into ``{wave: (rc, body)}``.

    Returns ``(batch_complete, results)``. ``batch_complete`` is True iff the
    trailing ``__HPC_BATCH_END__`` line is present — its absence is the
    truncation signal (E3). ``results`` holds only waves whose BEGIN and matching
    END sentinels both arrived; a wave cut mid-frame is simply absent, so the
    caller falls back to a per-wave call for it rather than trusting a partial
    body.
    """
    lines = stdout.splitlines()
    batch_complete = any(ln.strip() == BATCH_END_SENTINEL for ln in lines)
    results: dict[int, tuple[int, str]] = {}
    cur_wave: int | None = None
    buf: list[str] = []
    for ln in lines:
        stripped = ln.strip()
        begin = _WAVE_BEGIN_RE.match(stripped)
        if begin:
            cur_wave = int(begin.group(1))
            buf = []
            continue
        end = _WAVE_END_RE.match(stripped)
        if end and cur_wave is not None and int(end.group(1)) == cur_wave:
            results[cur_wave] = (int(end.group(2)), "\n".join(buf))
            cur_wave = None
            buf = []
            continue
        if cur_wave is not None:
            buf.append(ln)
    return batch_complete, results


def run_combiner_batch(
    *,
    ssh_target: str,
    remote_path: str,
    wave_forces: list[tuple[int, bool]],
    run_id: str,
    timeout: float | None = _DEFAULT,
    remote_activation: str = "",
) -> subprocess.CompletedProcess[str]:
    """Run the on-cluster combiner for EVERY wave in *wave_forces* in one ssh exec.

    *wave_forces* is a list of ``(wave, force)`` pairs — one per wave to combine,
    each carrying its own ``--force`` decision (a fresh wave is un-forced; a
    retry of a previously-failed wave is forced). See :func:`_build_batch_command`
    for the sentinel-framing contract. The single ``timeout`` bounds the whole
    fused exec (matching the per-wave contract's default-vs-explicit shape).
    """
    cmd = _build_batch_command(
        remote_path=remote_path,
        run_id=run_id,
        wave_forces=wave_forces,
        remote_activation=remote_activation,
    )

    def _do() -> subprocess.CompletedProcess[str]:
        if timeout is _DEFAULT:
            return ssh_run(cmd, ssh_target=ssh_target)
        return ssh_run(cmd, ssh_target=ssh_target, timeout=timeout)

    label = "combine: waves " + ",".join(str(int(w)) for w, _ in wave_forces)
    return run_with_stage_heartbeat(label, ssh_target, _do)


def run_combiner_batch_checked(
    *,
    ssh_target: str,
    remote_path: str,
    wave_forces: list[tuple[int, bool]],
    run_id: str,
    timeout: float | None = _DEFAULT,
    remote_activation: str = "",
) -> dict[int, tuple[bool, str, str]] | None:
    """Run the fused combine and return ``{wave: (ok, stdout, stderr)}``.

    ``ok`` is ``True`` iff that wave's combiner exited 0. Because the combiner's
    stderr is folded into the framed stdout (``2>&1``), each wave's ``stdout`` and
    ``stderr`` in the returned tuple are the SAME framed body — enough for the
    caller's "output already exists (use --force)" refusal-recognition guard,
    which reads stderr, and for the success/failure message it surfaces.

    Returns ``None`` when the ``__HPC_BATCH_END__`` sentinel is absent — positive
    evidence the fused stream was truncated — so the caller degrades to per-wave
    combines (E3: never parse-and-trust a truncated batch). A wave present in
    *wave_forces* but missing its END frame is simply omitted from the dict; the
    caller falls back to a per-wave call for exactly that wave.
    """
    result = run_combiner_batch(
        ssh_target=ssh_target,
        remote_path=remote_path,
        wave_forces=wave_forces,
        run_id=run_id,
        timeout=timeout,
        remote_activation=remote_activation,
    )
    # Check BEFORE the truncation branch. A guarded absent artifact produces no
    # ``__HPC_BATCH_END__``, which is indistinguishable from a NAT truncation to
    # the sentinel scanner — so without this the fused path would degrade to N
    # per-wave calls and only then raise, paying N-1 pointless round-trips for
    # evidence it already holds.
    _raise_if_combiner_absent(
        proc_or_streams=(result.stdout or "", result.stderr or ""),
        ssh_target=ssh_target,
        remote_path=remote_path,
    )
    batch_complete, parsed = _parse_batch_output(result.stdout or "")
    if not batch_complete:
        return None
    return {wave: (rc == 0, body, body) for wave, (rc, body) in parsed.items()}
