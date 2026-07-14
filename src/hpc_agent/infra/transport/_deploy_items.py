"""The runtime-deploy ship list + content-hash deploy cache (#242, #252).

Enumerates every file :func:`deploy_runtime` places on the cluster, each hashed
for the content-hash cache, and the local/remote manifest helpers that decide
which of those files actually need re-shipping. This is the single source of
truth for *what* the deploy ships; the engine (``__init__``) owns the transfer
itself.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

# Remote path (relative to ``remote_path``) of the content-hash cache the
# deploy step keys on to skip re-shipping unchanged files (#242). It maps
# each deployed file's remote-relative path to the sha256 of the bytes last
# placed there, alongside the package version that produced them.
_DEPLOY_MANIFEST_REL: Final[str] = ".hpc/.deploy_state.json"


def _sha256_bytes(data: bytes) -> str:
    """Hex sha256 of *data* — the content identity used by the deploy cache."""
    return hashlib.sha256(data).hexdigest()


def _pkg_version() -> str:
    """Installed ``hpc-agent`` version, or a stable placeholder when absent.

    Keys the deploy cache (#242) so a ``pip install -U`` invalidates the
    whole manifest even when individual file bytes look unchanged. The
    placeholder is process-stable, so a source checkout that is not
    pip-installed still produces a consistent (always-self-consistent) key.
    """
    from importlib.metadata import PackageNotFoundError, version

    for dist in ("hpc-agent", "hpc_agent"):
        try:
            return version(dist)
        except PackageNotFoundError:
            continue
    return "0+unknown"


@dataclass(frozen=True)
class _DeployItem:
    """One file the runtime deploy ships, with its content identity.

    Exactly one of *src_path* (a verbatim package file, scp'd directly so
    error/backoff labels name the real path) or *content* (text rendered at
    deploy time, e.g. a per-scheduler array script) is set. *sha* is the
    sha256 of the bytes that land on the cluster — file bytes for *src_path*,
    UTF-8 of *content* for *content* — and is what the deploy cache compares.
    """

    dst_rel: str
    sha: str
    src_path: Path | None
    content: str | None


def _build_deploy_items(*, scheduler: str | None) -> list[_DeployItem]:
    """Enumerate every file :func:`deploy_runtime` ships, hashed for caching.

    The single source of truth for *what* the deploy places and the content
    sha of each piece, shared by :func:`deploy_runtime` and
    :func:`_local_deploy_manifest` so the cache key the deploy writes is the
    same key it later compares against. Order is deterministic but no longer
    load-bearing — copies are fired concurrently (#245).
    """
    from hpc_agent.infra.backends import get_backend_class, template_ext_for

    # This file lives at ``hpc_agent/infra/transport/_deploy_items.py``, so the
    # package root ``hpc_agent/`` is THREE parents up (was two when transport
    # was a flat module — do not drop a ``.parent`` or the reads below miss).
    pkg_dir = Path(__file__).parent.parent.parent
    items: list[_DeployItem] = []

    def add_file(src: Path, dst_rel: str) -> None:
        items.append(_DeployItem(dst_rel, _sha256_bytes(src.read_bytes()), src, None))

    def add_text(content: str, dst_rel: str) -> None:
        items.append(_DeployItem(dst_rel, _sha256_bytes(content.encode("utf-8")), None, content))

    # Importable stubs (used inside cluster jobs by user code):
    #   - ``from hpc_agent.execution.mapreduce.metrics_io import write_metrics``
    #     in user executor scripts (executor_template.py).
    #   - ``from hpc_agent.executor_cli import flag, generic_args, gpu_args``
    #     in user .hpc/tasks.py. Both modules are stdlib-only (AST-scanned)
    #     so they ship without dragging in the rest of the package.
    add_file(
        pkg_dir / "execution" / "mapreduce" / "metrics_io.py",
        "hpc_agent/execution/mapreduce/metrics_io.py",
    )
    add_file(pkg_dir / "executor_cli.py", "hpc_agent/executor_cli.py")

    # Framework executor inside .hpc/.
    add_file(pkg_dir / "execution" / "mapreduce" / "dispatch.py", ".hpc/_hpc_dispatch.py")

    # Per-scheduler cpu/gpu array scripts, RENDERED from the profile rather
    # than shipped verbatim. Remote paths are preserved exactly
    # (``.hpc/templates/cpu_array.{sh,slurm}`` etc.) so downstream submit code
    # keeps resolving them. Deploy only the cluster's own family when
    # *scheduler* is known; fall back to sge+slurm otherwise. A single-family
    # deploy is what keeps pbspro/torque (shared ``.pbs`` ext) from colliding.
    schedulers = (scheduler,) if scheduler else ("sge", "slurm")
    for sched in schedulers:
        backend_cls = get_backend_class(sched)
        ext = template_ext_for(sched).lstrip(".")
        # ``mpi`` (single multi-rank job, #293) ships alongside the cpu/gpu
        # array bodies so a submit with an ``mpi`` block finds its template.
        for basename, kind in (("cpu_array", "cpu"), ("gpu_array", "gpu"), ("mpi", "mpi")):
            add_text(backend_cls.render_script(kind=kind), f".hpc/templates/{basename}.{ext}")

    # Shared preambles sourced by the templates above.
    for common_name in ("hpc_preamble.sh", "gpu_preamble.sh"):
        add_file(
            pkg_dir / "execution" / "mapreduce" / "templates" / "runtime" / "common" / common_name,
            f".hpc/templates/common/{common_name}",
        )

    # Combiner inside .hpc/.
    add_file(pkg_dir / "execution" / "mapreduce" / "combiner.py", ".hpc/_hpc_combiner.py")

    # Status REPORTER + its stdlib-only EAGER (import-time) closure (#349).
    #
    # The reporter runs cluster-side as
    # ``python -m hpc_agent.execution.mapreduce.reduce.status`` (reconcile's
    # remote_activation path, 0.10.12). Shipping its module-load closure here
    # lets the *deployed* copy satisfy that import under any python, so the
    # framework's runtime no longer needs a full ``hpc_agent`` in the job
    # conda env. This is ADDITIVE: the deployed tree is a PEP 420 namespace
    # package (no ``__init__.py``), so when the env DOES carry a regular
    # ``hpc_agent`` install it wins by namespace-package precedence and these
    # files are inert.
    #
    # SCOPE: only the *eager* (module-load) closure is shipped — every module
    # ``status`` imports at top level, transitively. It is stdlib-only
    # (verified by tests/contracts/test_cluster_runtime_self_contained.py,
    # which imports the reporter under ``python -S`` with the installed
    # ``hpc_agent`` invisible). The reporter's *function-local* runtime
    # closure (``state.runs``, ``infra.backends``, ``infra.clusters``,
    # ``recovery.registry``, and ``hpc_agent/__init__.py`` via
    # ``from hpc_agent import load_tasks_module``) pulls in pydantic / yaml /
    # jsonschema and is deliberately NOT shipped — those are the experiment
    # env's job, and flipping the env to python-only is the separate,
    # cluster-gated half of #349.
    reporter_closure = (
        # The reporter entry module itself.
        ("execution/mapreduce/reduce/status.py", "hpc_agent/execution/mapreduce/reduce/status.py"),
        # Eager intra-package deps of status (all stdlib-only):
        ("execution/mapreduce/reduce/rollup.py", "hpc_agent/execution/mapreduce/reduce/rollup.py"),
        ("_kernel/contract/task_id.py", "hpc_agent/_kernel/contract/task_id.py"),
        ("_kernel/contract/vocabulary.py", "hpc_agent/_kernel/contract/vocabulary.py"),
        ("errors.py", "hpc_agent/errors.py"),
        ("infra/time.py", "hpc_agent/infra/time.py"),
        # The #159 import-sanity guard the reporter's _main() invokes (after
        # arg-parse, so ``--help`` never reaches it). Stdlib-only; shipped so
        # a real reporter run resolves it from the deployed copy too.
        ("execution/mapreduce/_guard.py", "hpc_agent/execution/mapreduce/_guard.py"),
    )
    for src_rel, dst_rel in reporter_closure:
        add_file(pkg_dir / Path(src_rel), dst_rel)
    return items


def _local_deploy_manifest(*, scheduler: str | None) -> dict[str, Any]:
    """The deploy-cache manifest the CURRENT local sources would produce.

    ``{"pkg_version": <version>, "files": {dst_rel: sha256, ...}}`` — exactly
    what :func:`deploy_runtime` writes to :data:`_DEPLOY_MANIFEST_REL` after a
    deploy. Comparing it against the manifest read back from the cluster is
    how the cache decides which files (if any) actually need re-shipping.
    """
    items = _build_deploy_items(scheduler=scheduler)
    return {
        "pkg_version": _pkg_version(),
        "files": {it.dst_rel: it.sha for it in items},
    }


def _parse_remote_manifest(stdout: str) -> dict[str, Any] | None:
    """Parse the cluster-side deploy manifest, or ``None`` on any problem.

    A missing file (``cat`` printed nothing), truncated/corrupt JSON, or a
    shape that isn't ``{"files": {...}}`` all collapse to ``None`` — which
    :func:`deploy_runtime` treats as a full cache miss (re-deploy everything),
    the safe fallback the issue's risk note calls for (mitigation b).
    """
    raw = (stdout or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and isinstance(data.get("files"), dict):
        return data
    return None
