"""Aggregate preconditions / postconditions / provenance.

These helpers are framework-agnostic guarantees around the user-supplied
combiner.  They check plumbing (every task produced output, the combiner
wrote what it claimed to write, the aggregated artifact carries provenance
tied to the run) without learning anything about experiment semantics.
Both /aggregate and ``hpc-agent aggregate`` use them.
"""

from __future__ import annotations

import json
import shlex
from typing import TYPE_CHECKING, Any

from hpc_agent.errors import CombinerMissing, RemoteCommandFailed
from hpc_agent.execution.mapreduce.deployed_artifact import (
    COMBINER_REL,
    CombinerProbe,
    combiner_probe_snippet,
    split_combiner_probe,
)
from hpc_agent.infra import remote
from hpc_agent.infra.ssh_validation import parse_remote_json, split_ack, wrap_with_ack
from hpc_agent.infra.time import utcnow_iso

if TYPE_CHECKING:
    from hpc_agent.state.run_record import RunRecord

# Sentinel-ack for the per-task output existence check (the positive-evidence
# transport rule, docs/design/connection-broker.md). The check's success signal
# is the ABSENCE of ``MISSING:`` lines, so a severed/truncated rc-0 channel that
# delivered nothing used to read as "all outputs present" — silence-as-success
# at the aggregate PRECONDITION gate. The ack proves the remote loop ran to
# completion; its absence is UNKNOWN and must never green the gate.
_OUTPUTS_ACK_PREFIX = "__HPC_OUTPUTS_ACK__="


def _read_remote_sidecar(*, ssh_target: str, remote_path: str, run_id: str) -> dict[str, Any]:
    """SSH-cat the per-run sidecar at ``.hpc/runs/<run_id>.json``.

    The sidecar-only view, for callers outside the combine precondition (e.g.
    ``aggregate_flow``) that have no use for the folded artifact probe.
    """
    sidecar, _probe = _read_remote_sidecar_and_probe(
        ssh_target=ssh_target, remote_path=remote_path, run_id=run_id
    )
    return sidecar


def _read_remote_sidecar_and_probe(
    *, ssh_target: str, remote_path: str, run_id: str
) -> tuple[dict[str, Any], CombinerProbe | None]:
    """SSH-cat the per-run sidecar; also read the deployed combiner's identity.

    Returns ``(sidecar, combiner_probe)``. The probe is FOLDED into this exec
    (U5) rather than costing its own: this ``cat`` is the first and
    unconditional round-trip of the wave-combine precondition check, so
    "is the artifact we are about to depend on actually deployed?" rides it for
    the price of one ``test -f`` and one ``sha256sum``. Folding it here — not
    into the per-task loop below — also means the gate holds for a wave with NO
    tasks, which would otherwise short-circuit past every check.

    The probe line is peeled back off before the JSON parse, so
    :func:`parse_remote_json` sees exactly the bytes it saw before. ``None``
    means the probe line never arrived: UNKNOWN, never "absent".

    Two properties of the composed command are load-bearing:

    * the ``cat`` stays FIRST, so the command still reads as the sidecar read
      it has always been (several fixtures dispatch on that prefix, and so does
      anyone reading an ssh log);
    * its exit code is saved and re-raised as the command's own. The probe
      always exits 0, so a naive ``cat …; <probe>`` would hand back the
      PROBE's success and silently swallow a failed sidecar read — turning the
      guard added to close one silent failure into the cause of another.
    """
    sidecar_rel = f".hpc/runs/{run_id}.json"
    cmd = (
        f"cat {shlex.quote(f'{remote_path}/{sidecar_rel}')}; __hpc_rc=$?; "
        f"{combiner_probe_snippet(root=remote_path)}"
        f'exit "$__hpc_rc"'
    )
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    if proc.returncode != 0:
        raise RemoteCommandFailed(
            f"failed to read remote sidecar at {remote_path}/{sidecar_rel}: "
            f"{proc.stderr.strip()[:500]}"
        )
    probe, stdout = split_combiner_probe(proc.stdout)
    return (
        parse_remote_json(
            stdout, source_label=f"remote sidecar at {remote_path}/{sidecar_rel}"
        ),
        probe,
    )


def _wave_task_ids(sidecar: dict[str, Any], wave: int) -> list[int]:
    """Return task ids belonging to *wave* per ``sidecar['wave_map']``.

    Falls back to "every task" when ``wave==0`` and no wave_map is present
    (un-batched submissions ship a single implicit wave-0).
    """
    wave_map = sidecar.get("wave_map") or {}
    if wave_map:
        members = wave_map.get(str(wave))
        return [int(t) for t in members] if members else []
    if wave == 0:
        return list(range(int(sidecar.get("task_count", 0))))
    return []


