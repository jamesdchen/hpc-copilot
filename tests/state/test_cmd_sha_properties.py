"""Property-based tests for ``compute_cmd_sha``.

The function is load-bearing for cluster-side dedup: two campaigns
that materialize identical task lists must collide on cmd_sha; two
campaigns that differ in any kwargs value must not. Before this file
the function had zero direct tests — it was only exercised through
``test_interview.py`` (which checks one negative case) and
``test_idempotency.py`` (which uses the sha as a dedup key without
testing the hash itself).

Properties pinned here:

* **Determinism** — same input → same output across calls.
* **Output shape** — 64-char lowercase hex.
* **Key-order invariance within each kwargs dict** — JSON
  serialization sorts keys, so reordering keys inside any kwargs
  produces the same hash.
* **Position sensitivity** — reordering the task list (the i-axis)
  changes the hash. Two campaigns with the same tasks but in
  different order are distinct campaigns by design.

Hypothesis catches edge cases the example suite would never enumerate
(unicode kwargs keys, deeply pathological dict orderings, single-task
edge case, etc.) and shrinks failures to the smallest reproducer.
"""

from __future__ import annotations

import re
from typing import Any

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from hpc_agent.state.run_sha import compute_cmd_sha


class _FakeTasksModule:
    """Stand-in for the user's ``tasks.py`` module.

    ``compute_cmd_sha`` only requires ``total()`` and ``resolve(i)``;
    we don't need a real Python module for property testing.
    """

    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        self._tasks = tasks

    def total(self) -> int:
        return len(self._tasks)

    def resolve(self, i: int) -> dict[str, Any]:
        return self._tasks[i]


# JSON-safe scalars only — compute_cmd_sha's inner ``json.dumps`` rejects
# NaN/Inf and non-string keys, which is the wire contract we want to pin.
_json_scalar = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=20),
)
_kwargs = st.dictionaries(
    keys=st.text(min_size=1, max_size=10),
    values=_json_scalar,
    max_size=6,
)
_tasks = st.lists(_kwargs, min_size=1, max_size=12)

_HEX_64 = re.compile(r"^[0-9a-f]{64}$")


@given(_tasks)
@settings(max_examples=50)
def test_compute_cmd_sha_is_deterministic(tasks: list[dict[str, Any]]) -> None:
    m = _FakeTasksModule(tasks)
    assert compute_cmd_sha(m) == compute_cmd_sha(m)


@given(_tasks)
@settings(max_examples=50)
def test_compute_cmd_sha_output_is_64_lowercase_hex(tasks: list[dict[str, Any]]) -> None:
    sha = compute_cmd_sha(_FakeTasksModule(tasks))
    assert _HEX_64.fullmatch(sha) is not None, sha


@given(_tasks)
@settings(max_examples=75)
def test_compute_cmd_sha_invariant_under_kwargs_key_reorder(
    tasks: list[dict[str, Any]],
) -> None:
    """Reversing the iteration order of every kwargs dict (Python dicts
    preserve insertion order since 3.7) must not change the hash. The
    invariant comes from ``json.dumps(..., sort_keys=True)`` inside
    ``compute_cmd_sha``."""
    a = compute_cmd_sha(_FakeTasksModule(tasks))
    reversed_tasks = [dict(reversed(list(t.items()))) for t in tasks]
    b = compute_cmd_sha(_FakeTasksModule(reversed_tasks))
    assert a == b


@given(_tasks)
@settings(max_examples=75)
def test_compute_cmd_sha_position_sensitive(tasks: list[dict[str, Any]]) -> None:
    """Reordering the task list (the i-axis) changes the hash. Two
    campaigns that materialize the same tasks in different order are
    different campaigns by design — the cluster must dispatch them
    separately, not dedup."""
    # Reversal is a no-op when the list is a palindrome of dict-equal
    # entries. Skip those — the property is undefined.
    rev = list(reversed(tasks))
    assume(rev != tasks)
    assert compute_cmd_sha(_FakeTasksModule(tasks)) != compute_cmd_sha(_FakeTasksModule(rev))


# ---------------------------------------------------------------------------
# Reserved bookkeeping keys are stripped before hashing (campaign seam)
# ---------------------------------------------------------------------------


def test_trial_token_excluded_from_cmd_sha() -> None:
    """A reserved ``trial_token`` is bookkeeping, not a swept parameter, so it
    must not change parameter identity / bust dedup. Two task lists that
    differ ONLY in trial_token hash identically."""
    base = _FakeTasksModule([{"lr": 0.1, "seed": 1}])
    with_token = _FakeTasksModule([{"lr": 0.1, "seed": 1, "trial_token": 7}])
    other_token = _FakeTasksModule([{"lr": 0.1, "seed": 1, "trial_token": 99}])
    assert compute_cmd_sha(base) == compute_cmd_sha(with_token)
    assert compute_cmd_sha(with_token) == compute_cmd_sha(other_token)


def test_non_reserved_key_still_changes_cmd_sha() -> None:
    """Control: a genuine swept-parameter difference DOES change the hash —
    the strip is surgical to the reserved key, not a blanket ignore."""
    a = _FakeTasksModule([{"lr": 0.1, "seed": 1}])
    b = _FakeTasksModule([{"lr": 0.1, "seed": 2}])
    assert compute_cmd_sha(a) != compute_cmd_sha(b)


def test_compute_cmd_sha_does_not_mutate_resolve_dict() -> None:
    """Stripping the reserved key hashes a copy; the caller's dict (which the
    dispatcher still exports as HPC_KW_* for the executor) is untouched."""
    task = {"lr": 0.1, "trial_token": 5}
    compute_cmd_sha(_FakeTasksModule([task]))
    assert task == {"lr": 0.1, "trial_token": 5}
