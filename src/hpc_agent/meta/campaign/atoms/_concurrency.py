"""Single source of truth for the async-refill pool-occupancy target K.

The framework default ``max_in_flight`` / ``k_cap`` — the upper guardrail on
how many campaign iterations run concurrently when async refill is on but the
manifest / CLI leaves the pool size unset (#362). ``campaign-advance`` (the
refill count), ``decide-concurrency`` (the parallelism guardrail), and the
manifest opt-in all resolve to this ONE value, so the routing target and the
refill target can never drift apart — the bug three "kept in sync" copies of a
bare ``4`` invited.
"""

from __future__ import annotations

__all__ = ["DEFAULT_MAX_IN_FLIGHT"]

# Conservative, connection-storm-safe default. An explicit manifest
# ``max_in_flight`` / ``--max-in-flight`` / ``--k-cap`` always overrides it.
DEFAULT_MAX_IN_FLIGHT = 4