def refuse_absent_combiner(
    probe: CombinerProbe | None,
    *,
    ssh_target: str,
    remote_path: str,
    run_id: str | None = None,
) -> None:
    """Raise :class:`CombinerMissing` when *probe* proves the artifact is gone.

    ONE definition of the aggregate-side refusal, so the wave-combine preflight
    and any future consumer name the same cause with the same registry-composed
    remediation (kind ``combiner_missing`` — whose first option is the
    ``redeploy-runtime`` repair, with ``<run_id>`` substituted here when known).

    Fails OPEN on ``None``: a probe line that never arrived is UNKNOWN, and
    unknown must never green OR red a gate. The refusal fires only on positive
    evidence of absence — never on a stale sha (that is a version-skew question
    the deploy cache owns, not a reason to refuse a combine that may well work).
    """
    if probe is None or probe.present:
        return
    from hpc_agent.recovery.registry import remediation_for

    placeholders = {"run_id": run_id} if run_id else None
    raise CombinerMissing(
        f"the deployed combiner {COMBINER_REL} is absent at {ssh_target}:{remote_path}; "
        "refusing to start the combine leg that depends on it. Every per-task output is "
        "untouched — this is a DEPLOY dropout, not a data failure.",
        remediation=remediation_for("combiner_missing", placeholders=placeholders),
    )


# NOTE (F5, 2026-07-30): a standalone ``verify_deployed_combiner()`` lived here
# and had zero callers — the probe it wrapped is folded into
# ``_read_remote_sidecar_and_probe``'s exec, which is the only place this
# module needs the answer. Deleted rather than kept as a second, untested way
# to ask the same question with an extra round-trip.


def verify_per_task_outputs(
    *,
    ssh_target: str,
    remote_path: str,
    run_id: str,
    wave: int,
    template: str,
) -> list[str]:
    """Check every per-task output named by *template* exists on the cluster.

    *template* may include ``{task_id}``; it is substituted with each task
    id in the wave (per the per-run sidecar's ``wave_map``).  Paths are
    interpreted relative to *remote_path* unless absolute.

    Returns the list of *missing* paths (relative to remote_path or
    absolute as written).  Empty list = all expected outputs are present.

    U5: this is the wave-combine PRECONDITION exec — the last thing that runs
    before ``combine-wave`` invokes the cluster combiner — so the deployed
    combiner's own presence+sha rides along in it, for free. Refusing HERE, by
    name and with the redeploy command, is the early gate the 2026-07-30
    incident lacked: the alternative was discovering the artifact's absence
    hours later, second-hand, through a cross-wave reduce complaining about
    wave partials. Raises :class:`CombinerMissing` before the missing-outputs
    verdict is even computed — a combine that cannot run is not a question
    about data.
    """
    sidecar, probe = _read_remote_sidecar_and_probe(
        ssh_target=ssh_target,
        remote_path=remote_path,
        run_id=run_id,
    )
    # FIRST, before anything about data: a combine that cannot run is not a
    # question about which task outputs are present. Gating here also covers
    # the empty-wave short-circuit below.
    refuse_absent_combiner(
        probe, ssh_target=ssh_target, remote_path=remote_path, run_id=run_id
    )
    task_ids = _wave_task_ids(sidecar, wave)
    if not task_ids:
        return []
    # Bare string replace — mirrors ``_reducer_contract.format_output_rel``:
    # other literal braces in a user-supplied template (``{horizon}``,
    # ``{...}``) must not raise from ``str.format``. Only ``{task_id}`` is
    # recognised.
    expected = [template.replace("{task_id}", str(tid)) for tid in task_ids]
    paths_inline = " ".join(shlex.quote(p) for p in expected)
    script = (
        f"cd {shlex.quote(remote_path)} && "
        f"for f in {paths_inline}; do "
        f'[ -f "$f" ] || echo "MISSING:$f"; '
        f"done"
    )
    proc = remote.ssh_run(wrap_with_ack(script, _OUTPUTS_ACK_PREFIX), ssh_target=ssh_target)
    if proc.returncode != 0:
        raise RemoteCommandFailed(
            f"per-task output existence check failed: {proc.stderr.strip()[:500]}"
        )
    clean, ack_rc = split_ack(proc.stdout or "", _OUTPUTS_ACK_PREFIX)
    if ack_rc is None:
        raise RemoteCommandFailed(
            "per-task output existence check returned no positive-evidence ack "
            "(channel severed / output truncated); refusing to read silence as "
            "'all outputs present'."
        )
    if ack_rc != 0:
        # The ack rides a ``;`` so the remote rc lands here, not on the ssh
        # returncode: a failed ``cd`` (bad remote_path) must stay loud.
        raise RemoteCommandFailed(
            f"per-task output existence check failed (remote rc={ack_rc}): "
            f"{proc.stderr.strip()[:500]}"
        )
    return [
        line[len("MISSING:") :].strip()
        for line in clean.splitlines()
        if line.startswith("MISSING:")
    ]


