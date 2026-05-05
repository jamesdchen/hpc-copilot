"""Per-experiment axes config + cold-start axis picker.

Lives at ``<experiment>/.hpc/axes.yaml``, a one-line file in the common
case::

    axes_schema_version: 1
    homogeneous_axes: [window]

The agent writes this once at deploy time (during ``setup_hpc`` or
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

import jsonschema
import yaml

__all__ = [
    "AXES_FILENAME",
    "AXES_SCHEMA_VERSION",
    "axes_path",
    "axes_schema",
    "compute_wave_map",
    "pick_array_axis",
    "pick_array_axis_warm",
    "read_axes",
    "validate_axes",
    "write_axes",
]

AXES_SCHEMA_VERSION: int = 1
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
    jsonschema.validate(instance=data, schema=axes_schema())


def write_axes(
    experiment_dir: Path | str,
    *,
    axes: list[dict[str, Any]] | None = None,
    homogeneous_axes: list[str] | None = None,
) -> Path:
    """Write the axes config atomically and return its path.

    Cross-validation: if both *axes* and *homogeneous_axes* are supplied,
    every name in *homogeneous_axes* must appear in *axes*; otherwise a
    :class:`ValueError` is raised before any file write.
    """
    payload: dict[str, Any] = {"axes_schema_version": AXES_SCHEMA_VERSION}
    if axes is not None:
        payload["axes"] = [dict(a) for a in axes]
    if homogeneous_axes is not None:
        payload["homogeneous_axes"] = list(homogeneous_axes)
    if axes is not None and homogeneous_axes:
        axis_names = {a["name"] for a in axes}
        unknown = [n for n in homogeneous_axes if n not in axis_names]
        if unknown:
            raise ValueError(f"homogeneous_axes references axes not in axes list: {unknown}")
    validate_axes(payload)
    target = axes_path(experiment_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
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
        raise ValueError(f"{path}: top-level YAML must be a mapping, got {type(data).__name__}")
    validate_axes(data)
    return data


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

    Reads runtime samples written by :mod:`claude_hpc.state.runtime_prior`,
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

    .. note::

       Inert until cluster-side dispatcher writes ``axis_bindings`` into
       runtime samples — see follow-up TODO. The function is wired in
       so callers can integrate now; it returns ``(None, "...")`` until
       samples grow the field.
    """
    config = read_axes(experiment_dir)
    if config is None or not config.get("axes"):
        return None, "no axes.yaml or no axes enumeration"

    try:
        from claude_hpc._internal.layout import RepoLayout
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
) -> dict[int, list[int]]:
    """Build a wave_map keyed by wave_id, valued by task_ids.

    Reads ``axes.yaml``'s ordered ``axes`` list and computes a wave per
    cross-product of the non-picked axes. Within each wave, ``task_ids``
    enumerate the picked axis. The cartesian-product convention is
    last-axis-varies-fastest (numpy / row-major)::

        task_id = sum(coords[i] * prod(sizes[i+1:]) for i in range(len(axes)))

    If ``axes.yaml`` is absent or has no ``axes`` enumeration, raises
    :class:`ValueError` — the caller (typically submit-flow) is expected
    to have already verified the file is present before calling.
    """
    from itertools import product as _product

    config = read_axes(experiment_dir)
    if config is None:
        raise ValueError("axes.yaml not found")
    axes = config.get("axes")
    if not axes:
        raise ValueError("axes.yaml has no 'axes' enumeration")

    names = [a["name"] for a in axes]
    sizes = [int(a["size"]) for a in axes]
    if picked_axis not in names:
        raise ValueError(f"picked_axis {picked_axis!r} not in axes {names!r}")
    picked_idx = names.index(picked_axis)

    # Strides for row-major task_id encoding.
    strides = [1] * len(axes)
    for i in range(len(axes) - 2, -1, -1):
        strides[i] = strides[i + 1] * sizes[i + 1]

    other_indices = [i for i in range(len(axes)) if i != picked_idx]
    other_sizes = [sizes[i] for i in other_indices]

    wave_map: dict[int, list[int]] = {}
    for wave_id, other_combo in enumerate(_product(*[range(s) for s in other_sizes])):
        coords = [0] * len(axes)
        for k, idx in enumerate(other_indices):
            coords[idx] = other_combo[k]
        task_ids: list[int] = []
        for picked_val in range(sizes[picked_idx]):
            coords[picked_idx] = picked_val
            tid = sum(c * strides[i] for i, c in enumerate(coords))
            task_ids.append(tid)
        wave_map[wave_id] = sorted(task_ids)

    return wave_map
