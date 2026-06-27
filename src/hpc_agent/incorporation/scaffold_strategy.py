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

# The materialized strategy IS the experiment's tasks.py — the framework
# imports it and calls total()/resolve() (and, on the orchestrator only,
# _propose()). The campaign loop reads it as `.hpc/tasks.py`.
_DEFAULT_DEST_REL = Path(".hpc") / "tasks.py"


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
        help="Scaffold a closed-loop campaign strategy (optuna|pbt) into a repo.",
        args=(
            CliArg(
                "--name",
                type=str,
                required=True,
                choices=tuple(_STRATEGY_ASSETS),
                help="Which strategy template to materialize: 'optuna' (scalar-objective "
                "ask/tell) or 'pbt' (artifact-carrying population-based training).",
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
    name: str,
    force: bool = False,
    async_refill: bool = False,
) -> dict[str, Any]:
    """Materialize the ``name`` strategy template into ``output_dir``.

    Copies ``templates/scaffolds/<name>_strategy.py`` byte-for-byte to
    ``<output_dir>/.hpc/tasks.py``. The template already wires the
    load-bearing invariants (ask/tell on the orchestrator only, the
    ``trial_token`` reserved round-trip, the batch per-trial-metrics
    shape); the agent customizes only the search space afterward.

    Parameters
    ----------
    output_dir:
        Experiment repo root. Must already exist. The strategy lands at
        ``<output_dir>/.hpc/tasks.py``.
    name:
        ``"optuna"`` or ``"pbt"``.
    force:
        Overwrite an existing ``.hpc/tasks.py``. Default ``False``.
    async_refill:
        Emit the continuous-async-refill variant (#362) when one exists for
        *name* (currently optuna). Default ``False`` → the synchronous asset.

    Returns
    -------
    ``{path, name, async_refill, source, output_dir}`` — the absolute path
    written, the strategy name, whether the async variant was emitted, the
    absolute template path it was copied from, and the resolved repo root.

    Raises
    ------
    errors.SpecInvalid
        If ``name`` is not a known strategy, if ``output_dir`` does not
        exist, if the template is missing on disk, or if the destination
        exists and ``force`` is False.
    """
    if name not in _STRATEGY_ASSETS:
        raise errors.SpecInvalid(f"unknown --name {name!r}; choose from {sorted(_STRATEGY_ASSETS)}")
    if not output_dir.is_dir():
        raise errors.SpecInvalid(f"output-dir {output_dir} does not exist or is not a directory")

    # Pick the async variant when requested and one exists for this strategy;
    # otherwise fall back to the synchronous asset (default + pbt).
    asset = _STRATEGY_ASSETS[name]
    if async_refill and name in _ASYNC_STRATEGY_ASSETS:
        asset = _ASYNC_STRATEGY_ASSETS[name]

    scaffold_dir = hpc_agent._PACKAGE_ROOT / "execution" / "mapreduce" / "templates" / "scaffolds"
    src = scaffold_dir / asset
    if not src.is_file():
        raise errors.SpecInvalid(f"strategy template missing on disk: {src}")

    dest = output_dir / _DEFAULT_DEST_REL
    if dest.exists() and not force:
        raise errors.SpecInvalid(f"refusing to overwrite {dest}; pass --force to overwrite")

    dest.parent.mkdir(parents=True, exist_ok=True)
    # Pin UTF-8 on both ends — HPC nodes with LC_ALL=C / LANG=POSIX would
    # otherwise decode/encode the template via the locale codec and either
    # raise UnicodeDecodeError or silently corrupt the box-drawing comment
    # rules the templates use.
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    return {
        "path": str(dest.resolve()),
        "name": name,
        "async_refill": bool(async_refill),
        "source": str(src),
        "output_dir": str(output_dir.resolve()),
    }


__all__ = ["scaffold_strategy"]