def verify_combiner_artifact(
    *,
    ssh_target: str,
    remote_path: str,
    expect_output: str,
) -> tuple[bool, str]:
    """Verify the combiner produced *expect_output* (relative to remote_path).

    Existence is always checked.  When the path ends in ``.json`` the file
    is also parsed via ``python3`` on the login node — combiners that exit
    0 but emit truncated/empty JSON don't pass.

    Returns ``(ok, detail)``.  *detail* is "ok" on success or a short
    human-readable reason on failure.
    """
    full_path = f"{remote_path.rstrip('/')}/{expect_output.lstrip('/')}"
    if expect_output.endswith(".json"):
        # python3 -c returns 0 on parse success; non-zero (with stderr) on
        # failure.  Login nodes universally have python3. The python source
        # is built separately and shell-quoted ONCE — embedding
        # ``json.dumps(full_path)`` inside outer single-quotes was unsafe
        # because ``json.dumps`` doesn't escape ASCII apostrophes, so an
        # ``expect_output`` containing ``'`` would close the outer quote
        # and inject shell.
        py_src = f"import json,sys; json.load(open({json.dumps(full_path)}))"
        script = (
            f"if [ ! -f {shlex.quote(full_path)} ]; then "
            f"echo MISSING; exit 0; fi; "
            f"python3 -c {shlex.quote(py_src)} "
            f"&& echo OK || echo INVALID_JSON"
        )
    else:
        script = f"[ -f {shlex.quote(full_path)} ] && echo OK || echo MISSING"
    proc = remote.ssh_run(script, ssh_target=ssh_target)
    if proc.returncode != 0:
        # The remote verifier script always exits 0 (MISSING/OK/
        # INVALID_JSON are echoed, not signalled via exit code), so a
        # non-zero rc is an SSH transport failure — raise rather than
        # misreport it as "unrecognised verifier output".
        raise RemoteCommandFailed(
            f"combiner-artifact verifier failed (rc={proc.returncode}): {proc.stderr.strip()[:200]}"
        )
    out_tail = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if out_tail == "OK":
        return True, "ok"
    if out_tail == "MISSING":
        return False, f"is missing at {full_path}"
    if out_tail == "INVALID_JSON":
        return False, f"at {full_path} is not valid JSON"
    return False, f"unrecognised verifier output: {proc.stdout.strip()[:200]!r}"


def build_provenance(record: RunRecord, *, wave: int) -> dict[str, Any]:
    """Build the provenance metadata block for an aggregated wave.

    Pure metadata — agnostic to experiment semantics.  Lets a downstream
    consumer (agent or human) verify that an aggregated artifact
    corresponds to the run they expect, without re-querying the journal.
    """
    return {
        "run_id": record.run_id,
        "wave": int(wave),
        "profile": record.profile,
        "cluster": record.cluster,
        "combined_at": utcnow_iso(),
    }


def write_remote_provenance(
    *,
    ssh_target: str,
    remote_path: str,
    expect_output: str,
    provenance: dict[str, Any],
) -> str:
    """Write ``_provenance.json`` next to the combiner's expected output.

    Path resolution: the sidecar lives in the same directory as
    *expect_output* on the cluster.  Returns the absolute remote path
    written.  Best-effort — callers may catch and log; provenance also
    appears in the aggregate envelope so this is a convenience, not a
    contract.
    """
    full_output = f"{remote_path.rstrip('/')}/{expect_output.lstrip('/')}"
    output_dir = full_output.rsplit("/", 1)[0] if "/" in full_output else remote_path
    sidecar = f"{output_dir.rstrip('/')}/_provenance.json"
    payload = json.dumps(provenance, sort_keys=True)
    # Ferry the JSON via base64 to dodge quoting hazards.
    import base64

    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    script = (
        f"mkdir -p {shlex.quote(output_dir)} && echo {b64} | base64 -d > {shlex.quote(sidecar)}"
    )
    proc = remote.ssh_run(script, ssh_target=ssh_target)
    if proc.returncode != 0:
        raise RemoteCommandFailed(
            f"failed to write provenance sidecar at {sidecar}: {proc.stderr.strip()[:500]}"
        )
    return sidecar
