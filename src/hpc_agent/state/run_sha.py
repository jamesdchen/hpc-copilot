"""Pure SHA computations over the user's ``tasks.py`` and materialized tasks.

Extracted from :mod:`hpc_agent.state.runs` so the run-sidecar lifecycle
module can stay focused on path helpers, sidecar I/O, and lifecycle
(find / prune / update). The two functions here are pure: given a
loaded ``tasks_module`` (or a path), they hash and return.

Callers import these directly from this module (see the pointer comment
in :mod:`hpc_agent.state.runs`).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "RESERVED_TASK_KEYS",
    "compose_node_sha",
    "compute_cmd_sha",
    "compute_data_sha",
    "compute_env_hash",
    "compute_tasks_py_sha",
]

# Per-task keys that ``resolve(i)`` may return but that are EXCLUDED from
# the cmd_sha hash. These carry strategy bookkeeping — an opaque
# ``trial_token`` a closed-loop optimizer round-trips through ``resolve()``
# to reconcile a result back to the proposal that produced it — rather than
# a swept experiment parameter. Folding them into cmd_sha would change the
# experiment's parameter identity and bust dedup (the #207 boundary: cmd_sha
# is parameter identity, not bookkeeping), so they are stripped before
# hashing. Dedup across deliberately-repeated campaign iterations is handled
# separately by the campaign-iteration rejection in
# :func:`hpc_agent.state.runs.find_run_by_cmd_sha`.
RESERVED_TASK_KEYS: frozenset[str] = frozenset({"trial_token"})


def compute_cmd_sha(tasks_module: Any) -> str:
    """Materialize the task list and return a deterministic SHA-256.

    Imports the user's ``tasks.py`` module (already loaded by the caller),
    calls ``total()``, then ``resolve(i)`` for every ``i`` in
    ``range(total())``. Each kwargs dict is normalized to sorted-keys JSON
    and the lines are joined with ``\\n`` before hashing. The resulting
    digest is stable across equivalent task lists and changes whenever any
    kwarg dict changes.

    Returns a 64-char hex string.

    Semantics — ``cmd_sha`` IS THE PARAMETER IDENTITY OF THE EXPERIMENT,
    NOT ITS CODE IDENTITY (#207). The hashed material is exclusively the
    materialized per-task kwargs (``[resolve(i) for i in range(total())]``);
    it deliberately does NOT fold in the executor's source, the rendered
    job command, or ``tasks.py``'s bytes. The design rationale: the swept
    parameters DEFINE the experiment, while the executor body is treated as
    provenance, captured separately as ``tasks_py_sha`` on the run sidecar
    (see :func:`compute_tasks_py_sha`). Two consequences follow directly:

    * Editing an executor's body (a bug fix, a refactor, a changed model
      hyperparameter that is NOT one of the swept ``resolve`` kwargs) while
      leaving every materialized kwargs dict unchanged produces the SAME
      ``cmd_sha``. A re-submit therefore dedups against the prior run and
      runs the OLD code forward — by design, the params say "same
      experiment".
    * To make that code edit force a fresh run, the caller must opt in:
      ``find_run_by_cmd_sha(..., tasks_py_sha=<current>,
      invalidate_on_code_change=True)`` folds the recorded ``tasks_py_sha``
      into the dedup decision for that one submit. The default path is
      unchanged — params alone still key the dedup, and a code edit with
      unchanged params still dedups (with only a drift warning, see
      :func:`find_run_by_cmd_sha`).

    Raises
    ------
    AttributeError
        If *tasks_module* lacks ``total`` or ``resolve``.
    TypeError
        If ``resolve(i)`` does not return a dict.
    """
    n = int(tasks_module.total())
    parts: list[str] = []
    for i in range(n):
        kwargs = tasks_module.resolve(i)
        if not isinstance(kwargs, dict):
            raise TypeError(f"tasks.resolve({i}) must return a dict, got {type(kwargs).__name__}")
        # Strip reserved bookkeeping keys (e.g. ``trial_token``) so they do
        # not change parameter identity / bust dedup. A copy is hashed; the
        # caller's dict is left untouched (the dispatcher still exports the
        # reserved keys as ``HPC_KW_*`` for the executor to read).
        hashable = {k: v for k, v in kwargs.items() if k not in RESERVED_TASK_KEYS}
        parts.append(json.dumps(hashable, sort_keys=True, separators=(",", ":")))
    joined = "\n".join(parts).encode()
    return hashlib.sha256(joined).hexdigest()


def compute_tasks_py_sha(tasks_py_path: Path) -> str:
    """Return SHA-256 of ``tasks.py``'s bytes — diagnostic only."""
    return hashlib.sha256(Path(tasks_py_path).read_bytes()).hexdigest()


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def compose_node_sha(cmd_sha: str, parent_node_shas: list[str]) -> str:
    """Compose a run's parameter identity with its parents' identities.

    The recursive-identity invariant for inter-run dependency (the
    ``docs/design/dag-kernel.md`` prototype): when run B consumes run
    A's outputs, B's dedup identity must change whenever A's does —
    otherwise a re-submit of B dedups against a result computed from a
    *different* A, silently. ``compose_node_sha`` is the Merkle step that
    makes identity compose::

        node_sha = H(canonical({"node": cmd_sha, "parents": sorted(set(parents))}))

    Properties (pinned in ``tests/state/test_node_sha_properties.py``):

    * **Zero parents degenerates to ``cmd_sha`` verbatim.** Every existing
      run is a 0-parent node whose identity is unchanged — today's
      dedup/journal keys need no migration.
    * **Parents are a set.** Order-invariant, duplicate-insensitive: an
      edge declares "depends on", not a sequence.
    * **Ancestor changes propagate.** A changed grandparent changes the
      parent's node_sha and therefore this node's — stale-subgraph reuse
      cannot pass a node_sha equality check.

    Like :func:`compute_cmd_sha`, this is parameter identity, not code
    identity (#207): the parent digests fold in the parents' *params*,
    never their executor bytes. Wired in via
    :func:`hpc_agent.state.runs.resolve_node_sha` (submit-side derivation
    from parents' sidecars) and the ``node_sha`` lever on
    :func:`hpc_agent.state.runs.find_run_by_cmd_sha` (effective-identity
    dedup); a submit that declares no ``parents`` never reaches the
    composed branch.

    Raises :class:`ValueError` if *cmd_sha* or any parent is not a 64-char
    lowercase hex digest — these strings are produced by this module's own
    hash functions, so a malformed one is a caller bug worth failing loud on.
    """
    if not _SHA256_HEX.match(cmd_sha):
        raise ValueError(f"compose_node_sha: cmd_sha is not a sha256 hex digest: {cmd_sha!r}")
    parents = sorted(set(parent_node_shas))
    for p in parents:
        if not _SHA256_HEX.match(p):
            raise ValueError(f"compose_node_sha: parent node_sha is not a sha256 hex digest: {p!r}")
    if not parents:
        return cmd_sha
    envelope = json.dumps(
        {"node": cmd_sha, "parents": parents}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(envelope.encode()).hexdigest()


# Chunk size for streaming a file's bytes through the digest. 1 MiB keeps a
# large parquet/csv input off the heap (we never materialize the whole file)
# while staying well above the per-read syscall floor.
_DATA_HASH_CHUNK = 1 << 20


def _dvc_pointer_md5(dvc_path: Path) -> str | None:
    """Return the recorded md5 from a ``<file>.dvc`` pointer, or ``None``.

    A DVC-tracked input ``data/train.parquet`` is committed to git as a
    small YAML pointer ``data/train.parquet.dvc`` whose ``outs[0].md5`` is
    the content hash DVC already computed at ``dvc add`` time. Reading that
    pointer is the cheap, exact data-identity DVC was designed to give us —
    no need to re-hash a multi-GB file (and the real bytes may live only in
    the DVC cache / remote, not on disk). Best-effort: any malformed /
    unreadable pointer returns ``None`` so the caller falls back to a
    content-hash of the path itself.

    DVC is an OPTIONAL dependency — we never import it. The ``.dvc`` file is
    plain YAML, so we read it with the ``yaml`` lib the package already
    depends on. ``md5`` is DVC's default; for a directory output DVC stores
    a ``<hash>.dir`` md5 over the directory manifest, which is equally
    stable, so we surface whatever ``md5`` the pointer carries verbatim.
    """
    try:
        import yaml  # type: ignore[import-untyped]  # noqa: PLC0415 — pkg dep, lazy
    except ImportError:  # pragma: no cover — yaml is a hard package dep
        return None
    try:
        loaded = yaml.safe_load(dvc_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None
    if not isinstance(loaded, Mapping):
        return None
    outs = loaded.get("outs")
    if not isinstance(outs, list) or not outs:
        return None
    first = outs[0]
    if not isinstance(first, Mapping):
        return None
    md5 = first.get("md5")
    return str(md5) if md5 else None


def _content_hash_file(path: Path) -> str:
    """Stream a single file's bytes through SHA-256. Caller guarantees it
    is a regular file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(_DATA_HASH_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_data_sha(input_paths: Iterable[str | Path], *, base_dir: Path | None = None) -> str:
    """Return a deterministic SHA-256 over a run's declared input dataset(s).

    ``cmd_sha``/``tasks_py_sha`` capture parameter and code identity; this
    captures DATA identity — the third leg of "reconstruct exactly what
    produced this result" (#222). For each declared input path, in the order
    the caller passes them:

    * If a sibling DVC pointer ``<path>.dvc`` exists, its recorded
      ``outs[0].md5`` is used — DVC already content-hashed the data at
      ``dvc add`` time, and the real bytes may live only in the DVC cache,
      so re-hashing the working-tree file would be wrong or impossible.
    * Otherwise the file's raw bytes are streamed through SHA-256.
    * A path that is neither a file nor a DVC pointer (missing, or a bare
      directory with no ``.dvc``) contributes the sentinel ``"absent"`` — we
      record that the declared input was not resolvable rather than raising,
      so a provenance manifest can still be emitted (the absence IS the
      provenance fact). Directories are intentionally NOT walked: a stable
      directory hash is DVC's job, and silently hashing a tree's first level
      would be a misleading half-measure.

    Each path contributes a line ``<relpath>\\t<per-path-hash>`` (relpath is
    relative to *base_dir* when given, else the path verbatim, so the digest
    is stable across machines that mount the experiment at different roots),
    the lines are sorted-by-relpath and ``\\n``-joined, and the join is
    hashed. Sorting makes the digest independent of declaration order;
    embedding the relpath makes ``{a: H, b: K}`` distinguishable from
    ``{a: K, b: H}``.

    Returns a 64-char hex string. An empty *input_paths* returns the SHA-256
    of the empty string (the well-defined "no declared data" identity).
    """
    lines: list[str] = []
    for raw in input_paths:
        p = Path(raw)
        resolved = p if p.is_absolute() else ((base_dir / p) if base_dir else p)
        # Key the line on the path AS DECLARED (relative when possible) so
        # the digest doesn't drift with the absolute mount point.
        if base_dir is not None and resolved.is_absolute():
            try:
                rel = str(resolved.relative_to(Path(base_dir).resolve()))
            except ValueError:
                rel = str(p)
        else:
            rel = str(p)
        dvc_pointer = resolved.with_name(resolved.name + ".dvc")
        per_path: str
        dvc_md5 = _dvc_pointer_md5(dvc_pointer) if dvc_pointer.is_file() else None
        if dvc_md5 is not None:
            per_path = f"dvc:{dvc_md5}"
        elif resolved.is_file():
            per_path = f"sha256:{_content_hash_file(resolved)}"
        else:
            per_path = "absent"
        lines.append(f"{rel}\t{per_path}")
    lines.sort()
    joined = "\n".join(lines).encode()
    return hashlib.sha256(joined).hexdigest()


def compute_env_hash(
    *,
    modules: Iterable[str] | None = None,
    conda_source: str | None = None,
    conda_envs: Iterable[str] | None = None,
    runtime: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> str:
    """Return a deterministic SHA-256 over the resolved execution environment.

    Captures ENVIRONMENT identity — the modules / conda source / conda envs
    a result was actually produced under, plus the ``HPC_RUNTIME`` selector
    (``uv`` or unset). Pairs with ``cmd_sha`` (params), ``tasks_py_sha``
    (code), and ``compute_data_sha`` (data) to make a result fully
    reconstructible (#222).

    The hashed material is a canonical sorted-keys JSON object of exactly the
    activation inputs the cluster preamble consumes (``$MODULES`` /
    ``$CONDA_SOURCE`` / ``$CONDA_ENV`` — see
    :class:`hpc_agent.infra.clusters.Activation`) plus ``runtime``. ``modules``
    and ``conda_envs`` are ORDER-SENSITIVE (``module load`` / ``conda
    activate`` apply in sequence and order changes the resolved env), so they
    are NOT sorted. ``extra`` is an open escape hatch for additional resolved
    facts a caller wants to fold in (e.g. a measured python/cuda version);
    its keys ARE sorted. Empty / unset fields are normalized to ``""`` / ``[]``
    so an all-unset env still hashes to a stable, recognizable value rather
    than raising.

    Returns a 64-char hex string.
    """
    payload: dict[str, Any] = {
        "modules": [str(m) for m in (modules or [])],
        "conda_source": str(conda_source or ""),
        "conda_envs": [str(e) for e in (conda_envs or [])],
        "runtime": str(runtime or ""),
    }
    if extra:
        payload["extra"] = {str(k): extra[k] for k in sorted(extra)}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(canonical).hexdigest()
