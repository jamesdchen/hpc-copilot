"""Per-experiment axes config + cold-start axis picker.

Lives at ``<experiment>/.hpc/axes.yaml``, a one-line file in the common
case::

    axes_schema_version: 1
    homogeneous_axes: [window]

The agent writes this once at deploy time (during ``setup-hpc`` or
``hpc-build-executor``); the framework reads it at submit time to
decide which axis becomes the task array. Field-mirror discipline:
the framework only stores fields it can independently act on, so
:data:`homogeneous_axes` is the only signal here. Experiment-specific
reasoning about WHY an axis is homogeneous lives in the agent's chat
context for that repo.

Two-path picker:

* **Warm** — when runtime priors exist for this ``cmd_sha``, the picker
  computes coefficient-of-variation per axis and picks the lowest-CV
  one (planned; not yet implemented).
* **Cold** — when no priors exist, fall back to the first axis in
  :data:`homogeneous_axes` that appears in ``tasks.py``'s ``AXES``
  declaration. This module implements the cold path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from hpc_agent import errors

__all__ = [
    "AXES_FILENAME",
    "AXES_SCHEMA_VERSION",
    "axes_path",
    "axes_schema",
    "compute_wave_map",
    "derive_stride_subset",
    "pick_array_axis",
    "pick_array_axis_warm",
    "read_axes",
    "read_executor",
    "upsert_executor",
    "validate_axes",
    "write_axes",
]

# v2 (additive over v1): adds the optional ``executors`` block — the
# classified DataAxis per @register_run function. Every write the
# framework makes now stamps version 2; v1 files on disk still validate.
AXES_SCHEMA_VERSION: int = 2
AXES_FILENAME: str = "axes.yaml"

_SCHEMA_PATH: Path = Path(__file__).resolve().parent.parent / "schemas" / "axes.json"


def axes_schema() -> dict[str, Any]:
    """Load and return the axes JSON Schema as a dict."""
    data: dict[str, Any] = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return data


def axes_path(experiment_dir: Path | str) -> Path:
    """Return ``<experiment_dir>/.hpc/axes.yaml`` (does not create it)."""
    return Path(experiment_dir) / ".hpc" / AXES_FILENAME


def validate_axes(data: dict[str, Any]) -> None:
    """Validate *data* against ``schemas/axes.json``.

    Raises :class:`jsonschema.ValidationError` on any schema violation.
    """
    from hpc_agent._kernel.contract.schema import validate as _validate

    _validate(data, axes_schema())


def write_axes(
    experiment_dir: Path | str,
    *,
    axes: list[dict[str, Any]] | None = None,
    homogeneous_axes: list[str] | None = None,
    executors: dict[str, dict[str, Any]] | None = None,
) -> Path:
    """Write the axes config atomically and return its path.

    Cross-validation: every name in *homogeneous_axes* must appear in
    the axes list — whether that list is supplied in this call or
    already on disk from a prior write. The on-disk fallback closes a
    silent-corruption window where a write that only supplied
    *homogeneous_axes* could replace the file with names that don't
    reference any declared axis. :class:`ValueError` is raised before
    any file write (atomic-write contract preserved).

    *executors* is the v2 classified-DataAxis block — a map from
    ``@register_run`` function name to its executor entry. Callers that
    only want to update one entry without clobbering the rest of the
    file should use :func:`upsert_executor` instead, which round-trips
    the existing ``axes`` / ``homogeneous_axes`` / ``executors`` fields.
    """
    payload: dict[str, Any] = {"axes_schema_version": AXES_SCHEMA_VERSION}
    if axes is not None:
        payload["axes"] = [dict(a) for a in axes]
    if homogeneous_axes is not None:
        payload["homogeneous_axes"] = list(homogeneous_axes)
    if executors is not None:
        payload["executors"] = {k: dict(v) for k, v in executors.items()}
    if axes is not None and homogeneous_axes:
        axis_names = {a["name"] for a in axes}
        unknown = [n for n in homogeneous_axes if n not in axis_names]
        if unknown:
            raise errors.SpecInvalid(
                f"homogeneous_axes references axes not in axes list: {unknown}"
            )
    elif axes is None and homogeneous_axes:
        # On-disk fallback: cross-validate against whatever axes the
        # existing file declared. If there is no file (or no axes
        # enumeration), we can't validate names → accept the write so
        # bootstrap flows (homogeneous_axes-only writes before axes are
        # known) still succeed. Read failures (corruption / schema
        # violation) are surfaced loudly by read_axes itself.
        import jsonschema

        try:
            existing = read_axes(experiment_dir)
        except (ValueError, OSError, yaml.YAMLError, jsonschema.ValidationError, errors.HpcError):
            existing = None
        if existing is not None:
            existing_axes = existing.get("axes") or []
            if existing_axes:
                axis_names = {a["name"] for a in existing_axes}
                unknown = [n for n in homogeneous_axes if n not in axis_names]
                if unknown:
                    raise errors.SpecInvalid(
                        f"homogeneous_axes references axes not in axes list: {unknown}"
                    )
    validate_axes(payload)
    target = axes_path(experiment_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Atomic + durable write — fixed-name ``.tmp`` sibling is replaced
    # with a mkstemp-allocated unique name (two concurrent writes to
    # the same axes.yaml don't collide on the tmp), and an explicit
    # ``fsync`` of both file and parent dir keeps the rename durable
    # across a kernel panic / power loss. Mirrors
    # ``infra.io.atomic_write_json``'s recipe but for YAML output.
    import contextlib as _contextlib
    import os as _os
    import tempfile as _tempfile

    serialized = yaml.safe_dump(payload, sort_keys=True)
    fd, tmp_name = _tempfile.mkstemp(
        prefix=target.name + ".",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with _os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(serialized)
            fh.flush()
            with _contextlib.suppress(OSError):
                _os.fsync(fh.fileno())
        _os.replace(tmp_name, target)
        try:
            dir_fd = _os.open(str(target.parent), _os.O_RDONLY)
            try:
                with _contextlib.suppress(OSError):
                    _os.fsync(dir_fd)
            finally:
                _os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        with _contextlib.suppress(OSError):
            _os.unlink(tmp_name)
        raise
    return target


def read_axes(experiment_dir: Path | str) -> dict[str, Any] | None:
    """Read and validate the axes config; return ``None`` if absent.

    Raises :class:`jsonschema.ValidationError` if the on-disk file
    violates the schema (corruption surfaces loudly).
    """
    path = axes_path(experiment_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise errors.JournalCorrupt(
            f"{path}: top-level YAML must be a mapping, got {type(data).__name__}"
        )
    validate_axes(data)
    return data


def read_executor(experiment_dir: Path | str, run_name: str) -> dict[str, Any] | None:
    """Return the stored ``executors.<run_name>`` entry, or ``None`` if absent.

    The entry is the classified-DataAxis record written by the
    ``classify-axis`` primitive: ``{run_signature_sha, data_axis,
    classified_by, classified_at}``. ``None`` covers both "no axes.yaml"
    and "axes.yaml has no executors block / no entry for this run".
    """
    config = read_axes(experiment_dir)
    if config is None:
        return None
    entry = (config.get("executors") or {}).get(run_name)
    return dict(entry) if isinstance(entry, dict) else None


def upsert_executor(
    experiment_dir: Path | str,
    run_name: str,
    *,
    executor_entry: dict[str, Any],
) -> Path:
    """Merge one classified-DataAxis entry into ``axes.yaml``; return its path.

    Reads the existing config, replaces (or inserts) ``executors.<run_name>``
    with *executor_entry*, and writes the whole file back — so the
    ``axes`` / ``homogeneous_axes`` scheduling hints and every other
    executor entry round-trip untouched. The merged payload is validated
    against the v2 schema before any disk write.
    """
    existing = read_axes(experiment_dir) or {}
    executors = {k: dict(v) for k, v in (existing.get("executors") or {}).items()}
    executors[run_name] = dict(executor_entry)
    return write_axes(
        experiment_dir,
        axes=existing.get("axes"),
        homogeneous_axes=existing.get("homogeneous_axes"),
        executors=executors,
    )


def pick_array_axis(
    experiment_dir: Path | str,
    *,
    available_axes: list[str] | None = None,
    profile: str | None = None,
    cluster: str | None = None,
) -> tuple[str | None, str]:
    """Cold-start axis picker — return ``(axis_name, reason)``.

    *axis_name* is the axis to promote onto the task array, or
    ``None`` when no decision can be made (the caller — typically the
    submit-flow / agent — falls back to asking the user).

    *available_axes*, if supplied, is the list of axis names from the
    user's ``tasks.py`` ``AXES`` declaration. The picker only returns
    names that appear in this list; entries in ``axes.yaml`` that don't
    match are silently skipped (a separate validator should warn about
    those at deploy time).

    *profile* / *cluster*, if supplied, scope the warm-path CV to a
    single ``runtimes/<profile>.<cluster>.json`` file — the right call
    at submit time when we know which combination is queueing. Omitted,
    the warm picker aggregates across every runtime file under the
    experiment, which can mix apples-to-oranges runtimes (different
    queue, different GPU) and produce misleading CV.

    Tries the warm path first (lowest-CV from runtime priors); falls
    back to the cold path (first homogeneous_axes entry) when warm
    has insufficient signal. The two-path behavior is silent: callers
    don't choose; the picker upgrades automatically as samples
    accumulate.
    """
    # Warm path: prefer observed CV when we have data.
    warm_name, _ = pick_array_axis_warm(experiment_dir, profile=profile, cluster=cluster)
    if warm_name is not None and (available_axes is None or warm_name in available_axes):
        return warm_name, f"warm-path lowest-CV axis ({warm_name!r})"
    # Warm picked None or an axis we can't honor; fall through to cold.

    # Cold path.
    config = read_axes(experiment_dir)
    if config is None:
        return None, "no axes.yaml"
    homogeneous = config.get("homogeneous_axes") or []
    if not homogeneous:
        return None, "homogeneous_axes is empty"
    if available_axes is None:
        chosen = homogeneous[0]
        return chosen, f"cold-path first homogeneous_axes entry ({chosen!r})"
    for name in homogeneous:
        if name in available_axes:
            return (
                name,
                f"cold-path first homogeneous_axes entry that matches tasks.py AXES ({name!r})",
            )
    return (
        None,
        f"no homogeneous_axes entry matches available axes {available_axes!r}",
    )


def pick_array_axis_warm(
    experiment_dir: Path | str,
    *,
    cmd_sha: str | None = None,
    profile: str | None = None,
    cluster: str | None = None,
    min_samples: int = 5,
) -> tuple[str | None, str]:
    """Warm-path picker — pick the lowest-CV axis from runtime priors.

    Reads runtime samples written by :mod:`hpc_agent.state.runtime_prior`,
    filters to those carrying a non-empty ``axis_bindings`` field (added
    when the cluster-side dispatcher records per-task axis values), groups
    by axis, and returns the axis name with the lowest coefficient of
    variation (stddev / mean of elapsed_sec). Falls back to
    ``(None, reason)`` when fewer than *min_samples* qualifying samples
    exist or no axis can be evaluated.

    *profile* / *cluster* scope the search to a single
    ``runtimes/<profile>.<cluster>.json`` file — recommended at submit
    time so CV isn't mixed across queues / GPU types. Omitted, the
    picker aggregates across every runtime file under the experiment;
    that's fine for an experiment running on one cluster but can produce
    apples-to-oranges CV when multiple clusters share an experiment.

    Pipeline that populates the samples this picker reads:

    1. Cluster-side dispatcher (``mapreduce/dispatch.py``) writes
       ``<result_dir>/_runtime.json`` per task with timing + axis_bindings.
    2. Cluster-side combiner aggregates them into
       ``_combiner/wave_<N>.runtime.json`` per wave.
    3. Local-side ``aggregate_flow`` rsync_pulls ``_combiner/`` and calls
       :func:`hpc_agent.state.runtime_prior.ingest_runtime_samples_from_combiner_dir`,
       which appends the rows to ``runtimes/<profile>.<cluster>.json``.

    The picker silently falls back to the cold path when no qualifying
    samples exist yet (first submit on a fresh experiment).
    """
    config = read_axes(experiment_dir)
    if config is None or not config.get("axes"):
        return None, "no axes.yaml or no axes enumeration"

    try:
        from hpc_agent._kernel.contract.layout import RepoLayout
    except ImportError:
        return None, "runtime_prior not importable"

    layout = RepoLayout(Path(experiment_dir))
    runtimes_dir = layout.runtimes
    samples: list[dict[str, Any]] = []
    if profile is not None and cluster is not None:
        # Scoped lookup — single file, no glob.
        target = layout.runtime_prior(profile, cluster)
        files: list[Path] = [target] if target.is_file() else []
    elif runtimes_dir.is_dir():
        files = sorted(runtimes_dir.glob("*.json"))
    else:
        files = []
    for runtime_file in files:
        try:
            doc = json.loads(runtime_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(doc, dict):
            continue
        for s in doc.get("samples") or []:
            if isinstance(s, dict):
                samples.append(s)
    if cmd_sha is not None:
        samples = [s for s in samples if s.get("cmd_sha") == cmd_sha]

    qualifying = [
        s
        for s in (samples or [])
        if isinstance(s, dict)
        and isinstance(s.get("axis_bindings"), dict)
        and len(s["axis_bindings"]) > 0
        and isinstance(s.get("elapsed_sec"), (int, float))
        and int(s.get("exit_code", 1)) == 0
    ]
    if len(qualifying) < min_samples:
        return None, f"only {len(qualifying)} qualifying samples (< {min_samples})"

    axis_names = [a["name"] for a in config["axes"]]
    cv_per_axis: dict[str, float] = {}
    for name in axis_names:
        # Group elapsed_sec by this axis's value, holding others fixed.
        # Implementation: bucket by all axis values *except* this one;
        # within each bucket compute CV across this axis; average.
        buckets: dict[tuple[Any, ...], list[float]] = {}
        for s in qualifying:
            bindings = s["axis_bindings"]
            if name not in bindings:
                continue
            other_key = tuple(sorted((k, v) for k, v in bindings.items() if k != name))
            buckets.setdefault(other_key, []).append(float(s["elapsed_sec"]))
        if not buckets:
            continue
        cvs: list[float] = []
        for values in buckets.values():
            if len(values) < 2:
                continue
            mean = sum(values) / len(values)
            if mean <= 0:
                continue
            var = sum((v - mean) ** 2 for v in values) / len(values)
            cvs.append((var**0.5) / mean)
        if cvs:
            cv_per_axis[name] = sum(cvs) / len(cvs)

    if not cv_per_axis:
        return None, "no axis had >=2-sample buckets to compute CV"

    chosen = min(cv_per_axis, key=lambda k: cv_per_axis[k])
    return chosen, f"lowest CV ({cv_per_axis[chosen]:.4f}) of {len(cv_per_axis)} axes"


def compute_wave_map(
    experiment_dir: Path | str,
    *,
    picked_axis: str,
) -> dict[str, list[int]]:
    """Build a wave_map keyed by wave_id (stringified), valued by task_ids.

    Reads ``axes.yaml``'s ordered ``axes`` list and computes a wave per
    cross-product of the non-picked axes. Within each wave, ``task_ids``
    enumerate the picked axis. The cartesian-product convention is
    last-axis-varies-fastest (numpy / row-major)::

        task_id = sum(coords[i] * prod(sizes[i+1:]) for i in range(len(axes)))

    Keys are emitted as ``str`` to match the on-disk JSON shape used by
    the sidecar (JSON objects only allow string keys, so wave_maps that
    round-trip through disk are already coerced). Returning strings here
    means in-memory callers don't have to think about whether they're
    holding a freshly-computed or freshly-loaded map.

    If ``axes.yaml`` is absent or has no ``axes`` enumeration, raises
    :class:`ValueError` — the caller (typically submit-flow) is expected
    to have already verified the file is present before calling.
    """
    from itertools import product as _product

    config = read_axes(experiment_dir)
    if config is None:
        raise errors.SpecInvalid("axes.yaml not found")
    axes = config.get("axes")
    if not axes:
        raise errors.JournalCorrupt("axes.yaml has no 'axes' enumeration")

    names = [a["name"] for a in axes]
    sizes = [int(a["size"]) for a in axes]
    if picked_axis not in names:
        raise errors.SpecInvalid(f"picked_axis {picked_axis!r} not in axes {names!r}")
    picked_idx = names.index(picked_axis)

    # Strides for row-major task_id encoding.
    strides = [1] * len(axes)
    for i in range(len(axes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]

    other_indices = [i for i in range(len(axes)) if i != picked_idx]
    other_sizes = [sizes[i] for i in other_indices]

    wave_map: dict[str, list[int]] = {}
    for wave_id, other_combo in enumerate(_product(*[range(s) for s in other_sizes])):
        coords = [0] * len(axes)
        for k, idx in enumerate(other_indices):
            coords[idx] = other_combo[k]
        task_ids: list[int] = []
        for picked_val in range(sizes[picked_idx]):
            coords[picked_idx] = picked_val
            tid = sum(c * strides[i] for i, c in enumerate(coords))
            task_ids.append(tid)
        wave_map[str(wave_id)] = sorted(task_ids)

    return wave_map


def derive_stride_subset(experiment_dir: Path | str) -> list[int]:
    """Derive the partial-reproduction subset MECHANICALLY from the axes.

    The determinism-fingerprint design center 5 (derived subsets): the canary
    task (task 0) plus, for EACH axis, one task per distinct axis value at that
    axis's fixed row-major stride over its coordinate range. Concretely, holding
    every other axis at coordinate 0, axis ``i`` with size ``s_i`` contributes the
    task ids ``0, strides[i], 2*strides[i], … (s_i-1)*strides[i]`` — a fixed,
    reproducible stride over that axis's range. The union across axes (task 0 is
    shared by every axis's ``v=0`` term, so the canary is always present) is the
    subset.

    A PURE function of the axis structure — no importance sampling, no
    metric-aware / "representative" heuristic (the Q1 boundary flag). The
    row-major encoding is the SAME one :func:`compute_wave_map` uses
    (last-axis-varies-fastest), computed here from the same ``axes.yaml`` ordered
    ``axes`` list so there is one definition of the coordinate→task_id mapping.

    Raises :class:`errors.SpecInvalid` when ``axes.yaml`` is absent and
    :class:`errors.JournalCorrupt` when it declares no ``axes`` enumeration — the
    derived mode cannot invent a subset without the axis structure (the caller
    should supply an explicit ``task_sample`` list instead).
    """
    config = read_axes(experiment_dir)
    if config is None:
        raise errors.SpecInvalid(
            "derive_stride_subset: axes.yaml not found — the derived subset mode "
            "needs the axis structure. Supply an explicit task_sample list instead."
        )
    axes = config.get("axes")
    if not axes:
        raise errors.JournalCorrupt(
            "derive_stride_subset: axes.yaml has no 'axes' enumeration to derive a "
            "subset from. Supply an explicit task_sample list instead."
        )

    sizes = [int(a["size"]) for a in axes]
    # Row-major strides: last axis varies fastest (identical to compute_wave_map).
    strides = [1] * len(sizes)
    for i in range(len(sizes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]

    indices: set[int] = {0}  # the canary task (also every axis's v=0 term)
    for i, size in enumerate(sizes):
        for value in range(size):
            indices.add(value * strides[i])
    return sorted(indices)
