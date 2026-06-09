"""``detect-entry-point``: composite primitive — entry-point discovery scan.

WS5 #4. Collapses the six raw-shell probe block (``ls`` / ``find`` /
``grep`` / ``head`` over candidate files) that ``hpc-wrap-entry-point``
SKILL.md duplicated VERBATIM across Step 0 (the greenfield branch) and
Step 1 (the mature-repo branch) into ONE deterministic CLI call. The
agent's role shrinks from "run six probes and eyeball the output twice"
to a single tool call whose ``data`` it branches on.

What the shell probes detected, and how each maps here:

* ``ls main.py train.py run.py experiment.py`` — root-level Python
  candidates by conventional name.
* ``ls src/main.py src/train.py src/run.py`` — the same under ``src/``.
* ``find . -maxdepth 4 -name __main__.py -not -path '*/.*'`` — package
  ``__main__.py`` modules (a ``python -m <pkg>`` invocation), excluding
  dotfile dirs.
* ``test -f pyproject.toml && grep -A1 '[project.scripts]'`` — declared
  console-script entry points.
* ``ls run.sh launch.sh ./simulator`` — non-Python (shell / binary)
  entry points.
* ``grep -rln '@register_run' notebooks/ src/ *.py`` — files already
  carrying ``@register_run`` decoration (Step 0's extra probe; surfaced
  as ``decoration_found``).

For each Python candidate the verb classifies the CLI surface
(``argv_kind``) by reading the file's imports / decorators, mirroring
the SKILL.md prose: ``argparse.ArgumentParser`` import, a ``@click`` /
``@<app>.command`` (typer) decorator, ``@hydra.main``, a ``fire.Fire``
call, or a bare ``if __name__ == "__main__":`` block. A package
``__main__.py`` whose surface is unclassifiable falls back to
``__main__``.

``kind`` is ``"greenfield"`` when NO entry-point candidate exists at
all, else ``"detected"`` — exactly the branch ``hpc-wrap-entry-point``
takes on the probe output.

Beyond the repo scan, the verb also surfaces the
``_materialized.entry_point`` block from ``interview.json`` (when a
wrapper-fallback onboarding already wrote one) as the optional
``materialized`` field. This lets ONE ``detect-entry-point`` call answer
submit.md Step 0b — honor a materialized wrapper (``materialized.kind``)
*and* run the mature-repo fallback probe (``candidates`` /
``decoration_found``) — so a headless worker on any harness drives the
decision through a single ``hpc-agent`` verb rather than a native Read /
Glob / Grep tool. When no ``interview.json`` exists (or it carries no
materialized entry point), ``materialized`` is absent and the repo-scan
output is unchanged.

I/O contracts:

* Input: see ``hpc_agent/schemas/detect_entry_point.input.json``.
* Output: a ``dict`` matching ``schemas/detect_entry_point.output.json``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

__all__ = [
    "detect_entry_point",
]

# Conventional Python entry-point filenames probed at the repo root,
# in by-likelihood order (used only for stable diagnostic ordering —
# the skill refuses on ties, it does not tie-break on order).
_ROOT_CANDIDATES: tuple[str, ...] = ("main.py", "train.py", "run.py", "experiment.py")

# The same convention under ``src/`` (the second ``ls`` probe).
_SRC_CANDIDATES: tuple[str, ...] = ("src/main.py", "src/train.py", "src/run.py")

# Non-Python entry points (the ``ls run.sh launch.sh ./simulator`` probe).
# These have no Python CLI surface to classify; argv_kind is ``shell``.
_SHELL_CANDIDATES: tuple[str, ...] = ("run.sh", "launch.sh", "simulator")

# Roots the ``@register_run`` grep walked (``notebooks/ src/ *.py``).
_DECORATION_ROOTS: tuple[str, ...] = ("notebooks", "src")

# ``find . -maxdepth 4`` — the probe capped recursion at depth 4 and
# skipped dotfile dirs (``-not -path '*/.*'``). Mirror both bounds so we
# detect exactly the ``__main__.py`` files the shell probe would. ``find``
# counts ``.`` as depth 0, so ``a/b/c/__main__.py`` (rel parts == 4) is the
# deepest match.
_MAIN_MAXDEPTH = 4


def _classify_python_argv(source: str, *, is_package_main: bool) -> str:
    """Classify a Python file's CLI surface from its source text.

    Mirrors the SKILL.md prose: inspect imports + decorators for the CLI
    library in use. Order matters — a file can both ``import argparse``
    and carry a ``@hydra.main`` decorator (hydra wraps the function and
    hides the signature), so the more-specific decorator forms are
    checked before the bare ``argparse`` import. Falls back to a bare
    ``__main__`` block when one is present with no recognized library,
    else ``__main__`` as the last resort (a package ``__main__.py`` with
    nothing recognizable is still a ``python -m`` target).
    """
    if re.search(r"@hydra\.main\b", source):
        return "hydra"
    # typer: ``import typer`` / ``@app.command`` on a Typer() app.
    if re.search(r"\btyper\b", source):
        return "typer"
    # click: ``@click.command`` / ``@click.group`` / ``import click``.
    if re.search(r"@click\.", source) or re.search(r"\bimport click\b", source):
        return "click"
    # fire: ``fire.Fire(...)``.
    if re.search(r"\bfire\.Fire\b", source):
        return "fire"
    # argparse: ``argparse.ArgumentParser`` or ``import argparse``.
    if re.search(r"argparse\.ArgumentParser\b", source) or re.search(
        r"\bimport argparse\b", source
    ):
        return "argparse"
    # Bare ``if __name__ == "__main__":`` block with no recognized library,
    # or a package __main__.py with nothing else recognizable — both are
    # ``__main__`` / ``python -m`` targets.
    return "__main__"


def _read_text(path: Path) -> str:
    """Read *path* as UTF-8 text; empty string on any read failure.

    A candidate we cannot read still counts as a candidate (the shell
    ``ls`` probe saw it too); it simply classifies to the ``__main__``
    fallback rather than crashing the scan.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _scan_python_candidates(root: Path) -> list[dict[str, str]]:
    """Find conventional Python entry-point files + package ``__main__.py``.

    Reproduces probes 1–3: the two ``ls`` conventional-name probes plus
    the ``find ... -name __main__.py`` package-module probe. Each match
    is classified by :func:`_classify_python_argv`. Paths are returned
    relative to *root* (matching the shell probes' relative output) and
    de-duplicated while preserving discovery order.
    """
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()

    def _add(rel: str, *, is_package_main: bool) -> None:
        if rel in seen:
            return
        seen.add(rel)
        source = _read_text(root / rel)
        candidates.append(
            {
                "path": rel,
                "argv_kind": _classify_python_argv(source, is_package_main=is_package_main),
            }
        )

    for name in (*_ROOT_CANDIDATES, *_SRC_CANDIDATES):
        if (root / name).is_file():
            _add(name, is_package_main=False)

    # ``find . -maxdepth 4 -name __main__.py -not -path '*/.*'`` — a
    # package ``__main__.py`` is a ``python -m <pkg>`` target.
    for main_path in sorted(root.rglob("__main__.py")):
        rel = main_path.relative_to(root)
        parts = rel.parts
        # -maxdepth 4: a file at ``a/b/c/__main__.py`` has 4 path parts.
        if len(parts) > _MAIN_MAXDEPTH:
            continue
        # -not -path '*/.*': skip any dotfile directory (.venv, .git, …).
        if any(part.startswith(".") for part in parts[:-1]):
            continue
        # ``.as_posix()`` so the relative path uses ``/`` on every OS —
        # the schema + the conventional-name candidates emit POSIX-style
        # paths, and consumers (and tests) compare against ``src/...``.
        _add(rel.as_posix(), is_package_main=True)

    return candidates


