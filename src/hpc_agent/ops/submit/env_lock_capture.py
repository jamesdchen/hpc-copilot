"""The canary's RESOLVED-environment capture — U-ENV1 (reproducibility program).

The #2 crisis gap's capture half. The canary already runs a real task under the
run's env; here — once it has verified — we resolve that env over SSH (in the
run's own activation, so ``python``/``pip`` bind the run's interpreter, exactly
as :mod:`hpc_agent.ops.aggregate.cluster_reduce` does) and reduce the snapshot to
an additive ``env_lock_sha`` stamped on the MAIN run's sidecar
(:func:`hpc_agent.state.runs.stamp_run_sidecar_env_lock`).

Snapshot-source resolution order (:data:`hpc_agent.state.env_lock.SOURCE_ORDER`):
``pip freeze`` (the fullest resolved dependency set) → a lockfile → ``python -V``
+ key package versions. The FIRST that resolves wins; when NONE does, an honest
``could_not_capture`` status is stamped — never a silent skip (no-silent-caps).

BEST-EFFORT by contract: capturing the environment must NEVER fail a submit whose
canary verified. Every failure (unreachable host, unreadable sidecar, empty
snapshot) degrades to a ``could_not_capture`` stamp, disclosed later at
verify/reproduce time. The SSH fetch is an injected seam (*fetch*) so the pure
resolve + stamp logic is unit-testable without a cluster.
"""

from __future__ import annotations

import contextlib
import shlex
from collections.abc import Callable
from typing import TYPE_CHECKING

from hpc_agent import errors
from hpc_agent.state.env_lock import EnvLockSnapshot, resolve_env_lock
from hpc_agent.state.runs import stamp_run_sidecar_env_lock

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["capture_and_stamp_env_lock", "EnvSnapshotFetch"]

#: A fetch resolves the raw snapshot texts for a run's environment. It returns
#: the ``{source: text}`` map :func:`resolve_env_lock` consumes (``pip_freeze`` /
#: ``lockfile`` / ``python_env``); a source that did not resolve is absent or
#: blank. Injected so the resolve + stamp path is testable without SSH.
EnvSnapshotFetch = Callable[..., dict[str, str]]

# Sentinel-delimited remote snapshot script. One SSH round-trip emits the
# resolved env's ``pip freeze`` then a ``python -V`` + key-package fallback,
# each behind an ``echo`` marker so the control plane parses them apart. Both
# arms are ``|| true`` so a missing ``pip`` (a bare-python env) never fails the
# whole read — the ``python -V`` fallback still resolves the ``python_env``
# source. Kept plain ``sh`` (no package import) so a broken run env still yields
# *something* rather than a hard error.
_PIP_MARKER = "<<<HPC_ENVLOCK:PIPFREEZE>>>"
_PYENV_MARKER = "<<<HPC_ENVLOCK:PYENV>>>"

#: Positive-evidence ack (run-12 finding 24): the token proves the remote shell
#: reached the END of the snapshot script, so a severed/truncated channel (rc 0
#: with a partial ``pip freeze``) can never masquerade as a valid — but WRONG —
#: env_lock_sha over a subset of packages. Its ABSENCE ⇒ UNKNOWN ⇒ could-not-capture.
_ENVLOCK_ACK_PREFIX = "__HPC_ENVLOCK_ACK__="


def _snapshot_script(*, runtime: str | None) -> str:
    """The remote snapshot body — ``pip freeze`` (uv-aware) + a python/version fallback."""
    pip_cmd = "uv pip freeze" if runtime == "uv" else "python3 -m pip freeze"
    return (
        f"echo {shlex.quote(_PIP_MARKER)}\n"
        f"{pip_cmd} 2>/dev/null || true\n"
        f"echo {shlex.quote(_PYENV_MARKER)}\n"
        "python3 -c 'import sys; print(\"python \"+sys.version.split()[0])' 2>/dev/null || true\n"
    )


def _split_snapshot(stdout: str) -> dict[str, str]:
    """Split the sentinel-delimited remote stdout into ``{source: text}``.

    ``pip freeze`` output lands under ``pip_freeze``; the ``python -V`` line under
    ``python_env``. A missing marker (torn stream) simply yields no key for that
    source — :func:`resolve_env_lock` then falls through the resolution order.
    """
    out: dict[str, str] = {}
    if _PIP_MARKER in stdout:
        after = stdout.split(_PIP_MARKER, 1)[1]
        pip_body = after.split(_PYENV_MARKER, 1)[0]
        if pip_body.strip():
            out["pip_freeze"] = pip_body
    if _PYENV_MARKER in stdout:
        py_body = stdout.split(_PYENV_MARKER, 1)[1]
        if py_body.strip():
            out["python_env"] = py_body
    return out


