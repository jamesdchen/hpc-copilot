"""``check-task-generator-mismatch``: validate verb — hpc-submit Step 3.

WS5 #9. Canonical-JSON compare between a caller-supplied
``task_generator`` and the cached/derived one (the ``interview.json``
``task_generator`` left in ``experiment_dir`` from earlier dev work).
Returns a structured match/mismatch so the caller can short-circuit
early: a match means the cached interview is authoritative and Step 3
continues; a mismatch is the seam where ``hpc-submit`` must branch on
``on_task_generator_mismatch`` (fail / refresh) rather than silently
letting a stale 8-seed generator shrink a 100-seed request.

The comparison is by **canonical content**, not Python ``==``: both
generators are normalized with ``json.dumps(..., sort_keys=True,
separators=(",", ":"))`` (the same recursive key-sort idiom
``state.run_sha`` hashes task kwargs with), so two generators that differ
only in key order or whitespace compare equal. The ``sha256`` of each
canonical form is returned for cheap downstream logging / equality.

A purely local, side-effect-free validator — no SSH, no disk writes.

I/O contracts:

* Input: see ``hpc_agent/schemas/check_task_generator_mismatch.input.json``.
* Output: a ``dict`` matching
  ``schemas/check_task_generator_mismatch.output.json``.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from hpc_agent._kernel.registry.primitive import primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

__all__ = [
    "canonical_json",
    "check_task_generator_mismatch",
]


def canonical_json(value: Any) -> str:
    """Canonicalize *value* to a stable JSON string for content comparison.

    Recursively key-sorted, whitespace-free — the same idiom
    ``state.run_sha`` uses to hash task kwargs. Two structurally-equal
    generators that differ only in key order or formatting produce
    identical output, so the mismatch check compares *content*, not
    Python object identity or dict insertion order.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _sha256(canonical: str) -> str:
    """SHA-256 hex digest of a canonical-JSON string."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@primitive(
    name="check-task-generator-mismatch",
    verb="validate",
    side_effects=[],
    idempotent=True,
    cli=CliShape(
        help=(
            "Canonical-JSON compare a caller-supplied task_generator against "
            "the cached/derived one (hpc-submit Step 3). Returns match=true "
            "when their canonical content is identical, else match=false with "
            "both shapes + their sha256 so the caller can branch on "
            "on_task_generator_mismatch (fail / refresh)."
        ),
        verb="check-task-generator-mismatch",
        args=(
            CliArg(
                "--caller-task-generator",
                type=str,
                required=True,
                help="Caller-supplied task_generator as a JSON object string.",
            ),
            CliArg(
                "--cached-task-generator",
                type=str,
                default=None,
                help=(
                    "Cached/derived task_generator (e.g. from interview.json) as "
                    "a JSON object string. Omit when no cached generator exists — "
                    "match is then vacuously true (nothing to diverge from)."
                ),
            ),
        ),
        requires_ssh=False,
    ),
    agent_facing=True,
)
def check_task_generator_mismatch(
    *,
    caller_task_generator: dict[str, Any] | str,
    cached_task_generator: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Compare two ``task_generator`` shapes by canonical JSON.

    Returns a dict matching
    ``schemas/check_task_generator_mismatch.output.json``. Each generator
    is accepted as a parsed ``dict`` (the in-process path) or a JSON
    object string (the CLI path) and is parsed before canonicalization.

    ``match`` is ``True`` when the two canonical forms are byte-identical
    — OR when ``cached_task_generator`` is ``None`` (no cached generator
    to diverge from; the caller's is vacuously authoritative). On a
    mismatch, both canonical forms and their ``sha256`` digests are
    returned under ``caller`` / ``cached`` so the caller can surface BOTH
    shapes in a ``task_generator_mismatch`` envelope.
    """
    caller = (
        json.loads(caller_task_generator)
        if isinstance(caller_task_generator, str)
        else caller_task_generator
    )
    caller_canonical = canonical_json(caller)
    caller_sha = _sha256(caller_canonical)

    if cached_task_generator is None:
        # No cached generator: nothing to diverge from. Caller wins by
        # default — a vacuous match, NOT a mismatch.
        return {
            "match": True,
            "reason": "no_cached_generator",
            "caller": {"canonical": caller_canonical, "sha256": caller_sha},
            "cached": None,
        }

    cached = (
        json.loads(cached_task_generator)
        if isinstance(cached_task_generator, str)
        else cached_task_generator
    )
    cached_canonical = canonical_json(cached)
    cached_sha = _sha256(cached_canonical)

    match = caller_canonical == cached_canonical
    return {
        "match": match,
        "reason": "identical" if match else "divergent",
        "caller": {"canonical": caller_canonical, "sha256": caller_sha},
        "cached": {"canonical": cached_canonical, "sha256": cached_sha},
    }
