"""Cross-source consistency between ``_kernel/contract/vocabulary.py``
StrEnums and ``_wire/_shared.py`` Pydantic Literals.

The runtime code uses StrEnums for type-safe equality checks
(``state == LifecycleState.COMPLETE``) while Pydantic models use
Literals on the wire. They serve different purposes but encode
the same value sets — drift between them is the same class of
bug B2 fixed for the four scattered status vocabularies pre-B2.

These tests pin the alignment so a future PR that adds a value
to one side without the other fails CI.
"""

from __future__ import annotations

import typing

from hpc_agent._kernel.contract.vocabulary import (
    TERMINAL_STATUSES,
    JournalStatus,
    LifecycleState,
)
from hpc_agent._wire._shared import (
    LifecycleStateObservable,
    LifecycleStateObservableWithTimeout,
    LifecycleStateTerminal,
)


def test_lifecycle_state_matches_observable_with_timeout() -> None:
    """``LifecycleState`` (StrEnum) must equal
    ``LifecycleStateObservableWithTimeout`` (Pydantic Literal).

    Both encode {in_flight, complete, failed, abandoned, timeout} —
    the StrEnum for runtime code, the Literal for wire validation
    on monitor-flow / status / reconcile output.
    """
    enum_values = {s.value for s in LifecycleState}
    literal_values = set(typing.get_args(LifecycleStateObservableWithTimeout))
    assert enum_values == literal_values, (
        f"LifecycleState (StrEnum) drifted from "
        f"LifecycleStateObservableWithTimeout (Literal): "
        f"enum={sorted(enum_values)} vs literal={sorted(literal_values)}"
    )


def test_journal_status_matches_observable() -> None:
    """``JournalStatus`` (StrEnum on RunRecord.status) must equal
    ``LifecycleStateObservable`` (Pydantic Literal on the
    point-in-time observation envelopes).

    Both encode {in_flight, complete, failed, abandoned}.
    """
    enum_values = {s.value for s in JournalStatus}
    literal_values = set(typing.get_args(LifecycleStateObservable))
    assert enum_values == literal_values, (
        f"JournalStatus (StrEnum) drifted from "
        f"LifecycleStateObservable (Literal): "
        f"enum={sorted(enum_values)} vs literal={sorted(literal_values)}"
    )


def test_terminal_statuses_matches_terminal_literal() -> None:
    """``TERMINAL_STATUSES`` (frozenset derived from JournalStatus)
    must equal ``LifecycleStateTerminal`` (Pydantic Literal).

    Both encode the four terminal lifecycle values:
    {complete, failed, abandoned, timeout}. Note the asymmetry
    with ``JournalStatus`` — the journal's ``in_flight`` is
    explicitly NON-terminal, so it's excluded here even though
    the Pydantic ``LifecycleStateObservable*`` Literals include
    it.
    """
    # TERMINAL_STATUSES is currently derived from JournalStatus (the
    # observable enum) which excludes "timeout". The Literal
    # LifecycleStateTerminal includes "timeout" because monitor-flow
    # emits it. They overlap on three values; the union is the wire
    # contract for terminal envelopes.
    runtime_terminal = {str(s) for s in TERMINAL_STATUSES}
    literal_terminal = set(typing.get_args(LifecycleStateTerminal))
    # Runtime terminal is a subset (no "timeout" because that's a
    # workflow-only state, not a journal status).
    assert runtime_terminal <= literal_terminal, (
        f"TERMINAL_STATUSES drifted from LifecycleStateTerminal: "
        f"runtime={sorted(runtime_terminal)} not subset of "
        f"literal={sorted(literal_terminal)}"
    )
    # Specifically the difference must be exactly {"timeout"}.
    assert literal_terminal - runtime_terminal == {"timeout"}, (
        "Expected literal-only difference to be {'timeout'} "
        "(workflow-only); got "
        f"{sorted(literal_terminal - runtime_terminal)}"
    )