def _ssh_fetch(experiment_dir: Path, canary_run_id: str) -> dict[str, str]:
    """Default fetch: run the snapshot script in the canary's env over SSH.

    Resolves the SSH coordinates from the CANARY's journal record + sidecar (its
    env is the main run's env, deployed by the same pipeline), threads the run's
    ``remote_activation`` so ``pip``/``python`` bind the run interpreter, and
    returns the parsed ``{source: text}`` map. Any transport/parse failure raises
    (the caller turns it into a could-not-capture stamp).
    """
    from hpc_agent.infra.clusters import remote_activation_for_sidecar, resolve_ssh_target
    from hpc_agent.infra.remote import ssh_run
    from hpc_agent.infra.ssh_validation import split_ack, wrap_with_ack
    from hpc_agent.state.journal import load_run
    from hpc_agent.state.runs import read_run_sidecar

    record = load_run(experiment_dir, canary_run_id)
    if record is None:
        raise errors.SpecInvalid(f"no journal record for canary {canary_run_id!r}")
    try:
        sidecar = read_run_sidecar(experiment_dir, canary_run_id)
    except (FileNotFoundError, OSError):
        sidecar = {}
    remote_activation = remote_activation_for_sidecar(sidecar, fallback_cluster=record.cluster)
    runtime = sidecar.get("runtime")
    cmd = (
        f"cd {shlex.quote(record.remote_path)} && "
        f"{remote_activation}"
        f"{_snapshot_script(runtime=runtime if isinstance(runtime, str) else None)}"
    )
    proc = ssh_run(
        wrap_with_ack(cmd.rstrip(), _ENVLOCK_ACK_PREFIX),
        ssh_target=resolve_ssh_target(record),
        timeout=120.0,
    )
    clean, ack_rc = split_ack(proc.stdout or "", _ENVLOCK_ACK_PREFIX)
    if ack_rc is None:
        # No ack ⇒ the remote shell never reached the end (severed/truncated
        # channel). Return nothing so the capture records could-not-capture — a
        # partial pip freeze must NEVER mint a wrong env_lock_sha (finding 24).
        return {}
    # The compound's own rc is not meaningful (the arms are ``|| true``); only ack
    # PRESENCE gates. Parse the clean stdout for the resolved snapshot sections.
    return _split_snapshot(clean)


def capture_and_stamp_env_lock(
    experiment_dir: Path,
    *,
    run_id: str,
    canary_run_id: str,
    fetch: EnvSnapshotFetch | None = None,
) -> EnvLockSnapshot:
    """Resolve the run's environment and stamp ``env_lock_sha`` on its sidecar.

    Resolves the RESOLVED-environment snapshot (via *fetch*, or the default SSH
    fetch in the canary's activation), reduces it to an ``env_lock_sha`` per the
    resolution order, and stamps it — with the capture ``env_lock_status`` — on
    ``run_id``'s sidecar. Returns the :class:`EnvLockSnapshot`.

    BEST-EFFORT: never raises. A fetch/transport failure or an empty snapshot
    yields a ``could_not_capture`` :class:`EnvLockSnapshot`, and the status is
    STILL stamped (no-silent-caps) so a later reproduction reads "environment
    identity not captured" rather than a silent absence. A missing MAIN sidecar
    (nothing to stamp) is swallowed too — the snapshot is returned for the caller
    to log.
    """
    try:
        if fetch is not None:
            texts = fetch(canary_run_id=canary_run_id)
        else:
            texts = _ssh_fetch(experiment_dir, canary_run_id)
    except Exception:  # noqa: BLE001 — best-effort capture never fails the gate
        texts = {}
    snap = resolve_env_lock(
        pip_freeze=texts.get("pip_freeze"),
        lockfile=texts.get("lockfile"),
        python_env=texts.get("python_env"),
    )
    # No sidecar to stamp (or an I/O hiccup) — the snapshot is still returned so
    # the caller can log; the sidecar simply carries no env_lock this run.
    with contextlib.suppress(FileNotFoundError, OSError):
        stamp_run_sidecar_env_lock(
            experiment_dir,
            run_id,
            env_lock_sha=snap.sha,
            env_lock_status=snap.status,
        )
    return snap
