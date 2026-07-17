"""``scaffold-strategy`` primitive — materialize a closed-loop strategy.

Sibling of ``build-executor``: copies a *correctly-wired* campaign
strategy template (``execution/mapreduce/templates/scaffolds/{optuna,
pbt}_strategy.py``) into the experiment repo as ``.hpc/tasks.py`` (or a
caller-chosen path). The agent then customizes ONLY the search space —
never the ask/tell plumbing, the ``trial_token`` round-trip, or the
``_propose``/``resolve`` orchestrator-vs-compute split.

The whole point is correct-by-construction discovery: the strategy
contract (who runs ask/tell, what ``trial_token`` is for, how per-trial
metrics flow back) lives in a verb + a pointed-to doc
(``docs/primitives/scaffold-strategy.md`` and the ``hpc-campaign``
SKILL's contract section), so an agent never has to ``Read`` the
framework's ``optuna_strategy.py`` from site-packages to learn it.

Asset-copy pattern mirrors ``incorporation/build/template.py``: read the
template asset with an explicit UTF-8 codec (HPC nodes with ``LC_ALL=C``
would otherwise corrupt it) and ``write_text`` it into the experiment
repo. Refuses to overwrite an existing destination without ``--force`` —
a customized ``tasks.py`` is easy to wipe out otherwise.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import hpc_agent
from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

# (strategy name → template asset under templates/scaffolds/). Both
# templates are cluster-safe + load-idempotent by construction; the agent
# customizes only the search space. See the module docstrings of each and
# docs/design/campaign-seam.md for the contract they encode.
_STRATEGY_ASSETS: dict[str, str] = {
    "optuna": "optuna_strategy.py",
    "pbt": "pbt_strategy.py",
}

# Continuous-async-refill variants (#362): emitted when ``--async-refill`` is
# set. Only strategies that can propose distinctly under concurrency have one —
# optuna's ``constant_liar`` + tell-by-trial_token variant. PBT already batches a
# whole generation, so it needs no separate async asset (``--async-refill`` is a
# no-op for it). A strategy absent from this map falls back to its synchronous
# asset regardless of the flag.
_ASYNC_STRATEGY_ASSETS: dict[str, str] = {
    "optuna": "optuna_async_strategy.py",
}

# The shapes this verb can materialize. ``strategy`` is the original ask/tell
# closed-loop behaviour (``--name`` selects optuna|pbt); ``grid`` is the
# NON-adaptive fixed sweep (one config file per arm, no iteration-on-result
# dependency) — run-#10 live evidence hand-wired a 2-arm grid (two yaml stubs +
# a tasks.py branch + consumption-semantics archaeology) that this shape emits as
# a marked-hole skeleton instead.
_SHAPES: tuple[str, ...] = ("strategy", "grid")

# The grid skeleton's ``tasks.py`` asset (byte-faithful copy, like the strategy
# assets). The N per-arm config STUBS are parametric on the arm count, so they
# are generated in code (:func:`_materialize_grid`) rather than shipped as fixed
# assets.
_GRID_TASKS_ASSET = "grid_strategy.py"

# A grid is at least two arms — a one-arm "grid" is just a single run, so refuse
# it up front rather than emit a degenerate skeleton.
_GRID_MIN_ARMS = 2

# Per-arm config stubs land under ``<output_dir>/configs/`` (the sibling dir the
# grid tasks.py globs). Kept relative so the return payload is repo-anchored.
_GRID_CONFIG_DIR_REL = Path("configs")

# The materialized strategy IS the experiment's tasks.py — the framework
# imports it and calls total()/resolve() (and, on the orchestrator only,
# _propose()). The campaign loop reads it as `.hpc/tasks.py`.
_DEFAULT_DEST_REL = Path(".hpc") / "tasks.py"


def _scaffolds_dir() -> Path:
    """The bundled scaffolds asset dir (``templates/scaffolds/``)."""
    return Path(hpc_agent._PACKAGE_ROOT) / "execution" / "mapreduce" / "templates" / "scaffolds"


def _copy_asset(asset: str, dest: Path, *, force: bool) -> Path:
    """Copy a bundled scaffold *asset* to *dest* byte-faithfully.

    Pins UTF-8 on both ends (HPC nodes with ``LC_ALL=C`` / ``LANG=POSIX`` would
    otherwise decode/encode via the locale codec and corrupt the box-drawing
    comment rules). Refuses to overwrite an existing *dest* without *force*.
    Returns the source asset path (for the return payload).
    """
    src = _scaffolds_dir() / asset
    if not src.is_file():
        raise errors.SpecInvalid(f"scaffold template missing on disk: {src}")
    if dest.exists() and not force:
        raise errors.SpecInvalid(f"refusing to overwrite {dest}; pass --force to overwrite")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return src


def _arm_stem(index: int, width: int) -> str:
    """The zero-padded arm id (config file stem == the ``arm`` kwarg).

    Padded so a lexical sort of the stems is a numeric sort — the grid tasks.py
    globs + sorts the config stems, so ``task_id`` ↔ ``arm`` must stay stable
    even past ten arms (``arm_09`` sorts before ``arm_10``, unlike ``arm_9``).
    """
    return f"arm_{index:0{width}d}"


def _grid_config_stub(stem: str) -> str:
    """The content-free per-arm config STUB (structure with a marked hole).

    Domain-vocabulary-free: the ONE knob the arm varies is a ``# HOLE:`` the
    caller fills. The framework never guesses which key varies (nomination is
    caller / pack territory — the lists-never-nominates rule).
    """
    return (
        f"# {stem}: one arm of a fixed grid (scaffold STUB — fill the HOLE).\n"
        "#\n"
        "# The file stem is the arm id resolve() returns as `arm`, which keys\n"
        "# result_dir_template's {arm} placeholder (set it to e.g. results/{arm}/\n"
        "# at submit time). One file per arm.\n"
        "#\n"
        "# HOLE: set the ONE knob this arm varies. The scaffold does NOT guess\n"
        "# which key varies (lists-never-nominates) — name it yourself, e.g.\n"
        "#   <knob>: <value-for-this-arm>\n"
    )


def _materialize_grid(output_dir: Path, *, arms: int, force: bool) -> dict[str, Any]:
    """Materialize the fixed-grid skeleton: ``.hpc/tasks.py`` + N config stubs.

    Emits STRUCTURE WITH MARKED HOLES — a tasks.py branch (one task per config
    file, an ``arm`` kwarg keying ``result_dir_template``, a scopes/tags note)
    and *arms* config stubs whose varied knob is a ``# HOLE:``. Never fills a
    knob. Refuses to overwrite any existing destination without *force*.
    """
    if arms < _GRID_MIN_ARMS:
        raise errors.SpecInvalid(
            f"--arms must be >= {_GRID_MIN_ARMS} for a grid (got {arms}); a one-arm grid is "
            "just a single run — use the ordinary submit path instead."
        )

    dest = output_dir / _DEFAULT_DEST_REL
    config_dir = output_dir / _GRID_CONFIG_DIR_REL
    width = max(2, len(str(arms - 1)))
    stems = [_arm_stem(i, width) for i in range(arms)]

    # Pre-flight the refuse-without-force check across EVERY destination before
    # writing any, so a partial materialization can't leave tasks.py written but
    # a colliding config refused (all-or-nothing).
    if not force:
        collisions = [dest] if dest.exists() else []
        collisions += [
            config_dir / f"{s}.yaml" for s in stems if (config_dir / f"{s}.yaml").exists()
        ]
        if collisions:
            joined = ", ".join(str(p) for p in collisions)
            raise errors.SpecInvalid(
                f"refusing to overwrite existing file(s): {joined}; pass --force"
            )

    src = _copy_asset(_GRID_TASKS_ASSET, dest, force=force)
    config_dir.mkdir(parents=True, exist_ok=True)
    config_paths: list[str] = []
    for stem in stems:
        cfg = config_dir / f"{stem}.yaml"
        cfg.write_text(_grid_config_stub(stem), encoding="utf-8")
        config_paths.append(str(cfg.resolve()))

    return {
        "path": str(dest.resolve()),
        "shape": "grid",
        "name": None,
        "async_refill": False,
        "arms": arms,
        "config_paths": config_paths,
        "source": str(src),
        "output_dir": str(output_dir.resolve()),
    }


@primitive(
    name="scaffold-strategy",
    verb="scaffold",
    side_effects=[
        SideEffect(
            "writes-file",
            "<output_dir>/.hpc/tasks.py (refuses to overwrite without --force)",
        ),
    ],
    error_codes=[errors.SpecInvalid],
    idempotent=True,
    idempotency_key="output_dir",
    cli=CliShape(
        help="Scaffold a campaign strategy (optuna|pbt ask/tell) or a fixed grid into a repo.",
        args=(
            CliArg(
                "--shape",
                type=str,
                default="strategy",
                choices=_SHAPES,
                help="What to materialize: 'strategy' (an ask/tell closed loop selected by "
                "--name; the default) or 'grid' (a fixed non-adaptive sweep — one config "
                "file per arm, count set by --arms).",
            ),
            CliArg(
                "--name",
                type=str,
                required=False,
                choices=tuple(_STRATEGY_ASSETS),
                help="Which strategy template to materialize (required for --shape strategy): "
                "'optuna' (scalar-objective ask/tell) or 'pbt' (artifact-carrying "
                "population-based training). Ignored for --shape grid.",
            ),
            CliArg(
                "--arms",
                type=int,
                default=_GRID_MIN_ARMS,
                help="Number of grid arms (config stubs) to emit for --shape grid; must be "
                f">= {_GRID_MIN_ARMS}. Ignored for --shape strategy.",
            ),
            CliArg(
                "--output-dir",
                type=Path,
                default=Path.cwd(),
                help="Experiment repo root (default: CWD). The strategy lands at "
                "<output_dir>/.hpc/tasks.py.",
            ),
            CliArg(
                "--force",
                action="store_true",
                help="Overwrite the destination .hpc/tasks.py if it already exists.",
            ),
            CliArg(
                "--async-refill",
                action="store_true",
                help=(
                    "Emit the continuous-async-refill variant of the strategy "
                    "(#362): keeps K trials in flight via tell-by-trial_token + a "
                    "constant_liar sampler. Only optuna has a distinct async "
                    "asset; a no-op for strategies that already batch (pbt)."
                ),
            ),
        ),
    ),
    agent_facing=True,
)
def scaffold_strategy(
    *,
    output_dir: Path,
    name: str | None = None,
    shape: str = "strategy",
    arms: int = _GRID_MIN_ARMS,
    force: bool = False,
    async_refill: bool = False,
) -> dict[str, Any]:
    """Materialize a campaign ``shape`` (strategy or grid) into ``output_dir``.

    ``shape="strategy"`` (default) copies ``templates/scaffolds/<name>_strategy.py``
    byte-for-byte to ``<output_dir>/.hpc/tasks.py``. The template already wires
    the load-bearing invariants (ask/tell on the orchestrator only, the
    ``trial_token`` reserved round-trip, the batch per-trial-metrics shape); the
    agent customizes only the search space afterward.

    ``shape="grid"`` materializes a fixed NON-adaptive sweep: the
    ``grid_strategy.py`` skeleton at ``<output_dir>/.hpc/tasks.py`` (one task per
    config file, an ``arm`` kwarg keying ``result_dir_template``, a scopes/tags
    note) plus *arms* per-arm config STUBS under ``<output_dir>/configs/`` whose
    varied knob is left as a marked ``# HOLE:``. The scaffold emits STRUCTURE
    WITH MARKED HOLES only — it never fills a knob (guessing which key varies is
    nomination, which is caller / pack territory: the lists-never-nominates rule).

    Parameters
    ----------
    output_dir:
        Experiment repo root. Must already exist. The tasks.py lands at
        ``<output_dir>/.hpc/tasks.py``.
    name:
        ``"optuna"`` or ``"pbt"`` — required for ``shape="strategy"``, ignored
        for ``shape="grid"``.
    shape:
        ``"strategy"`` (default) or ``"grid"``.
    arms:
        Number of grid arms (config stubs) to emit for ``shape="grid"``; must be
        ``>= 2``. Ignored for ``shape="strategy"``.
    force:
        Overwrite existing destination file(s). Default ``False``.
    async_refill:
        Emit the continuous-async-refill variant (#362) when one exists for
        *name* (currently optuna). Default ``False`` → the synchronous asset.
        Ignored for ``shape="grid"``.

    Returns
    -------
    For ``shape="strategy"``: ``{path, shape, name, async_refill, source,
    output_dir}``. For ``shape="grid"``: ``{path, shape, name (None), arms,
    config_paths, async_refill (False), source, output_dir}``.

    Raises
    ------
    errors.SpecInvalid
        If ``shape`` is unknown; if (strategy) ``name`` is missing/unknown; if
        (grid) ``arms`` < 2; if ``output_dir`` does not exist; if a template is
        missing on disk; or if a destination exists and ``force`` is False.
    """
    if shape not in _SHAPES:
        raise errors.SpecInvalid(f"unknown --shape {shape!r}; choose from {sorted(_SHAPES)}")
    if not output_dir.is_dir():
        raise errors.SpecInvalid(f"output-dir {output_dir} does not exist or is not a directory")

    if shape == "grid":
        return _materialize_grid(output_dir, arms=arms, force=force)

    # shape == "strategy" (the original ask/tell path).
    if name is None:
        raise errors.SpecInvalid("--name is required for --shape strategy (optuna|pbt)")
    if name not in _STRATEGY_ASSETS:
        raise errors.SpecInvalid(f"unknown --name {name!r}; choose from {sorted(_STRATEGY_ASSETS)}")

    # Pick the async variant when requested and one exists for this strategy;
    # otherwise fall back to the synchronous asset (default + pbt).
    asset = _STRATEGY_ASSETS[name]
    if async_refill and name in _ASYNC_STRATEGY_ASSETS:
        asset = _ASYNC_STRATEGY_ASSETS[name]

    dest = output_dir / _DEFAULT_DEST_REL
    src = _copy_asset(asset, dest, force=force)
    return {
        "path": str(dest.resolve()),
        "shape": "strategy",
        "name": name,
        "async_refill": bool(async_refill),
        "source": str(src),
        "output_dir": str(output_dir.resolve()),
    }


__all__ = ["scaffold_strategy"]