def _project_script_names(text: str) -> list[str]:
    """Return the ``[project.scripts]`` keys declared in *text*.

    Uses ``tomllib`` when available (Python 3.11+). On 3.10 — where
    ``tomllib`` is not in the stdlib and this project declares no
    ``tomli`` dependency — falls back to a minimal line scan of the
    ``[project.scripts]`` table, the same declarative shape the original
    ``grep -A1 '[project.scripts]'`` shell probe keyed on. Returns
    ``[]`` on malformed input.
    """
    try:
        import tomllib
    except ImportError:
        return _scan_scripts_table_lines(text)
    try:
        data = tomllib.loads(text)
    except ValueError:
        return []
    scripts = (data.get("project") or {}).get("scripts") or {}
    if not isinstance(scripts, dict):
        return []
    return [name for name in scripts if isinstance(name, str)]


def _scan_scripts_table_lines(text: str) -> list[str]:
    """Stdlib-only ``[project.scripts]`` parse — the Python 3.10 fallback.

    Collects the key of each ``name = "..."`` line inside the
    ``[project.scripts]`` table, stopping at the next table header.
    Quoted keys are unquoted. This covers the common declarative form the
    shell probe targeted; exotic TOML (dotted-key or inline-table
    ``scripts``) is out of scope — it never appeared in the prose the
    probe approximated, and on 3.11+ the ``tomllib`` path handles it
    anyway.
    """
    names: list[str] = []
    in_table = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_table = line.split("#", 1)[0].strip() == "[project.scripts]"
            continue
        if not in_table:
            continue
        key, sep, _value = line.partition("=")
        if not sep:
            continue
        key = key.strip().strip("\"'").strip()
        if key:
            names.append(key)
    return names


