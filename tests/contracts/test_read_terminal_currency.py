"""Contract: every ``read_terminal`` consumer is currency-aware, or exempt.

``state.block_terminal.read_terminal`` returns a block's recorded TERMINAL
outcome keyed by ``(run_id, block)`` and fingerprinted with the tree's
``cmd_sha``. A consumer that REPLAYS that outcome must first prove the record
is still CURRENT for the tree — recompute the live ``cmd_sha`` and refuse a
record whose stored ``cmd_sha`` no longer matches — or it replays a stale
outcome after a nudge moved the tree. Four replay readers do exactly this,
each through a ``read_run_cmd_sha(experiment_dir, run_id)`` recompute + a
``!= current_sha`` refusal — the ONE shared fingerprint reader in
``state/runs.py`` that collapsed the five byte-identical per-module
``_*_cmd_sha`` helpers this contract used to name:

* ``ops/aggregate_blocks.py``  (``aggregate_run``)
* ``ops/aggregate_flow.py``    (``aggregate_flow``)
* ``ops/campaign_run.py``      (``_replay_campaign_terminal``)
* ``ops/status_blocks.py``     (``_replay_watch_terminal``)

Two consumers legitimately read a terminal WITHOUT a currency compare, and
carry an explicit exemption here (the B7 "one-definition" enforcement row —
spec 9 / the sixth-consumer row in ``docs/plans/upstream-fixes-2026-07.md``):

* ``state/run_story.py`` (the SEEDED exemption, ``run_story.py:301-306``) —
  it NARRATES history: a run's story enumerates every terminal the run ever
  recorded, including superseded/stale ones. Currency-gating would erase the
  history it exists to tell.
* ``cli/dispatch.py`` — a WRITER-side idempotency guard: it reads
  ``read_terminal`` only to avoid clobbering an already-recorded terminal
  before it writes a detached-exit terminal. It replays nothing, so there is
  no cached outcome whose currency could go stale.

The failure mode this pins: a NEW ``read_terminal`` consumer that replays a
cached outcome without recomputing the tree ``cmd_sha`` — a silently stale
replay. Such a consumer fires this test until it either routes through the
canonical currency compare (a ``_*_cmd_sha`` recompute) or is added to
:data:`_CURRENCY_EXEMPT` as a reviewed decision with a cited reason.

The enforcement is deliberately semantic-but-coarse (per the repo's lint
posture): "the enclosing function recomputes a fresh sha via a ``*_cmd_sha``
helper" is the machine-checkable shape of the currency compare. A consumer
that follows the pattern is detected; anything else must be an explicit
exemption. The escape valve is an ``_CURRENCY_EXEMPT`` entry with a reason,
never a silent pass.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "hpc_agent"

# The block-terminal store's OWN module — where ``read_terminal`` and the
# migration-aware ``read_terminal_with_fallback`` are defined. Not a consumer.
_DEFINITION_MODULE = "state/block_terminal.py"

# Consumers that read a terminal WITHOUT a currency compare, by design. Each
# entry is a reviewed decision with a cited reason (see the module docstring).
_CURRENCY_EXEMPT: dict[str, str] = {
    "state/run_story.py": (
        "narrating-history: the run story enumerates EVERY terminal the run "
        "ever recorded (including superseded/stale ones); currency-gating "
        "would erase the history it exists to tell (run_story.py:301-306, the "
        "seeded B7 sixth-consumer exemption)."
    ),
    "cli/dispatch.py": (
        "writer-side idempotency guard: reads read_terminal only to avoid "
        "clobbering an already-recorded terminal before writing a "
        "detached-exit terminal — it replays nothing, so no cached outcome's "
        "currency can go stale."
    ),
}


def _rel(path: Path) -> str:
    return path.relative_to(_SRC).as_posix()


def _calls_read_terminal(node: ast.AST) -> bool:
    """True if *node*'s subtree calls the bare ``read_terminal`` (not the
    ``_with_fallback`` migration wrapper)."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            fn = child.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr
                if isinstance(fn, ast.Attribute)
                else None
            )
            if name == "read_terminal":
                return True
    return False


def _recomputes_cmd_sha(node: ast.AST) -> bool:
    """True if *node*'s subtree calls a fresh-sha recompute helper — a call to
    any function whose name ends in ``_cmd_sha`` (the canonical currency
    compare's fingerprint recompute)."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            fn = child.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr
                if isinstance(fn, ast.Attribute)
                else None
            )
            if name and name.endswith("_cmd_sha"):
                return True
    return False


def _consumer_functions() -> list[tuple[str, str, bool]]:
    """Every ``(module_rel, function_name, recomputes_cmd_sha)`` for each
    function in ``src/hpc_agent`` that calls the bare ``read_terminal`` —
    excluding the store's own definition module.
    """
    out: list[tuple[str, str, bool]] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = _rel(path)
        if rel == _DEFINITION_MODULE:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - defensive
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if _calls_read_terminal(fn):
                out.append((rel, fn.name, _recomputes_cmd_sha(fn)))
    return out


def test_every_read_terminal_consumer_is_currency_aware_or_exempt() -> None:
    """Each ``read_terminal`` replay consumer recomputes the tree ``cmd_sha``
    (canonical currency compare) or is an explicitly-cited exemption."""
    offenders: list[str] = []
    for rel, fn_name, recomputes in _consumer_functions():
        if recomputes or rel in _CURRENCY_EXEMPT:
            continue
        offenders.append(f"{rel}::{fn_name}")
    assert not offenders, (
        "read_terminal consumer(s) that neither recompute the tree cmd_sha "
        "(canonical currency compare via a `_*_cmd_sha` helper) nor carry an "
        f"exemption: {offenders}. A replay read of a block terminal without a "
        "currency compare replays a stale outcome after a nudge moves the "
        "tree. Route the read through a `_*_cmd_sha` recompute + "
        "`!= current_sha` refusal, OR add the module to _CURRENCY_EXEMPT in "
        "tests/contracts/test_read_terminal_currency.py with a cited reason "
        "(as run_story.py / cli/dispatch.py are)."
    )


def test_currency_detection_is_non_vacuous() -> None:
    """The four known replay readers ARE detected via the cmd_sha recompute —
    so the compliance path is exercised, not vacuously satisfied by the
    exemption list."""
    recomputing = {rel for rel, _fn, recomputes in _consumer_functions() if recomputes}
    expected = {
        "ops/aggregate_blocks.py",
        "ops/aggregate_flow.py",
        "ops/campaign_run.py",
        "ops/status_blocks.py",
    }
    missing = expected - recomputing
    assert not missing, (
        "expected these replay readers to be detected as currency-comparing "
        f"(they call a `_*_cmd_sha` recompute), but they were not: {sorted(missing)}. "
        "Either the currency-compare shape changed (update _recomputes_cmd_sha) "
        "or a reader dropped its currency guard (a real regression)."
    )


def test_currency_exemptions_are_not_stale() -> None:
    """Every exempt module actually reads a terminal — a stale exemption (the
    module no longer calls read_terminal) is a hit, mirroring the
    SKILL_ONLY_OK-must-be-present discipline (G10)."""
    consumer_modules = {rel for rel, _fn, _r in _consumer_functions()}
    stale = sorted(m for m in _CURRENCY_EXEMPT if m not in consumer_modules)
    assert not stale, (
        "_CURRENCY_EXEMPT names module(s) that no longer call read_terminal: "
        f"{stale}. Remove the stale exemption from "
        "tests/contracts/test_read_terminal_currency.py."
    )
