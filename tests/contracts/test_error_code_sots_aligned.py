"""Contract: the four error-code sources of truth stay byte-aligned.

The ``error_code`` enum appears in four places, each load-bearing for
a different consumer:

* **Set A** — :class:`hpc_agent.errors.HpcError` subclasses (the Python
  raise side; what the framework actually emits).
* **Set B** — :data:`hpc_agent._wire._shared.ErrorCode` Literal
  (the wire-contract Pydantic alias; consumed by every output schema
  that surfaces an error code inside ``data``).
* **Set C** — :data:`hpc_agent.integration.ERROR_CODES` frozenset (the
  integrator-facing constant; what external harnesses branch on).
* **Set D** — the ``error_code.enum`` array under
  ``ErrorEnvelope`` in :file:`src/hpc_agent/schemas/envelope.json` (the
  JSON-schema wire side; what schema validators check against).

When these drift, a real error_code slips through:

* a new ``HpcError`` subclass with no Literal entry → schema rejects
  the envelope (B disagrees with A);
* an integrator branches on a code never emitted (C disagrees with A);
* a JSON-schema validator rejects an envelope the Python emits
  (D disagrees with B).

The audit found ``precondition_failed`` raised by the runtime
(Set A) but absent from B, C, and D — exactly the drift class. PR A
adds it to all three; this test pins the alignment so the next
addition can't silently land in one place only.
"""

from __future__ import annotations

import json
import typing
from pathlib import Path

from hpc_agent import errors
from hpc_agent._wire import _shared
from hpc_agent.integration import ERROR_CODES as INTEGRATION_ERROR_CODES

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENVELOPE_SCHEMA = _REPO_ROOT / "src" / "hpc_agent" / "schemas" / "envelope.json"

# Codes that are deliberately asymmetric across the four sources. The
# default policy is "every code appears in all four"; entries here
# need a comment explaining which set the code belongs to and why it
# is omitted from the others.
_EXPECTED_ASYMMETRY: dict[str, set[str]] = {
    # ``internal`` is the framework-bug catch-all. Set B and Set D
    # include it (the wire envelope may carry it), but Set C
    # deliberately excludes it from the integrator contract — see the
    # docstring on :data:`hpc_agent.integration.ERROR_CODES`. Integrators
    # should treat ``internal`` as "framework bug; file an issue", not
    # as a stable code to branch on.
    "internal": {"A", "B", "D"},  # absent from C by design
}


def _set_a_errors_subclasses() -> set[str]:
    """Set A: ``error_code`` declared on every :class:`HpcError` and subclass.

    Includes the base :class:`HpcError` itself (``error_code='internal'``)
    because the framework instantiates it directly as the catch-all
    error when no more specific subclass applies.
    """
    return {cls.error_code for cls in errors.HpcError.__subclasses__()} | {
        errors.HpcError.error_code,
    }


def _set_b_literal_args() -> set[str]:
    """Set B: ``Literal`` args of :data:`_shared.ErrorCode`."""
    return set(typing.get_args(_shared.ErrorCode))


def _set_c_integration_constant() -> set[str]:
    """Set C: contents of :data:`hpc_agent.integration.ERROR_CODES`."""
    return set(INTEGRATION_ERROR_CODES)


def _set_d_envelope_schema() -> set[str]:
    """Set D: ``error_code.enum`` under ``ErrorEnvelope`` in envelope.json."""
    schema = json.loads(_ENVELOPE_SCHEMA.read_text(encoding="utf-8"))
    enum = schema["$defs"]["ErrorEnvelope"]["properties"]["error_code"]["enum"]
    return set(enum)


def test_error_code_sources_of_truth_aligned() -> None:
    """Sets A, B, C, D must agree (modulo :data:`_EXPECTED_ASYMMETRY`)."""
    sets = {
        "A": _set_a_errors_subclasses(),
        "B": _set_b_literal_args(),
        "C": _set_c_integration_constant(),
        "D": _set_d_envelope_schema(),
    }
    # Apply the expected asymmetry: a code in _EXPECTED_ASYMMETRY is
    # only required to appear in the listed sets.
    universe: set[str] = set().union(*sets.values())
    diffs: list[str] = []
    for code in sorted(universe):
        expected_sets = _EXPECTED_ASYMMETRY.get(code, {"A", "B", "C", "D"})
        for label, members in sets.items():
            present = code in members
            should_be_present = label in expected_sets
            if present is not should_be_present:
                where = "in" if present else "missing from"
                diffs.append(f"  {code!r}: {where} Set {label} (expected {expected_sets})")

    assert not diffs, (
        "error_code sources of truth disagree. Update _EXPECTED_ASYMMETRY "
        "ONLY when the asymmetry is deliberate (e.g. ``internal`` omitted "
        "from the integrator-facing constant). Otherwise sync all four:\n" + "\n".join(diffs)
    )
