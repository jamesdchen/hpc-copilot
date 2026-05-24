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

from hpc_agent.errors import RemoteCommandFailed
from hpc_agent.infra import remote
from hpc_agent.infra.remote import parse_remote_json
from hpc_agent.infra.time import utcnow_iso

if TYPE_CHECKING:
    from hpc_agent.state.session import RunRecord


def _read_remote_sidecar(*, ssh_target: str, remote_path: str, run_id: str) -> dict[str, Any]:
    """SSH-cat the per-run sidecar at ``.hpc/runs/<run_id>.json``."""
    sidecar_rel = f".hpc/runs/{run_id}.json"
    cmd = f"cat {shlex.quote(f'{remote_path}/{sidecar_rel}')}"
    proc = remote.ssh_run(cmd, ssh_target=ssh_target)
    if proc.returncode != 0:
        raise RemoteCommandFailed(
            f"failed to read remote sidecar at {remote_path}/{sidecar_rel}: "
            f"{proc.stderr.strip()[:500]}"
        )
    return parse_remote_json(
        proc.stdout, source_label=f"remote sidecar at {remote_path}/{sidecar_rel}"
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
    """
    sidecar = _read_remote_sidecar(
        ssh_target=ssh_target,
        remote_path=remote_path,
        run_id=run_id,
    )
    task_ids = _wave_task_ids(sidecar, wave)
    if not task_ids:
        return []
    expected = [template.format(task_id=tid) for tid in task_ids]
    paths_inline = " ".join(shlex.quote(p) for p in expected)
    script = (
        f"cd {shlex.quote(remote_path)} && "
        f"for f in {paths_inline}; do "
        f'[ -f "$f" ] || echo "MISSING:$f"; '
        f"done"
    )
    proc = remote.ssh_run(script, ssh_target=ssh_target)
    if proc.returncode != 0:
        raise RemoteCommandFailed(
            f"per-task output existence check failed: {proc.stderr.strip()[:500]}"
        )
    return [
        line[len("MISSING:") :].strip()
        for line in proc.stdout.splitlines()
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
        # failure.  Login nodes universally have python3.
        script = (
            f"if [ ! -f {shlex.quote(full_path)} ]; then "
            f"echo MISSING; exit 0; fi; "
            f"python3 -c 'import json,sys; json.load(open({json.dumps(full_path)}))' "
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