def _scan_console_scripts(root: Path) -> list[dict[str, str]]:
    """Reproduce ``grep -A1 '[project.scripts]' pyproject.toml``.

    Each declared console script is an installed-command entry point.
    We parse the ``[project.scripts]`` table and surface one candidate
    per script name (``argv_kind == "console_script"``). The script's
    target module is opaque to a filesystem scan, so the ``path`` is the
    registered command name — exactly what the shell probe's grep showed.
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return []
    text = _read_text(pyproject)
    if not text:
        return []
    return [{"path": name, "argv_kind": "console_script"} for name in _project_script_names(text)]


def _scan_shell_candidates(root: Path) -> list[dict[str, str]]:
    """Reproduce ``ls run.sh launch.sh ./simulator`` — non-Python entry points."""
    out: list[dict[str, str]] = []
    for name in _SHELL_CANDIDATES:
        if (root / name).is_file():
            out.append({"path": name, "argv_kind": "shell"})
    return out


def _scan_decoration(root: Path) -> list[str]:
    """Reproduce ``grep -rln '@register_run' notebooks/ src/ *.py``.

    Returns the relative paths of files containing a ``@register_run``
    decoration, searched under ``notebooks/`` + ``src/`` recursively and
    the repo-root ``*.py`` files (the exact roots the shell grep walked),
    de-duplicated and sorted for stable output.
    """
    found: set[str] = set()
    pattern = re.compile(r"@register_run\b")

    def _check(path: Path) -> None:
        if not path.is_file():
            return
        if pattern.search(_read_text(path)):
            # POSIX-style ``/`` separators on every OS (Windows would
            # otherwise emit ``src\\foo.py`` and break the contract).
            found.add(path.relative_to(root).as_posix())

    # ``*.py`` at the repo root.
    for path in sorted(root.glob("*.py")):
        _check(path)
    # ``notebooks/`` + ``src/`` recursively (-r). Include .py and .ipynb —
    # the grep was content-based and notebooks are JSON text on disk.
    for sub in _DECORATION_ROOTS:
        subdir = root / sub
        if not subdir.is_dir():
            continue
        for path in sorted(subdir.rglob("*")):
            if path.suffix in (".py", ".ipynb"):
                _check(path)

    return sorted(found)


# The fields of a ``_materialized.entry_point`` block this verb surfaces, per
# ``kind``. The interview primitive
# (``hpc_agent.ops.memory.interview``) writes the full block to
# ``interview.json``; we re-export only the subset a headless worker branches
# on at submit.md Step 0b — ``kind`` is always present, the rest are
# kind-specific and copied through verbatim when the source carries them. We
# deliberately do NOT surface ``frozen_shas`` (an internal identity detail the
# worker never reads).
_MATERIALIZED_FIELDS: tuple[str, ...] = (
    "run_name",
    "wrapper_path",
    "executor_cmd",
    "module",
    "function",
    "data_axis",
)


def _read_materialized_entry_point(root: Path) -> dict[str, Any] | None:
    """Surface ``interview.json``'s ``_materialized.entry_point`` block, if any.

    A wrapper-fallback onboarding (``hpc-wrap-entry-point`` /
    ``/wrap-entry-point-hpc``) persists the chosen entry point to
    ``<experiment_dir>/interview.json`` under
    ``_materialized.entry_point`` — a ``{kind, ...}`` block whose ``kind``
    is ``shell_command`` / ``register_run`` / ``python_module``. Folding it
    in here lets ONE ``detect-entry-point`` call answer submit.md Step 0b
    (honor a materialized wrapper) alongside the mature-repo fallback probe,
    so the headless worker never needs a native Read/Glob tool to inspect
    the file.

    The canonical location is the campaign-dir root (where the ``interview``
    primitive writes it); we also accept ``.hpc/interview.json`` defensively.
    Returns ``None`` — leaving the existing repo-scan output untouched — when
    no ``interview.json`` exists, when it is malformed, or when it carries no
    ``_materialized.entry_point`` block. Surfaces only the worker-facing
    subset of fields (``kind`` plus whichever of
    ``_MATERIALIZED_FIELDS`` the block declares).
    """
    for rel in ("interview.json", ".hpc/interview.json"):
        path = root / rel
        if not path.is_file():
            continue
        text = _read_text(path)
        if not text:
            return None
        try:
            doc = json.loads(text)
        except ValueError:
            # A half-written / malformed interview.json is treated as absent:
            # the repo-scan output stands, the worker falls through to the
            # mature-repo probe rather than crashing the scan.
            return None
        if not isinstance(doc, dict):
            return None
        entry = (doc.get("_materialized") or {}).get("entry_point")
        if not isinstance(entry, dict):
            return None
        kind = entry.get("kind")
        if not isinstance(kind, str):
            return None
        out: dict[str, Any] = {"kind": kind}
        for field in _MATERIALIZED_FIELDS:
            if field in entry:
                out[field] = entry[field]
        return out
    return None


@primitive(
    name="detect-entry-point",
    verb="query",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Scan an experiment dir for Python entry-point candidates "
            "(main.py/train.py/console-scripts/python -m targets/shell "
            "scripts), classify each candidate's argv style (argparse / "
            "click / typer / hydra / fire / __main__), and locate files "
            "carrying @register_run. Collapses the duplicated six-probe "
            "shell block in hpc-wrap-entry-point Steps 0 + 1. Also "
            "surfaces interview.json's materialized entry-point block "
            "(kind shell_command / register_run / python_module) so one "
            "call answers submit.md Step 0b's wrapper-honor + mature-repo "
            "probe."
        ),
        verb="detect-entry-point",
        args=(
            CliArg(
                "--experiment-dir",
                type=str,
                required=True,
                help="Absolute path to the experiment / repo directory to scan.",
            ),
        ),
        # Local filesystem scan only — no cluster round-trip.
        requires_ssh=False,
    ),
    agent_facing=True,
)
def detect_entry_point(*, experiment_dir: str | Path) -> dict[str, Any]:
    """Scan *experiment_dir* for entry-point candidates + ``@register_run``.

    Returns a dict matching ``schemas/detect_entry_point.output.json``;
    the CLI dispatcher wraps it in a SuccessEnvelope. *experiment_dir*
    accepts both ``str`` (the CLI path) and ``Path`` (the in-process
    path) and is coerced internally.

    Faithfully reproduces the six raw-shell probes the SKILL.md ran in
    Step 0 + Step 1: the two conventional-name ``ls`` probes, the
    ``find ... __main__.py`` package-module probe, the
    ``[project.scripts]`` console-script grep, the ``run.sh``/binary
    ``ls`` probe, and the ``@register_run`` grep. ``kind`` is
    ``"greenfield"`` only when NO candidate of any kind exists AND no
    ``@register_run`` is already on disk.

    Additionally surfaces ``interview.json``'s
    ``_materialized.entry_point`` block as the optional ``materialized``
    field when a wrapper-fallback onboarding already wrote one — absent
    otherwise, leaving the repo-scan fields untouched.
    """
    root = experiment_dir if isinstance(experiment_dir, Path) else Path(experiment_dir)

    candidates: list[dict[str, str]] = []
    candidates.extend(_scan_python_candidates(root))
    candidates.extend(_scan_console_scripts(root))
    candidates.extend(_scan_shell_candidates(root))

    decoration_found = _scan_decoration(root)

    # ``@register_run`` on disk is itself a signal the repo is already
    # onboarded — the SKILL.md Step 0 treats "a @register_run is already
    # on disk" as a non-greenfield match alongside an entry-point file.
    kind = "detected" if (candidates or decoration_found) else "greenfield"

    out: dict[str, Any] = {
        "kind": kind,
        "candidates": candidates,
        "decoration_found": decoration_found,
    }

    # If a wrapper-fallback onboarding already materialized an entry point,
    # surface its ``_materialized.entry_point`` block so submit.md Step 0b
    # honors it in the SAME call that runs the mature-repo probe — no native
    # Read tool needed. ``None`` when no interview.json / no materialized
    # block, leaving the repo-scan output above unchanged.
    materialized = _read_materialized_entry_point(root)
    if materialized is not None:
        out["materialized"] = materialized

    return out
