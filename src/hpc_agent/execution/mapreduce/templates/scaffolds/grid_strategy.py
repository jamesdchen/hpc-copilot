# ruff: noqa: E501, F401
"""Fixed-grid campaign SKELETON (scaffold — fill the marked holes).

A fixed grid is the NON-adaptive sweep: every arm is known up front (one config
file per arm under ``configs/``), and no iteration depends on an earlier
iteration's result. This is the counterpart to the ask/tell strategies
(``optuna`` / ``pbt``) for the case where the sweep set is fixed.

This file is a SKELETON, not a working strategy. The scaffold emits STRUCTURE
with the varied knob left as a marked ``# HOLE:`` — it never guesses WHICH knob
you are varying. Choosing the key that varies is nomination, which is the
caller's (or the pack's) call, never the framework's — the lists-never-nominates
rule. FILL every ``# HOLE:`` below before submitting.

Contract this skeleton wires (do not reinvent)
----------------------------------------------
* ``total()`` == the number of arms == the number of config files under
  ``configs/``. One task per config file.
* ``resolve(task_id)`` returns the kwargs the executor receives as ``HPC_KW_*``.
  The ``arm`` kwarg is an ordinary parameter (it is PART of ``cmd_sha``, so each
  arm has a distinct run identity) that ALSO keys ``result_dir_template``'s
  ``{arm}`` placeholder, so each arm's outputs land in a distinct dir. Set
  ``result_dir_template`` to something like ``results/{arm}/`` at interview /
  submit time.
* Config files are sorted by stem so ``task_id`` ↔ ``arm`` is STABLE across
  loads (the stems are zero-padded so a lexical sort is a numeric sort). Never
  index the grid by counting on-disk artifacts a prior load created.

scopes / tags
-------------
A grid arm is a natural grouping boundary: tag each arm's runs with its ``arm``
so ``status`` / ``aggregate`` can group results by arm. Set ``scopes`` on the
campaign spec at submit time — this note is the reminder, not a knob the
skeleton fills.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent.executor_cli import flag, generic_args

# repo root (parent of .hpc/), then the sibling configs/ dir the scaffold filled
# with one stub per arm. Sorted stems keep task_id ↔ arm stable across loads.
_CONFIG_DIR = Path(__file__).resolve().parent.parent / "configs"
_ARMS: list[str] = sorted(p.stem for p in _CONFIG_DIR.glob("*.yaml"))

FLAGS: dict[str, list] = {
    # HOLE: name your executor entry point (the import path the dispatcher runs)
    # and its args — mirror the executor's generic_args() plus one flag() per
    # knob a config arm sets. Example key: "src.train".
    "src.train": [
        *generic_args(),
        # flag("<knob>", <type>),  # HOLE: one flag() per varied knob
    ],
}


def total() -> int:
    """One task per config file — the whole grid is known up front."""
    return len(_ARMS)


def resolve(task_id: int) -> dict:
    """Return the kwargs for arm ``task_id`` (exported as ``HPC_KW_*``).

    ``arm`` keys ``result_dir_template``'s ``{arm}`` placeholder AND is part of
    the parameter identity (``cmd_sha``). The varied knob is a HOLE: read it out
    of the arm's config with your loader of choice.
    """
    arm = _ARMS[task_id]
    _config_text = (_CONFIG_DIR / f"{arm}.yaml").read_text(encoding="utf-8")  # noqa: F841
    # HOLE: parse _config_text (e.g. `import yaml; cfg = yaml.safe_load(...)`)
    # and pull each varied knob out of it into the dict below.
    return {
        "arm": arm,
        # HOLE: "<knob>": <value parsed from _config_text for this arm>,
    }
