"""Property-based tests for ``compose_node_sha``.

The function is the recursive-identity step of the DAG-kernel proposal
(``docs/design/dag-kernel.md``): a run that consumes another run's
outputs must change identity whenever its ancestor does, or memoized
resume over a run graph silently reuses stale subgraphs. The properties
pinned here are the entire contract — the function is not yet wired into
``find_run_by_cmd_sha`` / sidecars, so these tests are what hold the
invariant until it is.

Properties pinned here:

* **Zero-parent degeneracy** — ``compose_node_sha(c, []) == c``. Every
  existing run is a 0-parent node; landing the function changes no
  existing identity.
* **Determinism + output shape** — same input → same 64-char lowercase
  hex across calls.
* **Parent-set semantics** — order-invariant and duplicate-insensitive:
  an edge set, not a sequence.
* **Sensitivity** — distinct from the bare ``cmd_sha`` once any parent
  exists; changes when ``cmd_sha`` changes; changes when any parent
  changes.
* **Ancestor propagation** — a changed grandparent propagates through
  the parent's digest into the child's (the Merkle property).
* **Loud failure on malformed digests** — non-hex input raises
  ``ValueError`` (the guard's fire path, per engineering-principles).
"""

from __future__ import annotations

import hashlib
import re

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from hpc_agent.state.run_sha import compose_node_sha

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Generate digests the way production does: hash arbitrary bytes.
_sha = st.binary(max_size=64).map(lambda b: hashlib.sha256(b).hexdigest())
_sha_list = st.lists(_sha, max_size=6)


@given(_sha)
def test_zero_parents_degenerates_to_cmd_sha(cmd_sha: str) -> None:
    assert compose_node_sha(cmd_sha, []) == cmd_sha


@given(_sha, _sha_list)
def test_deterministic_and_well_shaped(cmd_sha: str, parents: list[str]) -> None:
    first = compose_node_sha(cmd_sha, parents)
    assert first == compose_node_sha(cmd_sha, parents)
    assert _HEX64.match(first)


@given(_sha, st.lists(_sha, min_size=1, max_size=6), st.randoms())
def test_parents_are_a_set(cmd_sha: str, parents: list[str], rng) -> None:
    """Reordering and duplicating parents leaves the digest unchanged."""
    shuffled = list(parents) + [parents[0]]  # duplicate one edge
    rng.shuffle(shuffled)
    assert compose_node_sha(cmd_sha, parents) == compose_node_sha(cmd_sha, shuffled)


@given(_sha, st.lists(_sha, min_size=1, max_size=6))
def test_parented_node_differs_from_bare_cmd_sha(cmd_sha: str, parents: list[str]) -> None:
    assert compose_node_sha(cmd_sha, parents) != cmd_sha


@given(_sha, _sha, _sha_list)
def test_cmd_sha_change_changes_node_sha(a: str, b: str, parents: list[str]) -> None:
    assume(a != b)
    assert compose_node_sha(a, parents) != compose_node_sha(b, parents)


@given(_sha, _sha, _sha, st.lists(_sha, max_size=4))
def test_parent_change_changes_node_sha(
    cmd_sha: str, parent_a: str, parent_b: str, rest: list[str]
) -> None:
    assume(parent_a != parent_b)
    assume(parent_a not in rest and parent_b not in rest)
    assert compose_node_sha(cmd_sha, [parent_a, *rest]) != compose_node_sha(
        cmd_sha, [parent_b, *rest]
    )


@given(_sha, _sha, _sha, _sha)
def test_grandparent_change_propagates(
    child: str, parent: str, grandparent_a: str, grandparent_b: str
) -> None:
    """The Merkle property: an ancestor edit reaches every descendant."""
    assume(grandparent_a != grandparent_b)
    parent_node_a = compose_node_sha(parent, [grandparent_a])
    parent_node_b = compose_node_sha(parent, [grandparent_b])
    assert compose_node_sha(child, [parent_node_a]) != compose_node_sha(child, [parent_node_b])


@pytest.mark.parametrize(
    ("cmd_sha", "parents"),
    [
        ("not-a-sha", []),
        ("ABC123", ["a" * 64]),  # uppercase / short
        ("a" * 64, ["not-a-sha"]),
        ("a" * 64, ["b" * 64, ""]),
    ],
)
def test_malformed_digest_raises(cmd_sha: str, parents: list[str]) -> None:
    with pytest.raises(ValueError, match="compose_node_sha"):
        compose_node_sha(cmd_sha, parents)
